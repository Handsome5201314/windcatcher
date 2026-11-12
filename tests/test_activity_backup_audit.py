"""An old snapshot remains expired even after the live database is pruned."""
from datetime import datetime, timedelta, timezone
import sqlite3

from pipeline import activity_monitor as monitor
from pipeline.activity_backup_audit import audit_directory


def test_audit_detects_expired_rows_in_old_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv(monitor.DIRECTORY_ENV, str(tmp_path / "live"))
    monkeypatch.delenv("WINDCATCHER_MULTIUSER", raising=False)
    now = datetime(2026, 11, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(monitor, "_now", lambda: now - timedelta(days=181))
    monitor.record_event({"kind": "ui", "action": "click"})
    backup_dir = tmp_path / "backups"
    old = monitor.backup_database(backup_dir / "old.sqlite3")
    monkeypatch.setenv("WINDCATCHER_MULTIUSER", "1")
    monkeypatch.setattr(monitor, "_now", lambda: now)
    assert monitor.prune_expired_events() == 1
    fresh = monitor.backup_database(backup_dir / "fresh.sqlite3")
    result = audit_directory(backup_dir, now=now)
    assert result["status"] == "review_required"
    assert [(item["file"], item["expired_events"]) for item in result["files"]] == [
        (fresh.name, 0), (old.name, 1)]
    with sqlite3.connect(old) as database:
        assert database.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_audit_flags_invalid_backup_without_modifying_it(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    bad = backup_dir / "broken.sqlite3"
    bad.write_bytes(b"not sqlite")
    result = audit_directory(backup_dir, now=datetime(2026, 11, 2, tzinfo=timezone.utc))
    assert result["status"] == "review_required"
    assert result["files"][0]["status"] == "review_required"
    assert bad.read_bytes() == b"not sqlite"


def test_audit_refuses_backup_with_unread_wal_sidecar(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    backup = backup_dir / "activity.sqlite3"
    backup.write_bytes(b"main database bytes")
    sidecar = backup_dir / "activity.sqlite3-wal"
    sidecar.write_bytes(b"unread committed rows")
    result = audit_directory(backup_dir)
    assert result["files"][0]["reason"] == "sqlite_sidecar_present"
    assert sidecar.read_bytes() == b"unread committed rows"


def test_future_event_requires_review(tmp_path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as database:
        database.execute("CREATE TABLE events (timestamp TEXT NOT NULL)")
        database.execute("INSERT INTO events VALUES ('2026-11-03T00:00:00Z')")
    result = audit_directory(tmp_path, now=datetime(2026, 11, 2, tzinfo=timezone.utc))
    assert result["status"] == "review_required"
    assert result["files"][0]["future_events"] == 1


def test_empty_backup_directory_is_not_a_release_pass(tmp_path):
    assert audit_directory(tmp_path)["status"] == "empty"
