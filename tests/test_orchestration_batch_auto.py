"""Synthetic real-geometry checks for bounded automatic three-port saves."""

from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

from pipeline import cases, endplane_auto_candidate, endplanes, orchestration_batch as batch
from pipeline.errors import PipelineError
from pipeline.volume import Volume, save_volume


@pytest.fixture
def auto_case(tmp_path, monkeypatch):
    (tmp_path / "cases").mkdir()
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path / "cases")
    case_id = cases.create_case("synthetic", "<synthetic automatic endplanes>", synthetic=True)
    for name in ("anonymize", "qc", "segment"):
        cases.update_stage(case_id, name, "ok")
    root = cases.case_dir(case_id)
    shape = (28, 38, 16)
    mask = np.zeros(shape, dtype=np.uint8)
    mask[4:11, 3:17, 5:12] = 1
    mask[17:24, 3:17, 5:12] = 1
    mask[4:24, 17:22, 5:12] = 1
    mask[9:19, 22:35, 5:12] = 1
    volume = Volume(array=np.zeros(shape, dtype=np.float32),
                    spacing=(1.0, 1.0, 1.0), origin=(0.0, 0.0, 0.0),
                    axis_dirs=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))
    save_volume(case_id, volume)
    np.save(root / "segment" / "mask.npy", mask)
    record = {
        "params": {"ap_axis": 1, "hu_high": -300.0},
        "mask_version": 3, "need_human_confirm": False,
        "candidate_cleaning": {"status": "ok"},
        "review_flags": {"external_air_suspected": False},
        "human_review": {"status": "已确认", "method": "synthetic test fixture"},
        "external_air_validation": {
            "mask_version": 3, "body_hu_threshold": -300.0,
            "outside_voxels": 0, "candidate_outside_voxels": 0,
        },
    }
    (root / "segment" / "segmentation.json").write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(batch.activity_monitor, "record_event", lambda _event: 1)
    monkeypatch.setattr(batch.segmentation_learning, "compare_current_if_pending",
                        lambda *_args, **_kwargs: None)
    return SimpleNamespace(case_id=case_id, root=root, record=record, monkeypatch=monkeypatch,
                           item={"case_id": case_id, "actor_id": "synthetic-tester", "use_harness": False})


def _snapshot(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def _run(flow, authorize=lambda _actor, _case: True):
    return batch._run_auto_endplanes(flow.item, authorize, batch.preflight(flow.item, authorize))


@pytest.mark.parametrize("issue", ["unconfirmed", "stale_air_validation", "external_air"])
def test_auto_endplanes_never_relabels_an_unreviewed_mask(auto_case, issue):
    flow = auto_case
    if issue == "unconfirmed":
        flow.record["need_human_confirm"] = True
    elif issue == "stale_air_validation":
        flow.record["external_air_validation"]["mask_version"] = 2
    else:
        flow.record["review_flags"]["external_air_suspected"] = True
    (flow.root / "segment" / "segmentation.json").write_text(json.dumps(flow.record), encoding="utf-8")
    before = _snapshot(flow.root)
    result = batch.execute(flow.item, lambda _actor, _case: True)
    assert result["task_status"] == "blocked"
    assert [entry["action"] for entry in result["actions"]] == ["wait_for_review"]
    assert _snapshot(flow.root) == before


@pytest.mark.parametrize("existing", ["segment/endplanes.json", "surface/airway.stl",
                                      "cfd/system/controlDict", "report/report.md"])
def test_auto_endplanes_preserve_existing_manual_or_downstream_artifacts(auto_case, existing):
    flow = auto_case
    path = flow.root / existing
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}" if existing.endswith(".json") else "existing artifact", encoding="utf-8")
    before = _snapshot(flow.root)
    result = batch.execute(flow.item, lambda _actor, _case: True)
    assert result["task_status"] == "blocked"
    assert [entry["action"] for entry in result["actions"]] == ["wait_for_review"]
    assert _snapshot(flow.root) == before


def test_real_three_cap_preflight_saves_technical_provenance_and_advances(auto_case):
    flow = auto_case
    original_segment = (flow.root / "segment" / "segmentation.json").read_bytes()
    original_mask = (flow.root / "segment" / "mask.npy").read_bytes()
    advances = []

    def downstream(case_id, retry=False):
        advances.append(case_id)
        plan, version = batch.orchestration._plan(case_id, flow.root)
        assert version == 3 and plan["confirmation_source"] == "automated_technical_gate"
        return {"status": "blocked", "reason": "synthetic downstream boundary",
                "next_action": "review_geometry"}

    flow.monkeypatch.setattr(batch.orchestration, "advance_case", downstream)
    result = batch.execute(flow.item, lambda _actor, _case: True)
    assert result["reason"] == "synthetic downstream boundary"
    assert [entry["action"] for entry in result["actions"]] == [
        "propose_endplanes", "prepare_surface_cfd"]
    assert advances == [flow.case_id]
    saved = endplanes.load_plan(flow.case_id, "main_airway")
    assert saved["confirmation_source"] == "automated_technical_gate"
    assert saved["human_confirmed"] is False
    assert saved["generator_version"] == endplane_auto_candidate.BILATERAL_GENERATOR_VERSION
    assert saved["mesh_preflight"]["closed_caps"] is True
    assert all(saved["mesh_preflight"][key] == 1
               for key in ("left_inlet_loops", "right_inlet_loops", "outlet_loops"))
    assert (flow.root / "segment" / "segmentation.json").read_bytes() == original_segment
    assert (flow.root / "segment" / "mask.npy").read_bytes() == original_mask
    assert cases.get_case(flow.case_id)["stages"]["cfd_run"]["status"] == "pending"


def test_real_mesh_failure_stops_before_formal_save(auto_case):
    flow = auto_case
    before = _snapshot(flow.root)

    def reject_mesh(*_args, **_kwargs):
        raise PipelineError("blocked", "synthetic three-cap failure")

    flow.monkeypatch.setattr(endplane_auto_candidate.local_preview, "build_bilateral_local_mesh", reject_mesh)
    result = batch.execute(flow.item, lambda _actor, _case: True)
    assert result["task_status"] == "blocked"
    assert result["next_action"] == "review_endplanes"
    assert "真实网格预检" in result["reason"]
    assert [entry["action"] for entry in result["actions"]] == ["propose_endplanes"]
    assert _snapshot(flow.root) == before


@pytest.mark.parametrize("stage", ["surface", "cfd_prep", "cfd_run", "report"])
def test_auto_endplanes_never_restarts_an_existing_downstream_stage(auto_case, stage):
    flow = auto_case
    cases.update_stage(flow.case_id, stage, "failed")
    before = _snapshot(flow.root)
    result = batch.execute(flow.item, lambda _actor, _case: True)
    assert result["task_status"] == "blocked"
    assert [entry["action"] for entry in result["actions"]] == ["wait_for_review"]
    assert _snapshot(flow.root) == before


def test_final_save_preflight_failure_preserves_all_case_artifacts(auto_case):
    flow = auto_case
    before = _snapshot(flow.root)

    def reject_save_preflight(*_args, **_kwargs):
        raise PipelineError("blocked", "synthetic final three-cap rejection", ["review local outlet"])

    flow.monkeypatch.setattr(endplanes, "_preflight_bilateral_local", reject_save_preflight)
    result = batch.execute(flow.item, lambda _actor, _case: True)
    assert result["task_status"] == "blocked"
    assert result["next_action"] == "review_endplanes"
    assert result["reason"] == "synthetic final three-cap rejection"
    assert _snapshot(flow.root) == before


@pytest.mark.parametrize("change", ["access", "same_version_mask", "new_mask_version"])
def test_candidate_generation_rechecks_access_and_actual_input_bytes(auto_case, change):
    flow = auto_case
    access = {"allowed": True}
    original = endplane_auto_candidate.generate_bilateral_candidate

    def changed(*args):
        result = original(*args)
        if change == "access":
            access["allowed"] = False
        elif change == "same_version_mask":
            path = flow.root / "segment" / "mask.npy"
            mask = np.load(path)
            mask[0, 0, 0] = 1
            np.save(path, mask)
        else:
            flow.record["mask_version"] += 1
            flow.record["external_air_validation"]["mask_version"] += 1
            (flow.root / "segment" / "segmentation.json").write_text(
                json.dumps(flow.record), encoding="utf-8")
        return result

    flow.monkeypatch.setattr(endplane_auto_candidate, "generate_bilateral_candidate", changed)
    with pytest.raises(PipelineError, match="权限已变化|输入版本已变化"):
        _run(flow, lambda _actor, _case: access["allowed"])
    assert not (flow.root / "segment" / "endplanes.json").exists()
    assert cases.get_case(flow.case_id)["stages"]["surface"]["status"] == "pending"


def test_final_mesh_preflight_rechecks_access_before_atomic_save(auto_case):
    flow = auto_case
    before = _snapshot(flow.root)
    access = {"allowed": True}
    original = endplanes._preflight_bilateral_local

    def revoke_during_final_mesh(*args, **kwargs):
        result = original(*args, **kwargs)
        access["allowed"] = False
        return result

    flow.monkeypatch.setattr(endplanes, "_preflight_bilateral_local", revoke_during_final_mesh)
    with pytest.raises(PipelineError, match="权限已变化"):
        _run(flow, lambda _actor, _case: access["allowed"])
    assert _snapshot(flow.root) == before


@pytest.mark.parametrize("invalid", ["generator_version", "mask_version", "scope", "ambiguous", "single_inlet"])
def test_untrusted_or_ambiguous_candidate_cannot_reach_saver(auto_case, invalid):
    flow = auto_case
    before = _snapshot(flow.root)
    original = endplane_auto_candidate.generate_bilateral_candidate

    def invalid_candidate(*args):
        result = original(*args)
        if invalid in {"generator_version", "scope"}:
            result[invalid] = "untrusted"
        elif invalid == "mask_version":
            result[invalid] = 99
        elif invalid == "ambiguous":
            result["candidates"] *= 2
        else:
            result["candidates"][0]["plan"]["format"] = endplanes.LOCAL_INLET_FORMAT
        return result

    flow.monkeypatch.setattr(endplane_auto_candidate, "generate_bilateral_candidate", invalid_candidate)
    with pytest.raises(PipelineError, match="候选来源或版本无效|候选结构无效|不是双侧"):
        _run(flow)
    assert _snapshot(flow.root) == before
