import importlib

import pytest

from pipeline import segmentation_learning as learning
from test_segmentation_learning import sample_case, correction


def frozen_case(sample_case):
    cid = correction(sample_case)
    sid = learning.list_changes(cid)[0]['sample_id']
    learning.review_reference(cid, sid, 2, reviewer_id='human-reviewer', disposition='approved',
        reason_code='external_residual', reference_checked=True, use_authorized=True, subject_group='subject-001')
    return {'case_id': cid, 'sample_id': sid, 'split': 'holdout'}


def test_frozen_cohort_evaluates_real_arrays_but_cannot_promote_synthetic_proof(sample_case):
    evaluation = importlib.import_module('pipeline.segmentation_evaluation')
    assert hasattr(evaluation, 'freeze_cohort'), 'frozen segmentation evaluation is missing'
    cohort = evaluation.freeze_cohort([frozen_case(sample_case)])
    report = evaluation.evaluate_cohort(cohort, 'remove_tiny_islands_v1')
    assert report['dataset_sha256'] == cohort['dataset_sha256']
    assert report['holdout']['samples'] == 1
    assert report['holdout']['regressions'] == 0
    assert report['holdout']['improved_samples'] == 1
    from pipeline.upgrade_gate import segmentation_readiness
    gate = segmentation_readiness(report)
    assert gate['status'] == 'blocked'
    assert 'synthetic_only' in gate['reasons']
    assert gate['auto_promotion'] is False


def test_cohort_rejects_unreviewed_samples_duplicates_and_patient_leakage(sample_case):
    evaluation = importlib.import_module('pipeline.segmentation_evaluation')
    assert hasattr(evaluation, 'freeze_cohort')
    cid = correction(sample_case)
    sid = learning.list_changes(cid)[0]['sample_id']
    entry = {'case_id': cid, 'sample_id': sid, 'split': 'holdout'}
    with pytest.raises(learning.LearningError):
        evaluation.freeze_cohort([entry])
    learning.review_reference(cid, sid, 2, reviewer_id='human-reviewer', disposition='approved',
        reason_code='external_residual', reference_checked=True, use_authorized=True, subject_group='subject-001')
    with pytest.raises(learning.LearningError):
        evaluation.freeze_cohort([entry, entry])
    with pytest.raises(learning.LearningError):
        evaluation.freeze_cohort([entry, {**entry, 'split': 'development'}])


def test_cohort_and_report_tampering_are_rejected(sample_case):
    evaluation = importlib.import_module('pipeline.segmentation_evaluation')
    assert hasattr(evaluation, 'freeze_cohort')
    cohort = evaluation.freeze_cohort([frozen_case(sample_case)])
    cohort['entries'][0]['record_sha256'] = '0' * 64
    with pytest.raises(learning.LearningError):
        evaluation.evaluate_cohort(cohort, 'identity_v1')
    cohort = evaluation.freeze_cohort([{'case_id': sample_case[0],
        'sample_id': learning.list_changes(sample_case[0])[0]['sample_id'], 'split': 'holdout'}])
    report = evaluation.evaluate_cohort(cohort, 'identity_v1')
    report['holdout']['regressions'] = -1
    from pipeline.upgrade_gate import segmentation_readiness, UpgradeError
    with pytest.raises(UpgradeError):
        segmentation_readiness(report)
