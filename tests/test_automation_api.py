"""Automation contract: anonymous summaries, human gates and shared mutation lock."""

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

import server
from pipeline import automation, cases, config, scenarios


@pytest.fixture
def client_case(tmp_path, monkeypatch):
    root = tmp_path / "cases"
    monkeypatch.setattr(cases, "CASES_DIR", root)
    monkeypatch.setattr(config, "CASES_DIR", root)
    monkeypatch.setattr(server, "CASES_DIR", root)
    case_id = cases.create_case("synthetic", "<test>", synthetic=True)
    with TestClient(server.app) as client:
        yield client, case_id


def ready(case_id):
    root = cases.case_dir(case_id)
    seg = {"mask_version": 1, "need_human_confirm": False, "params": {"hu_high": -300},
           "external_air_validation": {"mask_version": 1, "outside_voxels": 0, "candidate_outside_voxels": 0, "body_hu_threshold": -300}}
    (root / "segment" / "segmentation.json").write_text(json.dumps(seg), encoding="utf-8")
    (root / "cfd" / "system").mkdir()
    (root / "cfd" / "system" / "controlDict").write_text("test", encoding="utf-8")
    (root / "cfd" / "case_meta.json").write_text("{}", encoding="utf-8")
    cases.update_stage(case_id, "cfd_prep", "ok")


def terminal(client, batch_id):
    for _ in range(300):
        result = client.get(f"/api/automation/batches/{batch_id}").json()
        if result["status"] == "completed":
            return result
        time.sleep(0.01)
    pytest.fail("batch did not complete")


def test_no_ai_review_and_no_private_data(client_case):
    client, case_id = client_case
    assert client.get("/api/automation/status").json()["llm_tokens"] is None
    listed = client.get("/api/automation/cases").json()
    assert listed["cases"][0]["case_id"] == case_id
    assert "source_path" not in json.dumps(listed)
    detail = client.get(f"/api/automation/cases/{case_id}").json()
    assert detail["baseline"]["ready"] is False
    response = client.post("/api/automation/batches", json={"request_key": "blocked", "items": [{"case_id": case_id}]})
    assert response.status_code == 202
    assert response.json()["items"][0]["status"] == "blocked"
    assert not (cases.case_dir(case_id) / "segment" / "segmentation.json").exists()


@pytest.mark.parametrize("headers", [{"Host": "public.example"}, {"X-Forwarded-For": "203.0.113.1"}, {"Origin": "https://evil.example"}])
def test_automation_rejects_public_proxy_and_cross_origin(client_case, headers):
    client, _ = client_case
    assert client.get("/api/automation/status", headers=headers).status_code == 403


def test_live_batch_blocks_web_mutations_and_returns_truthful_gate(client_case, monkeypatch):
    client, case_id = client_case
    ready(case_id)
    entered, release = threading.Event(), threading.Event()

    def run(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {"status": "ran"}

    monkeypatch.setattr(automation.openfoam_runner, "run_case", run)
    monkeypatch.setattr(automation.openfoam_runner, "check_result_gate", lambda *args, **kwargs: {"eligible": False, "numeric_ok": False, "reasons": ["质量守恒未通过"]})
    request = {"request_key": "real-lock", "items": [{"case_id": case_id}]}
    response = client.post("/api/automation/batches", json=request)
    batch_id = response.json()["batch_id"]
    try:
        assert entered.wait(5)
        assert client.delete(f"/api/cases/{case_id}").status_code == 409
        assert client.post(f"/api/cases/{case_id}/segment/confirm", json={}).status_code == 409
        assert client.post("/api/automation/batches", json=request).json()["batch_id"] == batch_id
    finally:
        release.set()
    done = terminal(client, batch_id)
    assert done["counts"]["solved"] == 1
    assert done["items"][0]["result"]["gate"]["eligible"] is False
    assert cases.get_case(case_id)["stages"]["cfd_prep"]["status"] == "ok"


def test_pending_case_change_blocks_solver(client_case, monkeypatch):
    client, case_id = client_case
    ready(case_id)
    queue = server.app.state.batch_queue
    queue.stop()
    batch = queue.submit("change", [automation.BatchItem(case_id=case_id).model_dump()])
    (cases.case_dir(case_id) / "cfd" / "system" / "controlDict").write_text("changed", encoding="utf-8")
    calls = []
    queue.execute = lambda item: calls.append(item)
    queue.start()
    done = terminal(client, batch["batch_id"])
    assert done["counts"]["blocked"] == 1
    assert "排队期间" in done["items"][0]["result"]["reason"]
    assert calls == []


def test_parallel_decomposition_count_does_not_stale_batch_snapshot(client_case):
    _, case_id = client_case
    ready(case_id)
    root = cases.case_dir(case_id)
    missing = automation.preflight(automation.BatchItem(case_id=case_id).model_dump())
    automation.openfoam_runner._prepare_decomposition(root / "cfd", 8)
    parallel = automation.preflight(automation.BatchItem(case_id=case_id).model_dump())
    assert parallel == missing
    path = root / "cfd" / "system" / "decomposeParDict"
    path.write_text(path.read_text(encoding="utf-8").replace("method scotch;", "method simple;"), encoding="utf-8")
    assert automation.preflight(automation.BatchItem(case_id=case_id).model_dump()) != missing


def test_scenario_contract_and_preflight_binding(client_case):
    client, case_id = client_case
    ready(case_id)
    from test_scenario_jobs import _box_stl
    root = cases.case_dir(case_id)
    (root / "surface" / "airway.stl").write_bytes(_box_stl())
    (root / "surface" / "surface.json").write_text('{"mask_version":1}', encoding="utf-8")
    cases.update_stage(case_id, "surface", "ok", data={"mask_version": 1})
    scenario = scenarios.create_scenario(case_id, _box_stl(), "mm")
    scenario_id = scenario["scenario_id"]
    data = {"request_key": "unreviewed", "items": [{"case_id": case_id, "scenario_id": scenario_id, "version": 1}]}
    assert client.post("/api/automation/batches", json=data).json()["counts"]["blocked"] == 1
    placed = scenarios.update_placement(case_id, scenario_id, {})
    scenarios.record_review(case_id, scenario_id, approved=True, reviewer_role="engineer")
    item = automation.BatchItem(case_id=case_id, scenario_id=scenario_id, version=placed["current_version"]).model_dump()
    assert "scenario" in automation.preflight(item)
    invalid = {"request_key": "invalid", "items": [{"case_id": case_id, "scenario_id": scenario_id}]}
    assert client.post("/api/automation/batches", json=invalid).status_code == 422


def test_postprocessing_and_scenario_guards_are_shared(client_case):
    _client, case_id = client_case
    ready(case_id)
    automation.cfd_postprocess_jobs.reserve_mutation(case_id)
    try:
        with pytest.raises(automation.PipelineError):
            automation.preflight(automation.BatchItem(case_id=case_id).model_dump())
    finally:
        automation.cfd_postprocess_jobs.release_mutation(case_id)
    automation.scenario_jobs.reserve_mutation(case_id)
    try:
        with pytest.raises(automation.PipelineError):
            automation.preflight(automation.BatchItem(case_id=case_id).model_dump())
    finally:
        automation.scenario_jobs.release_mutation(case_id)


def test_provider_usage_is_reported_only_when_available():
    from pipeline.ai_report import _provider_usage
    assert _provider_usage({}) == {"available": False, "source": "provider_response"}
    value = _provider_usage({"usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19,
                                        "prompt_tokens_details": {"cached_tokens": 2, "private": "secret"},
                                        "completion_tokens_details": {"reasoning_tokens": 3}, "private": "secret"}})
    assert value["total_tokens"] == 19
    assert value["completion_tokens_details"]["reasoning_tokens"] == 3
    assert "secret" not in json.dumps(value)
