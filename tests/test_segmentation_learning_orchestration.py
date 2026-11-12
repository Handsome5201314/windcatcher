import pytest

from pipeline import cases, orchestration_batch, segmentation_learning as learning
from test_segmentation_learning import sample_case, correction


def test_harness_batch_compares_corrections_once_without_spending_tokens_or_confirming(sample_case, monkeypatch):
    cid = correction(sample_case)
    for stage in ('import','anonymize','qc'):
        cases.update_stage(cid, stage, 'ok')
    monkeypatch.setattr(orchestration_batch.harness_advice, 'actions_enabled',
        lambda: pytest.fail('deterministic comparison must not request a model'))
    item = {'case_id':cid,'actor_id':'human-tester','use_harness':True}
    original = (cases.case_dir(cid) / 'segment/mask.npy').read_bytes()
    result = orchestration_batch.execute(item, lambda *_:True)
    comparisons = [row for row in result['actions'] if row['action']=='compare_segmentation_candidates']
    assert len(comparisons) == 1
    assert len(comparisons[0]['comparison']['runs']) == 3
    assert comparisons[0]['comparison']['reference_available'] is False
    assert result['task_status']=='blocked'
    assert (cases.case_dir(cid) / 'segment/mask.npy').read_bytes() == original
    repeat = orchestration_batch.execute(item, lambda *_:True)
    assert not any(row['action']=='compare_segmentation_candidates' for row in repeat['actions'])
    sid = learning.list_changes(cid)[0]['sample_id']
    learning.review_reference(cid,sid,2,reviewer_id='human-tester',disposition='approved',
        reason_code='external_residual',reference_checked=True,use_authorized=True,subject_group='subject-001')
    reviewed = orchestration_batch.execute(item, lambda *_:True)
    comparisons = [row for row in reviewed['actions'] if row['action']=='compare_segmentation_candidates']
    assert comparisons[0]['comparison']['reference_available'] is True
    assert comparisons[0]['comparison']['auto_promotion'] is False
