"""GPU solve must be explicit, verified and isolated from CPU meshing."""

import json
import asyncio

import pytest
from pydantic import ValidationError

import server
from pipeline import cases, gpu_solver, mesh_cache, openfoam_runner
from pipeline.errors import PipelineError


def _image_inspect(labels):
    return json.dumps([{"Id": "sha256:" + "a" * 64, "Config": {"Labels": labels}}])


def test_gpu_probe_rejects_unverified_image(monkeypatch):
    monkeypatch.setenv("OPENFOAM_GPU_IMAGE", "windcatcher/openfoam-gpu:test")
    monkeypatch.setattr(gpu_solver, "_run", lambda cmd, timeout: (0, _image_inspect({}), ""))
    result = gpu_solver.probe_gpu_solver()
    assert result["available"] is False
    assert "验证" in result["reason"]


def test_gpu_probe_requires_runtime_gpu_and_compiler(monkeypatch):
    monkeypatch.setenv("OPENFOAM_GPU_IMAGE", "windcatcher/openfoam-gpu:test")
    labels = {
        "org.windcatcher.gpu-offload": "verified",
        "org.windcatcher.gpu-arch": "cc86",
        "org.windcatcher.openfoam-flavour": "esi",
        "org.windcatcher.gpu-cfd-smoke": "passed",
    }
    calls = []

    def run(cmd, timeout):
        calls.append(cmd)
        if cmd[:3] == ["docker", "image", "inspect"]:
            return 0, _image_inspect(labels), ""
        return 1, "", "GPU runtime unavailable"

    monkeypatch.setattr(gpu_solver, "_run", run)
    result = gpu_solver.probe_gpu_solver()
    assert result["available"] is False
    assert "GPU runtime unavailable" in result["reason"]
    assert any("--gpus" in cmd for cmd in calls)


def test_gpu_probe_reports_pinned_verified_image_and_device(monkeypatch):
    monkeypatch.setenv("OPENFOAM_GPU_IMAGE", "windcatcher/openfoam-gpu:test")
    labels = dict(gpu_solver.REQUIRED_LABELS)
    calls = []

    def run(cmd, timeout):
        calls.append(cmd)
        if cmd[:3] == ["docker", "image", "inspect"]:
            return 0, _image_inspect(labels), ""
        return 0, "WINDCATCHER_GPU_DEVICE=NVIDIA GeForce RTX 3090", ""

    monkeypatch.setattr(gpu_solver, "_run", run)
    result = gpu_solver.probe_gpu_solver()
    assert result["available"] is True
    assert result["device_name"] == "NVIDIA GeForce RTX 3090"
    assert result["image_id"] == "sha256:" + "a" * 64
    assert result["image_id"] in calls[-1]
    assert "compute_cap" in calls[-1][-1]


def test_gpu_solution_is_separate_from_cpu_dictionary(tmp_path):
    system = tmp_path / "system"
    system.mkdir()
    cpu = system / "fvSolution"
    original = "solver GAMG; smoother GaussSeidel; solver smoothSolver; smoother symGaussSeidel;"
    cpu.write_text(original, encoding="utf-8")
    gpu = gpu_solver.prepare_gpu_fv_solution(tmp_path)
    assert cpu.read_text(encoding="utf-8") == original
    assert gpu == system / "fvSolution.gpu"
    assert gpu.read_text(encoding="utf-8").count("twoStageGaussSeidel") == 2


def test_gpu_request_never_falls_back_to_cpu(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = cases.create_case("synthetic", "<合成测试>", synthetic=True)
    root = cases.case_dir(case_id)
    (root / "segment" / "segmentation.json").write_text('{"need_human_confirm": false}', encoding="utf-8")
    (root / "cfd" / "system").mkdir(parents=True)
    (root / "cfd" / "system" / "controlDict").write_text("application simpleFoam;", encoding="utf-8")
    monkeypatch.setattr(openfoam_runner.preview, "viewer_metadata", lambda _: {})
    monkeypatch.setattr(openfoam_runner, "detect_backends", lambda: {"available": ["docker"]})
    monkeypatch.setattr(openfoam_runner.gpu_solver, "probe_gpu_solver", lambda: {"available": False, "reason": "合成测试：GPU 镜像未验证"})
    with pytest.raises(PipelineError, match="GPU 镜像未验证"):
        openfoam_runner.run_case(case_id, compute_target="gpu")
    assert not (root / "cfd" / "run_status.json").exists()


def test_invalid_compute_target_is_rejected():
    with pytest.raises(ValueError, match="compute_target"):
        openfoam_runner.validate_compute_target("auto")


def test_api_defaults_to_cpu_and_advertises_verified_gpu_separately(monkeypatch):
    assert server.StageBody().compute_target == "cpu"
    with pytest.raises(ValidationError):
        server.StageBody(compute_target="auto")
    monkeypatch.setattr(server.capabilities, "probe_capabilities", lambda: {})
    monkeypatch.setattr(server, "detect_compute_capabilities", lambda: {"cpu": {"logical_cores": 8}})
    monkeypatch.setattr(server.gpu_solver, "probe_gpu_solver", lambda: {"available": False, "reason": "镜像未验证"})
    result = asyncio.run(server.api_capabilities())
    assert result["compute"]["cpu"]["logical_cores"] == 8
    assert result["compute"]["gpu_solver"] == {"available": False, "reason": "镜像未验证"}


@pytest.mark.parametrize(
    ("probe_output", "solver_exit", "expected_failure", "expected_message", "device_kernels", "evidence_status"),
    [
        ("WINDCATCHER_GPU_DEVICE_KERNELS=3\n", 0, None, None, 3, "observed"),
        ("", 0, "gpu_device_kernel_evidence_missing", "CUPTI 设备内核", None, "not_observed"),
        ("WINDCATCHER_GPU_PROBE_UNVERIFIED=kernels:0,dropped:0\n", 0, "gpu_probe_unverified", "完整性检查", None, "not_observed"),
        ("WINDCATCHER_GPU_DEVICE_KERNELS=0\n", 0, "gpu_device_kernel_evidence_missing", "唯一且有效", None, "not_observed"),
        ("WINDCATCHER_GPU_DEVICE_KERNELS=3\nWINDCATCHER_GPU_DEVICE_KERNELS=3\n", 0, "gpu_device_kernel_evidence_missing", "唯一且有效", None, "not_observed"),
        ("WINDCATCHER_GPU_DEVICE_KERNELS=3\nWINDCATCHER_GPU_PROBE_ERROR=flush:1\n", 0, "gpu_probe_unverified", "完整性检查", 3, "not_observed"),
        ("cudaLaunchKernel\n", 0, "gpu_device_kernel_evidence_missing", "CUPTI 设备内核", None, "not_observed"),
        ("WINDCATCHER_GPU_DEVICE_KERNELS=3\n", 1, "gpu_solver_failed", "实际退出码 1", 3, "observed"),
    ],
)
def test_gpu_run_uses_gpu_image_only_for_serial_solver(
    tmp_path, monkeypatch, probe_output, solver_exit, expected_failure, expected_message, device_kernels, evidence_status
):
    expect_success = expected_failure is None
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    monkeypatch.delenv("OPENFOAM_MPI_PROCS", raising=False)
    case_id = cases.create_case("synthetic", "<合成测试>", synthetic=True)
    root = cases.case_dir(case_id)
    (root / "segment" / "segmentation.json").write_text('{"need_human_confirm": false}', encoding="utf-8")
    cfd = root / "cfd"
    (cfd / "system").mkdir()
    (cfd / "system" / "controlDict").write_text("endTime 10;", encoding="utf-8")
    (cfd / "system" / "fvSolution").write_text(
        "solvers { p { solver GAMG; smoother GaussSeidel; } "
        '"(U|k|omega)" { solver smoothSolver; smoother symGaussSeidel; } }',
        encoding="utf-8",
    )
    (cfd / "system" / "decomposeParDict").write_text(
        "numberOfSubdomains 1;\nmethod scotch;\n", encoding="utf-8"
    )
    (cfd / "case_meta.json").write_text('{"openfoam_flavour":"esi", "parallel":{"profile":"legacy","requested_subdomains":2}}', encoding="utf-8")
    monkeypatch.setattr(openfoam_runner.preview, "viewer_metadata", lambda _: {})
    monkeypatch.setattr(openfoam_runner, "detect_backends", lambda: {"available": ["docker"]})
    monkeypatch.setattr(openfoam_runner, "configured_image", lambda: "microfluidica/openfoam:latest")
    monkeypatch.setattr(openfoam_runner, "_mesh_backend_identity", lambda *_: {"name": "docker", "openfoam_flavour": "esi"})
    gpu_id = "sha256:" + "b" * 64
    monkeypatch.setattr(openfoam_runner.gpu_solver, "probe_gpu_solver", lambda: {
        "available": True, "device_name": "RTX 3090", "image_id": gpu_id, "architecture": "cc86",
    })
    observed = {}

    def run(cmd, timeout, on_line, **kwargs):
        name = kwargs["log_path"].stem
        observed[name] = cmd
        output = "Mesh OK.\n" if name == "checkMesh" else ""
        if name == "simpleFoam":
            assert cmd[-1] == "simpleFoam"
            output = "Time = 1\n" + probe_output
        kwargs["log_path"].write_text(output, encoding="utf-8")
        return {"returncode": solver_exit if name == "simpleFoam" else 0, "output": output, "elapsed_seconds": .1}

    monkeypatch.setattr(openfoam_runner, "_run_streaming", run)
    result = openfoam_runner.run_case(case_id, compute_target="gpu")
    assert result["status"] == ("ran" if expect_success else "failed")
    assert result["execution"]["gpu_used"] is expect_success
    assert result["execution"]["gpu"]["kernel_evidence"]["status"] == evidence_status
    assert result["execution"]["gpu"]["kernel_evidence"]["source"] == "CUPTI_ACTIVITY_KIND_CONCURRENT_KERNEL"
    assert result["execution"]["gpu"]["kernel_evidence"]["device_kernel_count"] == device_kernels
    simple_step = next(step for step in result["steps"] if step["name"] == "simpleFoam")
    assert simple_step["solver_exit_code"] == solver_exit
    assert simple_step["returncode"] == solver_exit
    assert simple_step["validation_failure"] == expected_failure
    if not expect_success:
        assert result["failed_step"] == "simpleFoam"
        assert result["validation_failure"] == expected_failure
        assert expected_message in result["message"]
        assert cases.get_case(case_id)["stages"]["cfd_run"]["status"] == "failed"
        assert openfoam_runner.check_result_gate(case_id)["eligible"] is False
        solver_log = (cfd / "logs" / "simpleFoam.log").read_text(encoding="utf-8")
        assert "Time = 1" in solver_log
        assert expected_message in solver_log
        assert "foamToVTK" not in observed
    assert result["execution"]["mode"] == "gpu-single"
    assert "--gpus" in observed["simpleFoam"]
    assert "nsys" not in " ".join(observed["simpleFoam"])
    assert "CUDA_INJECTION64_PATH=/openfoam/libwindcatcher_gpu_kernel_count.so" in observed["simpleFoam"]
    assert "FOAM_MEMORY_POOL=managed; size=1024, incr=1024" in observed["simpleFoam"]
    assert gpu_id in observed["simpleFoam"]
    assert "/case/system/fvSolution:ro" in " ".join(observed["simpleFoam"])
    assert "microfluidica/openfoam:latest" in observed["snappyHexMesh"] or "microfluidica/openfoam:latest" in observed["checkMesh"]
    assert "--gpus" not in observed["snappyHexMesh"]
    assert "decomposePar" not in observed
    assert "reconstructPar" not in observed
    assert "numberOfSubdomains 2;" in (cfd / "system" / "decomposeParDict").read_text(encoding="utf-8")
    assert (cfd / "system" / "fvSolution").read_text(encoding="utf-8").count("symGaussSeidel") == 1

    monkeypatch.setattr(mesh_cache, "mesh_inputs_signature", lambda *_: {"valid": True, "signature": "same-mesh"})
    monkeypatch.setattr(mesh_cache, "inspect_reuse", lambda *_: {
        "reusable": True, "reason": "原网格已核对", "manifest": {
            "generation": {"mode": "parallel", "requested_subdomains": 2, "run_id": "earlier"},
            "check_mesh": {"run_id": "earlier"},
        },
    })
    monkeypatch.setattr(mesh_cache, "record_mesh", lambda *_, **__: {"recorded": True, "reason": "通过"})
    observed.clear()
    rerun = openfoam_runner.run_case(case_id, compute_target="gpu")
    assert rerun["status"] == ("ran" if expect_success else "failed")
    assert rerun["execution"]["gpu_used"] is expect_success
    assert rerun["execution"]["mesh"]["action"] == "reused"
    assert "blockMesh" not in observed
    assert "snappyHexMesh" not in observed
    assert "mpiPreflight" not in observed
    assert "simpleFoam" in observed
