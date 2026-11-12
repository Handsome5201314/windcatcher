"""Durability, idempotency, cancellation and race boundaries of real queue storage."""

import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from pipeline.batch_jobs import BatchQueue, ProcessLease
from pipeline.errors import PipelineError


def wait_for(queue, batch_id, predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = queue.get(batch_id)
        if predicate(value):
            return value
        time.sleep(0.01)
    pytest.fail("queue did not reach expected state")


def test_serial_execution_idempotency_and_cancel(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def execute(item):
        calls.append(item["case_id"])
        entered.set()
        assert release.wait(5)
        return {"task_status": "solved", "gate": {"eligible": False}}

    queue = BatchQueue(tmp_path / "queue.sqlite3", lambda item: {"version": 1}, execute)
    items = [{"case_id": "a"}, {"case_id": "b"}]
    batch = queue.submit("same-key", items)
    assert queue.submit("same-key", items)["batch_id"] == batch["batch_id"]
    with pytest.raises(PipelineError, match="不同批量参数"):
        queue.submit("same-key", items[:1])
    queue.start()
    try:
        assert entered.wait(5)
        assert queue.get(batch["batch_id"])["counts"] == {"queued": 1, "running": 1, "solved": 0, "blocked": 0, "failed": 0, "cancelled": 0}
        with pytest.raises(PipelineError, match="批量任务"):
            with queue.mutation("a"):
                pass
        assert queue.cancel(batch["batch_id"])["counts"]["cancelled"] == 1
        release.set()
        done = wait_for(queue, batch["batch_id"], lambda b: b["status"] == "completed")
        assert calls == ["a"]
        assert done["counts"]["solved"] == 1
        assert done["items"][0]["result"]["gate"]["eligible"] is False
        assert queue.submit("same-key", items)["status"] == "completed"
        with sqlite3.connect(queue.path) as db:
            assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 5
    finally:
        release.set()
        queue.stop()


def test_restart_runs_queued_but_never_silently_reruns_interrupted(tmp_path):
    path = tmp_path / "queue.sqlite3"
    queue = BatchQueue(path, lambda item: {"version": 1}, lambda item: {})
    batch = queue.submit("before-restart", [{"case_id": "a"}, {"case_id": "b"}])
    with sqlite3.connect(path) as db:
        db.execute("UPDATE items SET status='running' WHERE position=0")
    calls = []

    def execute(item):
        calls.append(item["case_id"])
        return {"task_status": "solved"}

    restored = BatchQueue(path, lambda item: {"version": 1}, execute)
    restored.start()
    try:
        done = wait_for(restored, batch["batch_id"], lambda b: b["status"] == "completed")
        assert calls == ["b"]
        assert done["items"][0]["status"] == "failed"
        assert done["items"][0]["result"]["stage"] == "interrupted"
    finally:
        restored.stop()


def test_stale_input_and_preflight_block_are_persisted(tmp_path):
    current = {"mask_version": 1}
    calls = []

    def preflight(item):
        if item["case_id"] == "unreviewed":
            raise PipelineError("blocked", "人工复核尚未完成", ["先确认"])
        return dict(current)

    queue = BatchQueue(tmp_path / "queue.sqlite3", preflight, lambda item: calls.append(item))
    batch = queue.submit("snapshot", [{"case_id": "unreviewed"}, {"case_id": "stale"}])
    current["mask_version"] = 2
    queue.start()
    try:
        done = wait_for(queue, batch["batch_id"], lambda b: b["status"] == "completed")
        assert done["counts"]["blocked"] == 2
        assert "排队期间" in done["items"][1]["result"]["reason"]
        assert calls == []
    finally:
        queue.stop()


def test_failure_does_not_skip_remaining_items(tmp_path):
    def execute(item):
        if item["case_id"] == "broken":
            raise RuntimeError("bad solver")
        return {"task_status": "solved"}

    queue = BatchQueue(tmp_path / "queue.sqlite3", lambda item: {}, execute)
    batch = queue.submit("failure", [{"case_id": "broken"}, {"case_id": "next"}])
    queue.start()
    try:
        done = wait_for(queue, batch["batch_id"], lambda b: b["status"] == "completed")
        assert [item["status"] for item in done["items"]] == ["failed", "solved"]
        assert done["items"][0]["result"]["error_type"] == "RuntimeError"
    finally:
        queue.stop()


def test_process_lease_blocks_second_service_and_releases(tmp_path):
    path = tmp_path / "service.lock"
    first = ProcessLease(path)
    first.acquire()
    try:
        with pytest.raises(PipelineError, match="已有工作台服务进程"):
            ProcessLease(path).acquire()
        probe = "\n".join((
            "import sys",
            "from pathlib import Path",
            "from pipeline.batch_jobs import ProcessLease",
            "from pipeline.errors import PipelineError",
            "lease = ProcessLease(Path(sys.argv[1]))",
            "try:", "    lease.acquire()",
            "except PipelineError:", "    sys.exit(23)",
        ))
        competing = subprocess.run([sys.executable, "-c", probe, str(path)],
                                   capture_output=True, text=True, timeout=10)
        assert competing.returncode == 23, competing.stderr
    finally:
        first.release()
    acquired = subprocess.run([sys.executable, "-c", probe, str(path)],
                              capture_output=True, text=True, timeout=10)
    assert acquired.returncode == 0, acquired.stderr


def test_two_queue_workers_cannot_mutate_same_case_together(tmp_path):
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def first_execute(_item):
        first_entered.set()
        assert release_first.wait(5)
        return {"task_status": "solved"}

    def second_execute(_item):
        second_entered.set()
        return {"task_status": "solved"}

    first = BatchQueue(tmp_path / "first.sqlite3", lambda _item: {}, first_execute)
    second = BatchQueue(tmp_path / "second.sqlite3", lambda _item: {}, second_execute)
    batch_a = first.submit("first", [{"case_id": "same"}])
    batch_b = second.submit("second", [{"case_id": "same"}])
    first.start()
    try:
        assert first_entered.wait(5)
        second.start()
        assert not second_entered.wait(0.2)
        with pytest.raises(PipelineError, match="批量任务"):
            with second.mutation("same"):
                pass
        release_first.set()
        assert wait_for(first, batch_a["batch_id"], lambda b: b["status"] == "completed")
        assert wait_for(second, batch_b["batch_id"], lambda b: b["status"] == "completed")
        assert second_entered.is_set()
    finally:
        release_first.set()
        first.stop()
        second.stop()


def test_submission_snapshot_returns_before_expensive_work_and_publishes_progress(tmp_path):
    entered, release = threading.Event(), threading.Event()
    current = {"version": 1}

    def expensive(_item):
        entered.set()
        assert release.wait(5)
        return dict(current)

    def execute(item, progress):
        progress({"current_action": "preflight", "actions": []})
        expensive(item)
        return {"task_status": "blocked", "reason": "review required"}

    queue = BatchQueue(tmp_path / "progress.sqlite3", expensive, execute,
                       snapshot=lambda _item: dict(current), execute_with_progress=True)
    batch = queue.submit("fast-submit", [{"case_id": "a"}, {"case_id": "b"}])
    assert not entered.is_set()
    queue.start()
    try:
        assert entered.wait(2)
        live = queue.get(batch["batch_id"])
        assert live["items"][0]["result"]["current_action"] == "preflight"
        assert live["counts"]["queued"] == 1
        # Status reads and cancellation must remain available during preflight.
        assert queue.cancel(batch["batch_id"])["counts"]["cancelled"] == 1
        release.set()
        assert wait_for(queue, batch["batch_id"], lambda b: b["status"] == "completed")["counts"]["blocked"] == 1
    finally:
        release.set()
        queue.stop()


def test_fast_snapshot_still_blocks_edits_while_queued(tmp_path):
    version = {"value": 1}
    calls = []
    queue = BatchQueue(tmp_path / "stale.sqlite3", lambda _: {},
                       lambda item: calls.append(item), snapshot=lambda _: dict(version))
    batch = queue.submit("stale", [{"case_id": "a"}])
    version["value"] = 2
    queue._run_one()
    assert queue.get(batch["batch_id"])["counts"]["blocked"] == 1
    assert not calls
