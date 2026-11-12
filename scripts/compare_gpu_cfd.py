"""Read-only CPU/GPU CFD comparison for the same synthetic mesh and iteration count.

Usage: python scripts/compare_gpu_cfd.py --cpu path/to/run_status.json --gpu path/to/run_status.json
The adjacent mesh_cache.json (including an archived run's mesh cache) supplies
the signature and mesh-file hashes, which run_status.json does not contain.
This reports measurements only; it does not grant any CFD or clinical approval.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path


class ComparisonError(ValueError):
    """The two recorded runs cannot be compared reliably."""


def _object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ComparisonError(f"无法读取 {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ComparisonError(f"{path} 必须是 JSON 对象")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ComparisonError(f"{label} 缺失或不是有限数值")
    return float(value)


def _mesh_proof(status_path: Path, status: dict) -> tuple[str, str]:
    mesh = (status.get("execution") or {}).get("mesh") or {}
    generation_id = mesh.get("source_run_id") or mesh.get("run_id")
    if not isinstance(generation_id, str) or not generation_id:
        raise ComparisonError(f"{status_path} 缺少网格生成 run_id")
    base = status_path.parent
    candidates = [base / "mesh_cache.json"]
    if base.parent.name == "run_history":
        candidates.append(base.parent.parent / "mesh_cache.json")
    for path in candidates:
        if not path.is_file():
            continue
        manifest = _object(path)
        if (manifest.get("generation") or {}).get("run_id") != generation_id:
            continue
        signature = manifest.get("signature")
        mesh_files = manifest.get("mesh_files")
        if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature):
            raise ComparisonError(f"{path} 缺少可核对的网格签名")
        if not isinstance(mesh_files, dict) or not mesh_files:
            raise ComparisonError(f"{path} 缺少网格文件指纹")
        digest = hashlib.sha256(json.dumps(mesh_files, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return signature, digest
    raise ComparisonError(f"{status_path} 找不到与该次网格生成记录匹配的 mesh_cache.json")


def _run_summary(path: Path, target: str) -> dict:
    status = _object(path)
    if status.get("status") != "ran" or status.get("compute_target") != target:
        raise ComparisonError(f"{path} 不是已完成的 {target.upper()} 求解记录")
    signature, mesh_files_digest = _mesh_proof(path, status)
    residuals = status.get("residuals") or {}
    iterations = residuals.get("iterations")
    if residuals.get("status") != "parsed" or isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 1:
        raise ComparisonError(f"{path} 缺少完整迭代残差记录")
    initial = residuals.get("initial_residual")
    final = residuals.get("final_residual")
    if not isinstance(initial, dict) or not initial or not isinstance(final, dict) or not final:
        raise ComparisonError(f"{path} 缺少末次残差")
    last_residuals = {
        kind: {field: _number(value, f"{target} {kind}.{field}") for field, value in values.items()}
        for kind, values in (("initial", initial), ("final", final))
    }
    steps = [step for step in status.get("steps", []) if isinstance(step, dict) and step.get("name") == "simpleFoam"]
    if len(steps) != 1 or steps[0].get("returncode") != 0:
        raise ComparisonError(f"{path} 缺少成功的 simpleFoam 步骤")
    solver_seconds = _number(steps[0].get("elapsed_seconds"), f"{target} simpleFoam 耗时")
    if solver_seconds <= 0:
        raise ComparisonError(f"{path} simpleFoam 耗时必须大于零")
    post = status.get("postprocessing") or {}
    if post.get("status") != "parsed" or post.get("samples_current") is not True:
        raise ComparisonError(f"{path} 缺少本次求解的后处理采样")
    if post.get("errors"):
        raise ComparisonError(f"{path} 后处理采样有错误，不能可靠对比")
    values = {
        key: _number(post.get(key), f"{target} {key}")
        for key in (
            "inlet_left_flow_m3s", "inlet_right_flow_m3s", "outlet_flow_m3s",
            "mass_conservation_error_percent", "net_mass_conservation_error_percent",
        )
    }
    if post.get("inlet_pressure_pa") is not None and post.get("outlet_pressure_pa") is not None:
        pressure_unit, pressure_suffix = "Pa", "pa"
    else:
        pressure_unit, pressure_suffix = "m2/s2", "m2s2"
    pressure_drop = (
        _number(post.get(f"inlet_pressure_{pressure_suffix}"), f"{target} 入口压力")
        - _number(post.get(f"outlet_pressure_{pressure_suffix}"), f"{target} 出口压力")
    )
    execution = status.get("execution") or {}
    evidence = (execution.get("gpu") or {}).get("kernel_evidence") or {}
    if target == "gpu":
        device_count = evidence.get("device_kernel_count")
        if not (
            execution.get("gpu_used") is True
            and evidence.get("status") == "observed"
            and evidence.get("source") == "CUPTI_ACTIVITY_KIND_CONCURRENT_KERNEL"
            and evidence.get("log") == "cfd/logs/simpleFoam.log"
            and isinstance(device_count, int) and not isinstance(device_count, bool) and device_count > 0
            and evidence.get("marker_count") == 1
            and evidence.get("probe_errors") == []
            and steps[0].get("solver_exit_code") == 0
            and not steps[0].get("validation_failure")
        ):
            raise ComparisonError(f"{path} 缺少可信的本次 CUPTI 设备内核证据")
        log_path = path.parent / "logs" / "simpleFoam.log"
        try:
            log_lines = [line.strip() for line in log_path.read_text(encoding="utf-8").splitlines()]
        except (OSError, UnicodeError) as exc:
            raise ComparisonError(f"无法核对 {log_path} 的 CUPTI 设备内核证据：{exc}") from exc
        markers = [line for line in log_lines if line.startswith("WINDCATCHER_GPU_DEVICE_KERNELS")]
        if (
            markers != [f"WINDCATCHER_GPU_DEVICE_KERNELS={device_count}"]
            or any(line.startswith("WINDCATCHER_GPU_PROBE_") for line in log_lines)
        ):
            raise ComparisonError(f"{log_path} 的 CUPTI 设备内核标记与运行记录不一致")
    return {
        "run_id": status.get("run_id"),
        "mesh_signature": signature, "mesh_files_digest": mesh_files_digest,
        "iterations": iterations, "last_time": residuals.get("last_time"),
        "simpleFoam_seconds": solver_seconds, "last_residuals": last_residuals,
        **values, "pressure_drop": {"value": pressure_drop, "unit": pressure_unit},
        "cuda_kernel_evidence": {"observed": execution.get("gpu_used") is True, "record": evidence} if target == "gpu" else None,
    }


def compare(cpu_path: Path, gpu_path: Path) -> dict:
    cpu = _run_summary(Path(cpu_path), "cpu")
    gpu = _run_summary(Path(gpu_path), "gpu")
    if cpu["mesh_signature"] != gpu["mesh_signature"] or cpu["mesh_files_digest"] != gpu["mesh_files_digest"]:
        raise ComparisonError("CPU 与 GPU 的网格签名或网格文件指纹不同，不能对比")
    if cpu["iterations"] != gpu["iterations"] or cpu["last_time"] != gpu["last_time"]:
        raise ComparisonError("CPU 与 GPU 的迭代数或末次时间不同，不能对比")
    if cpu["pressure_drop"]["unit"] != gpu["pressure_drop"]["unit"]:
        raise ComparisonError("CPU 与 GPU 的压力单位不同，不能对比")
    return {
        "scope": "CFD side-by-side measurements; no quality verdict",
        "mesh_signature": cpu["mesh_signature"],
        "mesh_files_digest": cpu["mesh_files_digest"],
        "iterations": cpu["iterations"],
        "cpu": {key: value for key, value in cpu.items() if key not in {"mesh_signature", "mesh_files_digest", "iterations"}},
        "gpu": {key: value for key, value in gpu.items() if key not in {"mesh_signature", "mesh_files_digest", "iterations"}},
        "solver_time_ratio_cpu_over_gpu": cpu["simpleFoam_seconds"] / gpu["simpleFoam_seconds"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpu", type=Path, required=True, help="CPU run_status.json")
    parser.add_argument("--gpu", type=Path, required=True, help="GPU run_status.json")
    args = parser.parse_args(argv)
    try:
        result = compare(args.cpu, args.gpu)
    except ComparisonError as exc:
        print(f"对比未生成：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
