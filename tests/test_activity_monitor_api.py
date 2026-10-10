"""通过 ASGI 请求验证操作监控边界；不进入 lifespan 或读取真实病例。"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from fastapi import HTTPException

httpx = pytest.importorskip("httpx", reason="HTTP 集成测试需要 httpx（项目测试依赖）")

import server
from pipeline import activity_monitor as monitor
from pipeline import cases


@pytest.fixture(autouse=True)
def isolated_api(tmp_path, monkeypatch):
    monkeypatch.setenv(monitor.DIRECTORY_ENV, str(tmp_path / "monitor"))
    monkeypatch.setattr(server, "_MONITOR_FAILURE", None)
    monkeypatch.setattr(server, "list_cases", lambda: [])
    # 仅本测试添加的 HTTP 路由在 fixture 结束时撤销。
    monkeypatch.setattr(server.app.router, "routes", list(server.app.router.routes))


def _request(method, path, *, remote_host="127.0.0.1", **kwargs):
    async def send():
        transport = httpx.ASGITransport(app=server.app, raise_app_exceptions=False, client=(remote_host, 46000))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(send())


def _ui_event(**updates):
    return {"kind": "ui", "action": "click", "target": "btn-view-3d", "route": "work", "session_id": "session-one", **updates}


def test_successful_request_logs_start_finish_and_correlation_headers():
    response = _request("GET", "/api/cases", headers={
        "X-Windcatcher-Request-Id": "request-one", "X-Windcatcher-Session": "session-one",
    })
    assert response.status_code == 200
    assert response.headers["X-Windcatcher-Request-Id"] == "request-one"
    events = monitor.query_events()["events"]
    assert [event["action"] for event in events] == ["started", "finished"]
    assert all(event["kind"] == "request" and event["target"] == "cases" for event in events)
    assert all(event["request_id"] == "request-one" and event["session_id"] == "session-one" for event in events)
    assert events[0]["details"] == {"method": "GET"}
    assert events[1]["details"]["status_code"] == 200
    assert events[1]["details"]["outcome"] == "ok"
    assert events[1]["details"]["duration_ms"] >= 0
    assert monitor.CURRENT_CONTEXT.get() is None


def test_case_template_and_stage_event_inherit_request_context():
    def stage(case_id: str):
        monitor.record_event({"kind": "stage", "action": "updated", "case_id": case_id, "details": {"stage": "surface", "status": "ok"}})
        return {"ok": True}

    server.app.add_api_route("/api/cases/{case_id}/monitor-test", stage, methods=["GET"])
    case_id = "case-20261009-112233-a1b2"
    response = _request("GET", f"/api/cases/{case_id}/monitor-test", headers={
        "X-Windcatcher-Request-Id": "request-stage", "X-Windcatcher-Session": "session-stage",
    })
    assert response.status_code == 200
    events = monitor.query_events(case_id=case_id)["events"]
    assert [event["kind"] for event in events] == ["request", "stage", "request"]
    assert events[0]["target"] == events[2]["target"] == "cases:item:monitor-test"
    assert all(event["request_id"] == "request-stage" and event["session_id"] == "session-stage" for event in events)
    assert monitor.CURRENT_CONTEXT.get() is None


def test_handled_failure_and_uncaught_500_record_different_outcomes():
    def blocked():
        raise HTTPException(409, detail={"reason": "test rejection"})

    def failed():
        raise RuntimeError("private filesystem path and secret must not enter activity logs")

    server.app.add_api_route("/api/monitor-test/blocked", blocked, methods=["GET"])
    server.app.add_api_route("/api/monitor-test/failed", failed, methods=["GET"])
    assert _request("GET", "/api/monitor-test/blocked").status_code == 409
    assert _request("GET", "/api/monitor-test/failed").status_code == 500
    events = monitor.query_events()["events"]
    assert [event["action"] for event in events] == ["started", "finished", "started", "failed"]
    assert events[1]["details"]["status_code"] == 409
    assert events[1]["details"]["outcome"] == "failed"
    assert events[3]["details"]["error_type"] == "RuntimeError"
    assert events[3]["details"]["outcome"] == "error"
    assert "private filesystem" not in json.dumps(events)
    assert len(monitor.query_events(errors_only=True)["events"]) == 2
    assert monitor.CURRENT_CONTEXT.get() is None


def test_unknown_route_and_invalid_headers_do_not_persist_raw_values():
    secret = "private-secret-example"
    response = _request("GET", f"/api/unknown/{secret}?api_key={secret}", headers={
        "X-Windcatcher-Request-Id": f"https://private.invalid/{secret}",
        "X-Windcatcher-Session": f"private/{secret}",
    })
    assert response.status_code == 404
    events = monitor.query_events()["events"]
    assert len(events) == 2
    assert all(event["target"] == "unknown" and event["session_id"] is None for event in events)
    assert all(event["request_id"] == response.headers["X-Windcatcher-Request-Id"] for event in events)
    assert secret not in json.dumps(events)


def test_request_body_is_not_in_request_logs_or_database(tmp_path):
    def accepts_body(body: dict):
        return {"ok": True}

    server.app.add_api_route("/api/monitor-test/body", accepts_body, methods=["POST"])
    secret = "sk-private-body-example"
    response = _request("POST", "/api/monitor-test/body", json={
        "api_key": secret, "path": "C:/private/patient", "patient_name": "private patient",
    })
    assert response.status_code == 200
    events = monitor.query_events()["events"]
    assert len(events) == 2
    assert secret not in json.dumps(events)
    with sqlite3.connect(tmp_path / "monitor" / "activity.sqlite3") as connection:
        rows = connection.execute("SELECT * FROM events").fetchall()
    assert secret not in repr(rows)
    assert "patient_name" not in repr(rows)


def test_batch_sanitization_and_monitor_requests_do_not_log_themselves():
    secret = "sk-private-event-example"
    response = _request("POST", "/api/monitor/events", json={"events": [_ui_event(
        api_key=secret, details={"value": secret, "body": secret, "checked": True},
    )]})
    assert response.status_code == 200
    assert response.json() == {"accepted": 1, "last_event_id": 1}
    assert _request("GET", "/api/monitor/status").json()["total_events"] == 1
    queried = _request("GET", "/api/monitor/events").json()
    assert len(queried["events"]) == 1
    assert queried["events"][0]["details"] == {"checked": True}
    assert secret not in json.dumps(queried)
    assert monitor.get_status()["total_events"] == 1


def test_batch_boundary_and_invalid_events_are_rejected_before_writing():
    oversized = _request("POST", "/api/monitor/events", json={"events": [_ui_event()] * 51})
    assert oversized.status_code == 422
    assert monitor.get_status()["total_events"] == 0
    invalid = _request("POST", "/api/monitor/events", json={"events": [_ui_event(), _ui_event(action="private/path")]})
    assert invalid.status_code == 422
    assert monitor.get_status()["total_events"] == 0
    accepted = _request("POST", "/api/monitor/events", json={"events": [_ui_event()] * 50})
    assert accepted.status_code == 200
    assert accepted.json()["accepted"] == monitor.get_status()["total_events"] == 50


@pytest.mark.parametrize("path,method", [("/api/monitor/status", "GET"), ("/api/monitor/events", "GET"), ("/api/monitor/events", "POST")])
def test_external_origin_is_rejected_for_every_monitor_endpoint(path, method):
    kwargs = {"json": {"events": [_ui_event()]}} if method == "POST" else {}
    response = _request(method, path, headers={"Origin": "https://external.invalid"}, **kwargs)
    assert response.status_code == 403
    assert monitor.get_status()["total_events"] == 0


def test_non_loopback_client_rejected_and_same_origin_allowed():
    assert _request("GET", "/api/monitor/status", remote_host="192.0.2.1").status_code == 403
    same_origin = _request("GET", "/api/monitor/status", headers={"Origin": "http://127.0.0.1:8765"})
    assert same_origin.status_code == 200
    assert same_origin.json()["last_event_id"] == 0


def test_query_tail_and_combined_filters():
    events = []
    for index in range(5):
        events.extend([
            _ui_event(kind="request", action="finished", case_id="synthetic-one", details={"status_code": 400 + index}),
            _ui_event(case_id="synthetic-two", session_id="session-two"),
        ])
    assert _request("POST", "/api/monitor/events", json={"events": events}).status_code == 200
    response = _request("GET", "/api/monitor/events", params={
        "latest": "true", "limit": 2, "case_id": "synthetic-one", "kind": "request",
        "session_id": "session-one", "errors_only": "true",
    })
    assert response.status_code == 200
    payload = response.json()
    assert [event["id"] for event in payload["events"]] == [7, 9]
    assert payload["next_after"] == 9
    assert payload["has_more"] is True
    assert _request("GET", "/api/monitor/status").json()["last_event_id"] == 10


@pytest.mark.parametrize("params", [{"after": -1}, {"limit": 501}, {"kind": "private"}, {"case_id": "private/patient"}])
def test_invalid_query_filters_return_422(params):
    assert _request("GET", "/api/monitor/events", params=params).status_code == 422
    assert monitor.get_status()["total_events"] == 0


@pytest.mark.parametrize("path,function", [("/api/monitor/status", "get_status"), ("/api/monitor/events", "query_events")])
def test_database_read_failures_return_503_without_raw_error(monkeypatch, caplog, path, function):
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("C:/private/secret-path")

    monkeypatch.setattr(monitor, function, broken)
    response = _request("GET", path)
    assert response.status_code == 503
    assert "secret-path" not in response.text
    assert "secret-path" not in caplog.text


def test_write_failure_exposes_monitor_state_without_breaking_business_request(monkeypatch, caplog):
    def broken(event):
        raise sqlite3.OperationalError("private-secret-db-failure")

    monkeypatch.setattr(monitor, "record_event", broken)
    ordinary = _request("GET", "/api/cases")
    assert ordinary.status_code == 200
    assert ordinary.headers["X-Windcatcher-Monitor"] == "unavailable"
    posted = _request("POST", "/api/monitor/events", json={"events": [_ui_event()]})
    assert posted.status_code == 503
    status = _request("GET", "/api/monitor/status")
    assert status.status_code == 200
    assert status.json()["status"] == "unavailable"
    assert status.json()["error_type"] == "OperationalError"
    assert "private-secret" not in posted.text
    assert "private-secret" not in caplog.text


def test_case_stage_lifecycle_logs_committed_changes_and_inherits_http_context(tmp_path, monkeypatch):
    case_root = tmp_path / "cases"
    monkeypatch.setattr("pipeline.config.CASES_DIR", case_root)
    monkeypatch.setattr(cases, "CASES_DIR", case_root)

    def update_case():
        case_id = cases.create_case("synthetic", "C:/private/patient-identifier", synthetic=True)
        cases.update_stage(case_id, "surface", "ok", message="private text", data={"api_key": "private-key"})
        cases.update_job(case_id, "running", "cfd_prep", "private progress text")
        cases.invalidate_downstream(case_id, "private invalidation text")
        return {"case_id": case_id}

    server.app.add_api_route("/api/monitor-test/case-stage", update_case, methods=["POST"])
    response = _request("POST", "/api/monitor-test/case-stage", headers={
        "X-Windcatcher-Request-Id": "request-lifecycle", "X-Windcatcher-Session": "session-lifecycle",
    })
    assert response.status_code == 200
    case_id = response.json()["case_id"]
    events = monitor.query_events(case_id=case_id)["events"]
    assert [event["action"] for event in events] == ["case_created", "updated", "updated", "invalidated", "invalidated", "invalidated", "invalidated"]
    assert events[1]["details"] == {"stage": "surface", "status": "ok"}
    assert events[2]["details"] == {"stage": "cfd_prep", "status": "running"}
    assert [event["details"]["stage"] for event in events[3:]] == ["surface", "cfd_prep", "cfd_run", "report"]
    assert all(event["request_id"] == "request-lifecycle" and event["session_id"] == "session-lifecycle" for event in events)
    assert "private" not in json.dumps(events)
    assert cases.get_case(case_id)["stages"]["surface"]["status"] == "pending"
    assert (case_root / case_id / "case.json").is_file()


def test_monitor_failure_does_not_change_successful_case_writes(tmp_path, monkeypatch, caplog):
    case_root = tmp_path / "cases"
    monkeypatch.setattr("pipeline.config.CASES_DIR", case_root)
    monkeypatch.setattr(cases, "CASES_DIR", case_root)

    def broken(event):
        raise sqlite3.OperationalError("private-path-secret")

    monkeypatch.setattr(monitor, "record_event", broken)
    case_id = cases.create_case("synthetic", "<test>", synthetic=True)
    stage = cases.update_stage(case_id, "segment", "ok")
    job = cases.update_job(case_id, "idle", "segment")
    cases.invalidate_downstream(case_id)
    assert stage["status"] == "ok"
    assert job["status"] == "idle"
    assert cases.get_case(case_id)["stages"]["segment"]["status"] == "ok"
    assert "OperationalError" in caplog.text
    assert "private-path-secret" not in caplog.text
