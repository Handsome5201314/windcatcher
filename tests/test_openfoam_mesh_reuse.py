"""Exercise runner reuse decisions and file isolation across whole CFD retries."""

import hashlib
import json

import pytest

from pipeline import cases, mesh_cache, openfoam_runner


BACKEND = {"name": "docker", "image_id": "sha256:" + "a" * 64, "openfoam_flavour": "org"}
CHECK_OUTPUT = "points: 8\nfaces: 6\ninternal faces: 0\ncells: 1\nMesh OK.\nEnd\n"


@pytest.fixture
def reusable_case(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    monkeypatch.delenv("OPENFOAM_MPI_PROCS", raising=False)
    monkeypatch.setattr(openfoam_runner.preview, "viewer_metadata", lambda _: {})
    monkeypatch.setattr(openfoam_runner, "detect_backends", lambda: {"available": ["docker"]})
    monkeypatch.setattr(openfoam_runner, "configured_image", lambda: "synthetic-openfoam-image")
    monkeypatch.setattr(openfoam_runner, "_mesh_backend_identity", lambda *_: BACKEND)
    case_id = cases.create_case("synthetic", "<synthetic>", synthetic=True)
    root = cases.case_dir(case_id)
    (root / "segment/segmentation.json").write_text('{"need_human_confirm": false}', encoding="utf-8")
    cfd = root / "cfd"
    files = {
        "system/blockMeshDict": "convertToMeters 0.001; blocks (hex (0 1 2 3 4 5 6 7) (10 10 10) simpleGrading (1 1 1));",
        "system/snappyHexMeshDict": "geometry { wall.stl { type triSurfaceMesh; name wall; } } castellatedMeshControls { locationInMesh (0 0 0); } snap true; addLayers false;",
        "system/fvSchemes": "gradSchemes { default Gauss linear; } divSchemes { default none; div(phi,U) Gauss upwind; } laplacianSchemes { default Gauss linear corrected; }",
        "system/fvSolution": "solvers { p { solver GAMG; tolerance 1e-7; } U { solver smoothSolver; tolerance 1e-8; } } SIMPLE { residualControl { p 1e-4; } } relaxationFactors { fields { p 0.3; } }",
        "system/controlDict": "application simpleFoam; startFrom startTime; startTime 0; endTime 1000; writeFormat ascii; writePrecision 8; writeCompression off;",
        "system/decomposeParDict": "numberOfSubdomains 16; method scotch;",
        "constant/triSurface/wall.stl": "solid wall\nendsolid wall\n",
        "0/U": "volumetricFlowRate 0.00025;",
        "case_meta.json": json.dumps({"parallel": {"profile": "strongest", "requested_subdomains": 16}, "stl_cleaning": {"open_edges": 0, "nonmanifold_edges": 0}}),
    }
    for relative, content in files.items():
        path = cfd / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
    assert mesh_cache.mesh_inputs_signature(cfd, BACKEND)["valid"]
    return case_id, cfd


def install_streaming(monkeypatch, cfd, *, solve_code=9, preflight_code=0, export_code=0, during_check=None, before_solve=None):
    calls = []

    def streaming(cmd, timeout, on_line, **kwargs):
        name = kwargs["log_path"].stem
        calls.append((name, cmd))
        code = 0
        output = "End\n"
        if name == "mpiPreflight":
            output = "WINDCATCHER_CPU_LOGICAL=24\nWINDCATCHER_CPU_PHYSICAL=12\nWINDCATCHER_MPI_MAX_PROCS=24\n"
            code = preflight_code
            if code:
                output += "WINDCATCHER_MPI_CAPACITY_EXCEEDED requested=16 available=8\n"
        elif name == "reconstructMesh":
            # Simulate files produced by the external mesher, signing actual bytes.
            geometry = hashlib.sha256((cfd / "constant/triSurface/wall.stl").read_bytes()).hexdigest()
            mesh = cfd / "constant/polyMesh"
            mesh.mkdir(parents=True, exist_ok=True)
            for filename in mesh_cache.MESH_CORE_FILES:
                (mesh / filename).write_text(f"complete {filename} for {geometry}\n", encoding="ascii")
        elif name == "checkMesh":
            output = CHECK_OUTPUT
            if during_check:
                during_check()
        elif name == "simpleFoam":
            if before_solve:
                before_solve()
            code = solve_code
            output = "deliberately interrupted synthetic solve\n" if code else "End\n"
        elif name == "foamToVTK":
            code = export_code
            output = "Internal: VTK/case_1000.vtk\n" if code else "End\n"
        kwargs["log_path"].write_text(output, encoding="utf-8")
        return {"returncode": code, "output": output, "elapsed_seconds": .1}

    monkeypatch.setattr(openfoam_runner, "_run_streaming", streaming)
    return calls


def test_docker_vtk_export_selects_final_fields_without_ascii_or_function_objects(reusable_case, monkeypatch):
    case_id, cfd = reusable_case
    calls = install_streaming(monkeypatch, cfd, solve_code=0)
    openfoam_runner.run_case(case_id)

    export = next(cmd[-1] for name, cmd in calls if name == "foamToVTK")
    assert "foamToVTK -latestTime -fields '(U p)' -noFunctionObjects" in export
    assert "-ascii" not in export


def test_optional_vtk_timeout_preserves_solved_fields_and_explains_browser_path(reusable_case, monkeypatch):
    case_id, cfd = reusable_case
    final_fields = {"U": b"existing final velocity", "p": b"existing final pressure", "phi": b"existing final flux"}

    def produce_fields():
        final = cfd / "1000"
        final.mkdir()
        for name, content in final_fields.items():
            (final / name).write_bytes(content)

    install_streaming(monkeypatch, cfd, solve_code=0, export_code=124, before_solve=produce_fields)
    result = openfoam_runner.run_case(case_id)

    assert result["status"] == "ran"
    stage = cases.get_case(case_id)["stages"]["cfd_run"]
    assert stage["data"]["solved"] is True
    assert stage["data"]["report_eligible"] is False
    export = next(step for step in result["steps"] if step["name"] == "foamToVTK")
    assert export["timed_out"] is True
    assert "附加 VTK" in export["message"] and "超时" in export["message"]
    assert any("病例流场" in hint and "直接读取" in hint for hint in export["remediation"])
    assert not any("重新求解" in hint and "不需要" not in hint for hint in export["remediation"])
    for name, content in final_fields.items():
        assert (cfd / "1000" / name).read_bytes() == content


def mesh_bytes(cfd):
    return {path.relative_to(cfd).as_posix(): path.read_bytes() for path in (cfd / "constant/polyMesh").rglob("*") if path.is_file()}


def seed_old_outputs(cfd):
    files = {
        "99/U": "old velocity field",
        "99/p": "old pressure field",
        "1e+02/phi": "old flux field",
        "postProcessing/inletFlowRate/0/surfaceFieldValue.dat": "100 -0.00025\n",
        "postProcessing/outletFlowRate/0/surfaceFieldValue.dat": "100 0.00025\n",
        "VTK/old-flow.vtk": "old visualization",
        "results/flow.json": '{"old":true}',
        "processor0/99/U": "old processor field",
        "0/polyMesh/points": "old time-zero mesh",
    }
    for relative, content in files.items():
        path = cfd / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
    return {relative: content.encode("utf-8") for relative, content in files.items()}


def test_checked_parallel_mesh_is_cached_even_when_solver_fails(reusable_case, monkeypatch):
    case_id, cfd = reusable_case
    calls = install_streaming(monkeypatch, cfd)
    result = openfoam_runner.run_case(case_id)

    assert result["status"] == "failed" and result["failed_step"] == "simpleFoam"
    assert [name for name, _ in calls] == ["mpiPreflight", "blockMesh", "decomposeMesh", "snappyHexMesh", "reconstructMesh", "checkMesh", "decomposePar", "simpleFoam"]
    manifest = json.loads((cfd / mesh_cache.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["generation"] == {"mode": "parallel", "requested_subdomains": 16, "run_id": result["run_id"]}
    assert manifest["check_mesh"]["run_id"] == result["run_id"]
    assert manifest["mesh_files"]
    assert result["execution"]["mesh"]["cache"]["status"] == "ready"
    assert mesh_cache.inspect_reuse(cfd, mesh_cache.mesh_inputs_signature(cfd, BACKEND))["reusable"]


def test_retry_changes_mpi_count_but_reuses_mesh_and_archives_old_fields(reusable_case, monkeypatch):
    case_id, cfd = reusable_case
    install_streaming(monkeypatch, cfd)
    original = openfoam_runner.run_case(case_id)
    original_mesh = mesh_bytes(cfd)
    generation = json.loads((cfd / mesh_cache.MANIFEST_FILENAME).read_text(encoding="utf-8"))["generation"]
    old_outputs = seed_old_outputs(cfd)
    zero_field = (cfd / "0/U").read_bytes()
    meta = json.loads((cfd / "case_meta.json").read_text(encoding="utf-8"))
    meta["parallel"] = {"profile": "custom", "requested_subdomains": 8}
    (cfd / "case_meta.json").write_text(json.dumps(meta), encoding="utf-8")

    def before_solve():
        assert not any((cfd / relative).exists() for relative in old_outputs)
        assert openfoam_runner.inspect_latest_fields(cfd)["latest_time"] is None
        assert openfoam_runner._read_postprocessing(cfd)["status"] == "missing"

    calls = install_streaming(monkeypatch, cfd, solve_code=0, before_solve=before_solve)
    result = openfoam_runner.run_case(case_id)
    assert result["status"] == "ran"
    assert [name for name, _ in calls] == ["mpiPreflight", "checkMesh", "decomposePar", "simpleFoam", "reconstructPar", "foamToVTK"]
    assert mesh_bytes(cfd) == original_mesh
    assert (cfd / "0/U").read_bytes() == zero_field
    assert result["execution"]["requested_subdomains"] == 8
    assert result["execution"]["mesh"]["action"] == "reused"
    assert result["execution"]["mesh"]["requested_subdomains"] == 16
    assert result["execution"]["mesh"]["source_run_id"] == original["run_id"]
    manifest = json.loads((cfd / mesh_cache.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["generation"] == generation
    assert manifest["check_mesh"]["run_id"] == result["run_id"]
    assert result["field_validation"]["latest_time"] is None
    assert result["field_validation"]["complete"] is False
    assert result["postprocessing"]["status"] == "missing"
    archive = cfd / result["execution"]["archive"]["path"]
    for relative, content in old_outputs.items():
        assert not (cfd / relative).exists()
        assert (archive / relative).read_bytes() == content
    assert json.loads((archive / "run_status.json").read_text(encoding="utf-8"))["run_id"] == original["run_id"]
    solver_cmd = next(cmd[-1] for name, cmd in calls if name == "simpleFoam")
    assert "-np 8 simpleFoam -parallel" in solver_cmd


def test_geometry_change_rebuilds_and_keeps_previous_mesh_in_history(reusable_case, monkeypatch):
    case_id, cfd = reusable_case
    install_streaming(monkeypatch, cfd)
    original = openfoam_runner.run_case(case_id)
    original_mesh = mesh_bytes(cfd)
    old_manifest = (cfd / mesh_cache.MANIFEST_FILENAME).read_bytes()
    (cfd / "constant/triSurface/wall.stl").write_text("solid changed\nendsolid changed\n", encoding="utf-8")
    calls = install_streaming(monkeypatch, cfd)
    result = openfoam_runner.run_case(case_id)

    assert result["execution"]["mesh"]["action"] == "rebuilt"
    assert "改变" in result["execution"]["mesh"]["reason"]
    assert {"blockMesh", "decomposeMesh", "snappyHexMesh", "reconstructMesh"} <= {name for name, _ in calls}
    assert mesh_bytes(cfd) != original_mesh
    archive = cfd / result["execution"]["archive"]["path"]
    for relative, content in original_mesh.items():
        assert (archive / relative).read_bytes() == content
    assert (archive / mesh_cache.MANIFEST_FILENAME).read_bytes() == old_manifest
    manifest = json.loads((cfd / mesh_cache.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["generation"]["run_id"] == result["run_id"] != original["run_id"]


@pytest.mark.parametrize("reuse", [False, True])
def test_input_change_during_mesh_check_stops_before_solving(reusable_case, monkeypatch, reuse):
    case_id, cfd = reusable_case
    if reuse:
        install_streaming(monkeypatch, cfd)
        openfoam_runner.run_case(case_id)

    def change_geometry():
        (cfd / "constant/triSurface/wall.stl").write_text("solid changed mid-run\nendsolid changed mid-run\n", encoding="utf-8")

    calls = install_streaming(monkeypatch, cfd, during_check=change_geometry)
    result = openfoam_runner.run_case(case_id)

    assert result["status"] == "failed"
    assert result["failed_step"] == "checkMesh"
    assert result["returncode"] != 0
    assert "运行期间发生变化" in result["message"]
    assert "simpleFoam" not in {name for name, _ in calls}
    assert not mesh_cache.inspect_reuse(cfd, mesh_cache.mesh_inputs_signature(cfd, BACKEND))["reusable"]


def test_preflight_failure_leaves_existing_mesh_and_old_fields_in_place(reusable_case, monkeypatch):
    case_id, cfd = reusable_case
    install_streaming(monkeypatch, cfd)
    openfoam_runner.run_case(case_id)
    old_mesh = mesh_bytes(cfd)
    old_manifest = (cfd / mesh_cache.MANIFEST_FILENAME).read_bytes()
    old_check = (cfd / "logs/checkMesh.log").read_bytes()
    old_outputs = seed_old_outputs(cfd)
    histories = set((cfd / "run_history").iterdir())
    calls = install_streaming(monkeypatch, cfd, preflight_code=2)
    result = openfoam_runner.run_case(case_id)

    assert result["failed_step"] == "mpiPreflight"
    assert [name for name, _ in calls] == ["mpiPreflight"]
    assert mesh_bytes(cfd) == old_mesh
    assert (cfd / mesh_cache.MANIFEST_FILENAME).read_bytes() == old_manifest
    assert (cfd / "logs/checkMesh.log").read_bytes() == old_check
    for relative, content in old_outputs.items():
        assert (cfd / relative).read_bytes() == content
    assert set((cfd / "run_history").iterdir()) == histories
    assert "archive" not in result["execution"]
