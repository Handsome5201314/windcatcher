"""鼻塞方案的独立 CFD 算例与基准对比。

每个已审核的摆位版本都在自己的版本目录生成 OpenFOAM 算例。鼻塞作为
流体域内的封闭 wall 障碍物加入 ``snappyHexMesh``，不覆盖基准病例的网格、
求解场或报告。求解完成后只保存可追溯的运行记录和同工况数值对比；工程
复核未通过时，结果保持为研究预览，不生成医疗建议。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

import numpy as np

from . import cfd_prep, cases, openfoam_runner, plug_models, scenarios
from .errors import PipelineError

try:  # VTK 已是项目的基础依赖；导入失败时明确阻断，不做几何猜测。
    import vtk
    from vtk.util.numpy_support import numpy_to_vtk
except Exception:  # pragma: no cover - 环境缺失由运行时错误路径覆盖
    vtk = None
    numpy_to_vtk = None


SCHEMA_VERSION = 1
_VERSION_RE = re.compile(r"^v(\d{4})$")
_FIELD_PATCHES = {
    "U": """    plug
    {
        type            noSlip;
    }
""",
    "p": """    plug
    {
        type            zeroGradient;
    }
""",
    "k": """    plug
    {
        type            kqRWallFunction;
        value           uniform 0;
    }
""",
    "omega": """    plug
    {
        type            omegaWallFunction;
        value           uniform 0;
    }
""",
    "nut": """    plug
    {
        type            nutkWallFunction;
        value           uniform 0;
    }
""",
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PipelineError("blocked", f"{label}缺失或无法读取", [f"检查 {path.name} 后重试"]) from exc
    if not isinstance(value, dict):
        raise PipelineError("blocked", f"{label}格式无效", ["重新生成基准 CFD 算例后重试"])
    return value


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _version_root(case_id: str, scenario_id: str, version: int | None = None) -> tuple[dict, Path, int]:
    scenario = scenarios.get_scenario(case_id, scenario_id)
    selected = int(scenario.get("current_version", 0)) if version is None else version
    if selected < 1 or selected > int(scenario.get("current_version", 0)):
        raise PipelineError("failed", "鼻塞方案版本无效", ["刷新方案详情后重新选择当前版本"])
    root = scenarios.scenario_dir(case_id, scenario_id)
    version_root = root / "versions" / f"v{selected:04d}"
    if _VERSION_RE.fullmatch(version_root.name) is None or not version_root.is_dir() or version_root.is_symlink():
        raise PipelineError("failed", f"鼻塞方案版本不存在：{selected}", ["刷新方案列表后重试"])
    return scenario, version_root, selected


def _polydata(mesh: plug_models.Mesh, points: np.ndarray | None = None):
    if vtk is None or numpy_to_vtk is None:
        raise PipelineError("blocked", "当前环境缺少 VTK，不能核对鼻塞与气道的空间关系", ["在服务虚拟环境安装项目 requirements.txt 后重试"])
    coords = np.asarray(points if points is not None else mesh.vertices, dtype=np.float64)
    output = vtk.vtkPolyData()
    vtk_points = vtk.vtkPoints()
    vtk_points.SetData(numpy_to_vtk(coords, deep=True))
    output.SetPoints(vtk_points)
    if points is None:
        cells = vtk.vtkCellArray()
        for face in mesh.faces:
            triangle = vtk.vtkTriangle()
            for index, vertex in enumerate(face):
                triangle.GetPointIds().SetId(index, int(vertex))
            cells.InsertNextCell(triangle)
        output.SetPolys(cells)
    else:
        vertices = vtk.vtkCellArray()
        for index in range(len(coords)):
            vertices.InsertNextCell(1)
            vertices.InsertCellPoint(index)
        output.SetVerts(vertices)
    return output


def _inside_flags(surface: plug_models.Mesh, points: np.ndarray) -> np.ndarray:
    selector = vtk.vtkSelectEnclosedPoints()
    selector.SetInputData(_polydata(plug_models.Mesh(tuple(map(tuple, surface.vertices)), surface.faces, surface.encoding), points))
    selector.SetSurfaceData(_polydata(surface))
    selector.SetTolerance(1e-6)
    selector.Update()
    array = selector.GetOutput().GetPointData().GetArray("SelectedPoints")
    if array is None:
        return np.zeros(len(points), dtype=bool)
    return np.asarray([bool(array.GetTuple1(index)) for index in range(array.GetNumberOfTuples())], dtype=bool)


def _placement_containment(base_mesh: plug_models.Mesh, plug_mesh: plug_models.Mesh) -> dict:
    """用封闭气道壳检查鼻塞是否完全位于流体域内且没有穿过气道壁。"""
    if vtk is None:
        raise PipelineError("blocked", "当前环境没有 VTK，不能核对鼻塞是否位于气道内", ["安装项目依赖后重试"])
    base_points = np.asarray(base_mesh.vertices, dtype=float)
    plug_points = np.asarray(plug_mesh.vertices, dtype=float)
    base_min, base_max = base_points.min(axis=0), base_points.max(axis=0)
    plug_min, plug_max = plug_points.min(axis=0), plug_points.max(axis=0)
    if np.any(plug_min < base_min) or np.any(plug_max > base_max):
        return {
            "status": "blocked", "vertex_inside_ratio": 0.0, "centroid_inside_ratio": 0.0,
            "reason": "鼻塞摆位包围盒超出当前气道封闭壳",
        }
    centroids = np.asarray([plug_points[list(face)].mean(axis=0) for face in plug_mesh.faces], dtype=float)
    vertex_flags = _inside_flags(base_mesh, plug_points)
    centroid_flags = _inside_flags(base_mesh, centroids)
    vertex_ratio = float(vertex_flags.mean()) if len(vertex_flags) else 0.0
    centroid_ratio = float(centroid_flags.mean()) if len(centroid_flags) else 0.0
    # 顶点和面心都在壳内仍可能漏掉一个穿过凹面或薄壁的三角面；
    # 这里用 VTK 的三角面相交过滤器做第二道检查，不能静默跳过。
    intersection_checked = False
    intersection_count = None
    if not hasattr(vtk, "vtkIntersectionPolyDataFilter"):
        raise PipelineError("blocked", "当前 VTK 不支持气道与鼻塞表面相交检查", ["升级项目 requirements.txt 中的 VTK 后重试"])
    intersection = vtk.vtkIntersectionPolyDataFilter()
    intersection.SetInputData(0, _polydata(base_mesh))
    intersection.SetInputData(1, _polydata(plug_mesh))
    if hasattr(intersection, "SplitFirstOutputOff"):
        intersection.SplitFirstOutputOff()
    if hasattr(intersection, "SplitSecondOutputOff"):
        intersection.SplitSecondOutputOff()
    intersection.Update()
    intersection_checked = True
    intersection_count = int(intersection.GetNumberOfIntersectionLines())
    passed = bool(vertex_ratio == 1.0 and centroid_ratio == 1.0 and intersection_count == 0)
    return {
        "status": "pass" if passed else "blocked",
        "vertex_inside_ratio": vertex_ratio,
        "centroid_inside_ratio": centroid_ratio,
        "vertex_count": int(len(vertex_flags)),
        "face_count": int(len(centroid_flags)),
        "intersection_checked": intersection_checked,
        "intersection_lines": intersection_count,
        "reason": None if passed else (
            "鼻塞表面与气道封闭壳相交，不能生成独立 CFD 算例"
            if intersection_count else "鼻塞顶点或三角面心落在气道封闭壳外，不能生成独立 CFD 算例"
        ),
    }


def _copy_base_case(base: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    for directory in ("0", "system"):
        source = base / directory
        if not source.is_dir():
            raise PipelineError("blocked", f"基准 CFD 缺少 {directory} 目录", ["重新生成基准 CFD 算例"])
        shutil.copytree(source, target / directory)
    source_constant = base / "constant"
    target_constant = target / "constant"
    target_constant.mkdir()
    if source_constant.is_dir():
        for path in source_constant.iterdir():
            if path.name in {"polyMesh", "triSurface"} or path.is_symlink():
                continue
            if path.is_file():
                shutil.copy2(path, target_constant / path.name)
    source_surface = source_constant / "triSurface"
    target_surface = target_constant / "triSurface"
    target_surface.mkdir()
    for name in ("wall.stl", "inlet.stl", "outlet.stl", "nasal_cfd.stl"):
        source = source_surface / name
        if not source.is_file() or source.is_symlink():
            raise PipelineError("blocked", f"基准 CFD 缺少 {name}", ["重新生成基准 CFD 算例"])
        shutil.copy2(source, target_surface / name)


def _insert_named_block(text: str, name: str, entry: str) -> str:
    start = re.search(rf"(?m)^\s*{re.escape(name)}\s*$", text)
    if not start:
        raise PipelineError("blocked", f"无法在 snappyHexMeshDict 中定位 {name} 配置", ["重新生成基准 CFD 算例"])
    opening = text.find("{", start.end())
    if opening < 0:
        raise PipelineError("blocked", f"{name} 配置缺少左大括号", ["重新生成基准 CFD 算例"])
    depth = 0
    closing = None
    for index in range(opening, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                closing = index
                break
    if closing is None:
        raise PipelineError("blocked", f"{name} 配置未闭合", ["重新生成基准 CFD 算例"])
    return text[:closing] + entry + text[closing:]


def _patch_snappy(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if "plug.stl" not in text:
        text = _insert_named_block(text, "geometry", """\n    plug.stl\n    {\n        type triSurfaceMesh;\n        name plug;\n    }\n""")
    if re.search(r"(?m)^\s*plug\s*\{", text) is None:
        text = _insert_named_block(text, "refinementSurfaces", """\n        plug   { level (3 4); patchInfo { type wall; } }\n""")
    path.write_text(text, encoding="utf-8")


def _patch_field(path: Path, field: str) -> None:
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    if re.search(r"(?m)^\s*plug\s*\{", text):
        return
    match = re.search(r"(?m)^(\s*)background\s*\{", text)
    if not match:
        raise PipelineError("blocked", f"无法在字段 {field} 中添加鼻塞 wall 边界", ["重新生成基准 CFD 算例"])
    text = text[:match.start()] + _FIELD_PATCHES[field] + text[match.start():]
    path.write_text(text, encoding="utf-8")


def _location_in_mesh(path: Path) -> tuple[float, float, float] | None:
    """读取 snappyHexMeshDict 中的 locationInMesh（OpenFOAM 使用米）。"""
    if not path.is_file():
        return None
    match = re.search(
        r"(?m)^\s*locationInMesh\s*\(\s*([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s*\)",
        path.read_text(encoding="utf-8"),
    )
    if not match:
        return None
    return tuple(float(match.group(index)) for index in range(1, 4))


def _condition_fingerprint(meta: dict) -> str:
    keys = (
        "solver", "turbulence_model", "inlet_mode", "total_flow_lpm", "total_flow_m3s",
        "rho_kg_m3", "nu", "inlet_pressure_pa", "outlet_reference_pressure_pa",
    )
    value = {key: meta.get(key) for key in keys}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _optional_file_sha256(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return plug_models.sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise PipelineError("failed", "CFD 运行记录无法读取", ["检查基准 CFD 目录后重试"]) from exc


def _cfd_result_signature(root: Path) -> str | None:
    """用结果/网格文件的元数据形成轻量签名，不读取巨型场文件。"""
    if not root.is_dir():
        return None
    paths = [root / "case_meta.json", root / "run_status.json"]
    paths.extend(root / "constant" / "polyMesh" / name for name in ("points", "faces", "owner", "neighbour", "boundary"))
    numeric = []
    for path in root.iterdir():
        if path.is_dir():
            try:
                float(path.name)
            except ValueError:
                continue
            numeric.append(path)
    if numeric:
        latest = max(numeric, key=lambda path: float(path.name))
        paths.extend(latest / name for name in ("U", "p", "phi"))
        paths.extend(latest / "polyMesh" / name for name in ("points", "faces", "owner", "neighbour", "boundary"))
    records = []
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError as exc:
            raise PipelineError("failed", "CFD 结果签名无法读取", ["检查 CFD 算例目录后重试"]) from exc
        records.append((path.relative_to(root).as_posix(), stat.st_size, stat.st_mtime_ns))
    if not records:
        return None
    return hashlib.sha256(json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _metric_summary(gate: dict) -> dict:
    metrics = gate.get("metrics") if isinstance(gate.get("metrics"), dict) else {}
    pressure_unit = metrics.get("pressure_unit")
    delta_pressure = metrics.get("delta_p_pa") if pressure_unit == "Pa" else metrics.get("delta_p_m2s2")
    flow = metrics.get("inlet_flow_m3s")
    flow_lpm = abs(float(flow)) * 60000.0 if isinstance(flow, (int, float)) and np.isfinite(flow) else None
    return {
        "delta_pressure": delta_pressure,
        "pressure_unit": pressure_unit,
        "inlet_flow_m3s": flow,
        "flow_lpm": flow_lpm,
        "numeric_ok": bool(gate.get("numeric_ok")),
        "eligible": bool(gate.get("eligible")),
    }


def _comparison(case_id: str, scenario_id: str, version: int, scenario_cfd: Path, scenario_meta: dict) -> dict:
    base = cases.case_dir(case_id) / "cfd"
    base_meta_path = base / "case_meta.json"
    if not base_meta_path.is_file():
        result = {"schema_version": SCHEMA_VERSION, "status": "missing", "reason": "基准 CFD 算例尚未生成"}
        _atomic_json(scenario_cfd / "comparison.json", result)
        return result
    base_meta = _read_json(base_meta_path, "基准 CFD 清单")
    try:
        current_scenario = scenarios.get_scenario(case_id, scenario_id)
    except PipelineError:
        current_scenario = None
    if current_scenario and (
        current_scenario.get("current_version") == version
        and current_scenario.get("base_status") != "current"
    ):
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "stale",
            "reason": "当前气道基准已变化，旧鼻塞结果不能继续比较",
            "base_status": current_scenario.get("base_status"),
            "version": version,
        }
        _atomic_json(scenario_cfd / "comparison.json", result)
        return result
    base_meta_sha256 = plug_models.sha256_bytes(base_meta_path.read_bytes())
    expected_base_meta_sha256 = scenario_meta.get("base_case_meta_sha256")
    if expected_base_meta_sha256 and expected_base_meta_sha256 != base_meta_sha256:
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "stale",
            "reason": "基准 CFD 清单已变化，旧鼻塞结果不能继续比较",
            "base_case_meta_sha256": base_meta_sha256,
            "scenario_base_case_meta_sha256": expected_base_meta_sha256,
            "version": version,
        }
        _atomic_json(scenario_cfd / "comparison.json", result)
        return result
    expected_base_run_sha256 = scenario_meta.get("base_run_status_sha256")
    current_base_run_sha256 = _optional_file_sha256(base / "run_status.json")
    if expected_base_run_sha256 and expected_base_run_sha256 != current_base_run_sha256:
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "stale",
            "reason": "基准 CFD 运行记录已变化，旧鼻塞结果不能继续比较",
            "base_run_status_sha256": current_base_run_sha256,
            "scenario_base_run_status_sha256": expected_base_run_sha256,
            "version": version,
        }
        _atomic_json(scenario_cfd / "comparison.json", result)
        return result
    expected_base_result_signature = scenario_meta.get("base_result_signature")
    current_base_result_signature = _cfd_result_signature(base)
    if expected_base_result_signature and expected_base_result_signature != current_base_result_signature:
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "stale",
            "reason": "基准网格或最终场已变化，旧鼻塞结果不能继续比较",
            "base_result_signature": current_base_result_signature,
            "scenario_base_result_signature": expected_base_result_signature,
            "version": version,
        }
        _atomic_json(scenario_cfd / "comparison.json", result)
        return result
    base_fingerprint = _condition_fingerprint(base_meta)
    scenario_fingerprint = scenario_meta.get("condition_fingerprint")
    if base_fingerprint != scenario_fingerprint:
        result = {
            "schema_version": SCHEMA_VERSION, "status": "blocked",
            "reason": "鼻塞算例与基准算例的入口工况或空气参数不一致，不能直接比较",
            "base_condition_fingerprint": base_fingerprint,
            "scenario_condition_fingerprint": scenario_fingerprint,
        }
        _atomic_json(scenario_cfd / "comparison.json", result)
        return result
    base_gate = openfoam_runner.check_result_gate(case_id)
    scenario_gate = openfoam_runner.check_result_gate(case_id, cfd_dir_override=scenario_cfd)
    base_metrics, scenario_metrics = _metric_summary(base_gate), _metric_summary(scenario_gate)
    pressure_delta = None
    if (
        base_metrics["delta_pressure"] is not None
        and scenario_metrics["delta_pressure"] is not None
        and base_metrics["pressure_unit"] == scenario_metrics["pressure_unit"]
        and base_metrics["delta_pressure"] != 0
    ):
        pressure_delta = (scenario_metrics["delta_pressure"] - base_metrics["delta_pressure"]) / abs(base_metrics["delta_pressure"]) * 100
    flow_delta = None
    if base_metrics["flow_lpm"] not in (None, 0) and scenario_metrics["flow_lpm"] is not None:
        flow_delta = (scenario_metrics["flow_lpm"] - base_metrics["flow_lpm"]) / base_metrics["flow_lpm"] * 100
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "checked" if base_gate.get("numeric_ok") and scenario_gate.get("numeric_ok") else "pending_validation",
        "case_id": case_id, "scenario_id": scenario_id, "version": version,
        "condition_fingerprint": scenario_fingerprint,
        "base_case_meta_sha256": base_meta_sha256,
        "base_run_status_sha256": current_base_run_sha256,
        "base_result_signature": current_base_result_signature,
        "scenario_meta_sha256": plug_models.sha256_bytes((scenario_cfd / "scenario_meta.json").read_bytes()) if (scenario_cfd / "scenario_meta.json").is_file() else None,
        "scenario_result_signature": _cfd_result_signature(scenario_cfd),
        "baseline": base_metrics, "scenario": scenario_metrics,
        "delta": {"pressure_percent": pressure_delta, "flow_percent": flow_delta},
        "eligibility": {
            "numeric_ok": bool(base_gate.get("numeric_ok") and scenario_gate.get("numeric_ok")),
            "engineering_review_required": True,
            "recommendation_allowed": False,
        },
        "created_at": _now(),
        "notes": [
            "比较使用同一入口流量/压力、空气密度和黏度记录。",
            "固定流量工况下，流量变化不等于患者实际吸气能力变化；优先核对压降、残差和质量守恒。",
            "numeric_ok 通过仍需网格敏感性与 CFD 工程师复核，不能直接作为医疗器械建议。",
        ],
    }
    _atomic_json(scenario_cfd / "comparison.json", result)
    return result


def prepare_scenario(case_id: str, scenario_id: str, *, version: int | None = None) -> dict:
    scenario, version_root, selected_version = _version_root(case_id, scenario_id, version)
    if selected_version != scenario.get("current_version"):
        raise PipelineError("blocked", "只能为当前摆位版本生成鼻塞 CFD", ["旧版本仅用于追溯和下载，请刷新后选择当前版本"])
    if scenario.get("base_status") != "current":
        raise PipelineError("blocked", "当前鼻塞方案绑定的气道基准已变化或不可用", ["重新生成气道 STL、端面和基准 CFD 算例后再审核鼻塞摆位"])
    review = scenario.get("review") or {}
    if review.get("status") != "approved" or not review.get("approved") or review.get("version") != selected_version:
        raise PipelineError("blocked", "鼻塞方案尚未完成人工审核，不能生成独立 CFD 算例", ["先完成摆位确认和人工审核"])
    root = scenarios.scenario_dir(case_id, scenario_id)
    manifest = _read_json(version_root / "manifest.json", "鼻塞方案清单")
    if not manifest.get("review_ready"):
        raise PipelineError("blocked", "鼻塞方案与当前气道基准未形成可用审核记录", ["重新确认气道基准和鼻塞摆位"])
    placed_path = version_root / "placed" / "model.stl"
    placed_mesh = plug_models.parse_stl(placed_path.read_bytes()) if placed_path.is_file() else None
    if placed_mesh is None:
        raise PipelineError("blocked", "鼻塞摆位模型缺失", ["重新保存摆位或重新上传模型"])
    placed_qc = plug_models.inspect_mesh(placed_mesh, unit="mm")
    if not placed_qc.get("can_use_for_cfd"):
        raise PipelineError("blocked", "鼻塞摆位模型未通过封闭几何预检", ["修复鼻塞 STL 后重新上传"])
    base_cfd = cases.case_dir(case_id) / "cfd"
    base_meta = _read_json(base_cfd / "case_meta.json", "基准 CFD 清单")
    base_shell_path = base_cfd / "constant" / "triSurface" / "nasal_cfd.stl"
    if not base_shell_path.is_file():
        raise PipelineError("blocked", "基准 CFD 缺少封闭气道壳", ["先完成基准 surface、几何质检和 CFD 算例准备"])
    base_mesh_raw = plug_models.parse_stl(base_shell_path.read_bytes())
    # 基准 OpenFOAM STL 使用米；内部检查统一换算成毫米。
    base_mesh = plug_models.Mesh(
        tuple(tuple(value * 1000.0 for value in vertex) for vertex in base_mesh_raw.vertices),
        base_mesh_raw.faces,
        base_mesh_raw.encoding,
    )
    base_qc = plug_models.inspect_mesh(base_mesh, unit="mm")
    if not base_qc.get("closed"):
        raise PipelineError("blocked", "基准气道壳不是闭合体，不能放入鼻塞障碍物", ["重新生成基准气道 STL 并修复几何质检"])
    containment = _placement_containment(base_mesh, placed_mesh)
    if containment.get("status") != "pass":
        raise PipelineError("blocked", containment.get("reason", "鼻塞不在气道流体域内"), ["在 3D 视图调整鼻塞平移、旋转或缩放后重新审核"] , {"containment": containment})

    # snappyHexMesh 的流体种子点必须落在鼻塞实体之外；复制基准字典后
    # 重新核对这一前提，避免把鼻塞内部误当成可保留的流体区。
    location_m = _location_in_mesh(base_cfd / "system" / "snappyHexMeshDict")
    if location_m is None:
        raise PipelineError(
            "blocked",
            "基准 snappyHexMeshDict 缺少可核对的 locationInMesh，不能生成独立 CFD",
            ["重新生成基准 CFD 算例并保存流体种子点后重试"],
        )
    if _inside_flags(placed_mesh, np.asarray(location_m, dtype=float)[None, :] * 1000.0).item():
        raise PipelineError(
            "blocked",
            "基准 locationInMesh 落在鼻塞实体内部，不能生成独立 CFD",
            ["在 3D 视图调整鼻塞摆位，避开基准流体种子点后重新审核"],
            {"location_in_mesh_m": list(location_m)},
        )

    cfd_root = version_root / "cfd"
    existing_meta_path = cfd_root / "scenario_meta.json"
    if existing_meta_path.is_file():
        existing = _read_json(existing_meta_path, "鼻塞 CFD 清单")
        if existing.get("plug_sha256") == manifest.get("sha256", {}).get("placed") and existing.get("version") == selected_version:
            if existing.get("base_case_meta_sha256") != _optional_file_sha256(base_cfd / "case_meta.json") or existing.get("base_result_signature") != _cfd_result_signature(base_cfd):
                raise PipelineError("blocked", "基准 CFD 已变化，已保留旧版本算例", ["保存新的摆位版本并重新审核后生成 CFD"])
            manifest["cfd_ready"] = True
            manifest["cfd_blockers"] = []
            scenarios._write_manifest(root, version_root, manifest)
            return {"scenario": scenarios.get_scenario(case_id, scenario_id), "prepared": existing}
    _copy_base_case(base_cfd, cfd_root)
    _patch_snappy(cfd_root / "system" / "snappyHexMeshDict")
    for field, patch in _FIELD_PATCHES.items():
        _patch_field(cfd_root / "0" / field, field)
    placed_vertices = np.asarray(placed_mesh.vertices, dtype=float)
    placed_faces = np.asarray(placed_mesh.faces, dtype=int)
    cfd_prep._write_ascii_stl(cfd_root / "constant" / "triSurface" / "plug.stl", "plug", placed_vertices[placed_faces])
    scenario_meta = {
        "schema_version": SCHEMA_VERSION, "case_id": case_id, "scenario_id": scenario_id,
        "version": selected_version, "created_at": _now(),
        "adapter": "snappyHexMesh_internal_plug_wall",
        "plug_sha256": manifest.get("sha256", {}).get("placed"),
        "base_reference": scenario.get("base_reference"),
        "base_case_meta_sha256": plug_models.sha256_bytes((base_cfd / "case_meta.json").read_bytes()),
        "base_run_status_sha256": _optional_file_sha256(base_cfd / "run_status.json"),
        "base_result_signature": _cfd_result_signature(base_cfd),
        "condition_fingerprint": _condition_fingerprint(base_meta),
        "containment": containment,
        "boundary_patch": "plug",
        "status": "prepared",
        "cfd_ready": True,
        "review_required": True,
    }
    manifest["cfd_ready"] = True
    manifest["cfd_blockers"] = []
    scenarios._write_manifest(root, version_root, manifest)
    patched_meta = dict(base_meta)
    patched_meta["scenario"] = {"scenario_id": scenario_id, "version": selected_version, "plug_patch": "plug", "condition_fingerprint": scenario_meta["condition_fingerprint"]}
    _atomic_json(cfd_root / "case_meta.json", patched_meta)
    _atomic_json(cfd_root / "scenario_meta.json", scenario_meta)
    comparison = _comparison(case_id, scenario_id, selected_version, cfd_root, scenario_meta)
    scenarios.update_cfd_state(case_id, scenario_id, version=selected_version, state="prepared", details={"cfd_ready": True, "cfd_path": str(cfd_root.relative_to(root)).replace("\\", "/"), "comparison_status": comparison.get("status")})
    return {"scenario": scenarios.get_scenario(case_id, scenario_id), "prepared": scenario_meta, "comparison": comparison}


def run_scenario(case_id: str, scenario_id: str, *, backend: str | None = None, timeout_s: int = 7200, version: int | None = None) -> dict:
    prepared = prepare_scenario(case_id, scenario_id, version=version)
    _scenario, version_root, selected_version = _version_root(case_id, scenario_id, version)
    cfd_root = version_root / "cfd"
    run = openfoam_runner.run_case(case_id, backend=backend, timeout_s=timeout_s, cfd_dir_override=cfd_root)
    gate = openfoam_runner.check_result_gate(case_id, cfd_dir_override=cfd_root)
    scenario_meta = _read_json(cfd_root / "scenario_meta.json", "鼻塞 CFD 清单")
    _atomic_json(cfd_root / "gate.json", gate)
    comparison = _comparison(case_id, scenario_id, selected_version, cfd_root, scenario_meta)
    status = "solved" if run.get("status") == "ran" else run.get("status", "failed")
    scenarios.update_cfd_state(case_id, scenario_id, version=selected_version, state=status, details={"cfd_ready": True, "run_status": run.get("status"), "numeric_ok": gate.get("numeric_ok"), "comparison_status": comparison.get("status")})
    return {"scenario": scenarios.get_scenario(case_id, scenario_id), "run": run, "gate": gate, "comparison": comparison, "prepared": prepared.get("prepared")}


def get_status(case_id: str, scenario_id: str, *, version: int | None = None) -> dict:
    scenario, version_root, selected_version = _version_root(case_id, scenario_id, version)
    if selected_version != scenario.get("current_version"):
        scenario = _read_json(version_root / "scenario.json", "鼻塞版本快照")
    cfd_root = version_root / "cfd"
    output = {"scenario": scenario, "version": selected_version, "status": (scenario.get("cfd") or {}).get("status", "not_started")}
    task_path = version_root / "scenario_task.json"
    if task_path.is_file() and not task_path.is_symlink():
        task = _read_json(task_path, "鼻塞 CFD 任务记录")
        output["task"] = task
        output["status"] = "running" if task.get("status") in {"queued", "running"} else task.get("status", output["status"])
    for name in ("scenario_meta.json", "run_status.json", "gate.json", "comparison.json"):
        path = cfd_root / name
        if path.is_file():
            output[name.removesuffix(".json")] = _read_json(path, name)
    return output


def get_comparison(case_id: str, scenario_id: str, *, version: int | None = None) -> dict:
    _scenario, version_root, _selected_version = _version_root(case_id, scenario_id, version)
    status = get_status(case_id, scenario_id, version=version)
    comparison = status.get("comparison")
    if not comparison:
        return {"status": "missing", "reason": "鼻塞方案尚未生成独立 CFD 对比"}
    scenario_meta = status.get("scenario_meta") or {}
    scenario = status.get("scenario") or {}
    if scenarios._base_status(scenario_meta.get("base_reference"), scenarios._base_reference(case_id)) != "current":
        return {"status": "stale", "reason": "气道基准已变化，旧鼻塞结果仅供追溯，不能用于当前对比"}
    current_base_meta = cases.case_dir(case_id) / "cfd" / "case_meta.json"
    current_base_sha = plug_models.sha256_bytes(current_base_meta.read_bytes()) if current_base_meta.is_file() else None
    if comparison.get("base_case_meta_sha256") and current_base_sha != comparison.get("base_case_meta_sha256"):
        return {"status": "stale", "reason": "鼻塞算例绑定的基准 CFD 清单已变化，不能继续比较"}
    current_base_run_sha = _optional_file_sha256(cases.case_dir(case_id) / "cfd" / "run_status.json")
    if comparison.get("base_run_status_sha256") and current_base_run_sha != comparison.get("base_run_status_sha256"):
        return {"status": "stale", "reason": "鼻塞算例绑定的基准 CFD 运行记录已变化，不能继续比较"}
    current_base_result_signature = _cfd_result_signature(cases.case_dir(case_id) / "cfd")
    if comparison.get("base_result_signature") and current_base_result_signature != comparison.get("base_result_signature"):
        return {"status": "stale", "reason": "鼻塞算例绑定的基准网格或最终场已变化，不能继续比较"}
    scenario_meta_path = version_root / "cfd" / "scenario_meta.json"
    if "scenario_meta_sha256" in comparison and (
        not scenario_meta_path.is_file()
        or plug_models.sha256_bytes(scenario_meta_path.read_bytes()) != comparison.get("scenario_meta_sha256")
    ):
        return {"status": "stale", "reason": "鼻塞 CFD 输入清单已变化，旧结果不能继续比较"}
    expected_scenario_result_signature = comparison.get("scenario_result_signature")
    if expected_scenario_result_signature and _cfd_result_signature(version_root / "cfd") != expected_scenario_result_signature:
        return {"status": "stale", "reason": "鼻塞 CFD 网格或最终场已变化，旧对比结果不能继续使用"}
    return comparison
