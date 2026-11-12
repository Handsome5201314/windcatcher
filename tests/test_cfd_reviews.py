"""CFD review records remain version-bound evidence, not gate overrides."""

import json

import pytest
from fastapi.testclient import TestClient

import cloud
import server
from pipeline import cases, cfd_reviews, config


@pytest.fixture
def review_case(monkeypatch, tmp_path):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path / "cases")
    monkeypatch.setattr(config, "CASES_DIR", tmp_path / "cases")
    monkeypatch.setattr(server, "CASES_DIR", tmp_path / "cases")
    case_id = cases.create_case("synthetic", "", synthetic=True)
    cfd = cases.case_dir(case_id) / "cfd"
    files = {
        "run_status.json": json.dumps({"status": "ran", "run_id": "run-verified-001"}),
        "logs/simpleFoam.log": "Time = 1000s\nEnd\n",
        "system/fvSolution": "SIMPLE { residualControl { p 1e-4; U 1e-5; } }",
        "system/controlDict": "application simpleFoam; endTime 1000;",
        "mesh_cache.json": json.dumps({"signature": "mesh-1"}),
        "case_meta.json": json.dumps({"total_flow_lpm": 15}),
    }
    for relative, content in files.items():
        target = cfd / relative
        target.parent.mkdir(exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return case_id, cfd


def mesh_payload(signature):
    return {
        "expected_signature": signature, "kind": "mesh_sensitivity",
        "reviewer": "测试工程师", "note": "三套网格压降变化仍需独立核实。",
        "evidence_ref": "mesh-report-001", "grids": [
            {"role": "coarse", "run_ref": "coarse-001", "cells": 100000, "pressure_drop_pa": 12.1},
            {"role": "medium", "run_ref": "medium-001", "cells": 200000, "pressure_drop_pa": 12.0},
            {"role": "fine", "run_ref": "fine-001", "cells": 400000, "pressure_drop_pa": 11.95},
        ],
    }


def test_review_evidence_is_bound_to_run_and_never_changes_gate(review_case):
    case_id, cfd = review_case
    original = (cfd / "run_status.json").read_bytes()
    current = cfd_reviews.list_reviews(case_id)["run"]
    assert current["status"] == "ready"
    payload = mesh_payload(current["signature"])
    saved = cfd_reviews.record_review(case_id, payload)
    assert saved["status"] == "recorded_unverified"
    assert saved["release_effect"] == "none"
    assert (cfd / "run_status.json").read_bytes() == original
    record = cfd_reviews.list_reviews(case_id)["records"][0]
    assert not record["stale"]
    assert record["grids"][2]["cells"] == 400000
    (cfd / "logs/simpleFoam.log").write_text("Time = 1001s\nEnd\n", encoding="utf-8")
    assert cfd_reviews.list_reviews(case_id)["records"][0]["stale"]
    with pytest.raises(cfd_reviews.CfdReviewError, match="发生变化"):
        cfd_reviews.record_review(case_id, payload)


def test_review_history_survives_upstream_invalidation(review_case):
    case_id, cfd = review_case
    signature = cfd_reviews.list_reviews(case_id)["run"]["signature"]
    cfd_reviews.record_review(case_id, mesh_payload(signature))
    database = cases.case_dir(case_id) / cfd_reviews.REVIEW_DB
    assert database.is_file()
    cases.invalidate_downstream(case_id)
    assert cfd.is_dir() and database.is_file()
    result = cfd_reviews.list_reviews(case_id)
    assert result["run"]["status"] == "missing"
    assert len(result["records"]) == 1
    assert result["records"][0]["stale"] is True


def test_review_becomes_stale_when_result_field_changes(review_case):
    case_id, cfd = review_case
    field = cfd / "1000" / "U"
    field.parent.mkdir(exist_ok=True)
    field.write_text("initial field", encoding="utf-8")
    signature = cfd_reviews.list_reviews(case_id)["run"]["signature"]
    cfd_reviews.record_review(case_id, mesh_payload(signature))
    field.write_text("updated final velocity field", encoding="utf-8")
    assert cfd_reviews.list_reviews(case_id)["records"][0]["stale"] is True


@pytest.mark.parametrize("change", [
    lambda p: p["grids"][2].update(cells=150000),
    lambda p: p["grids"][1].update(run_ref="coarse-001"),
    lambda p: p.update(evidence_ref="C:/private/report.pdf"),
])
def test_invalid_or_private_mesh_evidence_is_rejected(review_case, change):
    case_id, _ = review_case
    payload = mesh_payload(cfd_reviews.list_reviews(case_id)["run"]["signature"])
    change(payload)
    with pytest.raises(cfd_reviews.CfdReviewError):
        cfd_reviews.record_review(case_id, payload)
    assert cfd_reviews.list_reviews(case_id)["records"] == []


def test_engineering_opinion_is_a_record_not_an_approval(review_case):
    case_id, _ = review_case
    signature = cfd_reviews.list_reviews(case_id)["run"]["signature"]
    record = cfd_reviews.record_review(case_id, {
        "expected_signature": signature, "kind": "engineering_review",
        "reviewer": "测试工程师", "decision": "reviewed",
        "note": "检查计算日志后仍需复核物理模型。", "evidence_ref": "review-report-001",
    })
    assert record["status"] == "recorded_unverified"
    assert cfd_reviews.list_reviews(case_id)["records"][0]["decision"] == "reviewed"


def test_cloud_review_routes_require_login_and_same_origin(review_case, monkeypatch):
    case_id, _ = review_case
    monkeypatch.setenv("WINDCATCHER_CLOUD_USERNAME", "researcher")
    monkeypatch.setenv("WINDCATCHER_CLOUD_PASSWORD", "test-only-password-long-enough")
    monkeypatch.setenv("WINDCATCHER_CLOUD_SESSION_SECRET", "test-only-session-secret-32-characters-minimum")
    client = TestClient(cloud.create_app(), base_url="https://windcatcher.test")
    path = f"/api/cases/{case_id}/cfd-reviews"
    assert client.get(path).status_code == 401
    assert client.post("/login", data={"username": "researcher", "password": "test-only-password-long-enough"},
                       follow_redirects=False).status_code == 303
    signature = client.get(path).json()["run"]["signature"]
    payload = mesh_payload(signature)
    assert client.post(path, json=payload, headers={"Origin": "https://other.test"}).status_code == 403
    response = client.post(path, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["release_effect"] == "none"
    assert client.get(path).json()["records"][0]["reviewer"] == "researcher/测试工程师"
