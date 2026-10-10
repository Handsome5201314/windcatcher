from __future__ import annotations

import sys
import json
from pathlib import Path

import pytest
from pipeline import cases, openfoam_runner
from pipeline.config import openfoam_mpi_processes, resolve_parallel_plan
from pipeline.openfoam_runner import (
    _error_diagnosis,
    _mesh_summary,
    _run_streaming,
    _solver_progress,
    _write_status,
    read_latest_status,
)


def test_streaming_writes_log_and_heartbeat_while_process_is_alive(tmp_path):
    log = tmp_path / "live.log"
    snapshots = []
    result = _run_streaming(
        [sys.executable, "-c", "import time; print('Time = 3', flush=True); time.sleep(1.2)"],
        timeout=4,
        log_path=log,
        on_tick=lambda elapsed: snapshots.append((elapsed, log.read_text(encoding="utf-8"))),
    )
    assert result["returncode"] == 0
    assert any(elapsed >= .5 and "Time = 3" in content for elapsed, content in snapshots)


@pytest.mark.parametrize("value", ["bad", "0", "17"])
def test_invalid_mpi_setting_is_observable(monkeypatch, value):
    monkeypatch.setenv("OPENFOAM_MPI_PROCS", value)
    with pytest.raises(ValueError, match="OPENFOAM_MPI_PROCS"):
        openfoam_mpi_processes()


def test_old_case_and_override_use_consistent_mpi_configuration(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENFOAM_MPI_PROCS", raising=False)
    assert openfoam_mpi_processes() == 8
    assert openfoam_mpi_processes({"enabled": False, "requested_subdomains": 8}) == 1
    monkeypatch.setenv("OPENFOAM_MPI_PROCS", "4")
    assert openfoam_mpi_processes({"enabled": False}) == 4
    openfoam_runner._prepare_decomposition(tmp_path, 4)
    path = tmp_path / "system" / "decomposeParDict"
    assert "numberOfSubdomains 4;" in path.read_text(encoding="utf-8")
    openfoam_runner._prepare_decomposition(tmp_path, 8)
    assert "numberOfSubdomains 8;" in path.read_text(encoding="utf-8")


def test_workbench_parallel_profiles_use_detected_machine_capacity(monkeypatch):
    monkeypatch.delenv("OPENFOAM_MPI_PROCS", raising=False)
    capabilities = {
        "cpu": {"logical_cores": 32, "physical_cores": 16},
        "memory_gb": 91.7,
        "gpu": {"detected": True, "name": "RTX 3090", "memory_gb": 24.0, "used_by_simpleFoam": False},
        "parallel": {"strongest": 16, "balanced": 12, "custom_max": 32},
        "solver_gpu_note": "GPU 不参与标准 simpleFoam",
    }
    monkeypatch.setattr("pipeline.config.detect_compute_capabilities", lambda: capabilities)
    assert resolve_parallel_plan({"profile": "strongest"})["requested_subdomains"] == 16
    assert resolve_parallel_plan({"profile": "balanced"})["requested_subdomains"] == 12
    assert resolve_parallel_plan({"profile": "custom", "custom_subdomains": 32})["requested_subdomains"] == 32


def test_run_records_live_progress_and_current_mesh_even_on_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    monkeypatch.delenv("OPENFOAM_MPI_PROCS", raising=False)
    case_id = cases.create_case("synthetic", "<合成测试>", synthetic=True)
    root = cases.case_dir(case_id)
    (root / "segment" / "segmentation.json").write_text('{"need_human_confirm": false}', encoding="utf-8")
    cfd = root / "cfd"
    (cfd / "system").mkdir()
    (cfd / "system" / "controlDict").write_text("endTime 1000;", encoding="utf-8")
    monkeypatch.setattr(openfoam_runner.preview, "viewer_metadata", lambda _: {})
    monkeypatch.setattr(openfoam_runner, "detect_backends", lambda: {"available": ["docker"]})
    monkeypatch.setattr(openfoam_runner, "configured_image", lambda: "test-image")
    monkeypatch.setattr(openfoam_runner, "_mesh_backend_identity", lambda *_: {"name": "docker"})
    observed = []

    def run(cmd, timeout, on_line, **kwargs):
        name = kwargs["log_path"].stem
        observed.append(cmd)
        log_path = kwargs["log_path"]
        if name == "checkMesh":
            log_path.write_text("cells: 3058853\nMesh OK.\n", encoding="utf-8")
        else:
            log_path.write_text("", encoding="utf-8")
        if name == "simpleFoam":
            on_line("Time = 7\n", .5)
            on_line("Solving for Ux, Initial residual = 0.01, Final residual = 0.001, No Iterations 2\n", .5)
            kwargs["on_tick"](2)
            record = json.loads((cfd / "run_status.json").read_text(encoding="utf-8"))
            assert record["mesh"]["cells"] == 3058853
            assert record["solver"]["iteration_percent"] == .7
            assert cases.get_case(case_id)["stages"]["cfd_run"]["data"]["run_id"] == record["run_id"]
            return {"returncode": 124, "output": log_path.read_text(encoding="utf-8"), "elapsed_seconds": 2, "timed_out": True, "cleanup": {"status": "stopped"}}
        return {"returncode": 0, "output": log_path.read_text(encoding="utf-8"), "elapsed_seconds": .1}

    monkeypatch.setattr(openfoam_runner, "_run_streaming", run)
    result = openfoam_runner.run_case(case_id)
    assert result["timed_out"] is True
    assert result["execution"]["mode"] == "mpi"
    assert result["progress"]["completed"] == 8
    assert result["progress"]["total"] == 11
    assert result["mesh"]["cells"] == 3058853
    assert "numberOfSubdomains 8;" in (cfd / "system" / "decomposeParDict").read_text(encoding="utf-8")
    assert all("--name" in cmd for cmd in observed)
    assert len({cmd[cmd.index("--name") + 1] for cmd in observed}) == len(observed)


def test_streaming_runner_delivers_lines_before_process_exits():
    lines = []
    result = _run_streaming(
        [sys.executable, "-c", "import time; print('Time = 1', flush=True); time.sleep(.2); print('done', flush=True)"],
        timeout=3,
        on_line=lambda line, elapsed: lines.append((line, elapsed)),
    )

    assert result["returncode"] == 0
    assert any("Time = 1" in line for line, _ in lines)
    assert result["timed_out"] is False
    assert result["elapsed_seconds"] >= 0.1


def test_streaming_runner_marks_timeout_and_preserves_output():
    result = _run_streaming(
        [sys.executable, "-c", "import time; print('Time = 3', flush=True); time.sleep(3)"],
        timeout=1,
    )

    assert result["returncode"] == 124
    assert result["timed_out"] is True
    assert "Time = 3" in result["output"]
    reason, remediation = _error_diagnosis("simpleFoam", result, result["output"], 1)
    assert "超时" in reason
    assert remediation


def test_timeout_without_verified_container_stop_does_not_claim_it_stopped():
    reason, _ = _error_diagnosis("simpleFoam", {"returncode": 124, "cleanup": {"status": "unverified"}}, "", 1)
    assert "未能确认" in reason
    assert "已被终止" not in reason


def test_status_write_keeps_solver_alive_when_windows_rejects_replace(tmp_path, monkeypatch):
    path = tmp_path / "run_status.json"
    original_replace = Path.replace

    def reject_target(self, target):
        if Path(target) == path:
            raise PermissionError(5, "access denied")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", reject_target)
    _write_status(path, {"status": "running", "progress": {"percent": 42}})

    assert not path.exists()
    assert read_latest_status(path)["progress"]["percent"] == 42


def test_solver_progress_uses_real_outer_iterations(tmp_path: Path):
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "fvSolution").write_text(
        "SIMPLE { residualControl { p 1e-4; U 1e-5; } }", encoding="utf-8"
    )
    (tmp_path / "system" / "controlDict").write_text("endTime 1000;", encoding="utf-8")
    progress = _solver_progress(
        "Time = 7\nSolving for Ux, Initial residual = 0.01, Final residual = 0.001, No Iterations 2\n",
        tmp_path,
    )

    assert progress["iteration"] == 1
    assert progress["time"] == 7
    assert progress["end_time"] == 1000
    assert progress["iteration_percent"] == 0.7


def test_mesh_summary_reports_checkmesh_counts_and_status(tmp_path: Path):
    cfd = tmp_path / "cfd"
    (cfd / "logs").mkdir(parents=True)
    (cfd / "logs" / "checkMesh.log").write_text(
        "points: 12\nfaces: 30\ninternal faces: 20\ncells: 7\nMesh OK.\n",
        encoding="utf-8",
    )
    (cfd / "case_meta.json").write_text(
        '{"stl_cleaning": {"open_edges": 0, "nonmanifold_edges": 0}}',
        encoding="utf-8",
    )

    summary = _mesh_summary(cfd)

    assert summary["cells"] == 7
    assert summary["points"] == 12
    assert summary["check_status"] == "checked"
    assert summary["mesh_ok"] is True
