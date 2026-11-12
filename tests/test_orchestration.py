"""Synthetic, filesystem-isolated checks for conservative case progression."""

from __future__ import annotations

import json
from contextlib import nullcontext
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import server
from pipeline import cases, orchestration
from pipeline.errors import PipelineError


@pytest.fixture
def flow(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    monkeypatch.setattr(orchestration.endplanes, "_case_save_lock", lambda _id: nullcontext())
    monkeypatch.setattr(orchestration.cfd_postprocess_jobs, "reserve_mutation", lambda _id: None)
    monkeypatch.setattr(orchestration.cfd_postprocess_jobs, "release_mutation", lambda _id: None)
    monkeypatch.setattr(orchestration.scenario_jobs, "reserve_mutation", lambda _id: None)
    monkeypatch.setattr(orchestration.scenario_jobs, "release_mutation", lambda _id: None)
    monkeypatch.setattr(orchestration.automation, "require_confirmed_mask", lambda _id: None)

    case_id = cases.create_case("local_path", "<synthetic test>", synthetic=False)
    for name in ("anonymize", "qc", "segment"):
        cases.update_stage(case_id, name, "ok")
    root = cases.case_dir(case_id)
    (root / "segment" / "segmentation.json").write_text(
        json.dumps({"mask_version": 2}), encoding="utf-8",
    )
    plan = {"mask_version": 2, "anterior_index": 10, "posterior_index": 40}
    monkeypatch.setattr(orchestration.endplanes, "load_plan", lambda _id, _scope: plan)
    calls = {"surface": 0, "qc": 0, "prep": 0}

    def build_surface(_id, params):
        assert params == {"model_scope": "main_airway"}
        calls["surface"] += 1
        (root / "surface" / "airway.stl").write_bytes(b"synthetic STL")
        (root / "surface" / "surface.json").write_text(
            json.dumps({"mask_version": 2, "params": {"model_scope": "main_airway"},
                        "endplanes": {**plan, "source": orchestration._ENDPLANE_SOURCES[plan.get("format")]}}),
            encoding="utf-8",
        )

    def check_geometry(_id):
        calls["qc"] += 1
        (root / "surface" / "geom_qc.json").write_text(
            json.dumps({"verdict": "pass"}), encoding="utf-8",
        )
        cases.update_stage(case_id, "surface", "ok")
        return {"verdict": "pass"}

    def prepare(_id):
        calls["prep"] += 1
        (root / "cfd" / "case_meta.json").write_text("{}", encoding="utf-8")
        (root / "cfd" / "system").mkdir(parents=True, exist_ok=True)
        (root / "cfd" / "system" / "controlDict").write_text("synthetic", encoding="utf-8")
        cases.update_stage(case_id, "cfd_prep", "ok")

    monkeypatch.setattr(orchestration.surface, "run_surface", build_surface)
    monkeypatch.setattr(orchestration.geom_qc, "run_geom_qc", check_geometry)
    monkeypatch.setattr(orchestration.cfd_prep, "prepare_case", prepare)
    return SimpleNamespace(case_id=case_id, root=root, plan=plan, calls=calls,
                           build_surface=build_surface, check_geometry=check_geometry,
                           prepare=prepare, monkeypatch=monkeypatch)


def test_prepares_once_and_never_calls_solver(flow):
    first = orchestration.advance_case(flow.case_id)
    second = orchestration.advance_case(flow.case_id)
    assert first == {"case_id": flow.case_id, "status": "prepared",
                     "completed_steps": ["surface", "cfd_prep"],
                     "next_action": "run_authoritative_cfd_preflight"}
    assert second["status"] == "prepared" and second["completed_steps"] == []
    assert flow.calls == {"surface": 1, "qc": 1, "prep": 1}
    assert cases.get_case(flow.case_id)["stages"]["cfd_run"]["status"] == "pending"


def test_mask_gate_blocks_before_any_artifact_mutation(flow):
    def reject(_id):
        raise PipelineError("blocked", "掩膜尚未人工确认", ["先复核掩膜"])

    flow.monkeypatch.setattr(orchestration.automation, "require_confirmed_mask", reject)
    result = orchestration.advance_case(flow.case_id)
    assert result["status"] == "blocked" and result["reason"] == "掩膜尚未人工确认"
    assert flow.calls == {"surface": 0, "qc": 0, "prep": 0}
    assert cases.get_case(flow.case_id)["stages"]["surface"]["status"] == "pending"


@pytest.mark.parametrize("plan", [None, {"mask_version": 1, "anterior_index": 10, "posterior_index": 40}])
def test_missing_or_stale_endplanes_block_without_running(flow, plan):
    flow.monkeypatch.setattr(orchestration.endplanes, "load_plan", lambda _id, _scope: plan)
    result = orchestration.advance_case(flow.case_id)
    assert result["status"] == "blocked" and flow.calls["surface"] == 0


def test_surface_failure_needs_explicit_retry(flow):
    def fail(_id, _params):
        flow.calls["surface"] += 1
        raise PipelineError("failed", "合成表面生成失败", ["检查合成输入"])

    flow.monkeypatch.setattr(orchestration.surface, "run_surface", fail)
    failed = orchestration.advance_case(flow.case_id)
    paused = orchestration.advance_case(flow.case_id)
    assert failed["status"] == "failed" and paused["status"] == "blocked"
    assert cases.get_case(flow.case_id)["stages"]["surface"]["status"] == "failed"
    assert flow.calls == {"surface": 1, "qc": 0, "prep": 0}

    flow.monkeypatch.setattr(orchestration.surface, "run_surface", flow.build_surface)
    retried = orchestration.advance_case(flow.case_id, retry=True)
    assert retried["status"] == "prepared" and retried["completed_steps"] == ["surface", "cfd_prep"]


def test_cfd_prep_failure_retries_without_regenerating_surface(flow):
    def fail(_id):
        flow.calls["prep"] += 1
        raise PipelineError("blocked", "合成入口不合格", ["修正端面"])

    flow.monkeypatch.setattr(orchestration.cfd_prep, "prepare_case", fail)
    failed = orchestration.advance_case(flow.case_id)
    assert failed["status"] == "blocked" and failed["completed_steps"] == ["surface"]
    assert cases.get_case(flow.case_id)["stages"]["cfd_prep"]["status"] == "blocked"
    assert orchestration.advance_case(flow.case_id)["status"] == "blocked"

    flow.monkeypatch.setattr(orchestration.cfd_prep, "prepare_case", flow.prepare)
    retried = orchestration.advance_case(flow.case_id, retry=True)
    assert retried["status"] == "prepared" and retried["completed_steps"] == ["cfd_prep"]
    assert flow.calls == {"surface": 1, "qc": 1, "prep": 2}


def test_local_inlet_never_reaches_bilateral_cfd_prep(flow):
    flow.plan.update(format=orchestration.endplanes.LOCAL_INLET_FORMAT,
                     anterior_plane={"synthetic": True}, posterior_plane={"synthetic": True},
                     anterior_seed_lps_mm=[0, 0, 0])
    result = orchestration.advance_case(flow.case_id)
    assert result["status"] == "blocked" and result["completed_steps"] == ["surface"]
    assert "局部单支" in result["reason"]
    assert flow.calls == {"surface": 1, "qc": 1, "prep": 0}


def test_confirmed_three_opening_plan_can_reach_cfd_preparation(flow):
    flow.plan.clear()
    flow.plan.update(
        format=orchestration.endplanes.BILATERAL_LOCAL_FORMAT,
        scope="main_airway", mask_version=2, complete=True,
        left_inlet_plane={"synthetic": "left"},
        right_inlet_plane={"synthetic": "right"},
        posterior_plane={"synthetic": "outlet"},
        left_inlet_seed_lps_mm=[-1, 0, 0],
        right_inlet_seed_lps_mm=[1, 0, 0],
        posterior_seed_lps_mm=[0, 1, 0],
    )
    result = orchestration.advance_case(flow.case_id)
    assert result["status"] == "prepared"
    assert result["completed_steps"] == ["surface", "cfd_prep"]
    assert flow.calls == {"surface": 1, "qc": 1, "prep": 1}


def test_legacy_global_oblique_real_case_is_not_automatically_prepared(flow):
    flow.plan.update(format="oblique_planes_v1", complete=True,
                     anterior_plane={"synthetic": True}, posterior_plane={"synthetic": True})
    result = orchestration.advance_case(flow.case_id)
    assert result["status"] == "blocked"
    assert "旧版贯穿全模型端面" in result["reason"]
    assert flow.calls == {"surface": 0, "qc": 0, "prep": 0}


def test_existing_solver_or_running_stage_blocks_without_overwrite(flow):
    cases.update_stage(flow.case_id, "cfd_run", "warn")
    assert orchestration.advance_case(flow.case_id)["status"] == "blocked"
    assert flow.calls == {"surface": 0, "qc": 0, "prep": 0}
    cases.update_stage(flow.case_id, "cfd_run", "pending")
    cases.update_stage(flow.case_id, "surface", "running")
    assert orchestration.advance_case(flow.case_id)["status"] == "blocked"
    assert flow.calls == {"surface": 0, "qc": 0, "prep": 0}


def test_changed_plan_rejects_successful_old_surface(flow):
    assert orchestration.advance_case(flow.case_id)["status"] == "prepared"
    flow.plan["anterior_index"] = 12
    result = orchestration.advance_case(flow.case_id)
    assert result["status"] == "blocked" and "表面模型" in result["reason"]
    assert flow.calls == {"surface": 1, "qc": 1, "prep": 1}


def test_rebuilt_surface_cannot_reuse_old_cfd_prep(flow):
    assert orchestration.advance_case(flow.case_id)["status"] == "prepared"
    cases.update_stage(flow.case_id, "surface", "pending")
    result = orchestration.advance_case(flow.case_id)
    assert result["status"] == "blocked" and "旧 CFD 算例" in result["reason"]
    assert flow.calls == {"surface": 1, "qc": 1, "prep": 1}


def test_http_advance_rejects_invalid_body_and_repeated_call_is_noop(flow, monkeypatch):
    monkeypatch.setenv("WINDCATCHER_MULTIUSER", "0")
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    url = f"/api/cases/{flow.case_id}/orchestration/advance"
    assert client.post(url, json={"retry": "yes"}).status_code == 422
    assert client.post(url, json={"extra": True}).status_code == 422
    first = client.post(url, json={})
    second = client.post(url, json={})
    assert first.status_code == 200, first.text
    assert first.json()["completed_steps"] == ["surface", "cfd_prep"]
    assert second.status_code == 200, second.text
    assert second.json()["completed_steps"] == []
    assert flow.calls == {"surface": 1, "qc": 1, "prep": 1}


def test_http_advance_reports_mask_gate_without_mutation(flow, monkeypatch):
    monkeypatch.setenv("WINDCATCHER_MULTIUSER", "0")
    def reject(_case_id):
        raise PipelineError("blocked", "掩膜尚未人工确认", ["先复核掩膜"])
    flow.monkeypatch.setattr(orchestration.automation, "require_confirmed_mask", reject)
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    response = client.post(f"/api/cases/{flow.case_id}/orchestration/advance", json={})
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "掩膜尚未人工确认"
    assert flow.calls == {"surface": 0, "qc": 0, "prep": 0}


def test_http_advance_reserves_case_against_legacy_surface_route(flow, monkeypatch):
    monkeypatch.setenv("WINDCATCHER_MULTIUSER", "0")
    active = set()
    entered = Event()
    release = Event()
    response = []

    def reserve(case_id):
        if case_id in active:
            raise PipelineError("blocked", "病例处理已占用", ["等待后重试"])
        active.add(case_id)

    def unreserve(case_id):
        active.discard(case_id)

    def slow_surface(case_id, params):
        entered.set()
        assert release.wait(5)
        return flow.build_surface(case_id, params)

    monkeypatch.setattr(orchestration.cfd_postprocess_jobs, "reserve_mutation", reserve)
    monkeypatch.setattr(orchestration.cfd_postprocess_jobs, "release_mutation", unreserve)
    monkeypatch.setattr(orchestration.surface, "run_surface", slow_surface)
    url = f"/api/cases/{flow.case_id}/orchestration/advance"

    def run_advance():
        response.append(TestClient(server.app, base_url="http://127.0.0.1:8765").post(url, json={}))

    worker = Thread(target=run_advance)
    worker.start()
    try:
        assert entered.wait(5)
        legacy = TestClient(server.app, base_url="http://127.0.0.1:8765").post(
            f"/api/cases/{flow.case_id}/surface", json={"params": {}})
        assert legacy.status_code == 409, legacy.text
        assert "病例处理已占用" in legacy.text
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(response) == 1 and response[0].status_code == 200, response
    assert flow.calls == {"surface": 1, "qc": 1, "prep": 1}
    assert not active
