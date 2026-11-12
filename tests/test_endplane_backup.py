"""The learning ledger survives a WAL-aware backup and a guarded restore."""

from __future__ import annotations

import json
import sqlite3

import pytest

from pipeline import endplane_history
from scripts import backup_endplane_history


CASE_ID = "synthetic-backup"


@pytest.fixture(autouse=True)
def isolated_learning_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(endplane_history.DIRECTORY_ENV, str(tmp_path / "修订记录"))


def _plan(index: int) -> dict:
    return {"scope": "main_airway", "mask_version": 3,
            "anterior_index": index, "posterior_index": 8}


def test_backup_includes_wal_and_restore_preserves_every_status(tmp_path, monkeypatch):
    first = endplane_history.begin_revision(CASE_ID, "main_airway", 3, None, _plan(20))
    endplane_history.commit_revision(first)
    pending = endplane_history.begin_revision(CASE_ID, "main_airway", 3, _plan(20), _plan(21))
    live_path = endplane_history._path()
    observer = sqlite3.connect(live_path)
    try:
        assert observer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        source_rows = endplane_history.list_revisions(CASE_ID, include_pending=True)
        backup_path = tmp_path / "快照" / "learning-20261102.sqlite3"
        info = backup_endplane_history.backup_database(backup_path)
        assert info == {"schema_version": 1, "revisions": 2, "committed": 1}
        assert backup_path.is_file()

        restored_path = tmp_path / "restored" / "learning.sqlite3"
        assert backup_endplane_history.restore_database(backup_path, restored_path) == info
        monkeypatch.setenv(endplane_history.DIRECTORY_ENV, str(restored_path.parent))
        restored_rows = endplane_history.list_revisions(CASE_ID, include_pending=True)
        assert restored_rows == source_rows
        assert [row["id"] for row in restored_rows] == [first, pending]
        with sqlite3.connect(restored_path) as restored:
            assert restored.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        observer.close()


def test_missing_source_and_existing_targets_fail_without_mutation(tmp_path):
    missing = endplane_history._path()
    backup_path = tmp_path / "backup.sqlite3"
    with pytest.raises(FileNotFoundError):
        backup_endplane_history.backup_database(backup_path)
    assert not missing.exists() and not backup_path.exists()

    revision = endplane_history.begin_revision(CASE_ID, "main_airway", 3, None, _plan(20))
    endplane_history.commit_revision(revision)
    backup_endplane_history.backup_database(backup_path)
    original = backup_path.read_bytes()
    with pytest.raises(FileExistsError):
        backup_endplane_history.backup_database(backup_path)
    assert backup_path.read_bytes() == original

    restored_path = tmp_path / "restored" / "learning.sqlite3"
    backup_endplane_history.restore_database(backup_path, restored_path)
    original_restored = restored_path.read_bytes()
    with pytest.raises(FileExistsError):
        backup_endplane_history.restore_database(backup_path, restored_path)
    assert restored_path.read_bytes() == original_restored


def test_restore_rejects_bad_schema_and_orphan_sidecar(tmp_path):
    invalid = tmp_path / "invalid.sqlite3"
    with sqlite3.connect(invalid) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER)")
    destination = tmp_path / "restore" / "learning.sqlite3"
    with pytest.raises(ValueError, match="incompatible revision table"):
        backup_endplane_history.restore_database(invalid, destination)
    assert not destination.exists()

    revision = endplane_history.begin_revision(CASE_ID, "main_airway", 3, None, _plan(20))
    endplane_history.commit_revision(revision)
    backup_path = tmp_path / "backup.sqlite3"
    backup_endplane_history.backup_database(backup_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    sidecar = destination.with_name(destination.name + "-wal")
    sidecar.write_bytes(b"old sidecar")
    with pytest.raises(FileExistsError):
        backup_endplane_history.restore_database(backup_path, destination)
    assert sidecar.read_bytes() == b"old sidecar"
    assert not destination.exists()


def test_cli_prints_only_verified_counts(tmp_path, capsys):
    revision = endplane_history.begin_revision(CASE_ID, "main_airway", 3, None, _plan(20))
    endplane_history.commit_revision(revision)
    backup_path = tmp_path / "backup.sqlite3"
    assert backup_endplane_history.main(["backup", str(backup_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "verified", "schema_version": 1, "revisions": 1, "committed": 1,
    }
