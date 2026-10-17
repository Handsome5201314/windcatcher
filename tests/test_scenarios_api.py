"""鼻塞方案 HTTP 契约测试。"""

from __future__ import annotations

import zipfile
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

import server
from pipeline import cases, config, plug_models


def _box_stl():
    vertices = (
        (0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0),
        (0, 0, 10), (10, 0, 10), (10, 10, 10), (0, 10, 10),
    )
    faces = (
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
        (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    )
    return plug_models.mesh_to_binary_stl(plug_models.Mesh(vertices, faces, "test"))


@pytest.fixture
def scenario_client(tmp_path, monkeypatch):
    root = tmp_path / "cases"
    monkeypatch.setattr(config, "CASES_DIR", root)
    monkeypatch.setattr(cases, "CASES_DIR", root)
    monkeypatch.setattr(server, "CASES_DIR", root)
    case_id = cases.create_case("synthetic", "<合成测试>", synthetic=True)
    with TestClient(server.app) as client:
        yield client, case_id


def test_scenario_api_upload_place_review_and_download(scenario_client):
    client, case_id = scenario_client
    response = client.post(
        f"/api/cases/{case_id}/scenarios",
        data={"unit": "mm"},
        files={"model": ("plug.stl", _box_stl(), "model/stl")},
    )
    assert response.status_code == 200
    scenario = response.json()
    scenario_id = scenario["scenario_id"]
    assert scenario["download_url"].endswith("/download")

    missing_version = client.put(
        f"/api/cases/{case_id}/scenarios/{scenario_id}/placement",
        json={"translation_mm": [0, 0, 0], "rotation_deg": [0, 0, 0], "scale": [1, 1, 1]},
    )
    assert missing_version.status_code == 422

    blocked = client.post(
        f"/api/cases/{case_id}/scenarios/{scenario_id}/review",
        json={"approved": True, "reviewer_role": "engineer", "expected_version": 1},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["status"] == "blocked"

    placed = client.put(
        f"/api/cases/{case_id}/scenarios/{scenario_id}/placement",
        json={"translation_mm": [1, 2, 3], "rotation_deg": [0, 0, 10], "scale": [1, 1, 1], "expected_version": 1},
    )
    assert placed.status_code == 200
    assert placed.json()["current_version"] == 2

    approved = client.post(
        f"/api/cases/{case_id}/scenarios/{scenario_id}/review",
        json={"approved": True, "reviewer_role": "engineer", "notes": "已人工核对", "expected_version": 2},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "reviewed"

    stale = client.post(
        f"/api/cases/{case_id}/scenarios/{scenario_id}/review",
        json={"approved": True, "reviewer_role": "engineer", "expected_version": 1},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["status"] == "blocked"

    download = client.get(f"/api/cases/{case_id}/scenarios/{scenario_id}/download")
    assert download.status_code == 200
    with zipfile.ZipFile(BytesIO(download.content)) as archive:
        assert "input/original.stl" in archive.namelist()
        assert "manifest.json" in archive.namelist()


def test_scenario_api_rejects_unknown_unit_and_non_multipart(scenario_client):
    client, case_id = scenario_client
    non_multipart = client.post(f"/api/cases/{case_id}/scenarios", json={"unit": "mm"})
    assert non_multipart.status_code == 400
    assert "multipart/form-data" in non_multipart.json()["detail"]["reason"]

    bad_unit = client.post(
        f"/api/cases/{case_id}/scenarios",
        data={"unit": "yard"},
        files={"model": ("plug.stl", _box_stl(), "model/stl")},
    )
    assert bad_unit.status_code == 400
    assert "单位" in bad_unit.json()["detail"]["reason"]


def test_scenario_cfd_run_returns_background_task(scenario_client, monkeypatch):
    client, case_id = scenario_client
    uploaded = client.post(
        f"/api/cases/{case_id}/scenarios",
        data={"unit": "mm"},
        files={"model": ("plug.stl", _box_stl(), "model/stl")},
    )
    scenario_id = uploaded.json()["scenario_id"]
    monkeypatch.setattr(server.scenario_jobs, "start", lambda *args, **kwargs: {
        "job_id": "job-test", "status": "queued", "case_id": case_id,
        "scenario_id": scenario_id, "version": 1,
    })

    response = client.post(
        f"/api/cases/{case_id}/scenarios/{scenario_id}/cfd-run",
        json={"version": 1},
    )
    assert response.status_code == 202
    assert response.json()["task"]["job_id"] == "job-test"
    assert response.json()["scenario"]["download_url"].endswith("/download")
