"""旧体外空气清理记录不得让未验证的掩膜进入几何建模。"""

from __future__ import annotations

import json

import numpy as np
import pytest
from fastapi import HTTPException

from pipeline import cases, preview, surface
from pipeline.errors import PipelineError
from pipeline.volume import Volume, save_volume
from server import _require_confirmed_mask


@pytest.fixture
def airway_case(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = cases.create_case("synthetic", "<合成测试>", synthetic=True)
    root = cases.case_dir(case_id)
    shape = (12, 30, 20)
    save_volume(
        case_id,
        Volume(
            array=np.zeros(shape, dtype=np.float32),
            spacing=(1.0, 1.0, 1.0),
            origin=(0.0, 0.0, 0.0),
            axis_dirs=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
            meta={"shape": list(shape)},
        ),
    )
    mask = np.zeros(shape, dtype=np.uint8)
    mask[4:8, 8:21, 8:12] = 1
    np.save(root / "segment" / "mask.npy", mask)
    np.save(root / "segment" / "candidate_mask.npy", mask)
    record = {
        "params": {"ap_axis": 1, "hu_high": -300.0},
        "mask_version": 2,
        "candidate_mask_available": True,
        "candidate_cleaning": {"status": "ok", "body_hu_threshold": -500.0},
        "need_human_confirm": False,
        "review_flags": {"external_air_suspected": False},
        "human_review": {"status": "已确认"},
    }
    record_path = root / "segment" / "segmentation.json"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    return case_id, root, record, record_path


def _validation(version: int = 2, *, outside: int = 0, candidate_outside: int = 0) -> dict:
    return {
        "mask_version": version,
        "body_hu_threshold": -300.0,
        "outside_voxels": outside,
        "candidate_outside_voxels": candidate_outside,
    }


@pytest.mark.parametrize("scope", ["main_airway", "full_candidate"])
def test_surface_rejects_old_confirmed_mask_without_current_validation(airway_case, scope):
    case_id, root, record, record_path = airway_case
    with pytest.raises(PipelineError, match="缺少有效的体外空气验证") as caught:
        surface.run_surface(case_id, {"model_scope": scope})
    assert caught.value.status == "blocked"
    assert not (root / "surface" / "airway.stl").exists()

    record["external_air_validation"] = _validation(version=1)
    record_path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(PipelineError, match="缺少有效的体外空气验证"):
        surface.run_surface(case_id, {"model_scope": scope})

    record["external_air_validation"] = _validation(outside=1)
    record_path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(PipelineError, match="缺少有效的体外空气验证"):
        surface.run_surface(case_id, {"model_scope": scope})


def test_surface_accepts_current_zero_outside_validation(airway_case):
    case_id, root, record, record_path = airway_case
    record["external_air_validation"] = _validation()
    record_path.write_text(json.dumps(record), encoding="utf-8")

    result = surface.run_surface(case_id, {"model_scope": "main_airway"})

    assert result["faces"] > 0
    assert (root / "surface" / "airway.stl").exists()


def test_download_gate_requires_current_external_air_validation(airway_case):
    case_id, _root, record, record_path = airway_case
    with pytest.raises(HTTPException) as caught:
        _require_confirmed_mask(case_id)
    assert caught.value.status_code == 409

    record["external_air_validation"] = _validation()
    record_path.write_text(json.dumps(record), encoding="utf-8")
    assert _require_confirmed_mask(case_id) is None


def test_preview_flags_legacy_candidate_until_current_validation(airway_case):
    case_id, root, record, record_path = airway_case
    old = preview.viewer_metadata(case_id)
    assert old["mask_source"] == "segment/candidate_mask.npy"
    assert old["mask_external_air_suspected"] is True
    assert old["mask_edit_status"] == "体外空气待复核"
    assert old["mask_review"]["need_human_confirm"] is True
    assert old["can_undo_external_air"] is False

    record["external_air_validation"] = _validation()
    record_path.write_text(json.dumps(record), encoding="utf-8")
    checked = preview.viewer_metadata(case_id)
    assert checked["mask_external_air_suspected"] is False
    assert checked["mask_edit_status"] == "已确认"

    record["external_air_validation"]["mask_version"] = 1
    record_path.write_text(json.dumps(record), encoding="utf-8")
    stale = preview.viewer_metadata(case_id)
    assert stale["mask_external_air_suspected"] is True


def test_preview_requires_re_review_without_claiming_external_air_for_nonlegacy_mask(airway_case):
    case_id, _root, record, record_path = airway_case
    record["candidate_cleaning"]["body_hu_threshold"] = -300.0
    record_path.write_text(json.dumps(record), encoding="utf-8")

    unvalidated = preview.viewer_metadata(case_id)
    assert unvalidated["mask_external_air_suspected"] is False
    assert unvalidated["mask_edit_status"] == "需重新复核"
    assert unvalidated["mask_review"]["need_human_confirm"] is True

    record["external_air_validation"] = _validation()
    record_path.write_text(json.dumps(record), encoding="utf-8")
    validated = preview.viewer_metadata(case_id)
    assert validated["mask_external_air_suspected"] is False
    assert validated["mask_edit_status"] == "已确认"
    assert validated["mask_review"]["need_human_confirm"] is False


def test_preview_undo_available_only_for_matching_snapshot(airway_case):
    case_id, root, record, record_path = airway_case
    record["external_air_undo"] = {"after_version": 2}
    record_path.write_text(json.dumps(record), encoding="utf-8")
    assert preview.viewer_metadata(case_id)["can_undo_external_air"] is False

    np.save(root / "segment" / "mask_before_external_air.npy", np.load(root / "segment" / "mask.npy"))
    np.save(root / "segment" / "candidate_before_external_air.npy", np.load(root / "segment" / "candidate_mask.npy"))
    assert preview.viewer_metadata(case_id)["can_undo_external_air"] is True

    record["mask_version"] = 3
    record_path.write_text(json.dumps(record), encoding="utf-8")
    assert preview.viewer_metadata(case_id)["can_undo_external_air"] is False
