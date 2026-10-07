from __future__ import annotations

import json

import numpy as np
import pytest

from pipeline import cases, mask_review, preview, segmentation, slicer_review, surface
from pipeline.cases import case_dir, create_case
from pipeline.errors import PipelineError
from pipeline.volume import Volume, save_volume


def test_run_segment_persists_full_candidate_without_replacing_main_mask(tmp_path, monkeypatch):
    monkeypatch.setattr("pipeline.config.CASES_DIR", tmp_path)
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    monkeypatch.setattr("pipeline.mask_review.EXTERNAL_AIR_MIN_RETAINED_VOXELS", 1)
    case_id = create_case("synthetic", "<合成测试>", synthetic=True)

    array = np.full((8, 30, 20), -2000.0, dtype=np.float32)
    array[:, 1:29, 1:19] = 0.0
    array[2:6, 5:25, 8:12] = -1000.0
    array[3:5, 10:14, 5:7] = -1000.0
    save_volume(
        case_id,
        Volume(
            array=array,
            spacing=(1.0, 1.0, 1.0),
            origin=(0.0, 0.0, 0.0),
            axis_dirs=((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
            meta={"shape": list(array.shape), "axis_dirs": [[1, 0, 0], [0, -1, 0], [0, 0, 1]]},
        ),
    )

    payload = segmentation.run_segment(
        case_id,
        {
            "min_component_mm3": 1.0,
            "min_ap_coverage": 0.5,
            "fill_enclosed_cavities": False,
        },
    )

    main = np.load(case_dir(case_id) / "segment" / "mask.npy")
    candidate = np.load(case_dir(case_id) / "segment" / "candidate_mask.npy")
    assert candidate.sum() > main.sum()
    assert payload["candidate_mask_available"] is True
    assert payload["candidate_metrics"]["components_total"] == 2
    assert payload["candidate_metrics"]["voxels"] == int(candidate.sum())
    assert payload["candidate_components"]
    assert payload["main_component_selection"]["status"] == "pending_manual"
    assert payload["candidate_cleaning"]["status"] == "pending_manual"
    assert payload["review_flags"]["external_air_suspected"] is False
    assert preview.viewer_metadata(case_id)["mask_source"] == "segment/candidate_mask.npy"
    assert preview.viewer_metadata(case_id)["mask_external_air_cleanup_pending"] is True
    with pytest.raises(PipelineError, match="尚未执行体外空气清理"):
        mask_review.confirm_mask(case_id)
    with pytest.raises(PipelineError, match="尚未清理体外空气"):
        mask_review.export_mask(case_id, "npy", "full_candidate")
    with pytest.raises(PipelineError, match="缺少有效的体外空气验证"):
        surface.run_surface(case_id)

    # No exterior voxels need removal here, but the user's explicit check must
    # still complete the pending step and leave human confirmation outstanding.
    result = mask_review.auto_remove_external_air(case_id)
    assert result["candidate_removed_voxels"] == 0
    cleaned_record = json.loads((case_dir(case_id) / "segment" / "segmentation.json").read_text(encoding="utf-8"))
    assert cleaned_record["candidate_cleaning"]["status"] == "ok"
    assert cleaned_record["need_human_confirm"] is True
    assert preview.viewer_metadata(case_id)["mask_review"]["candidate_external_air_cleanup"]["removed_voxels"] == 0
    with monkeypatch.context() as patch:
        def fail_invalidation(*_args):
            raise OSError("synthetic locked downstream file")

        patch.setattr(mask_review, "invalidate_downstream", fail_invalidation)
        with pytest.raises(OSError, match="synthetic locked downstream"):
            mask_review.confirm_mask(case_id)
    still_unconfirmed = json.loads((case_dir(case_id) / "segment" / "segmentation.json").read_text(encoding="utf-8"))
    assert still_unconfirmed["need_human_confirm"] is True
    assert "external_air_validation" not in still_unconfirmed


def test_run_segment_keeps_raw_candidate_if_later_cleanup_is_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr("pipeline.config.CASES_DIR", tmp_path)
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = create_case("synthetic", "<合成测试>", synthetic=True)

    array = np.full((8, 30, 20), -2000.0, dtype=np.float32)
    array[2:6, 5:25, 8:12] = -1000.0
    array[3:5, 10:14, 2:4] = -1000.0
    save_volume(
        case_id,
        Volume(
            array=array,
            spacing=(1.0, 1.0, 1.0),
            origin=(0.0, 0.0, 0.0),
            axis_dirs=((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
            meta={"shape": list(array.shape), "axis_dirs": [[1, 0, 0], [0, -1, 0], [0, 0, 1]]},
        ),
    )

    payload = segmentation.run_segment(
        case_id,
        {
            "min_component_mm3": 1.0,
            "min_ap_coverage": 0.5,
            "fill_enclosed_cavities": False,
        },
    )

    main = np.load(case_dir(case_id) / "segment" / "mask.npy")
    candidate = np.load(case_dir(case_id) / "segment" / "candidate_mask.npy")
    assert payload["candidate_mask_available"] is True
    assert payload["candidate_cleaning"]["status"] == "pending_manual"
    assert candidate.sum() > main.sum()
    np.testing.assert_array_equal(np.load(case_dir(case_id) / "segment" / "candidate_mask_original.npy"), candidate)
    before_record = (case_dir(case_id) / "segment" / "segmentation.json").read_bytes()
    with pytest.raises(PipelineError, match="未修改当前掩膜"):
        mask_review.auto_remove_external_air(case_id)
    np.testing.assert_array_equal(np.load(case_dir(case_id) / "segment" / "mask.npy"), main)
    np.testing.assert_array_equal(np.load(case_dir(case_id) / "segment" / "candidate_mask.npy"), candidate)
    assert (case_dir(case_id) / "segment" / "segmentation.json").read_bytes() == before_record


def test_cleanup_reselects_main_from_clean_candidate_and_undo_restores_pending(tmp_path, monkeypatch):
    monkeypatch.setattr("pipeline.config.CASES_DIR", tmp_path)
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    monkeypatch.setattr(mask_review, "EXTERNAL_AIR_MIN_RETAINED_VOXELS", 1)
    case_id = create_case("synthetic", "<合成测试>", synthetic=True)
    hu = np.full((16, 48, 48), -1000.0, dtype=np.float32)
    hu[2:14, 8:40, 8:40] = 0.0
    hu[4:12, 10:38, 20:28] = -1000.0
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
    root = case_dir(case_id)
    segmentation.run_segment(case_id, {"min_component_mm3": 1.0, "min_ap_coverage": 0.1})
    raw_main = np.load(root / "segment" / "mask.npy")
    raw_candidate = np.load(root / "segment" / "candidate_mask.npy")
    assert raw_main.sum() > 10_000  # Outside air was the raw largest component.
    assert raw_candidate[0, 0, 0] == 1

    result = mask_review.auto_remove_external_air(case_id)
    cleaned_main = np.load(root / "segment" / "mask.npy")
    cleaned_candidate = np.load(root / "segment" / "candidate_mask.npy")
    record = json.loads((root / "segment" / "segmentation.json").read_text(encoding="utf-8"))
    assert result["candidate_removed_voxels"] > 10_000
    assert cleaned_main.sum() < raw_main.sum()
    assert cleaned_main[6, 20, 23] == 1
    assert cleaned_main[0, 0, 0] == 0
    assert cleaned_candidate[0, 0, 0] == 0
    assert record["main_component_selection"]["status"] == "ok"
    assert record["candidate_cleaning"]["status"] == "ok"
    assert record["need_human_confirm"] is True
    np.testing.assert_array_equal(np.load(root / "segment" / "candidate_mask_original.npy"), raw_candidate)

    mask_review.undo_external_air(case_id)
    np.testing.assert_array_equal(np.load(root / "segment" / "mask.npy"), raw_main)
    np.testing.assert_array_equal(np.load(root / "segment" / "candidate_mask.npy"), raw_candidate)
    undone = json.loads((root / "segment" / "segmentation.json").read_text(encoding="utf-8"))
    assert undone["candidate_cleaning"]["status"] == "pending_manual"
    assert "candidate_external_air_cleanup" not in undone
    with pytest.raises(PipelineError, match="尚未执行体外空气清理"):
        mask_review.confirm_mask(case_id)

    mask_review.auto_remove_external_air(case_id)
    mask_review.reset_mask(case_id)
    np.testing.assert_array_equal(np.load(root / "segment" / "candidate_mask.npy"), raw_candidate)
    reset = json.loads((root / "segment" / "segmentation.json").read_text(encoding="utf-8"))
    assert reset["candidate_cleaning"]["status"] == "pending_manual"
    assert "candidate_external_air_cleanup" not in reset


def test_new_segmentation_discards_previous_reset_and_undo_snapshots(tmp_path, monkeypatch):
    monkeypatch.setattr("pipeline.config.CASES_DIR", tmp_path)
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = create_case("synthetic", "<合成测试>", synthetic=True)
    hu = np.full((8, 30, 20), -2000.0, dtype=np.float32)
    hu[:, 1:29, 1:19] = 0.0
    hu[2:6, 5:25, 8:12] = -1000.0
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
    root = case_dir(case_id)
    params = {"min_component_mm3": 1.0, "min_ap_coverage": 0.1}
    segmentation.run_segment(case_id, params)
    stale = np.zeros_like(hu, dtype=np.uint8)
    for name in ("mask_candidate.npy", "mask_before_external_air.npy", "candidate_before_external_air.npy"):
        np.save(root / "segment" / name, stale)

    segmentation.run_segment(case_id, params)
    for name in ("mask_candidate.npy", "mask_before_external_air.npy", "candidate_before_external_air.npy"):
        assert not (root / "segment" / name).exists()
    current = np.load(root / "segment" / "candidate_mask.npy")
    mask_review.reset_mask(case_id)
    np.testing.assert_array_equal(np.load(root / "segment" / "candidate_mask.npy"), current)


def test_slicer_receives_same_full_candidate_as_browser(tmp_path, monkeypatch):
    sitk = pytest.importorskip("SimpleITK")
    monkeypatch.setattr("pipeline.config.CASES_DIR", tmp_path)
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = create_case("synthetic", "<合成测试>", synthetic=True)
    hu = np.zeros((4, 12, 12), dtype=np.float32)
    save_volume(
        case_id,
        Volume(
            array=hu,
            spacing=(1.0, 1.0, 1.0),
            origin=(0.0, 0.0, 0.0),
            axis_dirs=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
            meta={"shape": list(hu.shape)},
        ),
    )
    root = case_dir(case_id)
    main = np.zeros_like(hu, dtype=np.uint8)
    main[1:3, 3:9, 4:8] = 1
    candidate = main.copy()
    candidate[1, 2, 2] = 1
    np.save(root / "segment" / "mask.npy", main)
    np.save(root / "segment" / "candidate_mask.npy", candidate)
    (root / "segment" / "segmentation.json").write_text(json.dumps({"candidate_mask_available": True}), encoding="utf-8")

    _volume_nrrd, candidate_nrrd = slicer_review._write_nrrd(case_id)

    assert preview.viewer_metadata(case_id)["mask_source"] == "segment/candidate_mask.npy"
    np.testing.assert_array_equal(sitk.GetArrayFromImage(sitk.ReadImage(str(candidate_nrrd))), candidate)
