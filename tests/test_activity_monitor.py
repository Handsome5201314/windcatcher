"""操作监控的本机持久化、脱敏、游标和并发边界，只使用临时目录。"""

from __future__ import annotations

import importlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from pipeline import activity_monitor as monitor


@pytest.fixture(autouse=True)
def isolated_monitor(tmp_path, monkeypatch):
    monkeypatch.setenv(monitor.DIRECTORY_ENV, str(tmp_path / "monitor"))
    return tmp_path / "monitor"


def _event(**updates):
    return {"kind": "ui", "action": "click", "target": "btn-view-3d", "route": "work", "case_id": "synthetic-one", "session_id": "session-one", **updates}


def test_persistence_server_timestamp_and_status(isolated_monitor):
    event_id = monitor.record_event(_event(timestamp="forged", id=500))
    reloaded = importlib.reload(monitor)
    result = reloaded.query_events()
    assert len(result["events"]) == 1
    entry = result["events"][0]
    assert entry["id"] == event_id == 1
    assert entry["timestamp"].endswith("Z")
    assert entry["timestamp"] != "forged"
    assert reloaded.get_status() == {
        "status": "recording", "total_events": 1, "last_event_at": entry["timestamp"],
        "last_event_id": event_id,
        "retention_days": 14, "max_events": 50_000, "database_path": ".local-monitor/activity.sqlite3",
    }
    assert (isolated_monitor / "activity.sqlite3").is_file()


def test_cursor_filters_and_error_request_results():
    first = monitor.record_event(_event())
    failed = monitor.record_event(_event(kind="request", action="response", details={"method": "POST", "status_code": 422, "duration_ms": 10.2}))
    monitor.record_event(_event(kind="error", action="unhandled", case_id="synthetic-two", session_id="session-two"))
    fourth = monitor.record_event(_event(kind="request", action="response", details={"status_code": 200}))
    page = monitor.query_events(limit=2)
    assert [entry["id"] for entry in page["events"]] == [first, failed]
    assert page["next_after"] == failed
    assert page["has_more"] is True
    next_page = monitor.query_events(after=page["next_after"], limit=2)
    assert len(next_page["events"]) == 2
    assert next_page["next_after"] == fourth
    assert next_page["has_more"] is False
    filtered = monitor.query_events(case_id="synthetic-one", kind="request", session_id="session-one", errors_only=True)
    assert [entry["id"] for entry in filtered["events"]] == [failed]
    assert monitor.query_events(after=fourth) == {"events": [], "next_after": fourth, "has_more": False}


def test_latest_returns_matching_tail_in_ascending_order():
    matching = []
    for sequence in range(7):
        event_id = monitor.record_event(_event(kind="request", action="response", details={"status_code": 422 if sequence % 2 else 200}))
        if sequence % 2:
            matching.append(event_id)
        monitor.record_event(_event(case_id="synthetic-two", session_id="session-two"))
    page = monitor.query_events(latest=True, limit=2, case_id="synthetic-one", kind="request", session_id="session-one", errors_only=True)
    assert [entry["id"] for entry in page["events"]] == matching[-2:]
    assert page["next_after"] == matching[-1]
    assert page["has_more"] is True
    assert monitor.query_events(after=page["next_after"], kind="request", errors_only=True)["events"] == []
    all_errors = monitor.query_events(latest=True, limit=3, errors_only=True)
    assert [entry["id"] for entry in all_errors["events"]] == matching
    assert all_errors["has_more"] is False


def test_empty_status_and_latest_query():
    status = monitor.get_status()
    assert status["last_event_id"] == 0
    assert status["last_event_at"] is None
    assert monitor.query_events(latest=True) == {"events": [], "next_after": 0, "has_more": False}


def test_context_fills_missing_correlation_fields_and_reset_isolated():
    token = monitor.CURRENT_CONTEXT.set({"session_id": "session-context", "request_id": "request-context"})
    try:
        monitor.record_event({"kind": "stage", "action": "update", "details": {"stage": "surface", "status": "ok"}})
        monitor.record_event({"kind": "stage", "action": "update", "session_id": "explicit-session", "request_id": "explicit-request"})
        monitor.record_event({"kind": "stage", "action": "update", "session_id": "private/path", "request_id": "https://private.invalid"})
    finally:
        monitor.CURRENT_CONTEXT.reset(token)
    monitor.record_event({"kind": "stage", "action": "update"})
    events = monitor.query_events()["events"]
    assert (events[0]["session_id"], events[0]["request_id"]) == ("session-context", "request-context")
    assert (events[1]["session_id"], events[1]["request_id"]) == ("explicit-session", "explicit-request")
    assert events[2]["session_id"] is events[2]["request_id"] is None
    assert events[3]["session_id"] is events[3]["request_id"] is None


def test_context_values_are_sanitized():
    token = monitor.CURRENT_CONTEXT.set({"session_id": "private/path", "request_id": "https://private.invalid"})
    try:
        monitor.record_event({"kind": "stage", "action": "update"})
    finally:
        monitor.CURRENT_CONTEXT.reset(token)
    event = monitor.query_events()["events"][0]
    assert event["session_id"] is event["request_id"] is None


def test_retention_prunes_on_read_and_keeps_boundary(monkeypatch):
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    monkeypatch.setattr(monitor, "_now", lambda: now - timedelta(days=14, seconds=1))
    expired = monitor.record_event(_event())
    monkeypatch.setattr(monitor, "_now", lambda: now - timedelta(days=14))
    boundary = monitor.record_event(_event())
    monkeypatch.setattr(monitor, "_now", lambda: now)
    assert monitor.get_status()["total_events"] == 1
    assert [entry["id"] for entry in monitor.query_events(after=expired)["events"]] == [boundary]


def test_maximum_events_retains_latest_and_ids_remain_monotonic(monkeypatch):
    monkeypatch.setattr(monitor, "MAX_EVENTS", 3)
    identifiers = [monitor.record_event(_event()) for _ in range(7)]
    assert monitor.get_status()["total_events"] == 3
    assert [entry["id"] for entry in monitor.query_events()["events"]] == identifiers[-3:]
    assert monitor.record_event(_event()) == identifiers[-1] + 1


def test_drops_unknown_fields_input_contents_and_raw_errors(isolated_monitor):
    secret = "sk-private-example-DO-NOT-STORE"
    monitor.record_event(_event(
        api_key=secret, body={"password": secret}, url="https://example.invalid/" + secret,
        details={
            "api_key": secret, "body": secret, "message": secret, "stack": secret,
            "value": secret, "source": "C:/private/" + secret, "status": secret,
            "error_type": secret, "controls": {"key": secret}, "duration_ms": 12,
            "view": "axial", "index": 23, "overlay": True,
        },
    ))
    entry = monitor.query_events()["events"][0]
    assert entry["details"] == {"error_type": "UnknownError", "duration_ms": 12, "view": "axial", "index": 23, "overlay": True}
    assert secret not in json.dumps(entry)
    # 验证数据库本体也未保存被剔除字段；不是仅在查询时隐藏。
    with sqlite3.connect(isolated_monitor / "activity.sqlite3") as connection:
        stored = connection.execute("SELECT * FROM events").fetchone()
    assert secret not in repr(stored)


def test_rejects_paths_urls_and_unknown_case_ids():
    monitor.record_event(_event(
        route="/work?api_key=private", target="C:\\private\\input", case_id="patient-A",
        request_id="https://example.invalid/key", session_id="session/private",
    ))
    entry = monitor.query_events()["events"][0]
    assert all(entry[key] is None for key in ("route", "target", "case_id", "request_id", "session_id"))
    actual = "case-20261009-112233-a1b2"
    monitor.record_event(_event(case_id=actual))
    assert len(monitor.query_events(case_id=actual)["events"]) == 1


@pytest.mark.parametrize("event", [None, [], {"kind": "private", "action": "click"}, {"kind": "ui", "action": "https://private.invalid"}])
def test_invalid_event_contract_raises(event):
    with pytest.raises(ValueError):
        monitor.record_event(event)


def test_finite_scalars_only_and_bounded_numeric_arrays():
    monitor.record_event(_event(details={
        "level": float("nan"), "width": float("inf"), "value": "private",
        "index": True, "checked": "true", "status_code": 900,
        "duration_ms": 10 ** 1000, "delta": 2.5, "controls": [1.0, -2, 3.5],
    }))
    assert monitor.query_events()["events"][0]["details"] == {"delta": 2.5, "controls": [1.0, -2, 3.5]}
    monitor.record_event(_event(details={"controls": list(range(17))}))
    assert monitor.query_events()["events"][1]["details"] == {}


def test_client_operation_time_does_not_control_retention(monkeypatch):
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    monkeypatch.setattr(monitor, "_now", lambda: now)
    monitor.record_event(_event(details={"client_time_ms": 0, "sequence": 1}))
    monitor.record_event(_event(details={"client_time_ms": 10_000_000_000_000, "sequence": 2}))
    monitor.record_event(_event(details={"client_time_ms": 10 ** 1000, "sequence": "private"}))
    events = monitor.query_events()["events"]
    assert events[0]["details"] == {"client_time_ms": 0, "sequence": 1}
    assert events[1]["details"] == {"client_time_ms": 10_000_000_000_000, "sequence": 2}
    assert events[2]["details"] == {}
    assert monitor.get_status()["total_events"] == 3
    monkeypatch.setattr(monitor, "_now", lambda: now + timedelta(days=14, seconds=1))
    assert monitor.get_status()["total_events"] == 0


@pytest.mark.parametrize("filters", [{"after": -1}, {"limit": 0}, {"limit": 501}, {"kind": "private"}, {"case_id": "patient-name"}, {"session_id": "../../private"}, {"errors_only": "true"}, {"latest": "true"}])
def test_invalid_filters_raise_without_querying(filters):
    with pytest.raises(ValueError):
        monitor.query_events(**filters)


def test_concurrent_writes_and_reads_have_no_missing_or_duplicate_events():
    # 包含尚未建库时页面请求与后台任务同时首次写入的情况。
    def write_batch(worker):
        result = []
        for sequence in range(20):
            result.append(monitor.record_event(_event(session_id=f"worker-{worker}", details={"count": sequence})))
            if sequence % 5 == 0:
                monitor.query_events(limit=3)
        return result

    with ThreadPoolExecutor(max_workers=6) as pool:
        batches = list(pool.map(write_batch, range(6)))
    written = [event_id for batch in batches for event_id in batch]
    entries = monitor.query_events(limit=500)["events"]
    assert len(written) == len(set(written)) == len(entries) == 120
    assert [entry["id"] for entry in entries] == sorted(written)
    assert monitor.get_status()["total_events"] == 120


def test_database_io_failure_propagates(isolated_monitor):
    isolated_monitor.write_text("not a directory", encoding="utf-8")
    with pytest.raises(OSError):
        monitor.record_event(_event())
