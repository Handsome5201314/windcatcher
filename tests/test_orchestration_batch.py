"""Synthetic checks for the durable, permission-rechecked Harness batch adapter."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import server
from pipeline import batch_jobs, cases, orchestration_batch
from pipeline.access_policy import Policy, classify
from pipeline.config import STAGES
from pipeline.errors import PipelineError


@pytest.fixture
def flow(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = cases.create_case("synthetic", "<test>", synthetic=True)
    for name in ("anonymize", "qc"):
        cases.update_stage(case_id, name, "ok")

    def projection(current_id):
        assert current_id == case_id
        stages = {name: cases.get_case(case_id)["stages"][name]["status"] for name in STAGES}
        return {"schema_version": 1, "case_id": case_id, "stages": stages,
                "baseline_ready": stages["cfd_prep"] == "ok",
                "numeric_ok": True if stages["cfd_run"] == "ok" else None,
                "eligible": True if stages["cfd_run"] == "ok" else None,
                "state_sha256": json.dumps(stages, sort_keys=True)}

    monkeypatch.setattr(orchestration_batch.harness_projection, "project_case_state", projection)
    monkeypatch.setattr(orchestration_batch.endplanes, "load_plan", lambda _id, _scope: None)
    monkeypatch.setattr(orchestration_batch.activity_monitor, "record_event", lambda _event: 1)
    return SimpleNamespace(case_id=case_id, root=cases.case_dir(case_id),
                           item={"case_id": case_id, "actor_id": "tester", "use_harness": False},
                           monkeypatch=monkeypatch)


def test_candidate_generation_stops_at_real_mask_review(flow):
    calls = []

    def segment(case_id):
        calls.append(case_id)
        (flow.root / "segment" / "segmentation.json").write_text(
            json.dumps({"mask_version": 1, "need_human_confirm": True}), encoding="utf-8")
        cases.update_stage(case_id, "segment", "warn")
        return {"mask_version": 1}

    flow.monkeypatch.setattr(orchestration_batch.segmentation, "run_segment", segment)
    result = orchestration_batch.execute(flow.item, lambda _actor, _case: True)
    assert result["task_status"] == "blocked"
    assert result["actions"][0]["action"] == "segment_candidate"
    assert result["actions"][-1]["action"] == "wait_for_review"
    assert "掩膜" in result["reason"] and calls == [flow.case_id]
    assert cases.get_case(flow.case_id)["stages"]["segment"]["status"] == "warn"
    assert result["llm_usage"]["status"] == "unknown"


def test_single_branch_plan_never_becomes_bilateral_cfd(flow):
    cases.update_stage(flow.case_id, "segment", "ok")
    (flow.root / "segment" / "segmentation.json").write_text(
        json.dumps({"mask_version": 1}), encoding="utf-8")
    flow.monkeypatch.setattr(orchestration_batch.endplanes, "load_plan",
                             lambda _id, _scope: {"format": "oblique_local_inlet_v2", "mask_version": 1})
    result = orchestration_batch.execute(flow.item, lambda _actor, _case: True)
    assert result["task_status"] == "blocked"
    assert "双侧 CFD" in result["reason"]
    assert [event["action"] for event in result["actions"]] == ["wait_for_review"]
    assert cases.get_case(flow.case_id)["stages"]["surface"]["status"] == "pending"


def test_harness_selection_rejects_case_changed_while_model_runs(flow):
    flow.item["use_harness"] = True
    flow.monkeypatch.setattr(orchestration_batch.harness_advice, "actions_enabled", lambda: True)
    called = []

    def select(projection, allowed, actor):
        assert "segment_candidate" in allowed and actor == "tester"
        cases.update_stage(flow.case_id, "qc", "warn")
        return {"action": "segment_candidate", "state_sha256": projection["state_sha256"],
                "input_sha256": "a" * 64, "metering": {"status": "reported", "total_tokens": 12}}

    flow.monkeypatch.setattr(orchestration_batch.harness_advice, "request_action", select)
    flow.monkeypatch.setattr(orchestration_batch.segmentation, "run_segment", lambda _id: called.append(_id))
    result = orchestration_batch.execute(flow.item, lambda _actor, _case: True)
    assert result["task_status"] == "blocked" and "版本已变化" in result["reason"]
    assert result["actions"][0]["result"] == "rejected_stale" and not called
    assert result["llm_usage"]["total_tokens"] == 12


def test_harness_selection_rechecks_revoked_edit_grant(flow):
    flow.item["use_harness"] = True
    flow.monkeypatch.setattr(orchestration_batch.harness_advice, "actions_enabled", lambda: True)
    access = {"allowed": True}

    def select(projection, _allowed, _actor):
        access["allowed"] = False
        return {"action": "segment_candidate", "state_sha256": projection["state_sha256"],
                "input_sha256": "a" * 64, "metering": {"status": "reported", "total_tokens": 12}}

    flow.monkeypatch.setattr(orchestration_batch.harness_advice, "request_action", select)
    result = orchestration_batch.execute(flow.item, lambda _actor, _case: access["allowed"])
    assert result["task_status"] == "blocked"
    assert result["actions"][0]["result"] == "rejected_after_selection"
    assert cases.get_case(flow.case_id)["stages"]["segment"]["status"] == "pending"


def test_harness_selects_bounded_action_and_records_reported_usage(flow):
    flow.item["use_harness"] = True
    flow.monkeypatch.setattr(orchestration_batch.harness_advice, "actions_enabled", lambda: True)
    selections = []

    def select(projection, allowed, actor):
        selections.append((tuple(allowed), actor))
        return {"action": allowed[0], "state_sha256": projection["state_sha256"],
                "input_sha256": "a" * 64,
                "metering": {"status": "reported", "total_tokens": 12}}

    def segment(case_id):
        (flow.root / "segment" / "segmentation.json").write_text(
            json.dumps({"mask_version": 1, "need_human_confirm": True}), encoding="utf-8")
        cases.update_stage(case_id, "segment", "warn")
        return {"mask_version": 1}

    flow.monkeypatch.setattr(orchestration_batch.harness_advice, "request_action", select)
    flow.monkeypatch.setattr(orchestration_batch.segmentation, "run_segment", segment)
    result = orchestration_batch.execute(flow.item, lambda _actor, _case: True)
    assert result["task_status"] == "blocked" and "掩膜" in result["reason"]
    assert selections == [(("segment_candidate", "wait_for_review"), "tester")]
    assert result["llm_usage"] == {"status": "reported", "total_tokens": 12, "cost_usd": None}
    assert [event["source"] for event in result["actions"]] == ["deepseek-harness", "server_rules"]


def test_review_only_never_calls_harness_or_requires_its_configuration(flow):
    cases.update_stage(flow.case_id, "segment", "ok")
    flow.item["use_harness"] = True
    flow.monkeypatch.setattr(orchestration_batch.harness_advice, "actions_enabled",
                             lambda: pytest.fail("Harness config must not be consulted"))
    flow.monkeypatch.setattr(orchestration_batch.harness_advice, "request_action",
                             lambda *_args: pytest.fail("review-only state must not consume tokens"))
    result = orchestration_batch.execute(flow.item, lambda _actor, _case: True)
    assert result["task_status"] == "blocked"
    assert "三局部端面" in result["reason"]
    assert result["llm_usage"]["status"] == "unknown"
    assert result["actions"][0]["source"] == "server_rules"


@pytest.mark.parametrize("eligible,expected", [(True, "solved"), (False, "blocked")])
def test_ready_bilateral_case_uses_authoritative_preflight_and_gate(flow, eligible, expected):
    cases.update_stage(flow.case_id, "segment", "ok")
    (flow.root / "segment" / "segmentation.json").write_text(
        json.dumps({"mask_version": 2}), encoding="utf-8")
    flow.monkeypatch.setattr(orchestration_batch, "_has_current_bilateral_plan", lambda _id: True)
    original_projection = orchestration_batch.harness_projection.project_case_state

    def projection(case_id):
        current = original_projection(case_id)
        if current["stages"]["cfd_run"] == "ok":
            current["eligible"] = eligible
        return current

    flow.monkeypatch.setattr(orchestration_batch.harness_projection, "project_case_state", projection)
    calls = {"surface": 0, "preflight": 0, "solver": 0}

    def advance(case_id, retry=False):
        calls["surface"] += 1
        cases.update_stage(case_id, "surface", "ok")
        cases.update_stage(case_id, "cfd_prep", "ok")
        return {"status": "prepared", "completed_steps": ["surface", "cfd_prep"]}

    def solver_preflight(item):
        calls["preflight"] += 1
        assert item["case_id"] == flow.case_id
        return {"input_sha256": "stable"}

    def solve(item):
        calls["solver"] += 1
        cases.update_stage(item["case_id"], "cfd_run", "ok")
        return {"task_status": "solved"}

    flow.monkeypatch.setattr(orchestration_batch.orchestration, "advance_case", advance)
    flow.monkeypatch.setattr(orchestration_batch.automation, "preflight", solver_preflight)
    flow.monkeypatch.setattr(orchestration_batch.automation, "execute", solve)
    flow.monkeypatch.setattr(orchestration_batch.openfoam_runner, "check_result_gate",
                             lambda _id: {"numeric_ok": True, "eligible": eligible, "reasons": []})

    def build_report(case_id):
        assert eligible is True
        output = flow.root / "report"
        output.mkdir(exist_ok=True)
        (output / "report.md").write_text("synthetic report", encoding="utf-8")
        (output / "report.json").write_text("{}", encoding="utf-8")
        cases.update_stage(case_id, "report", "ok")
        return {"cfd_eligible": True, "markdown_path": str(output / "report.md"),
                "json_path": str(output / "report.json")}

    flow.monkeypatch.setattr(orchestration_batch.report, "build_report", build_report)
    result = orchestration_batch.execute(flow.item, lambda _actor, _case: True)
    assert result["task_status"] == expected
    assert calls == {"surface": 1, "preflight": 2, "solver": 1}
    expected_actions = ["prepare_surface_cfd", "submit_ready_cfd"]
    if eligible:
        expected_actions.append("generate_report")
    expected_actions.append("inspect_numeric_gate")
    assert [entry["action"] for entry in result["actions"]] == expected_actions


def test_failed_surface_preparation_persists_failed_status(flow):
    cases.update_stage(flow.case_id, "segment", "ok")
    flow.monkeypatch.setattr(orchestration_batch, "_has_current_bilateral_plan", lambda _id: True)
    flow.monkeypatch.setattr(orchestration_batch.orchestration, "advance_case",
                             lambda *_args, **_kwargs: {"status": "failed", "reason": "synthetic failure"})
    result = orchestration_batch.execute(flow.item, lambda _actor, _case: True)
    assert result["task_status"] == "failed"
    assert result["reason"] == "synthetic failure"
    assert result["actions"][0]["result"] == "failed"


def test_new_public_routes_are_explicit_and_fail_closed():
    batch_id = "batch-" + "a" * 16
    assert classify("/api/orchestration/batches", "POST") == Policy("orchestration_submit")
    assert classify(f"/api/orchestration/batches/{batch_id}", "GET") == Policy(
        "orchestration_batch", resource_id=batch_id, capability="view")
    assert classify(f"/api/orchestration/batches/{batch_id}/cancel", "POST") == Policy(
        "orchestration_batch", resource_id=batch_id, capability="edit")
    assert classify("/api/orchestration/batches/not-a-batch", "GET") == Policy("deny")


def test_service_lease_rejects_second_worker_before_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("WINDCATCHER_AUTOMATION_DIR", str(tmp_path / "automation"))
    entered = []

    class IdleQueue:
        def health(self):
            return {"worker_alive": False}

    @asynccontextmanager
    async def fake_start(application):
        entered.append(application)
        application.state.batch_queue = IdleQueue()
        application.state.orchestration_queue = IdleQueue()
        yield

    monkeypatch.setattr(server, "_app_lifespan_impl", fake_start)
    first = SimpleNamespace(state=SimpleNamespace())
    second = SimpleNamespace(state=SimpleNamespace())

    async def run():
        async with server.app_lifespan(first):
            with pytest.raises(PipelineError, match="已有工作台服务进程"):
                async with server.app_lifespan(second):
                    pass
        async with server.app_lifespan(second):
            pass

    asyncio.run(run())
    assert entered == [first, second]


def test_batch_http_checks_all_grants_and_hides_actor_id(flow, monkeypatch):
    monkeypatch.setenv("WINDCATCHER_MULTIUSER", "1")
    allowed = {flow.case_id}
    calls = []

    class Queue:
        def health(self):
            return {"worker_alive": True}

        def submit(self, key, items):
            calls.append((key, items))
            return {"batch_id": "batch-" + "b" * 16, "status": "queued",
                    "counts": {"queued": 1}, "items": [{"item_id": 1, "input": items[0],
                    "status": "queued", "result": None, "updated_at": "now"}],
                    "poll_after_seconds": 60}

    monkeypatch.setattr(server.app.state, "orchestration_queue", Queue(), raising=False)
    monkeypatch.setattr(server.app.state, "orchestration_authorize",
                        lambda actor, case: actor == "human-1" and case in allowed, raising=False)
    request = SimpleNamespace(state=SimpleNamespace(principal={"id": "human-1", "kind": "human"}))
    body = server.OrchestrationBatchBody(request_key="first", case_ids=[flow.case_id])
    result = asyncio.run(server.api_orchestration_submit(body, request))
    assert result["items"][0]["input"] == {"case_id": flow.case_id, "use_harness": True,
                                            "retry_failed": False}
    assert calls[0][0] == "human-1:first" and calls[0][1][0]["actor_id"] == "human-1"
    allowed.clear()
    with pytest.raises(Exception) as error:
        asyncio.run(server.api_orchestration_submit(
            server.OrchestrationBatchBody(request_key="second", case_ids=[flow.case_id]), request))
    assert getattr(error.value, "status_code", None) == 404


def test_persisted_batch_is_idempotent_and_new_key_resumes_after_review(flow, tmp_path):
    def segment(case_id):
        (flow.root / "segment" / "segmentation.json").write_text(
            json.dumps({"mask_version": 1, "need_human_confirm": True}), encoding="utf-8")
        cases.update_stage(case_id, "segment", "warn")
        return {"mask_version": 1}

    flow.monkeypatch.setattr(orchestration_batch.segmentation, "run_segment", segment)
    queue = batch_jobs.BatchQueue(
        tmp_path / "orchestration.sqlite3",
        lambda item: orchestration_batch.preflight(item, lambda _actor, _case: True),
        lambda item: orchestration_batch.execute(item, lambda _actor, _case: True),
    )
    first = queue.submit("tester:first", [flow.item])
    assert first["items"][0]["status"] == "queued"
    assert queue._run_one() is True
    finished = queue.get(first["batch_id"])
    assert finished["items"][0]["status"] == "blocked"
    assert len(finished["items"][0]["result"]["actions"]) == 2
    assert queue.submit("tester:first", [flow.item])["batch_id"] == first["batch_id"]

    # Simulate only the operator's separate review; the batch does not write it.
    cases.update_stage(flow.case_id, "segment", "ok")
    second = queue.submit("tester:after-review", [flow.item])
    assert second["batch_id"] != first["batch_id"]
    assert queue._run_one() is True
    resumed = queue.get(second["batch_id"])["items"][0]
    assert resumed["status"] == "blocked"
    assert "三局部端面" in resumed["result"]["reason"]
    assert [item["action"] for item in resumed["result"]["actions"]] == ["wait_for_review"]


def test_submission_snapshot_detects_volume_changes_without_loading_or_projecting(flow, monkeypatch):
    monkeypatch.setattr(orchestration_batch.harness_projection, "project_case_state",
                        lambda _: pytest.fail("submission must not evaluate CFD quality"))
    before = orchestration_batch.submission_snapshot(flow.item, lambda *_: True)
    (flow.root / "volume").mkdir(exist_ok=True)
    (flow.root / "volume" / "hu.npy").write_bytes(b"changed")
    after = orchestration_batch.submission_snapshot(flow.item, lambda *_: True)
    assert before != after


def test_execute_exposes_preflight_before_waiting_for_review(flow):
    cases.update_stage(flow.case_id, "segment", "warn")
    progress = []
    result = orchestration_batch.execute(flow.item, lambda *_: True, progress.append)
    assert progress[0]["current_action"] == "preflight"
    assert result["task_status"] == "blocked"
