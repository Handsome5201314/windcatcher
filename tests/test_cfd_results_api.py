"""CFD resources must use the manifest's string path and reject old generations."""

import asyncio
import json

import pytest
from fastapi import HTTPException

import server
from pipeline import cases, cfd_results


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
