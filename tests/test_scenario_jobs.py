"""鼻塞 CFD 后台任务的持久状态和并发边界。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from pipeline import cases, config, plug_models, scenario_jobs, scenarios
from pipeline.errors import PipelineError


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
def scenario_case(tmp_path, monkeypatch):
    root = tmp_path / "cases"
    monkeypatch.setattr(config, "CASES_DIR", root)
    monkeypatch.setattr(cases, "CASES_DIR", root)
    scenario_jobs._ACTIVE.clear()
    scenario_jobs._MUTATIONS.clear()
    case_id = cases.create_case("synthetic", "<合成测试>", synthetic=True)
    case_root = cases.case_dir(case_id)
    (case_root / "segment" / "segmentation.json").write_text(json.dumps({"mask_version": 1}), encoding="utf-8")
    (case_root / "surface" / "airway.stl").write_bytes(_box_stl())
    (case_root / "surface" / "surface.json").write_text(json.dumps({"mask_version": 1}), encoding="utf-8")
    cases.update_stage(case_id, "surface", "ok", data={"mask_version": 1})
    created = scenarios.create_scenario(case_id, _box_stl(), "mm")
    scenarios.update_placement(case_id, created["scenario_id"], {})
    scenarios.record_review(case_id, created["scenario_id"], approved=True, reviewer_role="engineer")
    return case_id, created["scenario_id"]


def test_recover_interrupted_marks_persisted_task_failed(scenario_case):
    case_id, scenario_id = scenario_case
    root = scenarios.scenario_dir(case_id, scenario_id) / "versions" / "v0002"
    task_path = root / "scenario_task.json"
    task_path.write_text(json.dumps({"status": "running", "job_id": "old"}), encoding="utf-8")

    assert scenario_jobs.recover_interrupted() == 1
    task = json.loads(task_path.read_text(encoding="utf-8"))
    assert task["status"] == "failed"
    assert task["stage"] == "interrupted"


def test_running_task_blocks_mutation_and_finishes_in_background(scenario_case, monkeypatch):
    case_id, scenario_id = scenario_case
    started = threading.Event()
    release = threading.Event()

    def fake_run(case, scenario, **kwargs):
        assert (case, scenario) == (case_id, scenario_id)
        started.set()
        assert release.wait(3)
        return {"run": {"status": "ran"}, "comparison": {"status": "checked"}}

    monkeypatch.setattr("pipeline.scenario_cfd.run_scenario", fake_run)
    task = scenario_jobs.start(case_id, scenario_id, version=2)
    assert task["status"] == "queued"
    assert started.wait(3)
    with pytest.raises(PipelineError, match="正在执行鼻塞 CFD"):
        scenario_jobs.reserve_mutation(case_id)
    release.set()
    deadline = time.time() + 3
    while time.time() < deadline:
        current = scenario_jobs.get_task(case_id, scenario_id, version=2)
        if current and current.get("status") == "solved":
            break
        time.sleep(0.02)
    assert scenario_jobs.get_task(case_id, scenario_id, version=2)["status"] == "solved"
    with sqlite3.connect(cases.case_dir(case_id) / "research.sqlite3") as connection:
        task_document = connection.execute(
            "SELECT document_json FROM scenario_artifacts WHERE scenario_id=? AND version=2 AND kind='task'",
            (scenario_id,),
        ).fetchone()[0]
        assert json.loads(task_document)["status"] == "solved"
        assert connection.execute(
            "SELECT COUNT(*) FROM scenario_events WHERE scenario_id=? AND action='scenario_cfd_state_updated'",
            (scenario_id,),
        ).fetchone()[0] >= 2
    scenario_jobs._ACTIVE.clear()
    scenario_jobs._MUTATIONS.clear()


@pytest.mark.parametrize("outcome", ["solved", "failed", "blocked", "exception"])
def test_terminal_task_is_not_visible_before_ledger_commit(scenario_case, monkeypatch, outcome):
    case_id, scenario_id = scenario_case
    persisting = threading.Event()
    release = threading.Event()
    reader_started = threading.Event()
    reader_finished = threading.Event()
    worker_finished = threading.Event()
    observed = {}
    original_set_state = scenario_jobs._set_scenario_state
    original_worker = scenario_jobs._worker

    def fake_run(*args, **kwargs):
        if outcome == "blocked":
            raise PipelineError("blocked", "合成阻断", [])
        if outcome == "exception":
            raise RuntimeError("synthetic solver failure")
        return {"run": {"status": "ran" if outcome == "solved" else "failed"}}

    def delayed_set_state(*args, **kwargs):
        persisting.set()
        assert release.wait(3)
        original_set_state(*args, **kwargs)

    def tracked_worker(*args, **kwargs):
        try:
            original_worker(*args, **kwargs)
        finally:
            worker_finished.set()

    def read_task_and_ledger():
        reader_started.set()
        try:
            observed["task"] = scenario_jobs.get_task(case_id, scenario_id, version=2)
            with sqlite3.connect(cases.case_dir(case_id) / "research.sqlite3") as connection:
                document = connection.execute(
                    "SELECT document_json FROM scenario_artifacts WHERE scenario_id=? AND version=2 AND kind='task'",
                    (scenario_id,),
                ).fetchone()[0]
                observed["ledger"] = json.loads(document)
        except Exception as exc:
            observed["error"] = exc
        finally:
            reader_finished.set()

    monkeypatch.setattr("pipeline.scenario_cfd.run_scenario", fake_run)
    monkeypatch.setattr(scenario_jobs, "_set_scenario_state", delayed_set_state)
    monkeypatch.setattr(scenario_jobs, "_worker", tracked_worker)
    scenario_jobs.start(case_id, scenario_id, version=2)
    reader = threading.Thread(target=read_task_and_ledger)
    try:
        assert persisting.wait(3)
        reader.start()
        assert reader_started.wait(3)
        # Give a reader the chance to observe an incorrectly published terminal
        # state while the ledger write is deliberately held at the boundary.
        reader_finished.wait(0.2)
    finally:
        release.set()
        assert worker_finished.wait(3)
        if reader.ident is not None:
            reader.join(3)

    assert reader_finished.is_set()
    assert "error" not in observed
    expected = "failed" if outcome == "exception" else outcome
    assert observed["task"]["status"] == expected
    assert observed["ledger"]["status"] == expected
