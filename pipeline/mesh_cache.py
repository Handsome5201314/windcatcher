"""Reuse only meshes with matching content and explicit successful checkMesh proof.

The cache is local to the CFD case. A legacy polyMesh directory is never proof of
success, and a solver failure does not turn a checked mesh into a failed mesh.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from .cfd_validation import parse_mesh_checks, strip_comments

MANIFEST_FILENAME = "mesh_cache.json"
MESH_CORE_FILES = ("points", "faces", "owner", "neighbour", "boundary")
SCHEMA_VERSION = 1
MESH_STEPS = {"blockMesh", "snappyHexMesh", "reconstructMesh", "checkMesh"}
MESH_DICTIONARIES = ("blockMeshDict", "snappyHexMeshDict", "fvSchemes", "fvSolution", "controlDict")
_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|[{}();]|[^\s{}();]+')


class _Unverifiable(ValueError):
    pass


class _InputsChanged(_Unverifiable):
    pass


def _json_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _file_proof(path: Path, root: Path) -> dict:
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise _Unverifiable(f"依赖文件不在当前算例内：{path.name}")
    if path.name.startswith(".env") or path.suffix.lower() in {".pem", ".key"}:
        raise _Unverifiable("网格依赖引用了禁止读取的配置文件")
    before = path.stat()
    if not path.is_file() or before.st_size == 0:
        raise _Unverifiable(f"文件缺失或为空：{path.relative_to(root).as_posix()}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise _Unverifiable(f"读取期间文件发生变化：{path.name}")
    return {"size": after.st_size, "sha256": digest.hexdigest()}


def _entries(tokens: list[str]) -> list[tuple[str, list[str]]]:
    """Small conservative dictionary reader; unfamiliar syntax disables reuse."""
    entries, index = [], 0
    while index < len(tokens):
        if tokens[index] == ";":
            index += 1
            continue
        key = tokens[index]
        if key in "{}()":
            raise _Unverifiable("网格字典包含无法核对的条目语法")
        index += 1
        start, stack = index, []
        while index < len(tokens):
            token = tokens[index]
            if token in ("{", "("):
                stack.append(token)
            elif token in ("}", ")"):
                if not stack or stack.pop() != {"}": "{", ")": "("}[token]:
                    raise _Unverifiable("网格字典括号未闭合")
            index += 1
            if not stack and (token == ";" or (token == "}" and tokens[start] == "{")):
                break
        if stack or index == start:
            raise _Unverifiable("网格字典条目未闭合")
        entries.append((key, tokens[start:index]))
    return entries


def _block_entries(value: list[str]) -> list[tuple[str, list[str]]]:
    if value[:1] != ["{"] or value[-1:] != ["}"]:
        raise _Unverifiable("网格相关字典块无法解析")
    return _entries(value[1:-1])


def _effective_dictionary(text: str, name: str) -> list:
    entries = _entries(_TOKEN.findall(text))
    entries = [(key, value) for key, value in entries if key != "FoamFile"]
    if name == "controlDict":
        # Runtime controls/field sampling do not change topology. Output format,
        # precision and dynamically loaded libraries remain part of the signature.
        solver_only = {
            "application", "startFrom", "startTime", "stopAt", "endTime", "deltaT",
            "writeControl", "writeInterval", "purgeWrite", "timeFormat", "timePrecision",
            "runTimeModifiable", "adjustTimeStep", "maxCo", "maxDeltaT", "functions",
        }
        entries = [(key, value) for key, value in entries if key not in solver_only]
    elif name == "fvSolution":
        entries = [(key, value) for key, value in entries if key not in {"SIMPLE", "PIMPLE", "PISO", "relaxationFactors"}]
        filtered = []
        pure_flow_keys = {"p", "p_rgh", "U", "k", "omega", "epsilon", "nut", "nuTilda"}
        for key, value in entries:
            if key == "solvers":
                # Keep motion/mesh smoothing solvers. Only known exact field names
                # or regex groups of those names may be excluded.
                solvers = []
                for field, settings in _block_entries(value):
                    expression = field.strip('"')
                    components = set(expression.strip("()").split("|"))
                    if not components or not components.issubset(pure_flow_keys):
                        solvers.append((field, settings))
                if solvers:
                    filtered.append((key, solvers))
            else:
                filtered.append((key, value))
        entries = filtered
    return entries


def _dictionary_text(path: Path, root: Path, inputs: dict, active: set[Path]) -> str:
    path = path.resolve()
    if path in active:
        raise _Unverifiable("网格字典存在循环 include，不能确认网格依赖")
    if path.name.startswith(".env") or path.suffix.lower() in {".pem", ".key"}:
        raise _Unverifiable("网格字典引用了禁止读取的配置文件")
    proof = _file_proof(path, root)
    relative = path.relative_to(root).as_posix()
    inputs[relative] = proof
    text = strip_comments(path.read_text(encoding="utf-8"))
    active.add(path)
    try:
        def expand(match):
            kind, argument = match.group(1), match.group(2).strip()
            target_name = argument.strip('"<>')
            if kind not in {"include", "includeIfPresent"} or not target_name or any(char in target_name for char in "$#\n\r"):
                raise _Unverifiable(f"不支持的网格依赖指令 #{kind}，本次不复用网格")
            target = (path.parent / target_name).resolve()
            if not target.is_relative_to(root):
                raise _Unverifiable("include 指向算例外部，不能确认依赖内容")
            if kind == "includeIfPresent" and not target.exists():
                inputs[target.relative_to(root).as_posix()] = {"absent": True}
                return ""
            return _dictionary_text(target, root, inputs, active)
        text = re.sub(r'#(\w+)\s+("[^"\n]+"|<[^>\n]+>|[^\s;]+)', expand, text)
        if "#" in text or re.search(r"\$[A-Za-z{]", text):
            raise _Unverifiable("网格字典包含动态指令或未解析变量，本次不复用网格")
        return text
    finally:
        active.remove(path)


def _backend_identity(identity: dict) -> dict:
    if not isinstance(identity, dict):
        raise _Unverifiable("未获得可核对的 OpenFOAM 后端版本")
    name, flavour = identity.get("name"), identity.get("openfoam_flavour")
    if flavour not in {"org", "esi"}:
        raise _Unverifiable("OpenFOAM 分支信息缺失，不能复用网格")
    if name == "docker":
        digest = identity.get("image_id")
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise _Unverifiable("未获得 Docker 镜像实际 ID，不能凭镜像标签复用网格")
        return {"name": name, "image_id": digest, "openfoam_flavour": flavour}
    if name in {"native", "wsl"} and identity.get("version") and re.fullmatch(r"[0-9a-f]{64}", identity.get("installation_sha256", "")):
        return {key: identity[key] for key in ("name", "version", "installation_sha256", "openfoam_flavour")}
    raise _Unverifiable("OpenFOAM 安装内容或版本不可核对，本次不复用网格")


def _surface_references(text: str) -> list[str]:
    geometry = next((value for key, value in _entries(_TOKEN.findall(text)) if key == "geometry"), None)
    result = []
    if geometry is not None:
        for name, settings in _block_entries(geometry):
            properties = dict(_block_entries(settings))
            kind = properties.get("type", [])
            if kind and kind[0] == "triSurfaceMesh":
                filename = properties.get("file", [name])[0]
                result.append(filename.strip('"'))
    return result


def mesh_inputs_signature(cfd_dir: Path, backend_identity: dict) -> dict:
    """Hash real surfaces, dictionaries and locally resolvable mesh dependencies."""
    root = Path(cfd_dir).resolve()
    inputs, dictionaries, dependencies = {}, {}, {}
    try:
        identity = _backend_identity(backend_identity)
        for name in MESH_DICTIONARIES:
            local_inputs = {}
            path = root / "system" / name
            text = _dictionary_text(path, root, local_inputs, set())
            effective = _effective_dictionary(text, name)
            dictionaries[name] = _json_hash(effective)
            inputs.update(local_inputs)
            # Expanded text is signed semantically so a pure endTime/flow solver
            # change is not a mesh change. Included files are additionally signed.
            dependencies.update({key: value for key, value in local_inputs.items() if key != f"system/{name}"})
            if name == "snappyHexMeshDict":
                for filename in _surface_references(text):
                    target = root / "constant" / "triSurface" / filename
                    _file_proof(target, root)
            for match in re.finditer(r'\bfile\s+("[^"]+"|[^;\s]+)\s*;', text):
                filename = match.group(1).strip('"')
                target = root / "constant" / "triSurface" / filename
                if not target.exists():
                    target = root / filename
                proof = _file_proof(target, root)
                key = target.resolve().relative_to(root).as_posix()
                inputs[key] = dependencies[key] = proof
        surfaces = root / "constant" / "triSurface"
        if not surfaces.exists():
            raise _Unverifiable("triSurface 几何目录缺失")
        geometry = {}
        for path in sorted(surfaces.rglob("*")):
            if path.is_file():
                key = path.relative_to(root).as_posix()
                inputs[key] = geometry[key] = _file_proof(path, root)
        if not geometry:
            raise _Unverifiable("triSurface 几何文件缺失")
        evidence = {"schema_version": SCHEMA_VERSION, "backend_identity": identity, "dictionaries": dictionaries, "geometry": geometry, "dependencies": dependencies}
        return {"valid": True, "signature": _json_hash(evidence), "backend_identity": identity, "inputs": inputs, "evidence": evidence, "reason": "几何、网格设置及后端内容已核对"}
    except (OSError, UnicodeError, ValueError) as exc:
        return {"valid": False, "signature": None, "inputs": inputs, "reason": f"网格依赖无法确认：{exc}"}


def _mesh_files(root: Path) -> dict:
    mesh = root / "constant" / "polyMesh"
    for name in MESH_CORE_FILES:
        choices = [mesh / name, mesh / f"{name}.gz"]
        if sum(path.is_file() for path in choices) != 1:
            raise _Unverifiable(f"网格核心文件缺失或有歧义：{name}")
    # OpenFOAM 11 domainDecomposition::writeCompleteAddressing writes the
    # cell-to-processor assignment here. decomposePar changes it without changing
    # root mesh topology, so only these exact derived root files are excluded.
    partition_files = {mesh / "cellProc", mesh / "cellProc.gz"}
    return {path.relative_to(root).as_posix(): _file_proof(path, root) for path in sorted(mesh.rglob("*")) if path.is_file() and path not in partition_files}


def _load_manifest(root: Path) -> dict:
    path = root / MANIFEST_FILENAME
    if not path.exists():
        raise _Unverifiable("旧网格没有可靠复用记录，需要首次重建")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise _Unverifiable("网格复用记录版本不匹配，需要重建")
    proof = manifest.get("check_mesh") or {}
    if proof.get("returncode") != 0 or not proof.get("run_id") or not proof.get("quality", {}).get("mesh_ok") or not proof.get("log", {}).get("sha256"):
        raise _Unverifiable("网格复用记录缺少成功检查证据，需要重建")
    return manifest


def _check_manifest_files(root: Path, signature: dict, manifest: dict) -> dict:
    if not signature.get("valid"):
        raise _Unverifiable(signature.get("reason", "网格输入无法确认"))
    if manifest.get("signature") != signature.get("signature"):
        raise _Unverifiable("几何、网格设置或 OpenFOAM 后端已改变，需要重建网格")
    mesh_files = _mesh_files(root)
    if manifest.get("mesh_files") != mesh_files:
        raise _Unverifiable("网格文件内容改变或缺失，需要重建网格")
    return mesh_files


def inspect_reuse(cfd_dir: Path, signature: dict) -> dict:
    """Read-only decision; never register an unproven legacy mesh."""
    root = Path(cfd_dir).resolve()
    try:
        if not signature.get("valid"):
            raise _Unverifiable(signature.get("reason", "网格输入无法确认"))
        manifest = _load_manifest(root)
        _check_manifest_files(root, signature, manifest)
        status_paths = [root / "run_status.json", root / ".run_status.json.live"]
        status_paths = [path for path in status_paths if path.exists()]
        if status_paths:
            status = json.loads(max(status_paths, key=lambda path: path.stat().st_mtime_ns).read_text(encoding="utf-8"))
            if status.get("status") == "failed" and status.get("failed_step") in MESH_STEPS:
                raise _Unverifiable("上次网格步骤失败，需要重建；不能复用失败后的产物")
        proof = _file_proof(root / "logs" / "checkMesh.log", root)
        if manifest["check_mesh"]["log"] != proof:
            raise _Unverifiable("网格检查日志已改变或不完整，需要重建")
        quality = parse_mesh_checks(root)
        if not quality.get("mesh_ok"):
            raise _Unverifiable("缺少 Mesh OK 检查证据，需要重建")
        return {"reusable": True, "reason": "内容签名和历史 checkMesh 证据一致；本次仍需重新检查网格", "manifest": manifest, "quality": quality}
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        return {"reusable": False, "reason": str(exc)}


def _atomic_write(root: Path, payload: dict) -> None:
    target = root / MANIFEST_FILENAME
    temporary = root / f".mesh_cache.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(3):
            try:
                os.replace(temporary, target)
                return
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.03 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def record_mesh(cfd_dir: Path, signature: dict, run_id: str, *, check_result: dict, completed_steps: list[dict], generation: dict | None = None) -> dict:
    """Register only after THIS run successfully checked a complete mesh.

    ``check_result`` is the actual streaming command result. ``completed_steps``
    includes mesh construction/reconstruction or proves revalidation of an
    already signed cache. Write failure and changed inputs are reported separately
    so the caller can decide whether solving remains safe.
    """
    root = Path(cfd_dir).resolve()
    try:
        if not run_id or not signature.get("valid"):
            raise _Unverifiable(signature.get("reason", "本次网格检查标识缺失"))
        if check_result.get("returncode") != 0 or check_result.get("timed_out"):
            raise _Unverifiable("本次 checkMesh 失败或超时，不记录网格缓存")
        output = check_result.get("output", "")
        if not output or not re.search(r"\bMesh OK\.", output) or re.search(r"Failed\s+\d+\s+mesh checks", output, re.I):
            raise _Unverifiable("本次 checkMesh 未提供 Mesh OK 证据，不记录网格缓存")
        names = [item.get("name") for item in completed_steps]
        if not names or names[-1] != "checkMesh" or any(item.get("returncode") != 0 or item.get("timed_out") for item in completed_steps):
            raise _Unverifiable("本次网格前序步骤失败或检查记录缺失，不记录网格缓存")
        log_path = root / "logs" / "checkMesh.log"
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        if not log_text.endswith(output):
            raise _Unverifiable("本次 checkMesh 输出与日志不匹配，不记录网格缓存")
        rebuilt = "blockMesh" in names and "snappyHexMesh" in names and names.index("blockMesh") < names.index("snappyHexMesh")
        if not rebuilt and any(name in names for name in ("blockMesh", "snappyHexMesh", "reconstructMesh")):
            raise _Unverifiable("本次网格生成步骤不完整，不能注册或刷新网格缓存")
        if rebuilt and generation and generation.get("mode") == "parallel" and "reconstructMesh" not in names:
            raise _Unverifiable("并行网格未完成重构，不能缓存根目录网格")
        if not rebuilt:
            manifest = _load_manifest(root)
            _check_manifest_files(root, signature, manifest)
        # Recompute input and product hashes before committing, catching files
        # edited while meshing or checking rather than trusting initial snapshots.
        current = mesh_inputs_signature(root, signature["backend_identity"])
        if not current.get("valid") or current.get("signature") != signature["signature"]:
            raise _InputsChanged("运行期间网格输入已改变，不记录网格缓存")
        quality = parse_mesh_checks(root)
        if not quality.get("mesh_ok"):
            raise _Unverifiable("当前网格日志没有成功检查证据，不记录网格缓存")
        payload = {
            "schema_version": SCHEMA_VERSION, "signature": signature["signature"],
            "inputs": current["inputs"], "evidence": current["evidence"],
            "backend_identity": current["backend_identity"], "mesh_files": _mesh_files(root),
            "check_mesh": {"run_id": run_id, "returncode": 0, "log": _file_proof(log_path, root), "quality": quality},
            "completed_mesh_steps": [item for item in completed_steps if item.get("name") in MESH_STEPS],
            "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        if generation is not None:
            payload["generation"] = generation
        elif not rebuilt and manifest.get("generation") is not None:
            payload["generation"] = manifest["generation"]
        _atomic_write(root, payload)
        return {"recorded": True, "reason": "已记录本次网格内容及检查证据", "manifest": payload, "quality": quality}
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        return {"recorded": False, "input_changed": isinstance(exc, _InputsChanged), "reason": f"网格缓存未记录：{exc}"}
