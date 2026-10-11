from __future__ import annotations

import json

import numpy as np
import pytest

from pipeline import cases, mask_review, preview
from pipeline.config import STAGES
from pipeline.errors import PipelineError
from pipeline.volume import Volume, save_volume


@pytest.fixture
def edit_case(tmp_path, monkeypatch):
    # 只使用临时合成体数据，不接触患者病例目录。
    monkeypatch.setattr("pipeline.config.CASES_DIR", tmp_path)
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = cases.create_case("synthetic", "<合成测试>", synthetic=True)
    array = np.full((4, 5, 6), -1000.0, dtype=np.float32)
    save_volume(
        case_id,
        Volume(
            array=array,
            spacing=(1.0, 1.2, 1.4),
            origin=(10.0, 20.0, 30.0),
            axis_dirs=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
            meta={"shape": list(array.shape)},
        ),
    )
    root = cases.case_dir(case_id)
    mask = np.zeros(array.shape, dtype=np.uint8)
    mask[1, 2, 3] = 1
    np.save(root / "segment" / "mask.npy", mask)
    (root / "segment" / "segmentation.json").write_text(
        json.dumps(
            {
                "params": {"ap_axis": 1},
                "mask_version": 1,
                "need_human_confirm": True,
                "review_flags": {},
                "metrics": {},
                "human_review": {"status": "未复核"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return case_id, mask.copy()


def test_edit_maps_all_views_and_preserves_candidate(edit_case):
    case_id, original = edit_case
    mask_review.edit_mask(case_id, "axial", 1, "paint", [{"x": 0, "y": 0}], 0)
    mask_review.edit_mask(case_id, "coronal", 2, "paint", [{"x": 5, "y": 3}], 0)
    mask_review.edit_mask(case_id, "sagittal", 4, "paint", [{"x": 2, "y": 3}], 0)
    edited = np.load(cases.case_dir(case_id) / "segment" / "mask.npy")
    assert edited[1, 0, 0] == 1
    assert edited[3, 2, 5] == 1
    assert edited[3, 2, 4] == 1
    # 候选副本只在首次编辑前保存，后续笔划不会覆盖它。
    np.testing.assert_array_equal(np.load(cases.case_dir(case_id) / "segment" / "mask_candidate.npy"), original)
    assert all(cases.get_case(case_id)["stages"][stage]["status"] == "pending" for stage in STAGES[4:])


def test_reset_restores_candidate_and_requires_review(edit_case):
    case_id, original = edit_case
    mask_review.edit_mask(case_id, "axial", 1, "paint", [{"x": 0, "y": 0}], 0)
    mask_review.edit_mask(case_id, "axial", 1, "erase", [{"x": 3, "y": 2}], 0)
    result = mask_review.reset_mask(case_id)
    restored = np.load(cases.case_dir(case_id) / "segment" / "mask.npy")
    np.testing.assert_array_equal(restored, original)
    assert result["status"] == "reset"
    assert result["need_human_confirm"] is True
    assert json.loads((cases.case_dir(case_id) / "segment" / "segmentation.json").read_text(encoding="utf-8"))["mask_source"] == "mask_candidate_reset"


@pytest.mark.parametrize("synthetic", [False, True])
def test_mask_confirmation_guides_real_ct_to_manual_endplanes(edit_case, synthetic):
    case_id, _original = edit_case
    root = cases.case_dir(case_id)
    case = cases.get_case(case_id)
    case["synthetic"] = synthetic
    cases._write_case(case_id, case)
    # 使用封闭组织包络内的合成气道，让确认经过真实的体外空气校验。
    hu = np.full((8, 24, 24), -1000.0, dtype=np.float32)
    hu[:, 3:21, 3:21] = 0.0
    mask = np.zeros(hu.shape, dtype=np.uint8)
    mask[2:6, 10:14, 10:14] = 1
    hu[mask.astype(bool)] = -1000.0
    save_volume(
        case_id,
        Volume(
            array=hu, spacing=(1.0, 1.0, 1.0), origin=(0.0, 0.0, 0.0),
            axis_dirs=((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
            meta={"shape": list(hu.shape)},
        ),
    )
    np.save(root / "segment" / "mask.npy", mask)

    result = mask_review.confirm_mask(case_id)
    segment = cases.get_case(case_id)["stages"]["segment"]
    assert result["status"] == "confirmed"
    assert segment["status"] == "ok"
    assert segment["data"]["need_human_confirm"] is False
    assert not (root / "segment" / "endplanes.json").exists()
    for message in (result["message"], segment["message"]):
        if synthetic:
            assert "可以生成 STL" in message
        else:
            assert "人工选择并保存鼻孔端和鼻咽端裁切面，再生成 STL" in message
            assert "AI" not in message


def test_edit_updates_full_candidate_but_preserves_original(edit_case):
    case_id, original = edit_case
    root = cases.case_dir(case_id)
    candidate = original.copy()
    candidate[2, 1, 1] = 1
    np.save(root / "segment" / "candidate_mask.npy", candidate)
    np.save(root / "segment" / "candidate_mask_original.npy", candidate)
    segmentation_path = root / "segment" / "segmentation.json"
    segmentation = json.loads(segmentation_path.read_text(encoding="utf-8"))
    segmentation["candidate_mask_available"] = True
    segmentation_path.write_text(json.dumps(segmentation), encoding="utf-8")

    result = mask_review.edit_mask(case_id, "axial", 1, "paint", [{"x": 0, "y": 0}], 0)

    edited_main = np.load(root / "segment" / "mask.npy")
    edited_candidate = np.load(root / "segment" / "candidate_mask.npy")
    original_candidate = np.load(root / "segment" / "candidate_mask_original.npy")
    assert edited_main[1, 0, 0] == 1
    assert edited_candidate[1, 0, 0] == 1
    np.testing.assert_array_equal(original_candidate, candidate)
    assert result["candidate_available"] is True


def test_edit_does_not_promote_unsafe_candidate_fallback(edit_case):
    case_id, _original = edit_case
    result = mask_review.edit_mask(case_id, "axial", 1, "paint", [{"x": 0, "y": 0}], 0)
    assert result["candidate_available"] is False


def test_manual_edit_cannot_bypass_pending_external_air_cleanup(edit_case):
    case_id, original = edit_case
    root = cases.case_dir(case_id)
    np.save(root / "segment" / "candidate_mask.npy", original)
    seg_path = root / "segment" / "segmentation.json"
    segmentation = json.loads(seg_path.read_text(encoding="utf-8"))
    segmentation["candidate_mask_available"] = True
    segmentation["candidate_cleaning"] = {"status": "pending_manual"}
    seg_path.write_text(json.dumps(segmentation), encoding="utf-8")

    mask_review.edit_mask(case_id, "axial", 1, "paint", [{"x": 2, "y": 2}], 0)

    edited = json.loads(seg_path.read_text(encoding="utf-8"))
    assert edited["candidate_cleaning"]["status"] == "pending_manual"
    with pytest.raises(PipelineError, match="尚未执行体外空气清理"):
        mask_review.confirm_mask(case_id)


def test_viewer_uses_candidate_only_when_marked_available(edit_case):
    case_id, original = edit_case
    root = cases.case_dir(case_id)
    candidate = original.copy()
    candidate[2, 1, 1] = 1
    np.save(root / "segment" / "candidate_mask.npy", candidate)
    segmentation_path = root / "segment" / "segmentation.json"
    segmentation = json.loads(segmentation_path.read_text(encoding="utf-8"))
    segmentation["candidate_mask_available"] = True
    segmentation_path.write_text(json.dumps(segmentation), encoding="utf-8")

    available = preview.viewer_metadata(case_id)
    assert available["mask_source"] == "segment/candidate_mask.npy"
    assert available["mask_candidate_available"] is True

    segmentation["candidate_mask_available"] = False
    segmentation_path.write_text(json.dumps(segmentation), encoding="utf-8")
    fallback = preview.viewer_metadata(case_id)
    assert fallback["mask_source"] == "segment/mask.npy"
    assert fallback["mask_candidate_available"] is False


def test_edit_rejects_out_of_bounds_and_oversized_request(edit_case):
    case_id, _ = edit_case
    with pytest.raises(PipelineError, match="超出当前切片范围"):
        mask_review.edit_mask(case_id, "axial", 0, "paint", [{"x": 99, "y": 0}], 0)
    with pytest.raises(PipelineError, match="不能超过"):
        mask_review.edit_mask(case_id, "axial", 0, "paint", [{"x": 0, "y": 0}] * (mask_review.MAX_EDIT_POINTS + 1), 0)


def test_export_npy_is_downloadable(edit_case):
    case_id, _ = edit_case
    path = mask_review.export_mask(case_id, "npy", "main_airway")
    assert path.suffix == ".npy"
    np.testing.assert_array_equal(np.load(path), np.load(cases.case_dir(case_id) / "segment" / "mask.npy"))


def test_auto_remove_external_air_keeps_body_enclosed_airway():
    """外部候选与鼻腔内部候选相连时，不能用简单 clear-border 清空主体。"""
    shape = (20, 40, 40)
    hu = np.full(shape, -1000.0, dtype=np.float32)
    # 模拟头部轮廓：只在 z=3..16 存在软组织/骨性外壳，内部保留空气腔。
    hu[3:17, 5:35, 5:35] = 0.0
    mask = np.zeros(shape, dtype=np.uint8)
    mask[3:17, 0:35, 10:30] = 1  # 前后轴（axis 1）开口，且含有体外空气
    cleaned, detail = mask_review._remove_external_air_array(
        mask,
        hu,
        (1.0, 1.0, 1.0),
        ap_axis=1,
        closure_mm=1.0,
    )
    assert cleaned.sum() > 0
    assert detail["removed_voxels"] > 0
    assert detail["retained_voxels"] == int(cleaned.sum())
    assert detail["boundary_by_axis_after"][0] is False
    assert detail["boundary_by_axis_after"][2] is False


def test_auto_remove_external_air_removes_partial_volume_skin_rim(monkeypatch):
    """贴着皮肤的 -400 HU 薄环不应同时被当成体外空气和身体包络。"""
    monkeypatch.setattr(mask_review, "EXTERNAL_AIR_MIN_RETAINED_VOXELS", 1)
    hu = np.full((8, 64, 64), -1000.0, dtype=np.float32)
    hu[:, 9:55, 9:55] = -400.0
    hu[:, 10:54, 10:54] = 0.0
    hu[:, 25:40, 25:40] = -1000.0
    mask = np.zeros_like(hu, dtype=np.uint8)
    mask[:, 9, 9:55] = 1
    mask[:, 54, 9:55] = 1
    mask[:, 9:55, 9] = 1
    mask[:, 9:55, 54] = 1
    mask[:, 25:40, 25:40] = 1

    cleaned, detail = mask_review._remove_external_air_array(
        mask, hu, (1.0, 1.0, 1.0), ap_axis=1, closure_mm=1.0
    )

    assert cleaned[:, 9, 20].sum() == 0
    assert cleaned[:, 30, 30].sum() == 6  # 非前后轴最外两层按既有规则裁掉
    assert detail["removed_voxels"] > 0
    assert detail["body_hu_threshold"] == -300.0


def test_external_air_cleanup_can_be_undone_without_losing_current_mask(tmp_path, monkeypatch):
    monkeypatch.setattr("pipeline.config.CASES_DIR", tmp_path)
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    monkeypatch.setattr(mask_review, "EXTERNAL_AIR_MIN_RETAINED_VOXELS", 1)
    case_id = cases.create_case("synthetic", "<合成测试>", synthetic=True)
    root = cases.case_dir(case_id)
    hu = np.full((8, 64, 64), -1000.0, dtype=np.float32)
    hu[:, 9:55, 9:55] = -400.0
    hu[:, 10:54, 10:54] = 0.0
    hu[:, 25:40, 25:40] = -1000.0
    save_volume(
        case_id,
        Volume(
            array=hu,
            spacing=(1.0, 1.0, 1.0),
            origin=(0.0, 0.0, 0.0),
            axis_dirs=((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
            meta={"shape": list(hu.shape)},
        ),
    )
    original = np.zeros_like(hu, dtype=np.uint8)
    original[1:7, 9, 9:55] = 1
    original[1:7, 54, 9:55] = 1
    original[1:7, 9:55, 9] = 1
    original[1:7, 9:55, 54] = 1
    original[1:7, 25:40, 25:40] = 1
    original[3, 29, 29] = 0  # 模拟此前手动擦除的一个体素
    np.save(root / "segment" / "mask.npy", original)
    np.save(root / "segment" / "candidate_mask.npy", original)
    (root / "segment" / "segmentation.json").write_text(
        json.dumps({"params": {"ap_axis": 1, "hu_high": -300}, "mask_version": 1,
                    "candidate_mask_available": True, "need_human_confirm": True,
                    "human_review": {"status": "待复核"}}, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(PipelineError, match="头部外缘"):
        mask_review.confirm_mask(case_id)
    result = mask_review.auto_remove_external_air(case_id)
    assert result["candidate_removed_voxels"] > 0
    cleaned = np.load(root / "segment" / "candidate_mask.npy")
    assert cleaned[:, 9, 20].sum() == 0
    assert cleaned[3, 29, 29] == 0
    assert mask_review.confirm_mask(case_id)["status"] == "confirmed"
    version = json.loads((root / "segment" / "segmentation.json").read_text(encoding="utf-8"))["mask_version"]
    assert mask_review.auto_remove_external_air(case_id)["status"] == "unchanged"
    assert json.loads((root / "segment" / "segmentation.json").read_text(encoding="utf-8"))["mask_version"] == version
    assert mask_review.undo_external_air(case_id)["need_human_confirm"] is True
    np.testing.assert_array_equal(np.load(root / "segment" / "mask.npy"), original)
    np.testing.assert_array_equal(np.load(root / "segment" / "candidate_mask.npy"), original)


def test_auto_remove_external_air_blocks_when_no_internal_region_exists():
    shape = (12, 24, 24)
    hu = np.full(shape, -1000.0, dtype=np.float32)
    mask = np.ones(shape, dtype=np.uint8)
    with pytest.raises(PipelineError) as caught:
        mask_review._remove_external_air_array(mask, hu, (1.0, 1.0, 1.0), ap_axis=1)
    assert caught.value.status == "blocked"
    assert "未修改" in caught.value.reason
