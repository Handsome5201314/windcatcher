import asyncio

import pytest
from fastapi.testclient import TestClient

import server
from pipeline.access_policy import classify
from test_segmentation_learning import sample_case, correction


def test_segmentation_learning_routes_have_explicit_capabilities():
    root = '/api/cases/case-20261106-120000-abcd/segment/learning'
    assert classify(root, 'GET').capability == 'view'
    assert classify(root + '/candidate', 'POST').capability == 'view'
    assert classify(root + '/reference', 'POST').capability == 'review'
    assert classify(root + '/execute', 'POST').kind == 'deny'


def test_api_runs_real_candidate_and_never_accepts_machine_reference(sample_case):
    cid = correction(sample_case)
    client = TestClient(server.app)
    path = f'/api/cases/{cid}/segment/learning'
    result = client.get(path)
    assert result.status_code == 200, result.text
    sample = result.json()['samples'][0]
    run = client.post(path + '/candidate', json={'sample_id': sample['sample_id'], 'strategy_id': 'remove_tiny_islands_v1'})
    assert run.status_code == 200, run.text
    assert run.json()['evaluation_status'] == 'reference_required'
    payload = {'sample_id': sample['sample_id'], 'mask_version': 2, 'disposition': 'approved',
        'reason_code': 'external_residual', 'reference_checked': True, 'use_authorized': True, 'subject_group': 'subject-001'}
    denied = client.post(path + '/reference', headers={'X-Windcatcher-MCP-Token': 'synthetic-token'}, json=payload)
    assert denied.status_code == 403
    approved = client.post(path + '/reference', json=payload)
    assert approved.status_code == 200, approved.text
    run = client.post(path + '/candidate', json={'sample_id': sample['sample_id'], 'strategy_id': 'remove_tiny_islands_v1'})
    assert run.json()['metrics']['false_positive_voxels'] == 0
    assert client.post(path + '/candidate', json={'sample_id': sample['sample_id'], 'strategy_id': 'identity_v1',
        'file_path': 'private'}).status_code == 422
    assert client.post(path + '/reference', json={**payload, 'mask_version': True}).status_code == 422


def test_mcp_has_bounded_candidate_tools_but_no_human_label_tool(monkeypatch):
    pytest.importorskip('mcp')
    import windcatcher_mcp as adapter
    names = set(adapter.mcp._tool_manager._tools)
    assert {'inspect_segmentation_learning', 'run_segmentation_candidate'} <= names
    assert 'review_segmentation_reference' not in names
    calls = []
    async def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {'committed': False}
    monkeypatch.setattr(adapter, '_request', request)
    asyncio.run(adapter.run_segmentation_candidate('seg-' + 'a' * 32, 'identity_v1', 'synthetic-test'))
    assert calls == [('POST', '/api/cases/synthetic-test/segment/learning/candidate',
                     {'body': {'sample_id': 'seg-' + 'a' * 32, 'strategy_id': 'identity_v1'}})]
