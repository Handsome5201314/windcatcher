"""CFD resources must use the manifest's string path and reject old generations."""

import asyncio
import json

import pytest
from fastapi import HTTPException

import server
from pipeline import cases, cfd_results
from pipeline.cfd_validation import inspect_latest_fields


@pytest.fixture
def result_case(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = cases.create_case("synthetic", "<合成测试>", synthetic=True)
    root = cases.case_dir(case_id)
    segmentation = {
        "params": {"hu_high": -300.0},
        "mask_version": 1,
        "need_human_confirm": False,
        "review_flags": {"external_air_suspected": False},
        "external_air_validation": {
            "mask_version": 1, "body_hu_threshold": -300.0,
            "outside_voxels": 0, "candidate_outside_voxels": 0,
        },
    }
    (root / "segment/segmentation.json").write_text(json.dumps(segmentation), encoding="utf-8")
    payload = "cfd/results/generations/current/payload.json"
    path = root / payload
    path.parent.mkdir(parents=True)
    path.write_text('{"source":{"case_id":"synthetic"}}', encoding="utf-8")
    monkeypatch.setattr(cfd_results, "get_results", lambda _id: {"status": "ready", "payload": payload})
    return case_id, root, payload


def test_current_generation_string_path_can_be_read(result_case):
    case_id, root, payload = result_case
    response = asyncio.run(server.api_artifact(case_id, payload))
    assert response.status_code == 200
    assert response.path == root / payload
    assert json.loads(response.path.read_text())["source"]["case_id"] == "synthetic"
    viewer = asyncio.run(server.api_cfd_results_viewer(case_id))
    assert viewer.path == response.path


def test_old_generation_is_rejected_even_when_file_exists(result_case):
    case_id, root, _payload = result_case
    old = "cfd/results/generations/old/payload.json"
    path = root / old
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(HTTPException) as error:
        asyncio.run(server.api_artifact(case_id, old))
    assert error.value.status_code == 409
    assert "过期" in error.value.detail["reason"]


def test_unconfirmed_mask_blocks_result_resources(result_case):
    case_id, root, payload = result_case
    (root / "segment/segmentation.json").write_text('{"need_human_confirm":true}', encoding="utf-8")
    with pytest.raises(HTTPException) as error:
        asyncio.run(server.api_artifact(case_id, payload))
    assert error.value.status_code == 409
    with pytest.raises(HTTPException) as viewer_error:
        asyncio.run(server.api_cfd_results_viewer(case_id))
    assert viewer_error.value.status_code == 409


@pytest.fixture
def postprocess_case(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = cases.create_case("synthetic", "<合成测试>", synthetic=True)
    root = cases.case_dir(case_id)
    (root / "segment" / "segmentation.json").write_text(json.dumps({
        "params": {"hu_high": -300.0}, "mask_version": 1,
        "need_human_confirm": False, "review_flags": {"external_air_suspected": False},
        "external_air_validation": {
            "mask_version": 1, "body_hu_threshold": -300.0,
            "outside_voxels": 0, "candidate_outside_voxels": 0,
        },
    }), encoding="utf-8")
    mesh = root / "cfd" / "constant" / "polyMesh"
    mesh.mkdir(parents=True)
    (mesh / "owner").write_text('FoamFile {format ascii; class labelList; object owner; note "nCells:1 nInternalFaces:0";}\n6(0 0 0 0 0 0)', encoding="ascii")
    (mesh / "neighbour").write_text("FoamFile {format ascii; class labelList; object neighbour;}\n0()", encoding="ascii")
    (mesh / "points").write_text("FoamFile {format ascii; class vectorField; object points;}\n8((0 0 0)(1 0 0)(1 1 0)(0 1 0)(0 0 1)(1 0 1)(1 1 1)(0 1 1))", encoding="ascii")
    (mesh / "faces").write_text("FoamFile {format ascii; class faceList; object faces;}\n6(4(0 4 7 3)4(1 2 6 5)4(0 1 5 4)4(3 7 6 2)4(0 3 2 1)4(4 5 6 7))", encoding="ascii")
    (mesh / "boundary").write_text("FoamFile {format ascii; class polyBoundaryMesh; object boundary;}\n3(inlet {type patch; nFaces 1; startFace 0;} outlet {type patch; nFaces 1; startFace 1;} wall {type wall; nFaces 4; startFace 2;})", encoding="ascii")
    final = root / "cfd" / "1000"
    final.mkdir()
    for name, field_class, dimensions, value in [
        ("U", "volVectorField", "0 1 -1 0 0 0 0", "(0.1 0 0)"),
        ("p", "volScalarField", "0 2 -2 0 0 0 0", "0"),
        ("phi", "surfaceScalarField", "0 3 -1 0 0 0 0", "0"),
    ]:
        (final / name).write_text(
            f"FoamFile {{format ascii; class {field_class}; object {name};}}\n"
            f"dimensions [{dimensions}]; internalField uniform {value};\n"
            "boundaryField {" + " ".join(
                f"{patch} {{type fixedValue; value uniform {value};}}" for patch in ("inlet", "outlet", "wall")
            ) + "}", encoding="ascii",
        )
    (root / "cfd" / "run_status.json").write_text(json.dumps({
        "status": "ran", "backend": "docker", "steps": [
            {"name": "simpleFoam", "returncode": 0},
            {"name": "reconstructPar", "returncode": 0},
            {"name": "foamToVTK", "returncode": 124, "timed_out": True},
        ],
    }), encoding="utf-8")
    cases.update_stage(case_id, "segment", "ok", data={"need_human_confirm": False})
    cases.update_stage(case_id, "cfd_run", "warn", data={"solved": True})
    return case_id, root


def test_final_fields_allow_browser_generation_after_vtk_export_timeout(postprocess_case):
    case_id, root = postprocess_case
    assert inspect_latest_fields(root / "cfd")["complete"] is True
    summary = next(item for item in cases.list_cases() if item["case_id"] == case_id)
    assert summary["stages"]["cfd_run"] == "warn"

    result = asyncio.run(server.api_cfd_results(case_id))

    assert result["status"] == "missing"
    assert result["generation"]["can_generate"] is True
    assert result["generation"]["timestep"] == "1000"
    assert "无需重新求解" in result["generation"]["reason"]
    assert result["validation"]["eligible"] is False
    assert not (root / "cfd" / "VTK").exists()
    assert not (root / "cfd" / "results").exists()


def test_generation_reports_missing_final_field_without_inferring_from_stage(postprocess_case):
    case_id, root = postprocess_case
    (root / "cfd" / "1000" / "U").unlink()
    result = asyncio.run(server.api_cfd_results(case_id))
    assert result["generation"]["can_generate"] is False
    assert "缺少 U" in result["generation"]["reason"]


def test_unconfirmed_mask_still_blocks_result_api_and_generation(postprocess_case):
    case_id, root = postprocess_case
    path = root / "segment" / "segmentation.json"
    segmentation = json.loads(path.read_text(encoding="utf-8"))
    segmentation["need_human_confirm"] = True
    path.write_text(json.dumps(segmentation), encoding="utf-8")
    for endpoint in (server.api_cfd_results, server.api_cfd_postprocess):
        with pytest.raises(HTTPException) as error:
            asyncio.run(endpoint(case_id))
        assert error.value.status_code == 409
        assert "尚未人工确认" in error.value.detail["reason"]


@pytest.mark.parametrize("task_status", ["running", "queued"])
def test_active_postprocess_cannot_be_started_twice(postprocess_case, monkeypatch, task_status):
    case_id, _root = postprocess_case
    monkeypatch.setattr(server.cfd_postprocess_jobs, "get_task", lambda _id: {"status": task_status})
    result = asyncio.run(server.api_cfd_results(case_id))
    assert result["status"] == task_status
    assert result["generation"]["can_generate"] is False
    assert "正在处理" in result["generation"]["reason"]


def test_upstream_running_blocks_generation_even_with_existing_fields(postprocess_case):
    case_id, _root = postprocess_case
    cases.update_stage(case_id, "cfd_run", "running")
    result = asyncio.run(server.api_cfd_results(case_id))
    assert result["generation"]["can_generate"] is False
    assert "正在处理" in result["generation"]["reason"]
