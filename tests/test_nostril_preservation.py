"""Technical nostril-candidate preservation; not anatomic validation."""
import json

import numpy as np
import pytest

from pipeline import cases, mask_review
from pipeline.errors import PipelineError
from pipeline.mask_review import _remove_external_air_array
from pipeline.volume import Volume, save_volume


def _open_nostrils():
    hu = np.full((24, 64, 64), -1000., dtype=np.float32)
    hu[2:22, 8:63, 8:56] = 40.
    raw = np.zeros(hu.shape, dtype=np.uint8)
    raw[9:15, 4:21, 21:29] = 1
    raw[9:15, 4:21, 39:47] = 1
    raw[9:15, 8:60, 30:33] = 1
    raw[9:15, 8:60, 35:38] = 1
    raw[9:15, 20:22, 28:31] = 1
    raw[9:15, 20:22, 37:40] = 1
    hu[raw > 0] = -900.
    return hu, raw


def test_bounded_open_nostrils_are_preserved_without_creating_voxels(monkeypatch):
    monkeypatch.setattr(mask_review, 'EXTERNAL_AIR_MIN_RETAINED_VOXELS', 1)
    hu, raw = _open_nostrils()
    envelope, _ = mask_review._body_envelope(hu, (1., 1., 1.))
    outside = raw.astype(bool) & ~envelope
    assert outside.sum() > 0, "fixture must have an open nostril beyond the body envelope"
    protected, _ = mask_review._anterior_opening_candidates(raw, envelope, (1., 1., 1.), 1, -1.)
    clean, detail = _remove_external_air_array(raw, hu, (1., 1., 1.), 1)
    assert protected.sum() > 0
    np.testing.assert_array_equal(clean[protected], raw[protected])
    assert detail['anterior_opening_candidate']['preserved_components'] == 2
    assert detail['anterior_opening_candidate']['preserved_voxels'] == int(protected.sum())
    assert detail['anterior_opening_candidate']['status'] == 'near_nostril_review_required'
    assert not np.any(clean & ~raw)

    # Only the original, bounded candidate may be exempted at confirmation.
    assert mask_review._outside_body_envelope_counts(
        clean, clean, hu, (1., 1., 1.), -300., allowed_outside=protected
    ) == (0, 0)
    altered = clean.copy()
    altered[10, 5, 10] = 1
    assert mask_review._outside_body_envelope_counts(
        altered, altered, hu, (1., 1., 1.), -300., allowed_outside=protected
    )[0] > 0


def test_exterior_connected_to_image_edge_is_not_preserved(monkeypatch):
    monkeypatch.setattr(mask_review, 'EXTERNAL_AIR_MIN_RETAINED_VOXELS', 1)
    hu, raw = _open_nostrils()
    raw[9:15, 0:5, 21:29] = 1  # left nostril candidate reaches exterior image edge
    hu[raw > 0] = -900.
    clean, detail = _remove_external_air_array(raw, hu, (1., 1., 1.), 1)
    assert not clean[11, 0, 24]
    assert not clean[11, 5, 24]
    assert clean[11, 5, 42], "the other bounded nostril candidate remains available"
    assert detail['anterior_opening_candidate']['preserved_components'] == 1


def test_anterior_direction_is_observed_for_reversed_ct_axis(monkeypatch):
    monkeypatch.setattr(mask_review, 'EXTERNAL_AIR_MIN_RETAINED_VOXELS', 1)
    hu, raw = _open_nostrils()
    reversed_hu, reversed_raw = hu[:, ::-1, :].copy(), raw[:, ::-1, :].copy()
    cleaned, detail = _remove_external_air_array(
        reversed_raw, reversed_hu, (1., 1., 1.), 1, anterior_axis_sign=1.
    )
    assert detail['anterior_opening_candidate']['preserved_components'] == 2
    assert cleaned[11, 64 - 1 - 5, 24]
    assert cleaned[11, 64 - 1 - 5, 42]
    assert not np.any(cleaned & ~reversed_raw)


def test_confirmation_exempts_only_backed_up_nostril_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr('pipeline.config.CASES_DIR', tmp_path)
    monkeypatch.setattr(cases, 'CASES_DIR', tmp_path)
    monkeypatch.setattr(mask_review, 'EXTERNAL_AIR_MIN_RETAINED_VOXELS', 1)
    case_id = cases.create_case('synthetic', '<synthetic>', synthetic=True)
    root = cases.case_dir(case_id)
    hu, raw = _open_nostrils()
    save_volume(case_id, Volume(
        array=hu, spacing=(1., 1., 1.), origin=(0., 0., 0.),
        axis_dirs=((0., 0., 1.), (0., 1., 0.), (1., 0., 0.)),
        meta={'shape': list(hu.shape)},
    ))
    np.save(root / 'segment/mask.npy', raw)
    np.save(root / 'segment/candidate_mask.npy', raw)
    (root / 'segment/segmentation.json').write_text(json.dumps({
        'params': {'ap_axis': 1, 'hu_high': -300},
        'mask_version': 1, 'candidate_mask_available': True,
        'candidate_cleaning': {'status': 'manual_review_required'},
        'need_human_confirm': True, 'human_review': {'status': '待复核'},
    }), encoding='utf-8')
    outcome = mask_review.auto_remove_external_air(case_id)
    assert outcome['candidate_cleanup']['anterior_opening_candidate']['preserved_components'] == 2

    candidate_path = root / 'segment/candidate_mask.npy'
    cleaned = np.load(candidate_path, allow_pickle=False)
    altered = cleaned.copy()
    altered[10, 5, 10] = 1  # Not present in the immutable pre-cleanup backup.
    np.save(candidate_path, altered)
    with pytest.raises(PipelineError, match='包络|体外'):
        mask_review.confirm_mask(case_id)
    np.save(candidate_path, cleaned)
    mask_review.confirm_mask(case_id)  # Explicit manual confirmation path, not automatic approval.
    record = json.loads((root / 'segment/segmentation.json').read_text(encoding='utf-8'))
    validation = record['external_air_validation']
    assert validation['outside_voxels'] == 0
    assert validation['candidate_outside_voxels'] == 0
    assert validation['candidate_anterior_opening_candidate_voxels'] > 0


def test_open_airway_can_be_removed_by_old_envelope_and_candidate_keeps_it():
    from pipeline.segmentation_predictor import comparison_summary
    hu=np.full((32,48,48),-1000.,dtype=np.float32)
    hu[2:30,8:40,8:40]=40.
    raw=np.zeros(hu.shape,dtype=np.uint8)
    raw[5:27,4:30,22:26]=1
    raw[5:27,15:32,17:31]=1
    hu[raw>0]=-900.
    clean,_=_remove_external_air_array(raw,hu,(1.,1.,1.),1)
    assert clean.sum() < raw.sum(), 'fixture must reproduce old cleanup deletion'
    summary=comparison_summary(clean,raw,(1.,1.,1.))
    assert summary['added_voxels'] > 0
    assert summary['removed_voxels']==0
    assert raw[8,5,23]==1
    assert summary['accuracy_status']=='reference_required'
