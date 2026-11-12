"""Real arrays and private temporary case storage; no patient data or model calls."""
import importlib
import json

import numpy as np
import pytest

from pipeline import cases, mask_review
from pipeline.volume import Volume, save_volume


@pytest.fixture
def learning():
    assert importlib.util.find_spec('pipeline.segmentation_learning'), 'segmentation learning is not implemented'
    return importlib.import_module('pipeline.segmentation_learning')


@pytest.fixture
def sample_case(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, 'CASES_DIR', tmp_path / 'cases')
    monkeypatch.setattr('pipeline.config.CASES_DIR', tmp_path / 'cases')
    case_id = cases.create_case('synthetic', '<synthetic>', synthetic=True)
    root = cases.case_dir(case_id)
    shape = (9, 12, 15)
    save_volume(case_id, Volume(np.zeros(shape, dtype=np.float32), (2., 1., .5),
        (0., 0., 0.), ((0., 0., 1.), (0., 1., 0.), (1., 0., 0.)), {'shape': list(shape)}))
    mask = np.zeros(shape, dtype=np.uint8)
    mask[2:7, 3:9, 4:11] = 1
    mask[1, 1, 1] = 1
    np.save(root / 'segment/mask.npy', mask)
    np.save(root / 'segment/candidate_mask.npy', mask)
    (root / 'segment/segmentation.json').write_text(json.dumps({
        'mask_version': 1, 'params': {'ap_axis': 1, 'hu_high': -300},
        'need_human_confirm': True, 'metrics': {},
    }), encoding='utf-8')
    return case_id, mask


def correction(sample_case):
    case_id, _ = sample_case
    mask_review.edit_mask(case_id, 'axial', 1, 'erase', [{'x': 1, 'y': 1}], 0)
    return case_id


def test_edit_records_immutable_before_after_without_creating_reference(learning, sample_case):
    cid = correction(sample_case)
    records = learning.list_changes(cid)
    assert len(records) == 1
    record = records[0]
    assert record['before_version'] == 1 and record['after_version'] == 2
    assert record['removed_voxels'] == 1 and record['added_voxels'] == 0
    assert record['reference_status'] == 'unreviewed'
    before, after, _ = learning.load_pair(cid, record['sample_id'])
    np.testing.assert_array_equal(before, sample_case[1])
    assert after.sum() == before.sum() - 1
    assert 'actor_id' not in record and 'path' not in json.dumps(records)


def test_failed_mask_transaction_does_not_publish_learning_sample(learning, sample_case, monkeypatch):
    cid, original = sample_case
    replace = mask_review.os.replace
    def fail_metadata(src, dst):
        if str(src).endswith('.tmp') and str(dst).endswith('segmentation.json'):
            raise OSError('synthetic transaction interruption')
        return replace(src, dst)
    monkeypatch.setattr(mask_review.os, 'replace', fail_metadata)
    with pytest.raises(OSError):
        correction(sample_case)
    np.testing.assert_array_equal(np.load(cases.case_dir(cid) / 'segment/mask.npy'), original)
    assert learning.list_changes(cid) == []


def test_candidate_execution_is_isolated_and_reports_no_reference(learning, sample_case):
    cid = correction(sample_case)
    sid = learning.list_changes(cid)[0]['sample_id']
    root = cases.case_dir(cid)
    original = (root / 'segment/mask.npy').read_bytes()
    result = learning.run_candidate(cid, sid, 'remove_tiny_islands_v1')
    assert result['committed'] is False
    assert result['evaluation_status'] == 'reference_required'
    assert result['auto_promotion'] is False
    assert (root / 'segment/mask.npy').read_bytes() == original
    with pytest.raises(learning.LearningError):
        learning.run_candidate(cid, sid, '../../python.exe')


def test_explicit_reference_then_candidate_evaluation_detects_real_improvement(learning, sample_case):
    cid = correction(sample_case)
    sid = learning.list_changes(cid)[0]['sample_id']
    learning.review_reference(cid, sid, 2, reviewer_id='human-reviewer',
        disposition='approved', reason_code='external_residual',
        reference_checked=True, use_authorized=True, subject_group='subject-001')
    result = learning.run_candidate(cid, sid, 'remove_tiny_islands_v1')
    assert result['evaluation_status'] == 'evaluated'
    assert result['metrics']['false_positive_voxels'] == 0
    assert result['metrics']['false_negative_voxels'] == 0
    assert result['baseline_metrics']['false_positive_voxels'] == 1
    assert result['auto_promotion'] is False


def test_reference_requires_explicit_checks_current_version_and_immutable_content(learning, sample_case):
    cid = correction(sample_case)
    sid = learning.list_changes(cid)[0]['sample_id']
    kwargs = dict(reviewer_id='human-reviewer', disposition='approved',
        reason_code='missing_airway', reference_checked=True, use_authorized=True, subject_group='subject-001')
    with pytest.raises(learning.LearningError):
        learning.review_reference(cid, sid, 1, **kwargs)
    with pytest.raises(learning.LearningError):
        learning.review_reference(cid, sid, 2, **{**kwargs, 'reference_checked': False})
    altered = np.load(cases.case_dir(cid) / 'segment/mask.npy')
    altered[3, 4, 5] = 0
    np.save(cases.case_dir(cid) / 'segment/mask.npy', altered)
    with pytest.raises(learning.LearningError):
        learning.review_reference(cid, sid, 2, **kwargs)


def test_private_snapshots_are_not_generic_download_artifacts(learning, sample_case):
    cid = correction(sample_case)
    from pipeline.errors import PipelineError
    with pytest.raises(PipelineError):
        cases.artifact_path(cid, 'learning/segmentation/records/any.json')


def test_metrics_detect_thin_bridge_loss_despite_high_overlap(learning):
    metrics = importlib.import_module('pipeline.segmentation_evaluation').mask_metrics
    reference = np.zeros((20, 20, 20), dtype=np.uint8)
    reference[2:8, 2:8, 2:8] = 1
    reference[12:18, 2:8, 2:8] = 1
    reference[8:12, 4, 4] = 1
    candidate = reference.copy()
    candidate[10, 4, 4] = 0
    result = metrics(candidate, reference, (2, 1, .5))
    assert result['dice'] > .99
    assert result['candidate_components'] == 2 and result['reference_components'] == 1
    assert result['false_negative_voxels'] == 1
    assert result['false_negative_mm3'] == 1
    assert result['max_surface_distance_mm'] > 0


def test_metrics_reject_empty_reference_and_shape_or_spacing_mismatch(learning):
    metrics = importlib.import_module('pipeline.segmentation_evaluation').mask_metrics
    with pytest.raises(ValueError):
        metrics(np.zeros((3, 3, 3)), np.zeros((3, 3, 3)), (1, 1, 1))
    with pytest.raises(ValueError):
        metrics(np.ones((3, 3, 3)), np.ones((3, 3, 2)), (1, 1, 1))
    with pytest.raises(ValueError):
        metrics(np.ones((3, 3, 3)), np.ones((3, 3, 3)), (1, 0, 1))


def test_learning_retention_expires_private_snapshots_without_changing_official_mask(learning, sample_case, monkeypatch):
    from datetime import datetime, timezone, timedelta
    cid = correction(sample_case)
    sid = learning.list_changes(cid)[0]['sample_id']
    learning.run_candidate(cid, sid, 'identity_v1')
    official = (cases.case_dir(cid) / 'segment/mask.npy').read_bytes()
    later = (datetime.now(timezone.utc) + timedelta(days=181)).isoformat()
    monkeypatch.setattr(learning, '_now', lambda: later)
    import os
    for path in (cases.case_dir(cid) / 'learning/segmentation/blobs').glob('*.npz'):
        # Recomputing a derived mask must not extend its source sample lifetime.
        os.utime(path, (datetime.fromisoformat(later).timestamp(),) * 2)
    assert learning.list_changes(cid) == []
    assert learning.prune_case(cid)['expired_samples'] == 1
    assert not list((cases.case_dir(cid) / 'learning/segmentation/blobs').glob('*.npz'))
    assert (cases.case_dir(cid) / 'segment/mask.npy').read_bytes() == official


def test_reference_group_cannot_change_within_one_case(learning, sample_case):
    cid = correction(sample_case)
    sid = learning.list_changes(cid)[0]['sample_id']
    options = dict(reviewer_id='reviewer', disposition='approved', reason_code='mixed',
        reference_checked=True, use_authorized=True, subject_group='subject-001')
    learning.review_reference(cid, sid, 2, **options)
    mask_review.edit_mask(cid, 'axial', 2, 'erase', [{'x':4,'y':3}], 0)
    sid2 = learning.list_changes(cid)[0]['sample_id']
    with pytest.raises(learning.LearningError):
        learning.review_reference(cid, sid2, 3, **{**options, 'subject_group':'subject-002'})


def test_empty_reference_is_rejected_before_recording_approval(learning, sample_case):
    cid, mask = sample_case
    seg = json.loads((cases.case_dir(cid) / 'segment/segmentation.json').read_text(encoding='utf-8'))
    seg['mask_version'] = 2
    mask_review._commit_mask_change(cid, np.zeros_like(mask), seg)
    sid = learning.list_changes(cid)[0]['sample_id']
    with pytest.raises(learning.LearningError):
        learning.review_reference(cid,sid,2,reviewer_id='reviewer',disposition='approved',
            reason_code='missing_airway',reference_checked=True,use_authorized=True,subject_group='subject-001')
    assert learning.list_changes(cid)[0]['reference_status'] == 'unreviewed'
