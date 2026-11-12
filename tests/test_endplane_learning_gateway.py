"""Real gateway contracts for case-scoped proposals and human-only feedback.

The tests use synthetic anonymous case folders, an isolated identity store and
no app lifespan, so they neither load workstation credentials nor start CFD.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import cloud
import server
from pipeline import ai_report, cases, config, endplane_auto_candidate, endplane_learning, patient_shares


CASE = "case-20261104-123456-abcd"
OTHER = "case-20261104-123457-bcde"
PASSWORD = "synthetic-gateway-password-long-enough"
TOKEN = "T" * 43


def _plan(mask_version=3):
    return {
        "scope": "main_airway", "mask_version": mask_version,
        "format": "oblique_planes_v1",
        "anterior_plane": {"point_lps_mm": [10, 0, 0], "normal_lps": [0, -1, 0]},
        "posterior_plane": {"point_lps_mm": [30, 0, 0], "normal_lps": [0, 1, 0]},
    }


def _request(mask_version=3):
    return {
        "scope": "main_airway", "mask_version": mask_version,
        "plan": _plan(mask_version), "source": "model_candidate", "source_version": "v1",
    }


def _auto_result(mask_version=3):
    def entry(label, x):
        plan = {**_plan(mask_version), "format": "oblique_local_inlet_v2",
                "anterior_seed_lps_mm": [x, 0, 0]}
        plan["anterior_plane"] = {"point_lps_mm": [x, 0, 0], "normal_lps": [0, -1, 0]}
        return {"label": label, "plan": plan}
    return {"status": "available", "scope": "main_airway",
            "mask_version": mask_version, "generator_version": endplane_auto_candidate.GENERATOR_VERSION,
            "candidates": [entry("left", 9), entry("right", 11)]}


def _bilateral_auto_result(mask_version=3):
    def plane(x, y, normal_y):
        return {"point_lps_mm": [x, y, 8], "normal_lps": [0, normal_y, 0]}
    plan = {
        "format": "oblique_bilateral_local_v3", "scope": "main_airway",
        "mask_version": mask_version,
        "left_inlet_plane": plane(20, 9, -1),
        "right_inlet_plane": plane(7, 9, -1),
        "posterior_plane": plane(14, 29, 1),
        "left_inlet_seed_lps_mm": [20, 5, 8],
        "right_inlet_seed_lps_mm": [7, 5, 8],
        "posterior_seed_lps_mm": [14, 32, 8],
    }
    return {"status": "available", "scope": "main_airway",
            "mask_version": mask_version, "generator_version": endplane_auto_candidate.BILATERAL_GENERATOR_VERSION,
            "candidates": [{"label": "bilateral", "plan": plan}]}


def _path(case_id=CASE):
    return f"/api/cases/{case_id}/segment/endplanes/learning"


def _snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    root = tmp_path / "cases"
    root.mkdir()
    for case_id in (CASE, OTHER):
        folder = root / case_id
        (folder / "segment").mkdir(parents=True)
        (folder / "case.json").write_text(json.dumps({
            "case_id": case_id, "created_at": "2026-11-04T00:00:00+08:00",
            "source_type": "synthetic", "synthetic": True, "stages": {},
        }), encoding="utf-8")
        # Deliberately preserved sentinel: learning endpoints must not write or
        # normalize the authoritative saved plan while handling proposals.
        (folder / "segment" / "endplanes.json").write_bytes(b'{"synthetic_saved_plan":true}\n')
    for module in (server, cases, config):
        monkeypatch.setattr(module, "CASES_DIR", root)
    monkeypatch.setattr(ai_report, "CONFIG_PATH", tmp_path / "ai-report-config.json")
    monkeypatch.setattr(ai_report, "_USER_SESSION_KEYS", {})
    monkeypatch.setattr(ai_report, "_SESSION_API_KEY", "")
    monkeypatch.setattr(ai_report, "_SESSION_API_BASE_URL", "")
    monkeypatch.setattr(patient_shares, "SHARE_DIR", tmp_path / "shares")
    monkeypatch.setenv("WINDCATCHER_IDENTITY_DIR", str(tmp_path / "identity"))
    monkeypatch.setenv(endplane_learning.DIRECTORY_ENV, str(tmp_path / "learning"))
    monkeypatch.setenv("WINDCATCHER_MULTIUSER", "1")
    monkeypatch.setenv("WINDCATCHER_DEPLOYMENT", "public")
    monkeypatch.setenv("WINDCATCHER_PUBLIC_URL", "https://windcatcher.test")
    monkeypatch.setenv("WINDCATCHER_PUBLIC_USERNAME", "gateway-owner")
    monkeypatch.setenv("WINDCATCHER_PUBLIC_PASSWORD", PASSWORD)
    monkeypatch.setenv("WINDCATCHER_PUBLIC_SESSION_SECRET", "synthetic-gateway-session-secret-32-chars")
    monkeypatch.setenv("WINDCATCHER_MCP_TOKEN", TOKEN)
    monkeypatch.setattr(endplane_learning, "_DEFAULT_STORE", None)
    monkeypatch.setattr(endplane_learning, "_DEFAULT_STORE_PATH", None)
    current = {"mask_version": 3, "can_save": True}
    monkeypatch.setattr(server.endplanes, "get_endplanes", lambda case_id, scope: {
        "case_id": case_id, "scope": scope, "mask_version": current["mask_version"],
        "can_save": current["can_save"],
    })
    app = cloud.create_app()
    identity = app.state.identity
    owner_id = identity.authenticate("gateway-owner", PASSWORD)["id"]
    reviewer_id = identity.create_user(owner_id, "gateway-reviewer", PASSWORD)["id"]
    viewer_id = identity.create_user(owner_id, "gateway-viewer", PASSWORD)["id"]
    service_id = identity.mcp_principal()["id"]

    def human(username):
        client = TestClient(app, base_url="https://windcatcher.test")
        assert client.post("/login", data={"username": username, "password": PASSWORD},
                           follow_redirects=False).status_code == 303
        return client

    owner = human("gateway-owner")
    reviewer = human("gateway-reviewer")
    viewer = human("gateway-viewer")
    local = TestClient(app, base_url="https://127.0.0.1:8765", client=("127.0.0.1", 50000))

    def grant(user_id, role, case_id=CASE):
        identity.grant_case(owner_id, case_id, user_id, role)

    before = _snapshot(root)
    yield {
        "app": app, "owner": owner, "reviewer": reviewer, "viewer": viewer,
        "local": local, "headers": {"X-Windcatcher-MCP-Token": TOKEN},
        "owner_id": owner_id, "reviewer_id": reviewer_id, "viewer_id": viewer_id,
        "service_id": service_id, "grant": grant, "current": current,
    }
    assert _snapshot(root) == before, "Learning endpoints changed authoritative case files"


def _service_proposal(gateway, *, case_id=CASE):
    gateway["grant"](gateway["service_id"], "viewer", case_id)
    response = gateway["local"].post(_path(case_id) + "/propose",
                                    headers=gateway["headers"], json=_request())
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["human_review_required"] is True and result["committed"] is False
    return result["proposal"]


def test_local_mcp_token_can_read_and_propose_only_with_explicit_case_grant(gateway):
    local, headers = gateway["local"], gateway["headers"]
    assert local.get(_path(), headers=headers).status_code in {403, 404}
    assert local.post(_path() + "/propose", headers=headers, json=_request()).status_code in {403, 404}
    proposal = _service_proposal(gateway)
    response = local.get(_path(), headers=headers)
    assert response.status_code == 200, response.text
    assert [item["proposal_id"] for item in response.json()["proposals"]] == [proposal["proposal_id"]]


def test_local_auto_proposal_requires_case_grant_and_records_both_candidates(gateway, monkeypatch):
    monkeypatch.setattr(server.endplane_auto_candidate, "generate_auto_candidates",
                        lambda case_id, scope, version: _auto_result(version))
    path = _path() + "/auto-propose"
    payload = {"scope": "main_airway", "mask_version": 3}
    local, headers = gateway["local"], gateway["headers"]
    assert local.post(path, headers=headers, json=payload).status_code in {403, 404}
    gateway["grant"](gateway["service_id"], "viewer")
    result = local.post(path, headers=headers, json=payload)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["human_review_required"] is True and data["committed"] is False
    assert len(data["proposals"]) == 2
    assert data["proposal_id"] == data["proposal"]["proposal_id"]
    assert {item["source"] for item in data["proposals"]} == {
        "local_mask_geometry_left", "local_mask_geometry_right"}
    assert all(item["actor_id"] == gateway["service_id"] for item in data["proposals"])
    assert local.post(path, headers=headers, json=payload).json()["proposal_id"] == data["proposal_id"]
    assert len(local.get(_path(), headers=headers).json()["proposals"]) == 2
    assert local.post(_path() + "/feedback", headers=headers, json={
        "proposal_id": data["proposal_id"], "status": "accepted",
    }).status_code in {401, 403, 404}


def test_bilateral_auto_proposal_requires_grant_and_keeps_formal_case_unchanged(gateway, monkeypatch):
    monkeypatch.setattr(server.endplane_auto_candidate, "generate_bilateral_candidate",
                        lambda case_id, scope, version: _bilateral_auto_result(version))
    path = _path() + "/auto-propose-bilateral"
    payload = {"scope": "main_airway", "mask_version": 3}
    local, headers = gateway["local"], gateway["headers"]
    assert local.post(path, headers=headers, json=payload).status_code in {403, 404}
    assert gateway["viewer"].post(path, json=payload).status_code in {403, 404}
    gateway["grant"](gateway["service_id"], "viewer")
    gateway["grant"](gateway["viewer_id"], "viewer")
    result = local.post(path, headers=headers, json=payload)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["human_review_required"] is True and data["committed"] is False
    assert data["proposal"]["plan"]["format"] == "oblique_bilateral_local_v3"
    assert data["proposal"]["source"] == "bilateral_mask_geometry"
    assert data["proposal"]["actor_id"] == gateway["service_id"]
    assert gateway["viewer"].post(path, json=payload).status_code == 200
    assert local.post(_path() + "/feedback", headers=headers, json={
        "proposal_id": data["proposal_id"], "status": "accepted",
    }).status_code in {401, 403, 404}


def test_bilateral_auto_proposal_rejects_stale_and_invalid_without_ledger_write(gateway, monkeypatch):
    calls = []
    monkeypatch.setattr(server.endplane_auto_candidate, "generate_bilateral_candidate",
                        lambda *args: calls.append(args) or _bilateral_auto_result(args[2]))
    gateway["grant"](gateway["service_id"], "viewer")
    path = _path() + "/auto-propose-bilateral"
    local, headers = gateway["local"], gateway["headers"]
    for payload in ({"scope": "main_airway", "mask_version": 2},
                    {"scope": "main_airway", "mask_version": 3, "name": "forbidden"}):
        assert local.post(path, headers=headers, json=payload).status_code in {409, 422}
    gateway["current"]["can_save"] = False
    assert local.post(path, headers=headers, json={
        "scope": "main_airway", "mask_version": 3,
    }).status_code == 409
    assert calls == []
    gateway["current"]["can_save"] = True
    invalid = _bilateral_auto_result()
    invalid["candidates"][0]["plan"]["left_inlet_plane"]["normal_lps"] = [0, 0, 0]
    monkeypatch.setattr(server.endplane_auto_candidate, "generate_bilateral_candidate", lambda *args: invalid)
    assert local.post(path, headers=headers, json={
        "scope": "main_airway", "mask_version": 3,
    }).status_code == 502
    assert local.get(_path(), headers=headers).json()["proposals"] == []


def test_formal_ports_are_case_granted_and_read_only(gateway, monkeypatch):
    calls = []
    def formal(case_id, scope):
        calls.append((case_id, scope))
        return {"status": "not_generated", "scope": scope, "ports": []}
    monkeypatch.setattr(server.formal_ports, "get_formal_port_state", formal)
    path = f"/api/cases/{CASE}/segment/ports/formal?scope=main_airway"
    assert gateway["viewer"].get(path).status_code in {403, 404}
    gateway["grant"](gateway["viewer_id"], "viewer")
    response = gateway["viewer"].get(path)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "not_generated"
    assert calls == [(CASE, "main_airway")]


def test_auto_proposal_rejects_stale_unconfirmed_and_unknown_payload(gateway, monkeypatch):
    calls = []
    monkeypatch.setattr(server.endplane_auto_candidate, "generate_auto_candidates",
                        lambda *args: calls.append(args) or _auto_result(args[2]))
    gateway["grant"](gateway["service_id"], "viewer")
    path = _path() + "/auto-propose"
    local, headers = gateway["local"], gateway["headers"]
    for payload in ({"scope": "main_airway", "mask_version": 2},
                    {"scope": "main_airway", "mask_version": 3, "patient_name": "forbidden"},
                    {"scope": "main_airway", "mask_version": "3"}):
        assert local.post(path, headers=headers, json=payload).status_code in {409, 422}
    gateway["current"]["can_save"] = False
    assert local.post(path, headers=headers, json={
        "scope": "main_airway", "mask_version": 3,
    }).status_code == 409
    assert calls == []
    assert local.get(_path(), headers=headers).json()["proposals"] == []


def test_auto_proposal_unavailable_or_invalid_output_does_not_enter_ledger(gateway, monkeypatch):
    gateway["grant"](gateway["service_id"], "viewer")
    path = _path() + "/auto-propose"
    local, headers = gateway["local"], gateway["headers"]
    payload = {"scope": "main_airway", "mask_version": 3}
    monkeypatch.setattr(server.endplane_auto_candidate, "generate_auto_candidates", lambda *args: {
        "status": "unavailable", "scope": "main_airway", "mask_version": 3,
        "reason": "ambiguous_nostril_geometry", "candidates": [],
    })
    unavailable = local.post(path, headers=headers, json=payload)
    assert unavailable.status_code == 200 and unavailable.json()["status"] == "unavailable"
    invalid = _auto_result()
    invalid["candidates"][1]["plan"]["anterior_plane"]["normal_lps"] = [0, 0, 0]
    monkeypatch.setattr(server.endplane_auto_candidate, "generate_auto_candidates", lambda *args: invalid)
    assert local.post(path, headers=headers, json=payload).status_code == 502
    assert local.get(_path(), headers=headers).json()["proposals"] == []


def test_auto_proposal_rechecks_mask_before_ledger_write(gateway, monkeypatch):
    gateway["grant"](gateway["service_id"], "viewer")
    def changed_during_generation(*args):
        gateway["current"]["mask_version"] = 4
        return _auto_result()
    monkeypatch.setattr(server.endplane_auto_candidate, "generate_auto_candidates",
                        changed_during_generation)
    response = gateway["local"].post(_path() + "/auto-propose",
                                       headers=gateway["headers"],
                                       json={"scope": "main_airway", "mask_version": 3})
    assert response.status_code == 409, response.text
    assert gateway["local"].get(_path(), headers=gateway["headers"]).json()["proposals"] == []


def test_service_candidate_is_case_scoped_and_reviewable_by_authorized_human(gateway):
    proposal = _service_proposal(gateway)
    gateway["grant"](gateway["reviewer_id"], "reviewer")
    listed = gateway["reviewer"].get(_path())
    assert listed.status_code == 200, listed.text
    assert [item["proposal_id"] for item in listed.json()["proposals"]] == [proposal["proposal_id"]]
    assert listed.json()["proposals"][0]["actor_id"] == gateway["service_id"]
    accepted = gateway["reviewer"].post(_path() + "/feedback", json={
        "proposal_id": proposal["proposal_id"], "status": "accepted",
        "feedback": {"reason_code": "operator_confirmed"},
    })
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "accepted"
    assert accepted.json()["actor_id"] == gateway["service_id"], "proposal author must stay immutable"
    after = gateway["local"].get(_path(), headers=gateway["headers"])
    assert after.json()["proposals"][0]["status"] == "accepted"


def test_token_never_supplies_human_feedback_even_with_review_grant(gateway):
    proposal = _service_proposal(gateway)
    gateway["grant"](gateway["service_id"], "reviewer")
    response = gateway["local"].post(_path() + "/feedback", headers=gateway["headers"], json={
        "proposal_id": proposal["proposal_id"], "status": "accepted",
    })
    assert response.status_code in {401, 403, 404}
    rows = gateway["local"].get(_path(), headers=gateway["headers"]).json()["proposals"]
    assert rows[0]["status"] == "proposed"


def test_ungranted_human_and_viewer_cannot_label_candidate(gateway):
    proposal = _service_proposal(gateway)
    payload = {"proposal_id": proposal["proposal_id"], "status": "accepted"}
    assert gateway["viewer"].get(_path()).status_code in {403, 404}
    assert gateway["viewer"].post(_path() + "/feedback", json=payload).status_code in {403, 404}
    gateway["grant"](gateway["viewer_id"], "viewer")
    assert gateway["viewer"].get(_path()).status_code == 200
    assert gateway["viewer"].post(_path() + "/feedback", json=payload).status_code in {403, 404}
    rows = gateway["local"].get(_path(), headers=gateway["headers"]).json()["proposals"]
    assert rows[0]["status"] == "proposed"


def test_cross_case_path_cannot_read_or_label_another_cases_proposal(gateway):
    proposal = _service_proposal(gateway)
    gateway["grant"](gateway["service_id"], "viewer", OTHER)
    gateway["grant"](gateway["reviewer_id"], "reviewer", OTHER)
    listed = gateway["local"].get(_path(OTHER), headers=gateway["headers"])
    assert listed.status_code == 200, listed.text
    assert listed.json()["proposals"] == []
    response = gateway["reviewer"].post(_path(OTHER) + "/feedback", json={
        "proposal_id": proposal["proposal_id"], "status": "accepted",
    })
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("bad", [{"unknown": "forbidden"}, {"mask_version": "3"}])
def test_http_payload_contract_rejects_unknown_fields_and_coerced_versions(gateway, bad):
    gateway["grant"](gateway["service_id"], "viewer")
    response = gateway["local"].post(_path() + "/propose", headers=gateway["headers"],
                                    json={**_request(), **bad})
    assert response.status_code == 422, response.text
    listed = gateway["local"].get(_path(), headers=gateway["headers"])
    assert listed.json()["proposals"] == []


def test_stale_mask_proposal_and_human_feedback_return_409(gateway):
    proposal = _service_proposal(gateway)
    gateway["grant"](gateway["reviewer_id"], "reviewer")
    gateway["current"]["mask_version"] = 4
    stale = gateway["local"].post(_path() + "/propose", headers=gateway["headers"], json=_request())
    assert stale.status_code == 409, stale.text
    feedback = gateway["reviewer"].post(_path() + "/feedback", json={
        "proposal_id": proposal["proposal_id"], "status": "accepted",
    })
    assert feedback.status_code == 409, feedback.text
    listed = gateway["local"].get(_path(), headers=gateway["headers"])
    assert listed.json()["proposals"][0]["status"] == "proposed"


def test_unconfirmed_mask_blocks_proposal_and_human_feedback(gateway):
    proposal = _service_proposal(gateway)
    gateway["grant"](gateway["reviewer_id"], "reviewer")
    gateway["current"]["can_save"] = False
    proposed = gateway["local"].post(_path() + "/propose", headers=gateway["headers"], json=_request())
    assert proposed.status_code == 409, proposed.text
    feedback = gateway["reviewer"].post(_path() + "/feedback", json={
        "proposal_id": proposal["proposal_id"], "status": "accepted",
    })
    assert feedback.status_code == 409, feedback.text
    listed = gateway["local"].get(_path(), headers=gateway["headers"])
    assert listed.json()["proposals"][0]["status"] == "proposed"


def test_service_to_human_adjustment_to_frozen_independent_evaluation(gateway):
    from scripts import export_endplane_learning_dataset as exporter
    from scripts import evaluate_endplane_learning as evaluator

    proposal = _service_proposal(gateway)
    gateway["grant"](gateway["reviewer_id"], "reviewer")
    corrected = _plan()
    corrected["anterior_plane"]["point_lps_mm"][1] = 3
    result = gateway["reviewer"].post(_path() + "/feedback", json={
        "proposal_id": proposal["proposal_id"], "status": "adjusted",
        "feedback": {"adjusted_plan": corrected, "reason_code": "operator_adjusted"},
    })
    assert result.status_code == 200, result.text
    frozen = exporter.export_dataset(
        [proposal["proposal_id"]], source_cohort_sha256="a" * 64,
        actor_id=gateway["service_id"],
        review_references={proposal["proposal_id"]: {
            "evaluation_authorization_ref": "synthetic-authorization",
            "reference_confirmation_ref": "synthetic-reference",
            "reviewer_id": "synthetic-reviewer", "reviewed_at_utc": "2026-11-04T00:00:00Z",
        }},
    )
    assert frozen["records"][0]["plan"] == corrected
    assert frozen["records"][0]["feedback_status"] == "adjusted"
    candidates = {"schema_version": 1, "frozen_manifest_sha256": frozen["manifest_sha256"],
                  "candidates": [{"case_id": CASE, "scope": "main_airway",
                                  "mask_version": 3, "plan": _plan()}]}
    report = evaluator.evaluate_learning(
        frozen, candidates, tolerances={"max_plane_offset_mm": 1, "max_normal_deg": 5},
    )
    assert report["failed"] == 1 and report["passed"] == 0
    assert report["cases"][0]["metrics"]["anterior_plane_offset_mm"] == 3
    assert report["auto_promotion"] is False
    assert gateway["service_id"] not in json.dumps(frozen)
    assert gateway["reviewer_id"] not in json.dumps(frozen)
