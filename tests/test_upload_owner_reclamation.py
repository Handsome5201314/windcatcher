"""Upload ownership must not outlive abandoned staging sessions."""

import server
from pipeline import uploads
from test_cloud_multiuser import workspace


def _start(client):
    response = client.post('/api/uploads', json={
        'files': [{'path': 'synthetic.dcm', 'size': 3}], 'synthetic': True,
    })
    assert response.status_code == 200, response.text
    return response.json()['upload_id']


def test_cancel_releases_unfinished_upload_owner_and_retry_survives_db_failure(workspace, monkeypatch):
    app, _, member, admin_id, member_id, root = workspace
    upload_id = _start(member)
    identity = app.state.identity
    assert identity.authorize_upload(member_id, upload_id)
    assert not identity.forget_unfinished_upload(upload_id, admin_id)
    assert identity.authorize_upload(member_id, upload_id)
    original = identity.forget_unfinished_upload

    def fail_once(_upload_id, _user_id):
        raise OSError('synthetic identity write failure')

    monkeypatch.setattr(identity, 'forget_unfinished_upload', fail_once)
    first = member.post(f'/api/uploads/{upload_id}/cancel')
    assert first.status_code == 503, first.text
    assert identity.authorize_upload(member_id, upload_id)
    assert not (root / ('.windcatcher-chunks-' + upload_id)).exists()

    monkeypatch.setattr(identity, 'forget_unfinished_upload', original)
    second = member.post(f'/api/uploads/{upload_id}/cancel')
    assert second.status_code == 200, second.text
    assert not identity.authorize_upload(member_id, upload_id)
    assert member.post(f'/api/uploads/{upload_id}/complete').status_code == 404


def test_expired_retry_releases_owner_after_transient_db_failure(workspace, monkeypatch):
    app, _, member, _, member_id, root = workspace
    upload_id = _start(member)
    identity = app.state.identity
    now = uploads.time.time()
    monkeypatch.setattr(uploads.time, 'time', lambda: now + uploads.SESSION_TTL + 1)
    original = identity.forget_unfinished_upload

    def fail_once(_upload_id, _user_id):
        raise OSError('synthetic identity write failure')

    monkeypatch.setattr(identity, 'forget_unfinished_upload', fail_once)
    first = member.post(f'/api/uploads/{upload_id}/chunks?offset=0', content=b'a')
    assert first.status_code == 503, first.text
    assert identity.authorize_upload(member_id, upload_id)
    assert not (root / ('.windcatcher-chunks-' + upload_id)).exists()

    monkeypatch.setattr(identity, 'forget_unfinished_upload', original)
    second = member.post(f'/api/uploads/{upload_id}/chunks?offset=0', content=b'a')
    assert second.status_code == 404, second.text
    assert not identity.authorize_upload(member_id, upload_id)


def test_new_upload_reaps_expired_owner_but_preserves_live_session(workspace, monkeypatch):
    app, admin, member, _, member_id, root = workspace
    expired_id = _start(member)
    active_id = _start(member)
    active_folder = root / ('.windcatcher-chunks-' + active_id)
    saved = uploads._load(active_folder)
    now = uploads.time.time()
    saved['updated_at'] = now + uploads.SESSION_TTL
    uploads._save(active_folder, saved)
    monkeypatch.setattr(uploads.time, 'time', lambda: now + uploads.SESSION_TTL + 1)

    _start(admin)
    assert not app.state.identity.authorize_upload(member_id, expired_id)
    assert app.state.identity.authorize_upload(member_id, active_id)
    assert active_folder.is_dir()


def test_cancel_completed_staging_preserves_case_receipt(workspace, monkeypatch):
    app, _, member, _, member_id, _ = workspace
    upload_id = _start(member)
    assert member.post(f'/api/uploads/{upload_id}/chunks?offset=0', content=b'abc').status_code == 200
    monkeypatch.setattr(server, '_prepare_case', lambda *_args: None)
    completed = member.post(f'/api/uploads/{upload_id}/complete')
    assert completed.status_code == 200, completed.text
    case_id = completed.json()['case_id']
    assert app.state.identity.upload_case_id(member_id, upload_id) == case_id

    assert member.post(f'/api/uploads/{upload_id}/cancel').status_code == 200
    assert app.state.identity.upload_case_id(member_id, upload_id) == case_id
    _start(member)  # Opportunistic stale-owner pruning must keep committed receipts.
    assert app.state.identity.upload_case_id(member_id, upload_id) == case_id
    retry = member.post(f'/api/uploads/{upload_id}/complete')
    assert retry.status_code == 200, retry.text
    assert retry.json() == completed.json()
