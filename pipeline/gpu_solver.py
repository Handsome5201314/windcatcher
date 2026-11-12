"""Verified, single-GPU OpenFOAM solver capability and case dictionary.

The ordinary OpenFOAM image still builds and checks the mesh. A separate
NVIDIA-compiled image may solve the same case only after a synthetic CFD smoke
test has been recorded in its immutable image labels. Merely exposing a GPU to
the ordinary image does not make simpleFoam a GPU solver.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from .errors import PipelineError

DEFAULT_GPU_IMAGE = "windcatcher/openfoam-gpu:2606-cc86"
REQUIRED_LABELS = {
    "org.windcatcher.gpu-offload": "verified",
    "org.windcatcher.gpu-arch": "cc86",
    "org.windcatcher.openfoam-flavour": "esi",
    "org.windcatcher.gpu-cfd-smoke": "passed",
}


def _run(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=False)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except (FileNotFoundError, PermissionError, OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)


def probe_gpu_solver() -> dict:
    """Only advertise GPU when the pinned image and live CUDA runtime pass."""
    image_ref = os.environ.get("OPENFOAM_GPU_IMAGE", DEFAULT_GPU_IMAGE).strip()
    if not image_ref:
        return {"available": False, "reason": "未配置 OPENFOAM_GPU_IMAGE"}
    code, output, error = _run(["docker", "image", "inspect", image_ref], timeout=20)
    if code != 0:
        return {"available": False, "reason": f"GPU 求解镜像不可用：{error or output or image_ref}"}
    try:
        image = json.loads(output)[0]
        image_id = image["Id"]
        labels = image["Config"]["Labels"] or {}
    except (ValueError, TypeError, KeyError, IndexError):
        return {"available": False, "reason": "无法核对 GPU 镜像内容与验证标签"}
    if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        return {"available": False, "reason": "无法固定 GPU 镜像内容 ID"}
    if any(labels.get(key) != value for key, value in REQUIRED_LABELS.items()):
        return {"available": False, "reason": "GPU 镜像尚未通过编译及合成 CFD 验证；不会把普通 CPU 镜像当作 GPU 求解器"}
    preflight = (
        "set -euo pipefail; "
        "test \"${WM_COMPILER:-}\" = Nvidia-gpu; "
        "command -v simpleFoam >/dev/null; "
        "device=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -n 1); "
        "cap=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -n 1); "
        "test \"$cap\" = 8.6; "
        "test -n \"$device\"; printf 'WINDCATCHER_GPU_DEVICE=%s\\n' \"$device\""
    )
    code, output, error = _run(
        ["docker", "run", "--rm", "--gpus", "device=0", "--entrypoint", "/openfoam/bash", image_id, "-c", preflight],
        timeout=45,
    )
    if code != 0:
        return {"available": False, "reason": f"GPU 容器预检失败（需 CUDA 8.6 设备与 Nvidia-gpu 编译）：{error or output or code}"}
    match = re.search(r"(?m)^WINDCATCHER_GPU_DEVICE=(.+)$", output)
    if not match:
        return {"available": False, "reason": "GPU 容器没有报告可用 CUDA 设备"}
    return {
        "available": True,
        "reason": "GPU 镜像已通过合成 CFD 验证，CUDA 设备与编译配置可用",
        "device_name": match.group(1).strip(),
        "image": image_ref,
        "image_id": image_id,
        "architecture": "cc86",
    }


def prepare_gpu_fv_solution(cfd_dir: Path) -> Path:
    """Write a solver-only overlay; never alter the case's CPU dictionary."""
    system = Path(cfd_dir) / "system"
    source = system / "fvSolution"
    try:
        original = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise PipelineError("blocked", f"无法读取 CPU 求解配置：{exc}", ["重新生成 CFD 算例后重试"]) from exc
    result, pressure_count = re.subn(
        r"\b(smoother\s+)GaussSeidel(\s*;)", r"\1twoStageGaussSeidel\2", original
    )
    result, velocity_count = re.subn(
        r"\b(smoother\s+)symGaussSeidel(\s*;)", r"\1twoStageGaussSeidel\2", result
    )
    if pressure_count != 1 or velocity_count != 1:
        raise PipelineError(
            "blocked", "当前 fvSolution 不符合已核对的 GPU 算例模板",
            ["重新生成 CFD 算例；不要直接复用未知求解器配置"],
        )
    target = system / "fvSolution.gpu"
    temporary = system / "fvSolution.gpu.tmp"
    temporary.write_text(result, encoding="utf-8")
    temporary.replace(target)
    return target
