"""Durable operator metadata only; every test uses an isolated database."""
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest
from fastapi.testclient import TestClient

import cloud
import server
from pipeline import activity_monitor as monitor, cases, config


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(monitor.DIRECTORY_ENV, str(tmp_path / 'monitor'))
    monkeypatch.setenv('WINDCATCHER_MULTIUSER', '0')
    monkeypatch.setenv('WINDCATCHER_IDENTITY_DIR', str(tmp_path / 'identity'))
    for module in (config, cases, server):
        monkeypatch.setattr(module, 'CASES_DIR', tmp_path / 'cases')
    monkeypatch.setenv('WINDCATCHER_DEPLOYMENT', 'public')
    monkeypatch.setenv('WINDCATCHER_PUBLIC_URL', 'https://workbench.test')
    monkeypatch.setenv('WINDCATCHER_PUBLIC_USERNAME', 'operator')
    monkeypatch.setenv('WINDCATCHER_PUBLIC_PASSWORD', 'test-only-password-long-enough')
    monkeypatch.setenv('WINDCATCHER_PUBLIC_SESSION_SECRET', 'test-only-secret-at-least-32-characters')
    return tmp_path / 'monitor' / 'activity.sqlite3'


def event(**updates):
    result = dict(kind='ui', action='click', target='btn-view-3d', route='work',
                  session_id='00000000-0000-4000-8000-000000000001',
                  event_id='00000000-0000-4000-8000-000000000002')
    result.update(updates)
    return result


def test_events_remain_after_long_time_without_silent_pruning(monkeypatch):
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(monitor, '_now', lambda: now - timedelta(days=365))
    monitor.record_event(event())
    monkeypatch.setattr(monitor, '_now', lambda: now)
    assert monitor.get_status()['total_events'] == 1
    assert monitor.get_status()['retention_days'] is None
    assert monitor.get_status()['max_events'] is None


def test_retried_events_are_idempotent_and_versioned(isolated):
    first = monitor.record_event(event(details={'checked': True}))
    assert monitor.record_event(event(details={'checked': True})) == first
    saved = monitor.query_events()['events']
    assert len(saved) == 1
    assert saved[0]['event_id'] == event()['event_id']
    assert saved[0]['schema_version'] == 1
    assert saved[0]['app_version']
    with sqlite3.connect(isolated) as connection:
        assert connection.execute('PRAGMA quick_check').fetchone()[0] == 'ok'


def test_existing_database_is_migrated_without_losing_events(tmp_path):
    path = tmp_path / 'monitor' / 'activity.sqlite3'
    path.parent.mkdir()
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, kind TEXT NOT NULL, action TEXT NOT NULL, route TEXT, case_id TEXT, session_id TEXT, request_id TEXT, target TEXT, details TEXT NOT NULL, is_error INTEGER NOT NULL)')
        connection.execute("INSERT INTO events (timestamp, kind, action, details, is_error) VALUES ('2026-01-01T00:00:00Z', 'ui', 'click', '{}', 0)")
    old = monitor.query_events()['events']
    assert len(old) == 1 and old[0]['action'] == 'click' and old[0]['schema_version'] == 0
    monitor.record_event(event(event_id='00000000-0000-4000-8000-000000000003'))
    assert monitor.get_status()['total_events'] == 2


def test_online_backup_contains_committed_events_and_is_not_the_live_file(tmp_path, isolated):
    monitor.record_event(event())
    destination = tmp_path / 'backups' / 'activity.sqlite3'
    assert monitor.backup_database(destination) == destination
    with sqlite3.connect(destination) as connection:
        assert connection.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert connection.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 1
    with pytest.raises(ValueError, match='cannot replace'):
        monitor.backup_database(isolated)


def test_multiuser_backup_excludes_expired_events_without_prior_read(tmp_path, monkeypatch, isolated):
    monkeypatch.setenv('WINDCATCHER_MULTIUSER', '1')
    now = datetime(2026, 11, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(monitor, '_now', lambda: now - timedelta(days=181))
    monitor.record_event(event())
    monkeypatch.setattr(monitor, '_now', lambda: now - timedelta(days=180))
    kept_id = monitor.record_event(event(event_id='00000000-0000-4000-8000-000000000003'))
    monkeypatch.setattr(monitor, '_now', lambda: now)

    destination = tmp_path / 'backup' / 'activity.sqlite3'
    monitor.backup_database(destination)
    with sqlite3.connect(destination) as connection:
        assert connection.execute('SELECT id FROM events').fetchall() == [(kept_id,)]
        assert connection.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    with sqlite3.connect(isolated) as connection:
        assert connection.execute('SELECT id FROM events').fetchall() == [(kept_id,)]


def test_online_backup_never_overwrites_an_existing_destination(tmp_path):
    destination = tmp_path / 'existing.sqlite3'
    destination.write_bytes(b'keep this existing file')
    with pytest.raises(FileExistsError, match='already exists'):
        monitor.backup_database(destination)
    assert destination.read_bytes() == b'keep this existing file'


@pytest.fixture
def client():
    # Deliberately do not enter lifespan or recover real processing jobs.
    return TestClient(cloud.create_app(), base_url='https://workbench.test')


def login(client):
    assert client.post('/login', data={'username': 'operator',
        'password': 'test-only-password-long-enough'}, follow_redirects=False).status_code == 303


def test_only_authenticated_public_operator_can_send_and_read_events(client):
    payload = {'events': [event(details={'checked': True})]}
    assert client.post('/api/monitor/events', json=payload).status_code == 401
    assert client.get('/api/monitor/status').status_code == 401
    login(client)
    assert 'window.WindcatcherActivityAccess = true' in client.get('/').text
    assert client.get('/static/activity-monitor.js').status_code == 200
    assert 'window.WindcatcherActivityAccess = true' in client.get('/').text
    response = client.post('/api/monitor/events', json=payload,
        headers={'Origin': 'https://workbench.test'})
    assert response.status_code == 200
    assert response.json()['accepted'] == 1
    assert client.get('/api/monitor/status').json()['total_events'] == 1
    assert client.get('/api/monitor/events').json()['events'][0]['target'] == 'btn-view-3d'
    assert client.get('/api/monitor/unrecognized').status_code == 403


@pytest.mark.parametrize('method,path', [('GET', '/api/monitor/status'),
    ('GET', '/api/monitor/events'), ('POST', '/api/monitor/events')])
def test_public_monitor_rejects_cross_site_even_when_logged_in(client, method, path):
    login(client)
    assert client.request(method, path, headers={'Origin': 'https://external.test'}).status_code == 403
    assert client.request(method, path, headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    assert monitor.get_status()['total_events'] == 0


def test_public_event_rejects_free_text_actions_and_oversized_bodies(client):
    login(client)
    payload = event()
    payload['action'] = 'patient-John-Smith'
    assert client.post('/api/monitor/events', json={'events': [payload]}).status_code == 422
    assert client.post('/api/monitor/events', content='x' * 65537,
        headers={'Content-Type': 'application/json'}).status_code == 413
    assert monitor.get_status()['total_events'] == 0
