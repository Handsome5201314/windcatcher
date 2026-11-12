"""The advisory route cannot bypass identity or return stale case advice."""

import json

import pytest
from fastapi.testclient import TestClient

import cloud
import server
from pipeline import ai_report, cases, harness_advice, harness_projection
import pipeline.config as config
from scripts.harness_sdk_worker import extract_metering, validate_model_output


CASE_ID = "synthetic-harness-api"
ADMIN_PASSWORD = "test-harness-admin-password"
MEMBER_PASSWORD = "test-harness-member-password"


@pytest.fixture
def api(tmp_path, monkeypatch):
    root = tmp_path / "cases"
    root.mkdir()
    folder = root / CASE_ID
    folder.mkdir()
    (folder / "case.json").write_text(json.dumps({
        "case_id": CASE_ID, "created_at": "2026-11-03T00:00:00+08:00",
        "source_type": "synthetic", "synthetic": True, "stages": {},
    }), encoding="utf-8")
    for module in (server, cases, config):
        monkeypatch.setattr(module, "CASES_DIR", root)
    monkeypatch.setattr(ai_report, "CONFIG_PATH", tmp_path / "ai-report-config.json")
    monkeypatch.setenv("WINDCATCHER_IDENTITY_DIR", str(tmp_path / "identity"))
    monkeypatch.setenv("WINDCATCHER_MULTIUSER", "1")
    monkeypatch.setenv("WINDCATCHER_DEPLOYMENT", "cloud")
    monkeypatch.setenv("WINDCATCHER_CLOUD_USERNAME", "harness-admin")
    monkeypatch.setenv("WINDCATCHER_CLOUD_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("WINDCATCHER_CLOUD_SESSION_SECRET", "test-harness-session-secret-long-enough-32")
    monkeypatch.setenv("WINDCATCHER_HARNESS_ADVICE", "1")
    monkeypatch.delenv("WINDCATCHER_MCP_TOKEN", raising=False)
    app = cloud.create_app()  # No lifespan: no CFD worker and no real model.
    admin = TestClient(app, base_url="https://windcatcher.test")
    member = TestClient(app, base_url="https://windcatcher.test")
    assert admin.post("/login", data={"username": "harness-admin", "password": ADMIN_PASSWORD},
                      follow_redirects=False).status_code == 303
    admin_id = app.state.identity.authenticate("harness-admin", ADMIN_PASSWORD)["id"]
    member_id = app.state.identity.create_user(admin_id, "harness-member", MEMBER_PASSWORD)["id"]
    assert member.post("/login", data={"username": "harness-member", "password": MEMBER_PASSWORD},
                       follow_redirects=False).status_code == 303
    return app, admin, member, admin_id, member_id


def projection(digest="a" * 64):
    return {
        "schema_version": 1, "case_id": CASE_ID, "state_sha256": digest,
        "stages": {name: "ok" for name in config.STAGES},
        "baseline_ready": True, "numeric_ok": True, "eligible": True,
    }


def response(value):
    advice = validate_model_output(json.dumps({
        "next_step": {"code": "review_report", "stage": "report"},
        "failure_recap": [],
        "skill_draft": {"trigger_condition": "manual", "suggested_steps": ["report"],
                        "allowed_tools": []},
        "human_review_required": True, "automated_action": False,
    }), value)
    advice["metering"] = extract_metering([
        {"type": "turn/start", "data": {"turn": 1}},
        {"type": "step/start", "data": {"turn": 1, "step": 1}},
        {"type": "assistant/message", "data": {"turn": 1, "step": 1, "usage": {
            "inputTokens": 12, "outputTokens": 8, "totalTokens": 20,
        }}},
        {"type": "step/end", "data": {"turn": 1, "step": 1}},
        {"type": "turn/end", "data": {"turn": 1}},
    ], "gpt-6.1-sol")
    return advice


def test_unauthorized_account_never_projects_or_calls_harness(api, monkeypatch):
    _, admin, member, _, _ = api
    monkeypatch.setattr(harness_projection, "project_case_state",
                        lambda _id: pytest.fail("unauthorized projection"))
    monkeypatch.setattr(harness_advice, "request_advice",
                        lambda *_args: pytest.fail("unauthorized model call"))
    assert member.post(f"/api/cases/{CASE_ID}/harness-advice").status_code in {403, 404}
    assert TestClient(api[0]).post(f"/api/cases/{CASE_ID}/harness-advice").status_code == 401
    assert admin.get(f"/api/cases/{CASE_ID}/harness-advice").status_code in {403, 404, 405}


def test_authorized_advice_is_only_code_output(api, monkeypatch):
    _, admin, member, _, member_id = api
    assert admin.put(f"/api/cases/{CASE_ID}/grants/{member_id}",
                     json={"role": "viewer"}).status_code == 200
    value = projection()
    monkeypatch.setattr(harness_projection, "project_case_state", lambda _id: value)
    monkeypatch.setattr(harness_advice, "request_advice", lambda state, _actor: response(state))
    result = member.post(f"/api/cases/{CASE_ID}/harness-advice")
    assert result.status_code == 200, result.text
    assert result.json()["source"] == "deepseek-harness"
    assert result.json()["automated_action"] is False
    assert result.json()["metering"]["total_tokens"] == 20
    assert result.json()["metering"]["cost_usd"] is None


def test_revoked_while_model_runs_cannot_receive_result(api, monkeypatch):
    app, admin, member, admin_id, member_id = api
    assert admin.put(f"/api/cases/{CASE_ID}/grants/{member_id}",
                     json={"role": "viewer"}).status_code == 200
    monkeypatch.setattr(harness_projection, "project_case_state", lambda _id: projection())

    def revoke(state, _actor):
        app.state.identity.revoke_case_grant(admin_id, CASE_ID, member_id)
        return response(state)

    monkeypatch.setattr(harness_advice, "request_advice", revoke)
    assert member.post(f"/api/cases/{CASE_ID}/harness-advice").status_code == 404


def test_changed_state_after_model_call_is_rejected(api, monkeypatch):
    _, admin, _, _, _ = api
    states = iter([projection("a" * 64), projection("b" * 64)])
    monkeypatch.setattr(harness_projection, "project_case_state", lambda _id: next(states))
    monkeypatch.setattr(harness_advice, "request_advice", lambda state, _actor: response(state))
    assert admin.post(f"/api/cases/{CASE_ID}/harness-advice").status_code == 409


def test_disabled_pilot_does_not_read_case_or_call_model(api, monkeypatch):
    _, admin, _, _, _ = api
    monkeypatch.setenv("WINDCATCHER_HARNESS_ADVICE", "0")
    monkeypatch.setattr(harness_projection, "project_case_state",
                        lambda _id: pytest.fail("disabled projection"))
    result = admin.post(f"/api/cases/{CASE_ID}/harness-advice")
    assert result.status_code == 503
