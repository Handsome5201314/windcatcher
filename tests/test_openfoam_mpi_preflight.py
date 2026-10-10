"""Catch host/container CPU mismatches before a long real mesh build."""

import json

import pytest

from pipeline import cases, openfoam_runner


@pytest.fixture
def parallel_case(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    monkeypatch.delenv("OPENFOAM_MPI_PROCS", raising=False)
    monkeypatch.setattr(openfoam_runner.preview, "viewer_metadata", lambda _: {})
    monkeypatch.setattr(openfoam_runner, "detect_backends", lambda: {"available": ["docker"]})
    monkeypatch.setattr(openfoam_runner, "configured_image", lambda: "test-openfoam-image")
    monkeypatch.setattr(openfoam_runner, "_mesh_backend_identity", lambda *_: {"name": "docker"})
    case_id = cases.create_case("synthetic", "<synthetic>", synthetic=True)
    root = cases.case_dir(case_id)
    (root / "segment" / "segmentation.json").write_text(
        '{"need_human_confirm": false}', encoding="utf-8"
    )
    cfd = root / "cfd"
    (cfd / "system").mkdir()
    (cfd / "system" / "controlDict").write_text("endTime 1000;", encoding="utf-8")
    (cfd / "case_meta.json").write_text(json.dumps({
        "parallel": {"profile": "strongest", "requested_subdomains": 16},
    }), encoding="utf-8")
    return case_id, cfd


def test_slot_failure_explains_mpi_startup_without_claiming_divergence():
    output = "There are not enough slots available in the system to satisfy the 16\nslots that were requested by the application:\n  simpleFoam\n"
    reason, remediation = openfoam_runner._error_diagnosis(
        "simpleFoam", {"returncode": 1}, output, 7200
    )
    assert "MPI" in reason and "槽位不足" in reason and "16" in reason
    assert "未开始气流迭代" in reason
    assert any("Docker" in item and "CPU" in item for item in remediation)


def test_parallel_preflight_runs_before_mesh_and_preserves_existing_artifacts(parallel_case, monkeypatch):
    case_id, cfd = parallel_case
    (cfd / "constant" / "polyMesh").mkdir(parents=True)
    mesh_path = cfd / "constant" / "polyMesh" / "points"
    mesh_path.write_bytes(b"existing checked mesh")
    calls = []

    def run(cmd, timeout, on_line, **kwargs):
        name = kwargs["log_path"].stem
        calls.append(name)
        output = ("WINDCATCHER_CPU_LOGICAL=8\nWINDCATCHER_CPU_PHYSICAL=4\n"
                  "WINDCATCHER_MPI_MAX_PROCS=8\n"
                  "WINDCATCHER_MPI_CAPACITY_EXCEEDED requested=16 available=8\n")
        kwargs["log_path"].write_text(output, encoding="utf-8")
        return {"returncode": 2, "output": output, "elapsed_seconds": .1}

    monkeypatch.setattr(openfoam_runner, "_run_streaming", run)
    result = openfoam_runner.run_case(case_id)
    assert calls == ["mpiPreflight"]
    assert result["failed_step"] == "mpiPreflight"
    assert "16" in result["message"] and "8" in result["message"]
    assert result["execution"]["runtime"]["cpu"]["max_processes"] == 8
    assert mesh_path.read_bytes() == b"existing checked mesh"
    assert result["parallel"]["profile"] == "strongest"


def test_docker_16_processes_use_virtual_cpu_threads_after_successful_preflight(parallel_case, monkeypatch):
    case_id, cfd = parallel_case
    calls = []

    def run(cmd, timeout, on_line, **kwargs):
        name = kwargs["log_path"].stem
        calls.append((name, cmd[-1]))
        if name == "mpiPreflight":
            output = ("WINDCATCHER_CPU_LOGICAL=24\nWINDCATCHER_CPU_PHYSICAL=12\n"
                      "WINDCATCHER_MPI_MAX_PROCS=24\n")
            code = 0
        elif name == "simpleFoam":
            output, code = "test deliberately stops before long solve", 9
        else:
            output, code = "", 0
        kwargs["log_path"].write_text(output, encoding="utf-8")
        return {"returncode": code, "output": output, "elapsed_seconds": .1}

    monkeypatch.setattr(openfoam_runner, "_run_streaming", run)
    result = openfoam_runner.run_case(case_id)
    assert [name for name, _ in calls] == [
        "mpiPreflight", "blockMesh", "decomposeMesh", "snappyHexMesh", "reconstructMesh", "checkMesh", "decomposePar", "simpleFoam",
    ]
    launch = next(inner for name, inner in calls if name == "simpleFoam")
    preflight = calls[0][1]
    assert "--use-hwthread-cpus" in launch and "-np 16" in launch
    assert "--use-hwthread-cpus" in preflight and "-np 16" in preflight
    assert "--oversubscribe" not in launch + preflight
    assert "simpleFoam -help" in preflight
    runtime = result["execution"]["runtime"]
    assert runtime["cpu"] == {"logical_cores": 24, "physical_cores": 12, "max_processes": 24}
    assert runtime["allocation_mode"] == "hwthread"
    assert result["parallel"]["profile"] == "strongest"
    assert result["progress"]["total"] == 11
    meshing = next(inner for name, inner in calls if name == "snappyHexMesh")
    assert "-np 16 snappyHexMesh -parallel -overwrite" in meshing
    assert "-noFields" in next(inner for name, inner in calls if name == "decomposeMesh")
    assert "reconstructPar -constant -noFields -time 0" in next(inner for name, inner in calls if name == "reconstructMesh")
    assert "decomposePar -force -time 0" in next(inner for name, inner in calls if name == "decomposePar")
    assert result["execution"]["mesh"]["mode"] == "parallel"


def test_slots_startup_stderr_is_not_misreported_as_foam_fatal_error():
    reason, _ = openfoam_runner._error_diagnosis(
        "mpiPreflight", {"returncode": 1},
        "There are not enough slots available in the system to satisfy the 32\nslots that were requested", 60,
    )
    assert "MPI" in reason and "32" in reason
    assert "OpenFOAM 致命错误" not in reason
