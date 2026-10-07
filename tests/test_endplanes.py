from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from pipeline import cases, endplanes, segmentation, surface
from pipeline.errors import PipelineError
from pipeline.volume import Volume, save_volume
from fastapi import HTTPException
from server import EndplanesBody, api_get_endplanes, api_save_endplanes


@pytest.fixture
def confirmed_case(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = cases.create_case("local_path", "<合成 CT 测试>", synthetic=False)
    root = cases.case_dir(case_id)
    shape = (8, 30, 12)
    save_volume(
        case_id,
        Volume(
            array=np.zeros(shape, dtype=np.float32),
            spacing=(1.0, 1.0, 1.0),
            origin=(0.0, 0.0, 0.0),
            axis_dirs=((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
            meta={"shape": list(shape)},
        ),
    )
    mask = np.zeros(shape, dtype=np.uint8)
    mask[2:6, 5:25, 4:8] = 1
    mask[2, 4, 5] = 1     # 旧自动端面只有一个体素，不可用于 CFD
    mask[2, 25, 5] = 1
    np.save(root / "segment" / "mask.npy", mask)
    np.save(root / "segment" / "candidate_mask.npy", mask)
    record = {
        "params": {"ap_axis": 1, "hu_high": -300.0},
        "mask_version": 3,
        "candidate_mask_available": True,
        "candidate_cleaning": {"status": "ok"},
        "need_human_confirm": False,
        "review_flags": {"external_air_suspected": False},
        "human_review": {"status": "已确认"},
        "external_air_validation": {
            "mask_version": 3,
            "body_hu_threshold": -300.0,
            "outside_voxels": 0,
            "candidate_outside_voxels": 0,
        },
    }
    (root / "segment" / "segmentation.json").write_text(json.dumps(record), encoding="utf-8")
    return case_id, root, record, mask


def test_get_profile_is_read_only_and_uses_ct_anterior_direction(confirmed_case):
    case_id, root, _record, _mask = confirmed_case
    before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))

    response = endplanes.get_endplanes(case_id, "main_airway")

    assert response["scope"] == "main_airway"
    assert response["axis"] == 1
    assert response["view"] == "coronal"
    assert response["count"] == len(response["areas_mm2"]) == 30
    assert response["anterior_sign"] == 1
    assert response["mask_version"] == 3
    assert response["areas_mm2"][4] == 1.0
    assert response["areas_mm2"][20] == 16.0
    assert response["saved"] is None
    assert response["can_save"] is True
    assert sorted(path.relative_to(root).as_posix() for path in root.rglob("*")) == before


@pytest.mark.parametrize(
    "front,back,expected",
    [(8, 20, "前方"), (20, 20, "重合"), (26, 8, "太小")],
)
def test_save_rejects_wrong_order_same_plane_and_tiny_old_endpoint(confirmed_case, front, back, expected):
    case_id, root, _record, _mask = confirmed_case
    with pytest.raises(PipelineError, match=expected):
        endplanes.save_endplanes(case_id, "main_airway", front, back, 3)
    assert not (root / "segment" / "endplanes.json").exists()


def test_technical_area_cutoff_accepts_exactly_one_square_mm(confirmed_case):
    case_id, _root, _record, _mask = confirmed_case
    plan = endplanes.save_endplanes(case_id, "main_airway", 25, 8, 3)
    assert plan["anterior_area_mm2"] == 1.0


def test_save_rejects_index_out_of_range_and_disconnected_main_endpoint(confirmed_case):
    case_id, root, _record, mask = confirmed_case
    with pytest.raises(PipelineError, match="超出 CT 范围"):
        endplanes.save_endplanes(case_id, "main_airway", 30, 8, 3)
    with pytest.raises(PipelineError, match="必须是整数"):
        endplanes.save_endplanes(case_id, "main_airway", True, 8, 3)

    mask[2:4, 27, 4:6] = 1  # 面积足够，但与后方主气道断开。
    np.save(root / "segment" / "mask.npy", mask)
    with pytest.raises(PipelineError, match="没有同时落在"):
        endplanes.save_endplanes(case_id, "main_airway", 27, 8, 3)


def test_save_rejects_stale_and_unconfirmed_mask(confirmed_case):
    case_id, root, record, _mask = confirmed_case
    with pytest.raises(PipelineError, match="版本已变化"):
        endplanes.save_endplanes(case_id, "main_airway", 20, 8, 2)
    record["need_human_confirm"] = True
    (root / "segment" / "segmentation.json").write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(PipelineError, match="尚未清理并人工确认"):
        endplanes.save_endplanes(case_id, "main_airway", 20, 8, 3)


def test_legacy_record_without_mask_version_is_readable_but_cannot_save(confirmed_case):
    case_id, root, record, _mask = confirmed_case
    record.pop("mask_version")
    (root / "segment" / "segmentation.json").write_text(json.dumps(record), encoding="utf-8")

    profile = endplanes.get_endplanes(case_id)
    assert len(profile["areas_mm2"]) == profile["count"]
    assert profile["mask_version"] is None
    assert profile["migration_required"] is True
    assert profile["can_save"] is False
    with pytest.raises(PipelineError, match="版本已变化"):
        endplanes.save_endplanes(case_id, "main_airway", 20, 8, 0)


def test_failure_to_invalidate_keeps_previous_plan_and_geometry(confirmed_case, monkeypatch):
    case_id, root, _record, _mask = confirmed_case
    existing = {"main_airway": {"mask_version": 3, "anterior_index": 19, "posterior_index": 9}}
    plan_path = root / "segment" / "endplanes.json"
    plan_path.write_text(json.dumps(existing), encoding="utf-8")
    (root / "surface" / "airway.stl").write_bytes(b"previous")

    def fail_invalidation(*_args):
        raise OSError("synthetic locked STL")

    monkeypatch.setattr(endplanes, "invalidate_downstream", fail_invalidation)
    with pytest.raises(PipelineError, match="没有保存"):
        endplanes.save_endplanes(case_id, "main_airway", 20, 8, 3)
    assert json.loads(plan_path.read_text(encoding="utf-8")) == existing
    assert (root / "surface" / "airway.stl").read_bytes() == b"previous"


def test_save_invalidates_old_geometry_and_surface_uses_clipped_copy(confirmed_case):
    case_id, root, _record, mask = confirmed_case
    cases.update_stage(case_id, "surface", "ok", artifacts=["surface/airway.stl"])
    (root / "surface" / "airway.stl").write_bytes(b"old")

    with pytest.raises(PipelineError, match="还没有人工确认"):
        surface.run_surface(case_id, {"model_scope": "main_airway"})
    plan = endplanes.save_endplanes(case_id, "main_airway", 20, 8, 3)
    assert plan["anterior_index"] == 20
    assert plan["posterior_index"] == 8
    assert not (root / "surface" / "airway.stl").exists()
    assert cases.get_case(case_id)["stages"]["surface"]["status"] == "pending"

    result = surface.run_surface(case_id, {"model_scope": "main_airway"})
    effective = np.load(root / "surface" / "effective_mask.npy")
    assert result["faces"] > 0
    assert result["endplanes"]["source"] == "user_confirmed_ct_planes"
    assert result["mask_version"] == 3
    assert not effective[:, :8, :].any()
    assert not effective[:, 21:, :].any()
    assert effective[:, 8:21, :].any()
    np.testing.assert_array_equal(np.load(root / "segment" / "mask.npy"), mask)
    assert json.loads((root / "surface" / "surface.json").read_text(encoding="utf-8"))["endplanes"] == result["endplanes"]


def test_saved_plan_is_per_scope_and_stale_plan_blocks_surface(confirmed_case):
    case_id, root, record, _mask = confirmed_case
    endplanes.save_endplanes(case_id, "main_airway", 20, 8, 3)
    endplanes.save_endplanes(case_id, "full_candidate", 20, 8, 3)
    plans = json.loads((root / "segment" / "endplanes.json").read_text(encoding="utf-8"))
    assert set(plans) == {"main_airway", "full_candidate"}

    record["mask_version"] = 4
    record["external_air_validation"]["mask_version"] = 4
    (root / "segment" / "segmentation.json").write_text(json.dumps(record), encoding="utf-8")
    assert endplanes.get_endplanes(case_id, "main_airway")["saved"]["stale"] is True
    with pytest.raises(PipelineError, match="不匹配"):
        surface.run_surface(case_id, {"model_scope": "main_airway"})


def test_no_plan_is_still_allowed_for_synthetic_legacy_path(confirmed_case):
    case_id, root, _record, _mask = confirmed_case
    case = cases.get_case(case_id)
    case["synthetic"] = True
    cases._write_case(case_id, case)

    result = surface.run_surface(case_id, {"model_scope": "main_airway"})

    assert result["endplanes"] is None
    assert (root / "surface" / "effective_mask.npy").exists()


def test_switching_from_saved_planes_to_synthetic_legacy_refreshes_effective_mask(confirmed_case):
    case_id, root, _record, _mask = confirmed_case
    case = cases.get_case(case_id)
    case["synthetic"] = True
    cases._write_case(case_id, case)
    endplanes.save_endplanes(case_id, "main_airway", 20, 8, 3)
    surface.run_surface(case_id, {"model_scope": "main_airway"})
    clipped = np.load(root / "surface" / "effective_mask.npy")
    (root / "segment" / "endplanes.json").unlink()

    surface.run_surface(case_id, {"model_scope": "main_airway"})
    restored = np.load(root / "surface" / "effective_mask.npy")
    assert restored.sum() > clipped.sum()


def test_positive_and_negative_ct_axis_directions(confirmed_case):
    case_id, root, _record, _mask = confirmed_case
    meta_path = root / "volume" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["axis_dirs"][1] = [0.0, 1.0, 0.0]
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    profile = endplanes.get_endplanes(case_id)
    assert profile["anterior_sign"] == -1
    with pytest.raises(PipelineError, match="前方"):
        endplanes.save_endplanes(case_id, "main_airway", 20, 8, 3)
    plan = endplanes.save_endplanes(case_id, "main_airway", 8, 20, 3)
    assert plan["anterior_index"] == 8
    assert plan["posterior_index"] == 20


def test_api_get_and_post_share_zero_based_contract(confirmed_case):
    case_id, _root, _record, _mask = confirmed_case
    profile = asyncio.run(api_get_endplanes(case_id, "main_airway"))
    assert profile["view"] == "coronal"
    assert len(profile["areas_mm2"]) == profile["count"]
    saved = asyncio.run(api_save_endplanes(
        case_id,
        EndplanesBody(scope="main_airway", anterior_index=20, posterior_index=8, mask_version=3),
    ))
    assert saved["anterior_index"] == 20
    assert asyncio.run(api_get_endplanes(case_id, "main_airway"))["saved"]["stale"] is False
    with pytest.raises(HTTPException) as caught:
        asyncio.run(api_get_endplanes(case_id, "invalid"))
    assert caught.value.status_code == 400


def test_new_segmentation_removes_previous_endplane_choices(confirmed_case, monkeypatch):
    case_id, root, _record, mask = confirmed_case
    endplanes.save_endplanes(case_id, "main_airway", 20, 8, 3)

    def synthetic_segmentation(_volume, _params):
        return {
            "mask": mask.copy(),
            "candidate_mask": mask.copy(),
            "candidate_mask_available": True,
            "candidate_cleaning": {"status": "pending_manual"},
            "params": {"ap_axis": 1, "hu_low": -1024.0, "hu_high": -300.0},
            "metrics": {"volume_ml": 1.0, "ap_length_mm": 20.0},
            "candidate_metrics": {"components_total": 1},
            "review_flags": {"external_air_suspected": False, "mask_boundary_by_axis": [False] * 3},
            "candidate_review_flags": {"external_air_suspected": False, "mask_boundary_by_axis": [False] * 3},
        }

    monkeypatch.setattr(segmentation, "segment_airway", synthetic_segmentation)
    result = segmentation.run_segment(case_id)
    assert result["mask_version"] == 4
    assert not (root / "segment" / "endplanes.json").exists()
    assert endplanes.get_endplanes(case_id)["saved"] is None
