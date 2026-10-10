"""Content-based mesh reuse must never trust old directories or timestamps."""

import json

import pytest

from pipeline import mesh_cache


BACKEND = {"name": "docker", "image_id": "sha256:" + "a" * 64, "openfoam_flavour": "org"}


@pytest.fixture
def cfd(tmp_path):
    root = tmp_path / "cfd"
    for directory in ("system", "0", "logs", "constant/triSurface", "constant/polyMesh"):
        (root / directory).mkdir(parents=True)
    files = {
        "system/blockMeshDict": "convertToMeters 0.001; blocks (hex (0 1 2 3 4 5 6 7) (10 10 10) simpleGrading (1 1 1));",
        "system/snappyHexMeshDict": "geometry { wall.stl { type triSurfaceMesh; name wall; } } castellatedMeshControls { locationInMesh (0 0 0); } snap true; addLayers false;",
        "system/fvSchemes": "gradSchemes { default Gauss linear; } divSchemes { default none; div(phi,U) Gauss upwind; } laplacianSchemes { default Gauss linear corrected; }",
        "system/fvSolution": "solvers { p { solver GAMG; tolerance 1e-7; } U { solver smoothSolver; tolerance 1e-8; } } SIMPLE { residualControl { p 1e-4; } } relaxationFactors { fields { p 0.3; } }",
        "system/controlDict": "application simpleFoam; endTime 1000; writeFormat ascii; writePrecision 8; writeCompression off;",
        "system/decomposeParDict": "numberOfSubdomains 8; method scotch;",
        "constant/triSurface/wall.stl": "solid wall\nendsolid wall\n",
        "0/U": "volumetricFlowRate 0.00025;",
        "case_meta.json": json.dumps({"stl_cleaning": {"open_edges": 0, "nonmanifold_edges": 0}}),
    }
    for name in mesh_cache.MESH_CORE_FILES:
        files[f"constant/polyMesh/{name}"] = f"complete {name} list\n"
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    return root


def record(cfd, *, output="Mesh OK.\nEnd\n", code=0, steps=None):
    (cfd / "logs/checkMesh.log").write_text(output, encoding="utf-8")
    signature = mesh_cache.mesh_inputs_signature(cfd, BACKEND)
    return mesh_cache.record_mesh(
        cfd, signature, "mesh-run-1",
        check_result={"returncode": code, "timed_out": False, "output": output},
        completed_steps=steps if steps is not None else [
            {"name": "blockMesh", "returncode": 0},
            {"name": "snappyHexMesh", "returncode": 0},
            {"name": "checkMesh", "returncode": code},
        ],
    )


def reuse(cfd, backend=BACKEND):
    return mesh_cache.inspect_reuse(cfd, mesh_cache.mesh_inputs_signature(cfd, backend))


def test_old_mesh_without_manifest_is_not_registered(cfd):
    (cfd / "logs/checkMesh.log").write_text("Mesh OK.\n", encoding="utf-8")
    result = reuse(cfd)
    assert not result["reusable"]
    assert "记录" in result["reason"]
    assert not (cfd / mesh_cache.MANIFEST_FILENAME).exists()


def test_successful_mesh_is_reusable_and_has_sha_manifest(cfd):
    result = record(cfd)
    assert result["recorded"], result
    result = reuse(cfd)
    assert result["reusable"], result
    manifest = json.loads((cfd / mesh_cache.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert len(manifest["signature"]) == 64
    assert set(manifest["mesh_files"]) >= {f"constant/polyMesh/{name}" for name in mesh_cache.MESH_CORE_FILES}
    assert manifest["check_mesh"]["run_id"] == "mesh-run-1"
    assert manifest["check_mesh"]["quality"]["clean"] is True


@pytest.mark.parametrize("name", ["constant/triSurface/wall.stl", "system/blockMeshDict", "system/snappyHexMeshDict", "system/fvSchemes"])
def test_real_geometry_and_mesh_settings_invalidate_cache(cfd, name):
    assert record(cfd)["recorded"]
    path = cfd / name
    # Equal-size replacement with preserved mtime proves this does not use stat heuristics.
    old_stat = path.stat()
    old = path.read_bytes()
    path.write_bytes(old.replace(old[:1], b"X", 1))
    import os
    os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    assert not reuse(cfd)["reusable"]


def test_actual_backend_digest_and_flavour_invalidate_cache(cfd):
    assert record(cfd)["recorded"]
    assert not reuse(cfd, {**BACKEND, "image_id": "sha256:" + "b" * 64})["reusable"]
    assert not reuse(cfd, {**BACKEND, "openfoam_flavour": "esi"})["reusable"]
    assert not mesh_cache.mesh_inputs_signature(cfd, {"name": "docker", "image": "openfoam:latest"})["valid"]


def test_mpi_size_flow_and_solver_iterations_do_not_invalidate(cfd):
    assert record(cfd)["recorded"]
    (cfd / "system/decomposeParDict").write_text("numberOfSubdomains 16; method scotch;", encoding="utf-8")
    (cfd / "0/U").write_text("volumetricFlowRate 0.0005;", encoding="utf-8")
    path = cfd / "system/controlDict"
    path.write_text(path.read_text().replace("endTime 1000", "endTime 2000"), encoding="utf-8")
    path = cfd / "system/fvSolution"
    path.write_text(path.read_text().replace("tolerance 1e-7", "tolerance 1e-6").replace("p 1e-4", "p 1e-5"), encoding="utf-8")
    assert reuse(cfd)["reusable"]


def test_motion_solver_setting_and_mesh_precision_invalidate(cfd):
    path = cfd / "system/fvSolution"
    path.write_text(path.read_text().replace("solvers {", "solvers { cellDisplacement { solver PCG; tolerance 1e-8; }"), encoding="utf-8")
    assert record(cfd)["recorded"]
    path.write_text(path.read_text().replace("solver PCG", "solver GAMG"), encoding="utf-8")
    assert not reuse(cfd)["reusable"]


def test_mesh_write_precision_change_invalidates(cfd):
    assert record(cfd)["recorded"]
    path = cfd / "system/controlDict"
    path.write_text(path.read_text().replace("writePrecision 8", "writePrecision 6"), encoding="utf-8")
    assert not reuse(cfd)["reusable"]


def test_declared_surface_missing_disables_cache_even_with_other_surfaces(cfd):
    path = cfd / "system/snappyHexMeshDict"
    path.write_text(path.read_text().replace("name wall;", "name wall; file missing.stl;"), encoding="utf-8")
    signature = mesh_cache.mesh_inputs_signature(cfd, BACKEND)
    assert not signature["valid"]
    assert not record(cfd)["recorded"]


@pytest.mark.parametrize("operation", ["remove", "corrupt"])
@pytest.mark.parametrize("name", ["points", "faces", "owner", "neighbour", "boundary"])
def test_missing_or_corrupt_mesh_rebuilds(cfd, name, operation):
    assert record(cfd)["recorded"]
    path = cfd / "constant/polyMesh" / name
    if operation == "remove":
        path.unlink()
    else:
        path.write_bytes(b"corrupted mesh")
    assert not reuse(cfd)["reusable"]


def test_extra_mesh_zone_file_is_covered(cfd):
    path = cfd / "constant/polyMesh/cellZones"
    path.write_bytes(b"important mesh zone")
    assert record(cfd)["recorded"]
    path.write_bytes(b"different mesh zone")
    assert not reuse(cfd)["reusable"]


@pytest.mark.parametrize("filename", ["cellProc", "cellProc.gz"])
def test_repartitioning_auxiliary_cell_proc_does_not_change_mesh(cfd, filename):
    path = cfd / "constant/polyMesh" / filename
    path.write_bytes(b"cell-to-processor mapping for original MPI partition")
    assert record(cfd)["recorded"]
    path.write_bytes(b"cell-to-processor mapping for different MPI partition")
    assert reuse(cfd)["reusable"]
    path.unlink()
    assert reuse(cfd)["reusable"]


def test_only_exact_root_cell_proc_file_is_excluded(cfd):
    nested = cfd / "constant/polyMesh/sets/cellProc"
    nested.parent.mkdir()
    nested.write_bytes(b"actual mesh set with similar name")
    assert record(cfd)["recorded"]
    nested.write_bytes(b"changed actual mesh set")
    assert not reuse(cfd)["reusable"]


@pytest.mark.parametrize("output,code", [("Mesh OK.\n", 1), ("End\n", 0), ("Failed 1 mesh checks.\n", 0)])
def test_exit_zero_alone_or_stale_success_is_not_evidence(cfd, output, code):
    assert not record(cfd, output=output, code=code)["recorded"]
    assert not reuse(cfd)["reusable"]


def test_failed_preceding_mesh_step_never_registers(cfd):
    result = record(cfd, steps=[{"name": "blockMesh", "returncode": 0}, {"name": "snappyHexMesh", "returncode": 1}, {"name": "checkMesh", "returncode": 0}])
    assert not result["recorded"]


def test_log_damage_invalidates_mesh_proof(cfd):
    assert record(cfd)["recorded"]
    (cfd / "logs/checkMesh.log").write_text("Mesh OK.\nchanged evidence\n", encoding="utf-8")
    assert not reuse(cfd)["reusable"]


def test_mesh_ok_with_quality_warning_retains_warning(cfd):
    output = "Writing 4 conflicting points to set nonManifoldPoints\nMesh OK.\nEnd\n"
    result = record(cfd, output=output)
    assert result["recorded"]
    result = reuse(cfd)
    assert result["reusable"]
    assert result["quality"]["mesh_ok"] is True
    assert result["quality"]["clean"] is False
    assert result["quality"]["nonmanifold_points"] == 4


def test_local_included_mesh_dependency_is_hashed(cfd):
    dep = cfd / "system/qualityControls"
    dep.write_text("maxNonOrtho 65;", encoding="utf-8")
    path = cfd / "system/snappyHexMeshDict"
    path.write_text(path.read_text() + '\nmeshQualityControls { #include "qualityControls"\n }', encoding="utf-8")
    assert record(cfd)["recorded"]
    dep.write_text("maxNonOrtho 75;", encoding="utf-8")
    assert not reuse(cfd)["reusable"]


@pytest.mark.parametrize("directive", ['#include "missingDict"', '#includeEtc "caseDicts/meshQualityDict"', '#include "../../outsideDict"', '#codeStream { code #{ dangerous #}; }'])
def test_unknown_or_missing_dependency_explicitly_disables_reuse(cfd, directive):
    path = cfd / "system/snappyHexMeshDict"
    path.write_text(path.read_text() + "\n" + directive + "\n", encoding="utf-8")
    signature = mesh_cache.mesh_inputs_signature(cfd, BACKEND)
    assert not signature["valid"]
    assert signature["reason"]
    assert not record(cfd)["recorded"]


def test_no_fabricated_fresh_proof_from_old_log(cfd):
    output = "Mesh OK.\nEnd\n"
    (cfd / "logs/checkMesh.log").write_text(output, encoding="utf-8")
    result = mesh_cache.record_mesh(cfd, mesh_cache.mesh_inputs_signature(cfd, BACKEND), "new-run", check_result={"returncode": 0, "output": "End\n"}, completed_steps=[])
    assert not result["recorded"]


def test_successful_revalidation_refreshes_existing_manifest(cfd):
    assert record(cfd)["recorded"]
    result = record(cfd, output="Checking cached mesh\nMesh OK.\nEnd\n", steps=[{"name": "mpiPreflight", "returncode": 0}, {"name": "checkMesh", "returncode": 0}])
    assert result["recorded"]
    assert reuse(cfd)["reusable"]


def test_previous_failed_mesh_build_invalidates_cache(cfd):
    assert record(cfd)["recorded"]
    (cfd / "run_status.json").write_text(json.dumps({"status": "failed", "failed_step": "snappyHexMesh"}), encoding="utf-8")
    assert not reuse(cfd)["reusable"]


def test_running_input_change_prevents_registration(cfd):
    signature = mesh_cache.mesh_inputs_signature(cfd, BACKEND)
    (cfd / "constant/triSurface/wall.stl").write_text("solid modified\nendsolid modified\n", encoding="utf-8")
    output = "Mesh OK.\nEnd\n"
    (cfd / "logs/checkMesh.log").write_text(output, encoding="utf-8")
    result = mesh_cache.record_mesh(cfd, signature, "new-run", check_result={"returncode": 0, "output": output}, completed_steps=[{"name": name, "returncode": 0} for name in ("blockMesh", "snappyHexMesh", "checkMesh")])
    assert not result["recorded"]
    assert result["input_changed"] is True
    assert "运行期间" in result["reason"]


def test_parallel_cache_requires_successful_reconstruction(cfd):
    output = "Mesh OK.\nEnd\n"
    (cfd / "logs/checkMesh.log").write_text(output, encoding="utf-8")
    signature = mesh_cache.mesh_inputs_signature(cfd, BACKEND)
    steps = [{"name": name, "returncode": 0} for name in ("blockMesh", "snappyHexMesh", "checkMesh")]
    generation = {"mode": "parallel", "requested_subdomains": 16}
    result = mesh_cache.record_mesh(cfd, signature, "parallel-1", check_result={"returncode": 0, "output": output}, completed_steps=steps, generation=generation)
    assert not result["recorded"]
    steps.insert(2, {"name": "reconstructMesh", "returncode": 0})
    result = mesh_cache.record_mesh(cfd, signature, "parallel-1", check_result={"returncode": 0, "output": output}, completed_steps=steps, generation=generation)
    assert result["recorded"]
    assert result["manifest"]["generation"] == generation
    assert reuse(cfd)["reusable"]


def test_incomplete_build_cannot_refresh_previous_cache(cfd):
    assert record(cfd)["recorded"]
    result = record(cfd, steps=[{"name": "snappyHexMesh", "returncode": 0}, {"name": "checkMesh", "returncode": 0}])
    assert not result["recorded"]


def test_atomic_write_failure_is_observable_and_does_not_delete_old_manifest(cfd, monkeypatch):
    assert record(cfd)["recorded"]
    manifest = cfd / mesh_cache.MANIFEST_FILENAME
    before = manifest.read_bytes()
    original = mesh_cache.os.replace
    def locked(source, target):
        if target == manifest:
            raise PermissionError("simulated Windows lock")
        return original(source, target)
    monkeypatch.setattr(mesh_cache.os, "replace", locked)
    result = record(cfd)
    assert not result["recorded"]
    assert result["input_changed"] is False
    assert "缓存" in result["reason"]
    assert manifest.read_bytes() == before
    assert not list(cfd.glob(".mesh_cache.*.tmp"))
