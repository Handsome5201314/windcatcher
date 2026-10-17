"""OpenFOAM 求解后端探测与运行。

铁律：**没有可用求解后端就不产生任何结果**。
本机未安装 OpenFOAM 时返回 missing + 中文安装建议，绝不写出假的场数据或指标。
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import mesh_cache, preview
from .cfd_validation import (
    inspect_latest_fields, parse_mesh_checks, parse_residuals, pressure_metadata, read_json,
)
from .errors import PipelineError
from .config import openfoam_mpi_processes


def configured_image() -> str:
    """Docker OpenFOAM 镜像：环境变量优先，其次项目配置文件 .openfoam_image。"""
    image = os.environ.get("OPENFOAM_IMAGE") or os.environ.get("IMAGE") or ""
    if image:
        return image.strip()
    marker = Path(__file__).resolve().parent.parent / ".openfoam_image"
    if marker.exists():
        value = marker.read_text(encoding="utf-8").strip()
        if value:
            return value
    return ""


def remember_image(image: str) -> None:
    marker = Path(__file__).resolve().parent.parent / ".openfoam_image"
    marker.write_text(image.strip(), encoding="utf-8")


def _run(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=False)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except PermissionError as exc:
        return 126, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", "执行超时"
    except OSError as exc:
        return 125, "", str(exc)


def _stop_command(proc, cmd: list[str]) -> dict:
    """停止本次任务，Docker CLI 退出本身不能证明容器已停止。"""
    cleanup = {"status": "stopped"}
    if cmd[:2] == ["docker", "run"]:
        if "--name" not in cmd:
            cleanup = {"status": "unverified", "reason": "Docker 命令没有专属容器名，无法确认后台求解已停止"}
        else:
            name = cmd[cmd.index("--name") + 1]
            code, out, err = _run(["docker", "rm", "--force", name], timeout=20)
            if code != 0 and "No such container" not in out + err:
                cleanup = {"status": "unverified", "container": name, "reason": err or out or f"docker rm 退出码 {code}"}
            else:
                cleanup["container"] = name
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    return cleanup


def _run_streaming(
    cmd: list[str],
    timeout: int,
    on_line=None,
    *,
    on_tick=None,
    log_path: Path | None = None,
    cwd: Path | None = None,
) -> dict:
    """运行长任务并逐行回传输出，避免求解期间页面只看到空白等待。

    ``subprocess.run`` 只有进程结束后才会返回，无法支持实时残差和超时诊断。
    读取线程把 stdout 与主线程解耦，主线程因此可以同时执行超时检查和状态落盘。
    """
    started = time.monotonic()
    lines: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        return {"returncode": 127, "output": "", "timed_out": False, "elapsed_seconds": 0, "error": str(exc)}
    except PermissionError as exc:
        return {"returncode": 126, "output": "", "timed_out": False, "elapsed_seconds": 0, "error": str(exc)}
    except OSError as exc:
        return {"returncode": 125, "output": "", "timed_out": False, "elapsed_seconds": 0, "error": str(exc)}

    events: queue.Queue[str | None] = queue.Queue()

    def reader() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                events.put(line)
        finally:
            events.put(None)

    thread = threading.Thread(target=reader, name="openfoam-output", daemon=True)
    thread.start()
    closed, timed_out = False, False
    cleanup = None
    handle = None
    try:
        if log_path:
            handle = log_path.open("w", encoding="utf-8")
            handle.write(f"$ {' '.join(cmd)}\n\n")
            handle.flush()
        last_tick = started
        while True:
            try:
                line = events.get(timeout=0.25)
            except queue.Empty:
                line = ""
            if line is None:
                closed = True
            elif line:
                lines.append(line)
                if handle:
                    handle.write(line)
                    handle.flush()
                if on_line:
                    on_line(line, time.monotonic() - started)
            now = time.monotonic()
            if on_tick and now - last_tick >= 1:
                on_tick(now - started)
                last_tick = now
            if not timed_out and proc.poll() is None and now - started >= timeout:
                timed_out = True
                cleanup = _stop_command(proc, cmd)
            if proc.poll() is not None and closed:
                break
        if on_tick:
            on_tick(time.monotonic() - started)
    finally:
        if proc.poll() is None:
            _stop_command(proc, cmd)
        if handle:
            handle.close()
        thread.join(timeout=2)
    elapsed = round(time.monotonic() - started, 3)
    return {
        "returncode": 124 if timed_out else int(proc.returncode or 0),
        "output": "".join(lines),
        "timed_out": timed_out,
        "elapsed_seconds": elapsed,
        "cleanup": cleanup,
    }


def _solver_progress(log_text: str, cfd_dir: Path, controls: tuple[str, str] | None = None) -> dict:
    """从当前日志提取可证实的外层迭代、残差和 endTime。"""
    if controls is None:
        controls = tuple(path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
                         for path in (cfd_dir / "system" / "fvSolution", cfd_dir / "system" / "controlDict"))
    parsed = parse_residuals(log_text, *controls)
    last = parsed.get("history", [])[-1] if parsed.get("history") else {}
    end_time = parsed.get("end_time")
    current_time = parsed.get("last_time")
    iteration_percent = None
    if isinstance(end_time, (int, float)) and isinstance(current_time, (int, float)) and end_time > 0:
        iteration_percent = round(max(0.0, min(100.0, current_time / end_time * 100)), 3)
    return {
        "iteration": parsed.get("iterations", 0),
        "time": parsed.get("last_time"),
        "end_time": parsed.get("end_time"),
        "iteration_percent": iteration_percent,
        "initial_residual": parsed.get("initial_residual", {}),
        "final_residual": parsed.get("final_residual", {}),
        "status": parsed.get("status"),
        "last": last,
        "stop_reason": parsed.get("stop_reason"),
    }


def _write_status(path: Path, payload: dict) -> None:
    """原子写入运行记录，轮询时不会读到半截 JSON。"""
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    temp.write_text(text, encoding="utf-8")
    last_error = None
    # Windows may briefly deny replacing a file that the API reader or an
    # antivirus scanner has open. Retry the atomic replace before surfacing a
    # real status-write failure to the caller.
    for delay in (0.03, 0.08, 0.18, 0.35, 0.6):
        try:
            temp.replace(path)
            _clear_live_status(path)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(delay)

    # Windows Defender、索引器或浏览器读取器可能在整个重试窗口内暂时
    # 占用目标文件。状态记录不能反过来终止求解，因此保留一个旁路快照，
    # 读取端会优先使用它，下一次成功替换主文件时自动清理。
    live_path = _live_status_path(path)
    try:
        temp.replace(live_path)
        return
    except PermissionError:
        try:
            live_path.write_text(text, encoding="utf-8")
            temp.unlink(missing_ok=True)
            return
        except OSError:
            pass
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
    # 状态文件写入失败不应伪装成求解器失败。主流程会继续执行，下一次
    # 心跳或最终状态写入仍会再次尝试。
    return


def _live_status_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.live")


def _clear_live_status(path: Path) -> None:
    try:
        _live_status_path(path).unlink(missing_ok=True)
    except OSError:
        pass


def read_latest_status(path: Path) -> dict:
    """读取主状态或 Windows 锁定期间写入的旁路状态。"""
    live_path = _live_status_path(path)
    candidates = [candidate for candidate in (path, live_path) if candidate.exists()]
    if not candidates:
        raise FileNotFoundError(path)
    source = max(candidates, key=lambda candidate: candidate.stat().st_mtime_ns)
    return json.loads(source.read_text(encoding="utf-8"))


def status_exists(path: Path) -> bool:
    return path.exists() or _live_status_path(path).exists()


def _error_diagnosis(name: str, result: dict, output: str, timeout: int) -> tuple[str, list[str]]:
    if name == "foamToVTK":
        if result.get("timed_out") or result.get("returncode") == 124:
            cleanup = result.get("cleanup") or {}
            stop = "；容器停止未能确认，请检查 Docker" if cleanup.get("status") == "unverified" else ""
            reason = f"附加 VTK 文件导出超时（超过 {timeout} 秒）{stop}；已完成的 OpenFOAM 求解场保留"
        else:
            reason = f"附加 VTK 文件导出失败（退出码 {result.get('returncode')}）；已完成的 OpenFOAM 求解场保留"
        return reason, [
            "病例流场页面直接读取 OpenFOAM 的 U、p 场，可单独生成浏览器可视化，不需要重新求解",
            "VTK 文件供外部查看器使用，导出失败不等于气流计算失败；未完成的 VTK 文件不能用于复核",
            "查看 cfd/logs/foamToVTK.log 定位导出错误",
        ]
    if result.get("timed_out") or result.get("returncode") == 124:
        stopped = result.get("cleanup") or {}
        stop_message = "已被终止" if stopped.get("status") != "unverified" else "容器停止未能确认，请检查 Docker"
        return (
            f"{name} 超过 {timeout} 秒仍未完成，{stop_message}（这是超时，不是求解器返回的收敛失败）",
            [
                "查看最后一次外层迭代和残差，确认求解是否仍在推进",
                "优先使用 MPI 并行求解；若网格仍超过机器承受范围，再调整网格策略",
            ],
        )
    capacity = re.search(r"WINDCATCHER_MPI_CAPACITY_EXCEEDED requested=(\d+) available=(\d+)", output)
    slots = re.search(r"not enough slots.*?satisfy the\s+(\d+)\s+slots", output, flags=re.I | re.S)
    if capacity or slots:
        count = (capacity or slots).group(1)
        available = f"当前容器可用上限为 {capacity.group(2)} 个 CPU 进程" if capacity else "MPI 在当前运行环境中识别到的 CPU 槽位少于请求数"
        return (
            f"MPI 槽位不足：请求 {count} 个进程，{available}；未开始气流迭代",
            [
                "Windows 的 CPU 数量与 Docker / WSL 中实际可用的 CPU 数量可能不同",
                "新版会先检查并行启动，并在 Docker 中按可用虚拟 CPU 线程分配 MPI 槽位",
                "若请求数超过后端上限，请降低自定义进程数，或调整 Docker / WSL 的 CPU 资源后重试",
            ],
        )
    fatal = re.search(r"FOAM FATAL (?:ERROR|IO ERROR)\s*:\s*(.*?)(?:\n|$)", output, flags=re.I)
    if fatal:
        return (
            f"{name} 返回 OpenFOAM 致命错误：{fatal.group(1).strip()}",
            ["打开对应步骤日志查看完整上下文", "检查边界名称、字段边界条件与网格 patch 是否一致"],
        )
    return (
        f"{name} 退出码 {result.get('returncode')}" + (f"：{result['error']}" if result.get("error") else ""),
        [f"查看 cfd/logs/{name}.log 的最后 100 行", "不要把未完成的场数据当作成功结果"],
    )


def _progress_message(name: str, solver: dict, elapsed: float) -> str:
    if name == "foamToVTK":
        return f"正在导出最终场的附加 VTK 文件 · 已耗时 {elapsed:.0f} 秒；浏览器病例流场直接读取 OpenFOAM 场，该导出没有可核实的百分比"
    if name == "simpleFoam":
        iteration = solver.get("iteration") or 0
        end_time = solver.get("end_time")
        if isinstance(end_time, (int, float)) and solver.get("time") is not None:
            note = f" · 外层迭代上限参考 {solver['time']:g}/{end_time:g}（{solver.get('iteration_percent', 0):g}%）"
        elif isinstance(end_time, (int, float)):
            note = f" · 外层迭代上限 {end_time:g}"
        else:
            note = " · 总迭代数未知"
        if solver.get("initial_residual"):
            residual = ", ".join(f"{key}={value:.2e}" for key, value in solver["initial_residual"].items())
            return f"外层迭代 {iteration} · 初始残差 {residual}{note} · 已耗时 {elapsed:.0f} 秒"
        return f"等待求解器首轮残差 · 已耗时 {elapsed:.0f} 秒"
    return f"正在处理 · 已耗时 {elapsed:.0f} 秒；该步骤没有可核实的百分比"


def _mesh_summary(cfd_dir: Path) -> dict:
    """从 checkMesh 日志提取可核对的规模，失败时保留缺失原因。"""
    path = cfd_dir / "logs" / "checkMesh.log"
    if not path.exists():
        return {"check_status": "missing", "reason": "缺少 checkMesh 日志"}
    text = path.read_text(encoding="utf-8", errors="replace")
    counts = {}
    for label, pattern in (
        ("points", r"\bpoints:\s*(\d+)"),
        ("faces", r"\bfaces:\s*(\d+)"),
        ("internal_faces", r"\binternal faces:\s*(\d+)"),
        ("cells", r"\bcells:\s*(\d+)"),
    ):
        match = re.search(pattern, text)
        if match:
            counts[label] = int(match.group(1))
    checks = parse_mesh_checks(cfd_dir)
    return {
        **counts,
        "check_status": checks.get("status", "missing"),
        "mesh_ok": bool(checks.get("mesh_ok")),
        "clean": bool(checks.get("clean")),
        "reason": checks.get("reason"),
    }


def _execution_summary(backend: str, parallel_active: bool, requested_procs: int) -> dict:
    return {
        "backend": backend,
        "mode": "mpi" if parallel_active else "serial",
        "requested_subdomains": requested_procs if parallel_active else 1,
        "gpu_used": False,
        "note": "OpenFOAM simpleFoam 默认使用 CPU；GPU 不会自动参与" if not parallel_active else "MPI 并行使用 CPU；GPU 不会自动参与",
    }


def _docker_mpi_command(count: int, program: str) -> str:
    # Docker / WSL 的虚拟核心拓扑不等于 Windows 的物理核心拓扑。
    # 显式允许按线程分配，但仍由 MPI 检查实际槽位，不能 oversubscribe。
    return f"mpirun --allow-run-as-root --use-hwthread-cpus -np {count} {program}"


def _docker_mpi_preflight(count: int) -> str:
    """生成网格前验证真实容器资源和相同的 MPI 启动配置。"""
    return f'''set -eu
logical=$(nproc)
physical=0
if command -v lscpu >/dev/null 2>&1; then
    physical=$(lscpu -p=CORE,SOCKET | sed '/^#/d' | sort -u | wc -l)
fi
limit=$logical
# nproc respects affinity; also respect Docker cgroup CPU quotas.
quota=max
period=100000
if [ -r /sys/fs/cgroup/cpu.max ]; then
    read -r quota period < /sys/fs/cgroup/cpu.max
elif [ -r /sys/fs/cgroup/cpu/cpu.cfs_quota_us ]; then
    quota=$(cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us)
    period=$(cat /sys/fs/cgroup/cpu/cpu.cfs_period_us)
fi
if [ "$quota" != max ] && [ "$quota" -gt 0 ] && [ "$period" -gt 0 ]; then
    quota_limit=$(( (quota + period - 1) / period ))
    if [ "$quota_limit" -lt "$limit" ]; then limit=$quota_limit; fi
fi
printf 'WINDCATCHER_CPU_LOGICAL=%s\\nWINDCATCHER_CPU_PHYSICAL=%s\\nWINDCATCHER_MPI_MAX_PROCS=%s\\n' "$logical" "$physical" "$limit"
if [ {count} -gt "$limit" ]; then
    printf 'WINDCATCHER_MPI_CAPACITY_EXCEEDED requested={count} available=%s\\n' "$limit"
    exit 2
fi
command -v simpleFoam
simpleFoam -help >/dev/null
{_docker_mpi_command(count, 'true')}
'''


def _docker_runtime_summary(output: str, requested_procs: int) -> dict:
    cpu = {}
    for marker, key in (("CPU_LOGICAL", "logical_cores"), ("CPU_PHYSICAL", "physical_cores"),
                        ("MPI_MAX_PROCS", "max_processes")):
        match = re.search(rf"(?m)^WINDCATCHER_{marker}=(\d+)\s*$", output)
        if match and int(match.group(1)) > 0:
            cpu[key] = int(match.group(1))
    return {"backend": "docker", "cpu": cpu, "allocation_mode": "hwthread", "requested_subdomains": requested_procs}


def _prepare_decomposition(cfd_dir: Path, count: int) -> None:
    """确保分区文件与 mpirun 核数一致，保留已配置的分区方法。"""
    path = cfd_dir / "system" / "decomposeParDict"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if len(re.findall(r"(?m)^\s*numberOfSubdomains\s+\d+\s*;", text)) != 1:
            raise PipelineError("blocked", "无法确认 decomposeParDict 的分区数量", ["检查 numberOfSubdomains 配置后重试"])
        text = re.sub(r"(?m)^(\s*numberOfSubdomains\s+)\d+(\s*;)", rf"\g<1>{count}\2", text)
    else:
        text = ('FoamFile\n{\n    version 2.0;\n    format ascii;\n    class dictionary;\n'
                '    object decomposeParDict;\n}\n'
                f'numberOfSubdomains {count};\nmethod scotch;\ndistributed no;\nroots ();\n')
    path.write_text(text, encoding="utf-8")


def _mesh_backend_identity(chosen: str, meta: dict) -> dict:
    identity = {"name": chosen, "openfoam_flavour": meta.get("openfoam_flavour", "org")}
    if chosen == "docker":
        code, out, err = _run(["docker", "image", "inspect", "--format", "{{.Id}}", configured_image()], timeout=20)
        if code == 0 and re.fullmatch(r"sha256:[0-9a-f]{64}", out.strip()):
            identity["image_id"] = out.strip()
        else:
            identity["reason"] = err or out or "无法核对本机镜像内容标识"
    return identity


def _archive_run_outputs(cfd_dir: Path, run_id: str, *, rebuild: bool, previous_status: dict) -> dict:
    """Move old outputs aside so latestTime cannot silently select a previous run."""
    archive = cfd_dir / "run_history" / run_id
    if not archive.resolve().is_relative_to(cfd_dir.resolve()):
        raise PipelineError("blocked", "归档目录超出当前算例，无法安全保存旧结果", ["检查 cfd/run_history 后重试"])
    candidates = []
    for path in cfd_dir.iterdir():
        is_time = False
        try:
            value = float(path.name)
            is_time = math.isfinite(value) and value != 0
        except ValueError:
            pass
        if is_time or re.fullmatch(r"processors?\d+(?:_\d+-\d+)?", path.name) or path.name in {"postProcessing", "VTK", "results"}:
            candidates.append(path)
    for relative in ("0/polyMesh", *(["constant/polyMesh", mesh_cache.MANIFEST_FILENAME] if rebuild else [])):
        path = cfd_dir / relative
        if path.exists():
            candidates.append(path)
    archive.mkdir(parents=True, exist_ok=False)
    archived = []
    for path in candidates:
        if path.is_symlink() or not path.resolve().is_relative_to(cfd_dir.resolve()):
            raise PipelineError("blocked", "旧结果路径超出当前算例，无法安全归档", ["检查算例内的链接目录后重试"])
        relative = path.relative_to(cfd_dir)
        target = archive / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target))
        archived.append(relative.as_posix())
    old_logs = archive / "logs"
    for path in (cfd_dir / "logs").iterdir():
        if path.is_file() and path.name not in {"mpiPreflight.log", "prepareRun.log"}:
            old_logs.mkdir(exist_ok=True)
            shutil.copy2(path, old_logs / path.name)
    if previous_status:
        (archive / "run_status.json").write_text(json.dumps(previous_status, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": archive.relative_to(cfd_dir).as_posix(), "artifacts": archived}


def detect_backends() -> dict:
    """探测可用的求解后端：本机 / WSL / Docker。"""
    backends = []

    native = shutil.which("simpleFoam") or shutil.which("blockMesh")
    if native:
        code, out, _ = _run(["simpleFoam", "-help"], timeout=20)
        backends.append(
            {
                "name": "native",
                "status": "available",
                "detail": native,
                "remediation": [],
            }
        )
    else:
        backends.append(
            {
                "name": "native",
                "status": "missing",
                "detail": None,
                "remediation": [
                    "Windows 原生没有官方 OpenFOAM 发行版，推荐在 WSL2（Ubuntu）中安装",
                    "或准备本地 Docker OpenFOAM 镜像（镜像拉取需用户明确同意）",
                ],
            }
        )

    code, out, err = _run(["wsl.exe", "bash", "-lc", "command -v simpleFoam"], timeout=25)
    if code == 0 and out:
        backends.append({"name": "wsl", "status": "available", "detail": out, "remediation": []})
    elif code in (124, 125, 126, 127):
        backends.append(
            {
                "name": "wsl",
                "status": "unknown",
                "detail": None,
                "remediation": [
                    "本机执行 wsl.exe 被环境策略/权限拦截，无法判定是否已安装 OpenFOAM",
                    "请在允许执行 wsl.exe 的环境手动运行 `wsl bash -lc 'command -v simpleFoam'` 确认",
                ],
            }
        )
    else:
        backends.append(
            {
                "name": "wsl",
                "status": "missing",
                "detail": None,
                "remediation": [
                    f"WSL 中未找到 simpleFoam（退出码 {code}）",
                    "安装 WSL2 + Ubuntu 后按 OpenFOAM 官方步骤安装，再重试 cfd-run",
                ],
            }
        )

    docker = shutil.which("docker")
    if not docker:
        backends.append(
            {
                "name": "docker",
                "status": "missing",
                "detail": None,
                "remediation": ["安装并启动 Docker Desktop 后再试"],
            }
        )
    else:
        code, out, err = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=20)
        if code != 0 or not out:
            backends.append(
                {
                    "name": "docker",
                    "status": "unknown",
                    "detail": None,
                    "remediation": [f"docker 存在但守护进程未响应（退出码 {code}）：{err[:200]}"],
                }
            )
        else:
            # 守护进程在跑 ≠ 有 OpenFOAM：必须存在可用镜像才算后端可用
            image = configured_image()
            if not image:
                backends.append(
                    {
                        "name": "docker",
                        "status": "missing",
                        "detail": f"Docker 守护进程已运行（ServerVersion {out}），但未配置 OpenFOAM 镜像",
                        "remediation": [
                            "设置环境变量 OPENFOAM_IMAGE，或在 windcatcher 目录写入 .openfoam_image 文件（内容为镜像名）",
                            "镜像拉取涉及网络与大体积下载，需用户明确同意，本工具不会自动执行",
                        ],
                    }
                )
            else:
                code_img, out_img, err_img = _run(["docker", "image", "inspect", image], timeout=30)
                if code_img == 0:
                    backends.append(
                        {
                            "name": "docker",
                            "status": "available",
                            "detail": f"镜像 {image}",
                            "remediation": [],
                        }
                    )
                else:
                    backends.append(
                        {
                            "name": "docker",
                            "status": "missing",
                            "detail": f"镜像 {image} 不存在",
                            "remediation": [
                                f"先准备镜像：`docker pull {image}`（需用户同意，本工具不会自动拉取）",
                                "或改用 WSL 中的 OpenFOAM",
                            ],
                        }
                    )

    available = [b for b in backends if b["status"] == "available"]
    return {
        "backends": backends,
        "available": [b["name"] for b in available],
        "remediation": available[0]["remediation"] if available else [
            "本机未检测到任何可用 OpenFOAM 后端：simpleFoam 不在 PATH，WSL 无法探测，Docker 未就绪",
            "推荐顺序：① 安装 WSL2 + Ubuntu + OpenFOAM；② 或启动 Docker 并准备 OpenFOAM 镜像",
            "在求解后端就绪前，报告只会写“未生成 CFD 结果”，不会给出任何流场数值",
        ],
    }


def _read_postprocessing(case_dir: Path, fields: dict | None = None) -> dict:
    """读取 postProcessing 下的流量与压力采样；没有就报缺失，绝不猜数。"""
    base = case_dir / "postProcessing"
    if not base.exists():
        return {"status": "missing", "reason": "没有 postProcessing 目录（求解尚未运行或未输出）"}
    values: dict[str, float] = {}
    times: dict[str, float] = {}
    errors = []
    selected = {}
    for path in sorted(base.rglob("*.dat")):
        if not path.is_file():
            continue
        try:
            lines = [
                line for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
                if line.strip() and not line.startswith("#")
            ]
        except OSError as exc:
            errors.append(f"{path.relative_to(base)}: {exc}")
            continue
        if not lines:
            continue
        # v11 的函数对象目录名（inletFlowRate 等）才是稳定键；文件名可能是 surfaceFieldValue.dat
        key = path.parent.parent.name.lower()
        try:
            row = lines[-1].split()
            sampled_time = float(row[0])
            last_value = float(row[-1])
        except (ValueError, IndexError):
            errors.append(f"{path.relative_to(base)}: 最后数据行无法解析")
            continue
        if not math.isfinite(last_value) or not math.isfinite(sampled_time):
            errors.append(f"{path.relative_to(base)}: 数据包含非有限值")
            continue
        sample_name = next((name for token, name in (
            ("inletflowrate", "inlet_flow_m3s"), ("outletflowrate", "outlet_flow_m3s"),
            ("wallflowrate", "wall_flow_m3s"), ("inletpressure", "inlet_pressure_field"),
            ("outletpressure", "outlet_pressure_field"),
        ) if token in key), None)
        if sample_name and (sample_name not in times or sampled_time > times[sample_name]):
            values[sample_name] = last_value
            times[sample_name] = sampled_time
            selected[sample_name] = str(path.relative_to(case_dir))
    if not values:
        return {"status": "missing", "reason": "postProcessing 里没有可解析的流量/压力数据"}
    fields = fields if fields is not None else inspect_latest_fields(case_dir)
    phi = fields.get("fields", {}).get("phi", {})
    patch_fluxes = phi.get("patch_fluxes_m3s") or {}
    if phi.get("complete") and "wall" in patch_fluxes and "wall_flow_m3s" not in values:
        values["wall_flow_m3s"] = patch_fluxes["wall"]
        selected["wall_flow_m3s"] = f"{fields['latest_time']}/phi:wall"
    p_dimensions = fields.get("fields", {}).get("p", {}).get("dimensions")
    pressure = pressure_metadata(p_dimensions, read_json(case_dir / "case_meta.json"))
    result = {"status": "parsed", **values, "sample_times": times, "sources": selected, "errors": errors, "pressure": pressure}
    expected_time = fields.get("latest_time")
    result["samples_current"] = expected_time is not None and all(time == float(expected_time) for time in times.values())
    for end in ("inlet", "outlet"):
        value = values.get(f"{end}_pressure_field")
        if value is not None:
            if pressure.get("field_unit") == "m2/s2":
                result[f"{end}_pressure_m2s2"] = value
            if pressure.get("pa_multiplier") is not None:
                result[f"{end}_pressure_pa"] = value * pressure["pa_multiplier"]
    inlet = values.get("inlet_flow_m3s")
    outlet = values.get("outlet_flow_m3s")
    if inlet is not None and outlet is not None:
        result["flow_direction_ok"] = inlet < 0 and outlet > 0
        wall = values.get("wall_flow_m3s")
        result["wall_flow_checked"] = wall is not None
        all_patches_checked = bool(phi.get("complete")) and set(patch_fluxes) == set(fields.get("mesh", {}).get("patch_faces", {}))
        result["all_boundary_fluxes_checked"] = all_patches_checked
        result["patch_fluxes_m3s"] = patch_fluxes
        result["net_boundary_flow_m3s"] = sum(patch_fluxes.values()) if all_patches_checked else None
        result["inlet_outlet_imbalance_m3s"] = inlet + outlet
        if abs(inlet) > 1e-12:
            result["mass_conservation_error_percent"] = round(
                abs(inlet + outlet) / abs(inlet) * 100, 6
            )
            if wall is not None:
                result["wall_leakage_percent"] = abs(wall) / abs(inlet) * 100
            if all_patches_checked:
                result["net_mass_conservation_error_percent"] = abs(result["net_boundary_flow_m3s"]) / abs(inlet) * 100
        else:
            result["mass_conservation_error_percent"] = None
            result["mass_conservation_note"] = "入口流量接近 0，无法计算守恒误差"
    return result


def run_case(
    case_id: str,
    backend: str | None = None,
    timeout_s: int = 7200,
    *,
    cfd_dir_override: Path | None = None,
) -> dict:
    """尝试运行求解；没有后端就返回 missing，不写任何结果文件。"""
    from .cases import case_dir, update_stage

    root = case_dir(case_id)
    cfd_dir = Path(cfd_dir_override) if cfd_dir_override is not None else root / "cfd"
    if cfd_dir_override is not None:
        try:
            cfd_dir.resolve(strict=False).relative_to(root.resolve())
        except ValueError as exc:
            raise PipelineError("blocked", "鼻塞方案算例目录不在病例目录内", ["重新生成鼻塞方案 CFD 算例"]) from exc
    stage_case_id = case_id if cfd_dir_override is None else None

    def stage_update(status: str, *, message: str | None = None,
                     artifacts: list[str] | None = None, data: dict | None = None) -> None:
        if stage_case_id is None:
            return
        update_stage(stage_case_id, "cfd_run", status, message=message, artifacts=artifacts, data=data)
    seg_path = root / "segment" / "segmentation.json"
    if not seg_path.exists():
        raise PipelineError("blocked", "还没有分割记录，不能开始气流求解", ["先完成气道分割并人工确认"])
    segmentation = json.loads(seg_path.read_text(encoding="utf-8"))
    if segmentation.get("need_human_confirm", True):
        raise PipelineError(
            "blocked",
            "气道掩膜尚未人工确认，不能开始气流求解",
            ["先在 CT 复核视窗确认气道范围", "确认后重新生成 STL、几何质检和 CFD 算例"],
            {"stage": "segment", "human_review": segmentation.get("human_review", {})},
        )
    review = preview.viewer_metadata(case_id)
    if review.get("mask_external_air_suspected"):
        raise PipelineError(
            "blocked",
            "候选掩膜疑似包含体外空气，不能开始气流求解",
            [
                "先在 CT 复核视窗逐层确认并修正气道掩膜",
                "确认后重新执行气道分割、STL 与几何质检、CFD 算例",
            ],
            {"stage": "segment", "review": review},
        )
    if not (cfd_dir / "system" / "controlDict").exists():
        raise PipelineError(
            "blocked", "还没有算例文件", ["先执行 cfd-prep 生成算例"], {"stage": "cfd_prep"}
        )

    detection = detect_backends()
    if not detection["available"]:
        payload = {
            "status": "missing",
            "backend": None,
            "detection": detection,
            "remediation": detection["remediation"],
            "message": "本机未检测到可用的 OpenFOAM 后端，未执行求解，也没有生成任何 CFD 结果",
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        (cfd_dir / "run_status.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        stage_update(
            "blocked",
            message="未检测到 OpenFOAM 后端：未生成 CFD 结果",
            artifacts=["cfd/run_status.json"],
            data={"solved": False, "backend": None},
        )
        return payload

    chosen = backend or detection["available"][0]
    if chosen not in detection["available"]:
        raise PipelineError("blocked", f"所选 {chosen} 求解后端不可用", ["在环境页面检查所选后端"])
    (cfd_dir / "logs").mkdir(parents=True, exist_ok=True)

    try:
        meta = read_json(cfd_dir / "case_meta.json")
    except (OSError, ValueError) as exc:
        raise PipelineError("blocked", f"算例配置无法读取：{exc}", ["检查 cfd/case_meta.json 后重试"]) from exc
    parallel_meta = meta.get("parallel", {}) if isinstance(meta.get("parallel", {}), dict) else {}
    try:
        requested_procs = openfoam_mpi_processes(parallel_meta)
    except ValueError as exc:
        raise PipelineError("blocked", str(exc), ["修正并行核数配置后重试"]) from exc
    parallel_requested = requested_procs > 1
    if parallel_requested:
        if chosen == "native" and any(not shutil.which(tool) for tool in ("decomposePar", "mpirun", "reconstructPar")):
            raise PipelineError("blocked", "当前本机后端缺少 MPI 工具，不能按指定核数并行", ["安装 MPI 和 OpenFOAM 并行工具，或设置 OPENFOAM_MPI_PROCS=1 使用单进程"])
        _prepare_decomposition(cfd_dir, requested_procs)
    run_id = uuid.uuid4().hex
    status_path = cfd_dir / "run_status.json"
    previous_status = read_latest_status(status_path) if status_exists(status_path) else {}
    identity = _mesh_backend_identity(chosen, meta)
    signature = mesh_cache.mesh_inputs_signature(cfd_dir, identity)
    reuse = mesh_cache.inspect_reuse(cfd_dir, signature)
    reuse_mesh = reuse["reusable"]
    parallel_mesh = chosen == "docker" and parallel_requested
    generation = (reuse.get("manifest", {}).get("generation", {}) if reuse_mesh else
                  {"mode": "parallel" if parallel_mesh else "serial", "requested_subdomains": requested_procs if parallel_mesh else 1, "run_id": run_id})

    if chosen == "docker":
        # Pin the actual local image, so changing a tag cannot change this run.
        image = identity.get("image_id") or configured_image()
        volume_arg = f"{cfd_dir.resolve()}:/case"
        env_source = ". /opt/openfoam11/etc/bashrc >/dev/null 2>&1; "

        def command(inner: str, step: str) -> list[str]:
            name = f"windcatcher-{run_id}-{step}"
            return ["docker", "run", "--rm", "--name", name, "--entrypoint", "bash", "-v", volume_arg, "-w", "/case", image, "-c", env_source + inner]
        def mpi(program: str) -> str:
            return _docker_mpi_command(requested_procs, program)
    elif chosen == "wsl":
        prefix = f"cd '{cfd_dir.as_posix()}' && "
        def command(inner: str, step: str) -> list[str]:
            return ["wsl.exe", "bash", "-lc", prefix + inner]
        def mpi(program: str) -> str:
            return f"mpirun -np {requested_procs} {program}"
    else:  # native
        def command(inner: str, step: str) -> list[str]:
            return inner.split()
        def mpi(program: str) -> str:
            return f"mpirun -np {requested_procs} {program}"

    plan = []
    if chosen == "docker" and parallel_requested:
        plan.append(("mpiPreflight", _docker_mpi_preflight(requested_procs)))
    plan.append(("prepareRun", ""))
    if not reuse_mesh:
        plan.append(("blockMesh", "blockMesh"))
        if parallel_mesh:
            plan.extend([
                ("decomposeMesh", "decomposePar -force -noFields"),
                ("snappyHexMesh", mpi("snappyHexMesh -parallel -overwrite")),
                ("reconstructMesh", "reconstructPar -constant -noFields -time 0"),
            ])
        else:
            plan.append(("snappyHexMesh", "snappyHexMesh -overwrite"))
    plan.append(("checkMesh", "checkMesh"))
    if parallel_requested:
        plan.extend([
            ("decomposePar", "decomposePar -force -time 0"),
            ("simpleFoam", mpi("simpleFoam -parallel")),
            ("reconstructPar", "reconstructPar -latestTime"),
        ])
    else:
        plan.append(("simpleFoam", "simpleFoam"))
    if chosen == "docker":
        # External-viewer export is independent of the browser, which reads
        # OpenFOAM directly. Avoid converting the initial field and writing
        # millions of cell values as text on Windows bind mounts.
        plan.append(("foamToVTK", "foamToVTK -latestTime -fields '(U p)' -noFunctionObjects"))
    commands = [(command(inner, name) if inner else [], name) for name, inner in plan]
    parallel_active = parallel_requested
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    run_started = time.monotonic()
    mesh_summary = {"check_status": "pending", "reason": "等待本次 checkMesh 检查"}
    execution = _execution_summary(chosen, parallel_active, requested_procs)
    execution["mesh"] = {
        "action": "reused" if reuse_mesh else "rebuilt", **generation,
        "status": "pending",
        "reason": reuse["reason"], "cache": {"status": "checking", "reason": "等待本次网格质量检查"},
    }
    if reuse_mesh:
        execution["mesh"]["source_run_id"] = generation.get("run_id") or reuse["manifest"]["check_mesh"]["run_id"]
    parallel_record = {**parallel_meta, "enabled": parallel_active,
                       "requested_subdomains": requested_procs if parallel_active else 1}

    def live_status(current_step: str | None, progress: dict, solver: dict, message: str | None = None) -> dict:
        payload = {
            "status": "running",
            "run_id": run_id,
            "backend": chosen,
            "execution": execution,
            "parallel": parallel_record,
            "mesh": mesh_summary,
            "current_step": current_step,
            "started_at": started_at,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "elapsed_seconds": round(time.monotonic() - run_started, 1),
            "timeout_seconds": timeout_s,
            "steps": steps,
            "progress": progress,
            "solver": solver,
        }
        if message:
            payload["message"] = message
        return payload

    stage_update(
        "running",
        message=f"已选择 {chosen} 后端，准备执行 CFD 求解" + (f"（{requested_procs} 核并行）" if parallel_active else "（单进程）"),
        data={"solved": False, "backend": chosen, "current_step": None, "run_id": run_id},
    )
    steps = []
    solver_progress = {}
    _write_status(status_path, live_status(
        None,
        {"percent": 0, "completed": 0, "total": len(commands), "current_step_percent": None, "label": "准备执行"},
        solver_progress,
    ))
    for cmd, name in commands:
        step_started = time.monotonic()
        current_lines: list[str] = []
        solver_progress = {}
        solver_records = 0
        current_counted = False
        controls = tuple(path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
                         for path in (cfd_dir / "system" / "fvSolution", cfd_dir / "system" / "controlDict"))
        last_output_at = None
        progress = {
            "percent": round(len(steps) / len(commands) * 100, 1),
            "completed": len(steps),
            "total": len(commands),
            "current_step_percent": None,
            "label": f"正在执行 {name}",
        }
        _write_status(status_path, live_status(name, progress, solver_progress))

        def refresh_solver() -> None:
            nonlocal solver_progress, solver_records, current_counted
            if name != "simpleFoam":
                return
            # 只解析当前外层迭代，避免每行重扫从启动到现在的全部日志。
            parsed = _solver_progress("".join(current_lines), cfd_dir, controls)
            if parsed.get("iteration"):
                if not current_counted:
                    solver_records += 1
                    current_counted = True
                solver_progress = {**parsed, "iteration": solver_records}

        def on_line(line: str, elapsed: float) -> None:
            nonlocal current_counted, last_output_at
            last_output_at = datetime.now().astimezone().isoformat(timespec="seconds")
            if name == "simpleFoam":
                if re.match(r"^\s*Time\s*=", line):
                    refresh_solver()
                    current_lines.clear()
                    current_counted = False
                current_lines.append(line)

        def on_tick(elapsed: float) -> None:
            refresh_solver()
            message = _progress_message(name, solver_progress, elapsed)
            progress["current_step_percent"] = (
                solver_progress.get("iteration_percent") if name == "simpleFoam" else None
            )
            payload = live_status(name, {**progress, "label": message}, solver_progress)
            payload["step_elapsed_seconds"] = round(elapsed, 1)
            payload["last_output_at"] = last_output_at
            _write_status(status_path, payload)
        stage_update(
            "running",
            message=f"正在执行 {name}，请保持页面打开查看进度",
            data={"backend": chosen, "current_step": name, "solved": False, "run_id": run_id},
        )
        log_path = cfd_dir / "logs" / f"{name}.log"
        step_timeout = min(timeout_s, 60) if name == "mpiPreflight" else timeout_s
        if name == "prepareRun":
            try:
                execution["archive"] = _archive_run_outputs(cfd_dir, run_id, rebuild=not reuse_mesh, previous_status=previous_status)
                out = f"旧结果已归档：{execution['archive']['path']}\n"
                result = {"returncode": 0, "output": out, "elapsed_seconds": round(time.monotonic() - step_started, 3)}
            except (OSError, PipelineError) as exc:
                result = {"returncode": 1, "output": str(exc), "error": str(exc), "elapsed_seconds": round(time.monotonic() - step_started, 3)}
            log_path.write_text(result["output"], encoding="utf-8")
        else:
            result = _run_streaming(cmd, timeout=step_timeout, on_line=on_line, on_tick=on_tick,
                                    log_path=log_path, cwd=cfd_dir if chosen == "native" else None)
        code, out = result["returncode"], result.get("output", "")
        if result.get("error"):
            log_path.write_text(f"$ {' '.join(cmd)}\n\n{result['error']}\n", encoding="utf-8")
        if name == "mpiPreflight":
            execution["runtime"] = _docker_runtime_summary(out, requested_procs)
        if name == "checkMesh":
            mesh_summary = _mesh_summary(cfd_dir)
        step_elapsed = result.get("elapsed_seconds")
        steps.append({
            "name": name,
            "returncode": code,
            "log": f"cfd/logs/{name}.log",
            "elapsed_seconds": step_elapsed,
            "step_elapsed_seconds": step_elapsed,
            "timed_out": result.get("timed_out", False) or code == 124,
            "cleanup": result.get("cleanup"),
        })
        if name == "foamToVTK" and code != 0:
            reason, remediation = _error_diagnosis(name, result, out, step_timeout)
            steps[-1].update(message=reason, remediation=remediation)
        if name == "checkMesh" and code == 0:
            execution["mesh"]["status"] = "checked"
            if signature.get("valid"):
                current_signature = mesh_cache.mesh_inputs_signature(cfd_dir, identity)
                if not current_signature.get("valid") or current_signature.get("signature") != signature["signature"]:
                    code = 1
                    out += "\n网格输入在运行期间发生变化，已停止求解；请重新生成算例后重试\n"
                    result.update(returncode=code, output=out, error="网格输入在运行期间发生变化")
                    steps[-1]["returncode"] = code
            if code == 0:
                cache_result = mesh_cache.record_mesh(cfd_dir, signature, run_id, check_result=result,
                                                      completed_steps=steps, generation=generation)
                execution["mesh"]["cache"] = {"status": "ready" if cache_result["recorded"] else "unavailable", "reason": cache_result["reason"]}
                if cache_result.get("input_changed") or (reuse_mesh and not cache_result["recorded"]):
                    code = 1
                    out += f"\n复用网格复核失败：{cache_result['reason']}\n"
                    result.update(returncode=code, output=out, error=cache_result["reason"])
                    steps[-1]["returncode"] = code
        if code != 0 and name != "foamToVTK":  # 可视化转换失败不阻断求解状态
            reason, remediation = _error_diagnosis(name, result, out, step_timeout)
            if name in mesh_cache.MESH_STEPS:
                execution["mesh"]["status"] = "failed"
                execution["mesh"]["cache"] = {"status": "unavailable", "reason": reason}
            final_solver = _solver_progress(out, cfd_dir) if name == "simpleFoam" else solver_progress
            progress["current_step_percent"] = final_solver.get("iteration_percent") if name == "simpleFoam" else None
            progress["label"] = reason
            payload = {
                "status": "failed",
                "run_id": run_id,
                "backend": chosen,
                "execution": execution,
                "parallel": parallel_record,
                "mesh": mesh_summary,
                "failed_step": name,
                "current_step": name,
                "returncode": code,
                "timed_out": bool(result.get("timed_out") or code == 124),
                "cleanup": result.get("cleanup"),
                "steps": steps,
                "elapsed_seconds": round(time.monotonic() - run_started, 1),
                "step_elapsed_seconds": step_elapsed,
                "last_output_at": last_output_at,
                "timeout_seconds": timeout_s,
                "progress": progress,
                "solver": final_solver, "message": reason,
                "last_output": [line for line in out.splitlines() if line.strip()][-20:],
                "remediation": [*remediation, f"日志：cfd/logs/{name}.log"],
            }
            _write_status(status_path, payload)
            stage_update(
                "failed",
                message=reason,
                artifacts=["cfd/run_status.json"],
                data={"solved": False, "backend": chosen, "failed_step": name, "run_status": payload, "run_id": run_id},
            )
            return payload

    log_path = cfd_dir / "logs" / "simpleFoam.log"
    log_text = log_path.read_text(encoding="utf-8", errors="ignore") if log_path.exists() else ""
    residuals = _case_residuals(cfd_dir)
    fields = inspect_latest_fields(cfd_dir)
    post = _read_postprocessing(cfd_dir, fields)

    payload = {
        "status": "ran",
        "run_id": run_id,
        "backend": chosen,
        "execution": execution,
        "parallel": parallel_record,
        "mesh": mesh_summary,
        "steps": steps,
        "elapsed_seconds": round(time.monotonic() - run_started, 1),
        "progress": {"percent": 100, "completed": len(steps), "total": len(commands), "current_step_percent": None, "label": "命令流程已结束，结果待复核"},
        "residuals": residuals,
        "postprocessing": post,
        "field_validation": fields,
        "mesh_validation": parse_mesh_checks(cfd_dir),
        "report_eligible": False,
        "validation": {
            "status": "待 CFD 工程师复核",
            "note": "即使求解结束，也需要残差/质量守恒/网格敏感性复核后才能进入患者报告",
        },
    }
    _write_status(status_path, payload)
    stage_update(
        "warn",
        message=f"求解已在 {chosen} 后端执行完毕，等待 CFD 工程师复核",
        artifacts=["cfd/run_status.json"],
        data={"solved": True, "backend": chosen, "report_eligible": False, "run_id": run_id},
    )
    return payload


def _case_residuals(cfd_dir: Path) -> dict:
    def text(path: Path) -> str:
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    return parse_residuals(
        text(cfd_dir / "logs" / "simpleFoam.log"),
        text(cfd_dir / "system" / "fvSolution"),
        text(cfd_dir / "system" / "controlDict"),
    )


def check_result_gate(case_id: str, *, cfd_dir_override: Path | None = None) -> dict:
    """判断 CFD 结果是否够格进入患者报告。"""
    from .cases import case_dir

    root = case_dir(case_id)
    cfd_dir = Path(cfd_dir_override) if cfd_dir_override is not None else root / "cfd"
    if cfd_dir_override is not None:
        try:
            cfd_dir.resolve(strict=False).relative_to(root.resolve())
        except ValueError as exc:
            raise PipelineError("blocked", "鼻塞方案结果目录不在病例目录内", ["重新生成鼻塞方案 CFD 算例"]) from exc
    status_path = cfd_dir / "run_status.json"
    checks = []

    if not status_path.exists():
        return {
            "eligible": False, "numeric_ok": False,
            "checks": [{"name": "求解状态文件", "status": "missing", "detail": "cfd/run_status.json 不存在"}],
            "reasons": ["尚未执行求解（或本机没有 OpenFOAM 后端）"],
        }

    try:
        status = read_latest_status(status_path)
    except (OSError, ValueError) as exc:
        return {"eligible": False, "numeric_ok": False, "checks": [{"name": "求解状态文件", "status": "invalid", "detail": str(exc)}], "reasons": ["求解状态文件无法读取"]}
    if status.get("status") != "ran":
        return {
            "eligible": False, "numeric_ok": False,
            "checks": [{"name": "求解状态", "status": status.get("status", "unknown"), "detail": status.get("message", "")}],
            "reasons": ["求解未成功完成"] + status.get("remediation", []),
        }

    # 现场重读日志与 postProcessing（run_status.json 可能是早前写入的快照）
    residuals = _case_residuals(cfd_dir)
    fields = inspect_latest_fields(cfd_dir)
    post = _read_postprocessing(cfd_dir, fields)
    mesh = parse_mesh_checks(cfd_dir)
    residual_ok = residuals.get("status") == "parsed" and residuals.get("converged")
    checks.append(
        {
            "name": "残差收敛",
            "status": "ok" if residual_ok else "pending",
            "detail": {"initial_residual": residuals.get("initial_residual"), "residual_control": residuals.get("residual_control"), "stop_reason": residuals.get("stop_reason"), "reason": residuals.get("reason")},
        }
    )

    mass_error = post.get("mass_conservation_error_percent")
    mass_ok = (
        mass_error is not None and mass_error < 2.0 and post.get("flow_direction_ok")
        and post.get("samples_current") and post.get("all_boundary_fluxes_checked")
        and post.get("net_mass_conservation_error_percent", math.inf) < 2.0
        and post.get("wall_leakage_percent", math.inf) < 0.1 and not post.get("errors")
    )
    checks.append(
        {
            "name": "质量守恒",
            "status": "ok" if mass_ok else "pending",
            "detail": {"inlet_outlet_error_percent": mass_error, "flow_direction_ok": post.get("flow_direction_ok"), "net_boundary_flow_m3s": post.get("net_boundary_flow_m3s"), "wall_leakage_percent": post.get("wall_leakage_percent"), "samples_current": post.get("samples_current"), "errors": post.get("errors")},
        }
    )

    field_ok = fields.get("complete") and residuals.get("last_time") == float(fields["latest_time"])
    checks.append({"name": "最终场完整性", "status": "ok" if field_ok else "pending", "detail": fields})
    pressure_ok = post.get("pressure", {}).get("status") == "checked"
    checks.append({"name": "压力维度与单位", "status": "ok" if pressure_ok else "pending", "detail": post.get("pressure", {})})
    mesh_ok = mesh.get("clean") is True
    checks.append({"name": "网格与封闭壳", "status": "ok" if mesh_ok else "pending", "detail": mesh})

    validation = status.get("validation", {})
    sensitivity = validation.get("mesh_sensitivity", {})
    sensitivity_ok = (
        isinstance(sensitivity, dict) and sensitivity.get("status") == "passed"
        and bool(sensitivity.get("evidence"))
    )

    checks.append(
        {
            "name": "网格敏感性",
            "status": "ok" if sensitivity_ok else "pending",
            "detail": sensitivity if sensitivity else "尚未记录粗/中/细网格敏感性检查证据",
        }
    )
    review = validation.get("engineering_review", {})
    review_ok = (
        isinstance(review, dict) and review.get("status") == "approved"
        and bool(review.get("reviewed_at")) and bool(review.get("reviewer")) and bool(review.get("evidence"))
    )
    checks.append(
        {
            "name": "CFD 工程师复核",
            "status": "ok" if review_ok else "pending",
            "detail": review if review else validation.get("status", "待复核"),
        }
    )

    numeric_ok = bool(residual_ok and mass_ok and field_ok and pressure_ok and mesh_ok)
    eligible = numeric_ok and sensitivity_ok and review_ok
    metrics = {key: post.get(key) for key in (
        "inlet_flow_m3s", "outlet_flow_m3s", "wall_flow_m3s", "net_boundary_flow_m3s",
        "mass_conservation_error_percent", "net_mass_conservation_error_percent", "wall_leakage_percent",
        "inlet_pressure_pa", "outlet_pressure_pa", "inlet_pressure_m2s2", "outlet_pressure_m2s2",
    )}
    for unit in ("pa", "m2s2"):
        inlet = post.get(f"inlet_pressure_{unit}")
        outlet = post.get(f"outlet_pressure_{unit}")
        metrics[f"delta_p_{unit}"] = inlet - outlet if inlet is not None and outlet is not None else None
    metrics["pressure_unit"] = post.get("pressure", {}).get("display_unit")
    return {
        "eligible": eligible, "numeric_ok": numeric_ok,
        "checks": checks,
        "reasons": [f"{check['name']}尚未通过" for check in checks if check["status"] != "ok"],
        "metrics": metrics, "convergence": residuals, "fields": fields, "mesh": mesh, "postprocessing": post,
    }
