"""Synthetic, read-only comparison contract; never treats a missing GPU trace as proof."""

import json
from pathlib import Path

import pytest

from scripts import compare_gpu_cfd


def _pair(tmp_path):
    paths = {}
    for target, seconds in (("cpu", 88.0), ("gpu", 44.0)):
        root = tmp_path / target
        root.mkdir()
        manifest = {
            "signature": "a" * 64,
            "generation": {"run_id": "mesh-001"},
            "mesh_files": {"constant/polyMesh/points": {"size": 10, "sha256": "b" * 64}},
        }
        (root / "mesh_cache.json").write_text(json.dumps(manifest), encoding="utf-8")
        status = {
            "status": "ran", "compute_target": target, "run_id": f"{target}-run",
            "execution": {
                "mesh": {"run_id": "mesh-001"},
                "gpu_used": target == "gpu",
                "gpu": {"kernel_evidence": {
                    "status": "observed", "source": "CUPTI_ACTIVITY_KIND_CONCURRENT_KERNEL",
                    "log": "cfd/logs/simpleFoam.log", "device_kernel_count": 3,
                    "marker_count": 1, "probe_errors": [],
                }} if target == "gpu" else {},
            },
            "steps": [{"name": "simpleFoam", "returncode": 0, "solver_exit_code": 0 if target == "gpu" else None, "elapsed_seconds": seconds}],
            "residuals": {
                "status": "parsed", "iterations": 100, "last_time": 100.0,
                "initial_residual": {"Ux": 0.002, "p": 0.01},
                "final_residual": {"Ux": 0.00001, "p": 0.000001},
            },
            "postprocessing": {
                "status": "parsed", "samples_current": True,
                "inlet_left_flow_m3s": -0.00013, "inlet_right_flow_m3s": -0.00013,
                "outlet_flow_m3s": 0.00026,
                "inlet_pressure_pa": 18.0, "outlet_pressure_pa": 0.0,
                "mass_conservation_error_percent": 0.01,
                "net_mass_conservation_error_percent": 0.02,
            },
        }
        path = root / "run_status.json"
        path.write_text(json.dumps(status), encoding="utf-8")
        if target == "gpu":
            (root / "logs").mkdir()
            (root / "logs" / "simpleFoam.log").write_text(
                "Time = 100\nWINDCATCHER_GPU_DEVICE_KERNELS=3\n", encoding="utf-8"
            )
        paths[target] = path
    return paths


def _change(path, callback):
    value = json.loads(path.read_text(encoding="utf-8"))
    callback(value)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_reports_side_by_side_without_quality_verdict(tmp_path):
    paths = _pair(tmp_path)
    result = compare_gpu_cfd.compare(paths["cpu"], paths["gpu"])
    assert result["iterations"] == 100
    assert result["solver_time_ratio_cpu_over_gpu"] == 2
    assert result["cpu"]["pressure_drop"] == {"value": 18.0, "unit": "Pa"}
    assert result["gpu"]["inlet_left_flow_m3s"] == -0.00013
    assert result["gpu"]["cuda_kernel_evidence"]["observed"] is True
    assert "verdict" not in result


@pytest.mark.parametrize(
    ("file", "mutation", "message"),
    [
        ("mesh_cache.json", lambda value: value.update(signature="c" * 64), "网格签名"),
        ("run_status.json", lambda value: value["residuals"].update(iterations=99), "迭代数"),
    ],
)
def test_rejects_different_mesh_or_iteration(tmp_path, file, mutation, message):
    paths = _pair(tmp_path)
    _change(paths["gpu"].parent / file, mutation)
    with pytest.raises(compare_gpu_cfd.ComparisonError, match=message):
        compare_gpu_cfd.compare(paths["cpu"], paths["gpu"])


def test_no_kernel_evidence_is_not_reported_as_gpu_use(tmp_path):
    paths = _pair(tmp_path)
    _change(paths["gpu"], lambda value: value["execution"].update(gpu_used=False, gpu={"kernel_evidence": {"status": "not_observed"}}))
    with pytest.raises(compare_gpu_cfd.ComparisonError, match="CUPTI 设备内核证据"):
        compare_gpu_cfd.compare(paths["cpu"], paths["gpu"])


def test_rejects_gpu_claim_without_device_activity_record(tmp_path):
    paths = _pair(tmp_path)
    _change(paths["gpu"], lambda value: value["execution"]["gpu"]["kernel_evidence"].update(source="nsys cuda_api_sum"))
    with pytest.raises(compare_gpu_cfd.ComparisonError, match="CUPTI 设备内核证据"):
        compare_gpu_cfd.compare(paths["cpu"], paths["gpu"])


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["execution"]["gpu"]["kernel_evidence"].update(device_kernel_count=0),
        lambda value: value["execution"]["gpu"]["kernel_evidence"].update(marker_count=2),
        lambda value: value["execution"]["gpu"]["kernel_evidence"].update(probe_errors=["WINDCATCHER_GPU_PROBE_ERROR=flush:1"]),
        lambda value: value["steps"][0].update(solver_exit_code=1),
        lambda value: value["steps"][0].update(validation_failure="gpu_probe_unverified"),
    ],
)
def test_rejects_incomplete_cupti_contract(tmp_path, mutation):
    paths = _pair(tmp_path)
    _change(paths["gpu"], mutation)
    with pytest.raises(compare_gpu_cfd.ComparisonError, match="CUPTI 设备内核证据"):
        compare_gpu_cfd.compare(paths["cpu"], paths["gpu"])


@pytest.mark.parametrize(
    "log_text",
    [
        "Time = 100\nWINDCATCHER_GPU_DEVICE_KERNELS=2\n",
        "Time = 100\nWINDCATCHER_GPU_DEVICE_KERNELS=3\nWINDCATCHER_GPU_DEVICE_KERNELS=3\n",
        "Time = 100\nWINDCATCHER_GPU_DEVICE_KERNELS=3\nWINDCATCHER_GPU_PROBE_UNVERIFIED=kernels:3,dropped:1\n",
    ],
)
def test_rejects_stale_duplicate_or_unverified_probe_log(tmp_path, log_text):
    paths = _pair(tmp_path)
    (paths["gpu"].parent / "logs" / "simpleFoam.log").write_text(log_text, encoding="utf-8")
    with pytest.raises(compare_gpu_cfd.ComparisonError, match="CUPTI 设备内核标记"):
        compare_gpu_cfd.compare(paths["cpu"], paths["gpu"])


def test_archived_status_can_use_same_generation_mesh_cache(tmp_path):
    paths = _pair(tmp_path)
    archived = paths["cpu"].parent / "run_history" / "next-run"
    archived.mkdir(parents=True)
    cpu_archive = archived / "run_status.json"
    cpu_archive.write_bytes(paths["cpu"].read_bytes())
    result = compare_gpu_cfd.compare(cpu_archive, paths["gpu"])
    assert result["mesh_signature"] == "a" * 64


SYNTHETIC_CPU_BASELINE = Path("C:/Temp/windcatcher-y-poc-36pztbs2/case-20261111-183615-f045/cfd/run_status.json")


@pytest.mark.skipif(not SYNTHETIC_CPU_BASELINE.is_file(), reason="本机合成 CPU 基线不在此路径")
def test_existing_synthetic_cpu_baseline_contract(tmp_path):
    cpu = SYNTHETIC_CPU_BASELINE
    cpu_status = json.loads(cpu.read_text(encoding="utf-8"))
    gpu_root = tmp_path / "synthetic-gpu-record"
    gpu_root.mkdir()
    (gpu_root / "mesh_cache.json").write_bytes((cpu.parent / "mesh_cache.json").read_bytes())
    cpu_step = next(step for step in cpu_status["steps"] if step["name"] == "simpleFoam")
    gpu = json.loads(json.dumps(cpu_status))
    gpu["compute_target"] = "gpu"
    gpu["run_id"] = "synthetic-comparison-only"
    gpu["execution"]["gpu_used"] = True
    gpu["execution"]["gpu"] = {"kernel_evidence": {
        "status": "observed", "source": "CUPTI_ACTIVITY_KIND_CONCURRENT_KERNEL",
        "log": "cfd/logs/simpleFoam.log", "device_kernel_count": 3,
        "marker_count": 1, "probe_errors": [],
    }}
    gpu_step = next(step for step in gpu["steps"] if step["name"] == "simpleFoam")
    gpu_step["solver_exit_code"] = 0
    gpu_step["elapsed_seconds"] = cpu_step["elapsed_seconds"] / 2
    gpu_path = gpu_root / "run_status.json"
    gpu_path.write_text(json.dumps(gpu), encoding="utf-8")
    (gpu_root / "logs").mkdir()
    (gpu_root / "logs" / "simpleFoam.log").write_text(
        "Time = 100\nWINDCATCHER_GPU_DEVICE_KERNELS=3\n", encoding="utf-8"
    )
    result = compare_gpu_cfd.compare(cpu, gpu_path)
    assert result["iterations"] == 100
    assert result["cpu"]["simpleFoam_seconds"] == pytest.approx(88.486)
    assert result["cpu"]["pressure_drop"] == {"value": 18.0, "unit": "Pa"}
    assert result["cpu"]["inlet_left_flow_m3s"] < 0
    assert result["cpu"]["inlet_right_flow_m3s"] < 0
