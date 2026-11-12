import hashlib
import json
from types import SimpleNamespace

import pytest

from test_patient_shares import setup, login, approve, publish
from pipeline import cases, patient_shares, reference_cases


@pytest.fixture
def reference_setup(setup):
    client, case_id, version, model = setup
    root = cases.case_dir(case_id)
    from pipeline.surface import write_binary_stl
    import numpy as np
    write_binary_stl(root / 'surface/airway.stl', np.array([[0, 0, 0], [10, 0, 0], [0, 10, 0], [0, 0, 10]]),
                     np.array([[0, 2, 1], [0, 1, 3], [1, 2, 3], [2, 0, 3]]))
    cases.update_stage(case_id, 'surface', 'ok')
    return client, case_id, version, model


def metadata(**extra):
    return dict(title='参考结构 A', description='已核对的气道形态参考，仅用于展示。',
                tags=['septal_deviation', 'post_turbinate_outfracture'],
                authorization_confirmed=True, labels_confirmed=True,
                reviewer='测试审核员', review_note='已核对展示授权、病例标签与所发布的模型版本。',
                kind='anatomy', action='publish', **extra)


def publish_reference(client, case_id, **changes):
    body = metadata()
    body.update(changes)
    return client.post(f'/api/reference-admin/cases/{case_id}', json=body)


def test_candidates_use_existing_cases_and_drafts_stay_private(reference_setup):
    client, case_id, _, _ = reference_setup
    assert client.get('/api/reference-admin/cases').status_code == 401
    assert client.get('/api/references').json()['items'] == []
    login(client)
    result = client.get('/api/reference-admin/cases').json()
    assert [row['case_id'] for row in result['items']] == [case_id]
    saved = publish_reference(client, case_id, action='draft')
    assert saved.status_code == 200, saved.text
    assert saved.json()['status'] == 'draft'
    assert client.get('/api/references').json()['items'] == []


def test_structure_snapshot_publication_is_separate_from_failed_cfd(reference_setup):
    client, case_id, _, _ = reference_setup
    login(client)
    response = publish_reference(client, case_id)
    assert response.status_code == 200, response.text
    entry = response.json()
    client.cookies.clear()
    public = client.get('/api/references').json()['items']
    assert len(public) == 1 and public[0]['kind'] == 'anatomy'
    assert public[0]['tags'] == metadata()['tags']
    assert case_id not in json.dumps(public)
    model = client.get('/api/references/' + entry['id'] + '/model')
    assert model.status_code == 200, model.text
    payload = model.json()
    assert payload['kind'] == 'anatomy'
    assert payload['units'] == {'coordinates': 'm'}
    assert payload['bounds_m'] == [0, .01, 0, .01, 0, .01]
    assert 'speeds' not in payload['wall'] and 'pressure' not in payload['wall']
    assert case_id not in model.text and 'patient name' not in model.text
    assert client.get('/api/references?tag=septal_deviation').json()['items'] == public
    assert client.get('/api/references?tag=sinusitis').json()['items'] == []


@pytest.mark.parametrize('changes', [
    {'authorization_confirmed': False}, {'labels_confirmed': False}, {'tags': []},
    {'tags': ['made-up-diagnosis']}, {'title': '<script>alert(1)</script>'},
])
def test_publication_requires_explicit_verified_metadata(reference_setup, changes):
    client, case_id, _, _ = reference_setup
    login(client)
    assert publish_reference(client, case_id, **changes).status_code in {400, 422}
    assert client.get('/api/references').json()['items'] == []


def test_flow_requires_real_gate_and_pins_sanitized_payload(reference_setup, monkeypatch):
    client, case_id, version, model = reference_setup
    login(client)
    assert publish_reference(client, case_id, kind='flow').status_code == 409
    approve(monkeypatch)
    published = publish_reference(client, case_id, kind='flow')
    assert published.status_code == 200, published.text
    url = '/api/references/' + published.json()['id'] + '/model'
    before = client.get(url).content
    version['signature'] = 'b' * 64
    model.write_text('{}', encoding='utf-8')
    client.cookies.clear()
    assert client.get(url).content == before
    assert 'patient name' not in before.decode() and 'private_path' not in before.decode()


def test_unverified_flow_test_requires_explicit_consent_and_completed_solver(reference_setup, monkeypatch):
    client, case_id, _, _ = reference_setup
    login(client)
    body = dict(kind='flow_test', test_use_confirmed=True)
    assert publish_reference(client, case_id, **body).status_code == 409
    run_status = cases.artifact_path(case_id, 'cfd/run_status.json')
    run_status.write_text(json.dumps({'status': 'ran', 'run_id': 'test-run-001'}), encoding='utf-8')
    assert publish_reference(client, case_id, kind='flow_test').status_code == 400
    assert publish_reference(client, case_id, **body, authorization_confirmed=False).status_code == 400
    assert publish_reference(client, case_id, kind='flow').status_code == 409
    response = publish_reference(client, case_id, **body)
    assert response.status_code == 200, response.text
    published = response.json()
    assert published['kind'] == 'flow_test' and published['flow_verified'] is False
    assert '未复核' in published['warning']
    assert published['expires_at'] == published['updated_at'] + 86400
    assert 'patient name' not in client.get('/api/references/' + published['id'] + '/model').text
    assert client.get('/api/references').json()['items'][0]['kind'] == 'flow_test'
    entry = reference_cases.get_entry(published['id'])
    monkeypatch.setattr(reference_cases, 'time', SimpleNamespace(time=lambda: entry['updated_at'] + 86401))
    assert client.get('/api/references').json()['items'] == []
    assert client.get('/api/references/' + published['id'] + '/model').status_code == 404
    assert client.get('/api/reference-admin/cases').json()['items'][0]['entry']['status'] == 'expired'


def test_unverified_flow_test_is_unavailable_on_modelscope(reference_setup, monkeypatch):
    from fastapi.testclient import TestClient
    import cloud
    _, case_id, _, _ = reference_setup
    monkeypatch.setenv('WINDCATCHER_DEPLOYMENT', 'cloud')
    monkeypatch.setenv('WINDCATCHER_CLOUD_USERNAME', 'researcher')
    monkeypatch.setenv('WINDCATCHER_CLOUD_PASSWORD', 'test-only-password-long-enough')
    monkeypatch.setenv('WINDCATCHER_CLOUD_SESSION_SECRET', 'test-only-session-secret-32-characters-minimum')
    monkeypatch.setenv('WINDCATCHER_CLOUD_PUBLIC_ORIGIN', 'https://windcatcher.test')
    client = TestClient(cloud.create_app(), base_url='https://windcatcher.test')
    login(client)
    assert publish_reference(client, case_id, kind='flow_test', test_use_confirmed=True).status_code == 404
    assert client.get('/api/references').json()['items'] == []


def test_reference_revoke_and_case_delete_remove_public_access(reference_setup):
    client, case_id, _, _ = reference_setup
    login(client)
    published = publish_reference(client, case_id).json()
    url = '/api/references/' + published['id'] + '/model'
    revoke = '/api/reference-admin/' + published['id'] + '/revoke'
    assert client.post(revoke, headers={'Origin': 'https://other.test'}).status_code == 403
    assert client.post(revoke).status_code == 200
    assert client.get(url).status_code == 404
    assert client.get('/api/references').json()['items'] == []
    assert publish_reference(client, case_id).status_code == 200
    assert client.delete('/api/cases/' + case_id).status_code == 200
    assert client.get(url).status_code == 404


def test_local_direct_delete_revokes_patient_and_reference_snapshots(reference_setup, monkeypatch):
    from fastapi.testclient import TestClient
    import server

    client, case_id, _, _ = reference_setup
    approve(monkeypatch)
    login(client)
    shared = publish(client, case_id)
    published = publish_reference(client, case_id)
    assert published.status_code == 200, published.text
    reference_url = '/api/references/' + published.json()['id'] + '/model'
    patient_snapshot = patient_shares.SHARE_DIR / 'snapshots' / (shared['id'] + '.json')
    assert patient_snapshot.is_file()
    client.cookies.clear()
    assert client.get(shared['url']).status_code == 200
    assert client.post(shared['url'] + '/unlock', json={'access_code': shared['access_code']}).status_code == 200
    assert client.get(shared['url'] + '/model').status_code == 200
    assert client.get(reference_url).status_code == 200

    # The local route is callable directly, without the public gateway wrapper.
    local = TestClient(server.app, base_url='http://127.0.0.1')
    deleted = local.delete('/api/cases/' + case_id)
    assert deleted.status_code == 200, deleted.text
    assert not patient_snapshot.exists()
    assert not cases.case_dir(case_id).exists()
    assert client.get(shared['url']).status_code == 404
    assert client.get(shared['url'] + '/model').status_code == 404
    assert client.get(reference_url).status_code == 404
    assert client.get('/api/references').json()['items'] == []


def test_snapshot_tampering_fails_closed(reference_setup):
    client, case_id, _, _ = reference_setup
    login(client)
    published = publish_reference(client, case_id).json()
    entry = reference_cases.get_entry(published['id'])
    path = reference_cases.snapshot_path(entry['payload_sha256'])
    path.write_bytes(b'{}')
    assert client.get('/api/references/' + published['id'] + '/model').status_code == 503


def test_running_case_cannot_publish(reference_setup):
    client, case_id, _, _ = reference_setup
    login(client)
    cases.update_job(case_id, 'running', 'surface', 'generating')
    assert publish_reference(client, case_id).status_code == 409


def test_catalog_and_model_are_version_bound(reference_setup):
    client, case_id, _, _ = reference_setup
    login(client)
    first = publish_reference(client, case_id).json()
    url = '/api/references/' + first['id'] + '/model?version=' + first['version']
    assert client.get(url).status_code == 200
    assert publish_reference(client, case_id, tags=['sinusitis']).status_code == 200
    assert client.get(url).status_code == 409


def test_metadata_keeps_patient_access_scope(reference_setup, monkeypatch):
    from test_patient_shares import publish
    client, case_id, _, _ = reference_setup
    approve(monkeypatch)
    login(client)
    share = publish(client, case_id)
    client.cookies.clear()
    assert client.get(share['url'] + '/metadata').status_code == 401
    client.post(share['url'] + '/unlock', json={'access_code': share['access_code']})
    response = client.get(share['url'] + '/metadata')
    assert response.status_code == 200
    assert response.json()['flow_verified'] is True
    assert case_id not in response.text and 'reviewer' not in response.text
