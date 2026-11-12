"""Synthetic cases through the real public gateway; no production credentials."""
import pytest
from fastapi.testclient import TestClient

import cloud
import server
from pipeline import cases, config, ai_report, patient_shares
from test_segmentation_learning import sample_case, correction


@pytest.fixture
def gateway(sample_case, tmp_path, monkeypatch):
    cid = correction(sample_case)
    monkeypatch.setattr(server, 'CASES_DIR', cases.CASES_DIR)
    monkeypatch.setattr(ai_report, 'CONFIG_PATH', tmp_path / 'ai.json')
    monkeypatch.setattr(patient_shares, 'SHARE_DIR', tmp_path / 'shares')
    settings = {'WINDCATCHER_IDENTITY_DIR': str(tmp_path / 'identity'),
        'WINDCATCHER_MULTIUSER':'1', 'WINDCATCHER_DEPLOYMENT':'public',
        'WINDCATCHER_PUBLIC_URL':'https://windcatcher.test', 'WINDCATCHER_PUBLIC_USERNAME':'owner-test',
        'WINDCATCHER_PUBLIC_PASSWORD':'synthetic-test-password-long-enough',
        'WINDCATCHER_PUBLIC_SESSION_SECRET':'synthetic-session-secret-32-chars', 'WINDCATCHER_MCP_TOKEN':'T'*43}
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    app = cloud.create_app()
    identity = app.state.identity
    owner_id = identity.authenticate('owner-test', settings['WINDCATCHER_PUBLIC_PASSWORD'])['id']
    viewer_id = identity.create_user(owner_id, 'viewer-test', settings['WINDCATCHER_PUBLIC_PASSWORD'])['id']
    service_id = identity.mcp_principal()['id']
    owner = TestClient(app, base_url='https://windcatcher.test')
    viewer = TestClient(app, base_url='https://windcatcher.test')
    for client, username in [(owner,'owner-test'),(viewer,'viewer-test')]:
        assert client.post('/login', data={'username':username, 'password':settings['WINDCATCHER_PUBLIC_PASSWORD']}, follow_redirects=False).status_code == 303
    local = TestClient(app, base_url='https://127.0.0.1:8765', client=('127.0.0.1',50000))
    return locals()


def test_public_service_grants_are_required_and_human_review_stays_human(gateway):
    g = gateway
    path = f"/api/cases/{g['cid']}/segment/learning"
    local, owner, viewer = g['local'], g['owner'], g['viewer']
    headers = {'X-Windcatcher-MCP-Token':'T'*43}
    assert local.get(path, headers=headers).status_code in {403,404}
    assert viewer.get(path).status_code in {403,404}
    g['identity'].grant_case(g['owner_id'],g['cid'],g['service_id'],'viewer')
    g['identity'].grant_case(g['owner_id'],g['cid'],g['viewer_id'],'viewer')
    result = local.get(path, headers=headers)
    assert result.status_code == 200, result.text
    sid = result.json()['samples'][0]['sample_id']
    body = {'sample_id':sid,'strategy_id':'remove_tiny_islands_v1'}
    result = local.post(path+'/candidate',json=body,headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()['committed'] is False
    review = {'sample_id':sid,'mask_version':2,'disposition':'approved','reason_code':'external_residual',
        'reference_checked':True,'use_authorized':True,'subject_group':'subject-001'}
    assert local.post(path+'/reference',json=review,headers=headers).status_code in {401,403}
    assert viewer.post(path+'/reference',json=review).status_code in {403,404}
    result = owner.post(path+'/reference',json=review)
    assert result.status_code == 200, result.text
    assert result.json()['reference_status'] == 'approved'
    assert local.get(path,headers={**headers,'X-Forwarded-For':'127.0.0.1'}).status_code in {401,403}
    assert owner.get(f"/api/cases/{g['cid']}/artifact",params={'rel':'learning/segmentation/records/'+sid+'.json'}).status_code == 403
