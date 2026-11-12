"""Personal recipe hints never turn browser activity into an automatic action."""

from datetime import datetime, timedelta, timezone

import pytest

from pipeline import activity_monitor
from pipeline.operator_recipe_candidates import suggest_candidates


OWNER = "a" * 32
OTHER = "b" * 32
CASES = [f"case-20261102-00000{i}-{i:04x}" for i in range(1, 5)]


def signal(number, case_id, stage, *, owner=OWNER, status="ok",
           action="action_finished", kind="stage"):
    return {"id": number, "session_id": owner, "case_id": case_id,
            "kind": kind, "action": action,
            "details": {"stage": stage, "status": status}}


def test_three_distinct_authorized_cases_suggest_only_a_repeated_sequence():
    events = []
    number = 0
    for case_id in CASES[:3]:
        for stage in ("qc", "segment", "surface"):
            number += 1
            events.append(signal(number, case_id, stage))
    events.extend([
        signal(10, CASES[0], "qc"),  # Rerun does not add another case.
        signal(11, CASES[3], "cfd_run"),  # No guessed mapping to a recipe step.
        signal(12, CASES[3], "endplanes"),
        signal(13, CASES[3], "report", status="failed"),
        signal(14, CASES[3], "report", owner=OTHER),
        signal(15, None, "report"),
        signal(16, CASES[3], "report", action="action_started"),
    ])
    allowed = set(CASES)
    assert suggest_candidates(list(reversed(events)), OWNER, allowed) == [
        {"suggested_steps": ["qc", "segment", "surface"], "case_count": 3},
    ]
    assert suggest_candidates(events, OWNER, set(CASES[:2])) == []


def test_repeating_one_case_never_meets_support_threshold():
    events = [signal(index, CASES[0], "qc" if index % 2 else "segment")
              for index in range(1, 20)]
    assert suggest_candidates(events, OWNER, {CASES[0]}) == []


def test_signal_query_enforces_server_time_retention_and_account(monkeypatch, tmp_path):
    monkeypatch.setenv("WINDCATCHER_MULTIUSER", "1")
    monkeypatch.setenv(activity_monitor.DIRECTORY_ENV, str(tmp_path))
    now = datetime(2026, 11, 2, tzinfo=timezone.utc)

    def record(at, owner, case_id=CASES[0]):
        monkeypatch.setattr(activity_monitor, "_now", lambda: at)
        return activity_monitor.record_event({
            "kind": "stage", "action": "action_finished", "session_id": owner,
            "case_id": case_id, "details": {"stage": "qc", "status": "ok"},
        })

    record(now - timedelta(days=181), OWNER)
    boundary = record(now - timedelta(days=180), OWNER)
    record(now - timedelta(days=1), OTHER)
    record(now - timedelta(days=1), OWNER, case_id=CASES[1])
    monkeypatch.setattr(activity_monitor, "_now", lambda: now)
    rows = activity_monitor.query_recipe_signals(OWNER)
    assert [row["id"] for row in rows] == [boundary, boundary + 2]
    assert all(row["session_id"] == OWNER for row in rows)
    assert activity_monitor.query_recipe_signals(OTHER)[0]["case_id"] == CASES[0]
    monkeypatch.setenv("WINDCATCHER_MULTIUSER", "0")
    with pytest.raises(ValueError, match="personal operation history"):
        activity_monitor.query_recipe_signals(OWNER)
