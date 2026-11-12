"""Synthetic-only checks for the case-bound exterior-air draft."""

import json

import numpy as np
import pytest

from pipeline.errors import PipelineError
from pipeline.external_collar_draft import build_case_external_collar_draft


AXIS_DIRS_LPS = ((0, 0, 1), (0, 1, 0), (-1, 0, 0))


@pytest.fixture
def collar_case(tmp_path, monkeypatch):
    monkeypatch.setattr("pipeline.cases.CASES_DIR", tmp_path)
    root = tmp_path / "case-synthetic-collar"
    (root / "segment").mkdir(parents=True)
    (root / "volume").mkdir()
    shape = (25, 25, 35)
    hu = np.full(shape, -900.0, dtype=np.float32)
    hu[2:23, 7:23, 2:33] = 100.0
    confirmed = np.zeros(shape, dtype=np.uint8)
    confirmed[8:11, 8:11, 7:10] = 1
    confirmed[8:11, 8:11, 25:28] = 1
    hu[confirmed > 0] = -900.0
    hu[8:11, 2:8, 7:10] = -900.0
    hu[8:11, 2:8, 25:28] = -900.0
    np.save(root / "volume" / "hu.npy", hu)
    (root / "volume" / "meta.json").write_text(json.dumps({
        "shape": list(shape), "spacing_mm": [1, 1, 1], "origin_mm": [0, 0, 0],
        "axis_dirs": AXIS_DIRS_LPS,
    }), encoding="utf-8")
    np.save(root / "segment" / "mask.npy", confirmed)
    # This historical candidate has no exterior air, so a successful result
    # proves that the source is uncropped CT HU rather than a saved backup.
    np.save(root / "segment" / "candidate_mask_original.npy", confirmed)
    record = {
        "algorithm_version": "synthetic-threshold-v1",
        "mask_version": 4,
        "need_human_confirm": False,
        "human_review": {"status": "已确认"},
        "params": {"hu_low": -1024, "hu_high": -300},
        "external_air_validation": {
            "mask_version": 4, "body_hu_threshold": -300,
            "outside_voxels": 0, "candidate_outside_voxels": 0,
        },
    }
    (root / "segment" / "segmentation.json").write_text(
        json.dumps(record), encoding="utf-8",
    )
    return root, record


def _build():
    return build_case_external_collar_draft(
        "case-synthetic-collar", expected_mask_version=4,
        left_seed_ijk=(9, 9, 8), right_seed_ijk=(9, 9, 26),
        max_extent_mm=6.0,
    )


def test_case_draft_uses_uncropped_hu_and_never_changes_saved_case(collar_case):
    root, _ = collar_case
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    draft = _build()
    assert draft["status"] == "candidate_only"
    assert draft["eligible_for_cfd"] is False
    assert draft["format"] == "external_nostril_collar_case_draft_v1"
    assert draft["source"]["mask_version"] == 4
    assert draft["source"]["raw_air"] == "volume/hu.npy:uncropped_hu_window"
    assert len(draft["source"]["volume_sha256"]) == 64
    assert draft["source"]["left_seed_ijk"] == [9, 9, 8]
    assert draft["source"]["right_seed_ijk"] == [9, 9, 26]
    assert draft["left"]["stats"]["voxels"] > 0
    assert draft["right"]["stats"]["voxels"] > 0
    after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert before == after


def test_case_draft_blocks_stale_or_unconfirmed_mask(collar_case):
    root, record = collar_case
    with pytest.raises(PipelineError, match="版本与请求不一致"):
        build_case_external_collar_draft(
            "case-synthetic-collar", expected_mask_version=3,
            left_seed_ijk=(9, 9, 8), right_seed_ijk=(9, 9, 26), max_extent_mm=6,
        )
    record["need_human_confirm"] = True
    (root / "segment" / "segmentation.json").write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(PipelineError, match="人工确认"):
        _build()


def test_case_draft_blocks_source_changed_during_computation(collar_case, monkeypatch):
    root, record = collar_case
    from pipeline import external_collar_draft

    original = external_collar_draft.build_external_collar_candidates

    def change_record(*args, **kwargs):
        result = original(*args, **kwargs)
        record["mask_version"] = 5
        (root / "segment" / "segmentation.json").write_text(json.dumps(record), encoding="utf-8")
        return result

    monkeypatch.setattr(external_collar_draft, "build_external_collar_candidates", change_record)
    with pytest.raises(PipelineError, match="已变化"):
        _build()


def test_case_draft_blocks_mask_replacement_without_version_change(collar_case, monkeypatch):
    root, _ = collar_case
    from pipeline import external_collar_draft

    original = external_collar_draft.build_external_collar_candidates

    def replace_mask(*args, **kwargs):
        result = original(*args, **kwargs)
        mask_path = root / "segment" / "mask.npy"
        mask = np.load(mask_path, allow_pickle=False)
        mask[9, 9, 8] = 0
        np.save(mask_path, mask)
        return result

    monkeypatch.setattr(external_collar_draft, "build_external_collar_candidates", replace_mask)
    with pytest.raises(PipelineError, match="已变化"):
        _build()


def test_case_draft_blocks_ct_replacement_during_computation(collar_case, monkeypatch):
    root, _ = collar_case
    from pipeline import external_collar_draft

    original = external_collar_draft.build_external_collar_candidates

    def replace_volume(*args, **kwargs):
        result = original(*args, **kwargs)
        volume_path = root / "volume" / "hu.npy"
        hu = np.load(volume_path, mmap_mode="r+", allow_pickle=False)
        hu[0, 0, 0] = -850.0
        hu.flush()
        return result

    monkeypatch.setattr(external_collar_draft, "build_external_collar_candidates", replace_volume)
    with pytest.raises(PipelineError, match="已变化"):
        _build()


def test_case_draft_requires_saved_hu_window(collar_case):
    root, record = collar_case
    del record["params"]["hu_low"]
    (root / "segment" / "segmentation.json").write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(PipelineError, match="HU 下限"):
        _build()
