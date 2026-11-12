"""Online preference snapshots include WAL state and restore only in isolation."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pipeline import operator_preferences
from scripts import backup_operator_preferences


FIRST = "a" * 32
SECOND = "b" * 32


@pytest.fixture(autouse=True)
def isolated_preference_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(operator_preferences.DIRECTORY_ENV, str(tmp_path / "identity"))


def _preferences(step: str) -> dict:
    return {
        "view_pose": {"azimuth": 0.2, "elevation": -0.1, "zoom": 1.3},
        "display_ranges": {
            "velocity": {"min": 0, "max": 12.5, "unit": "m/s"},
            "pressure": {"min": -100, "max": 75, "unit": "Pa"},
        },
        "favorite_steps": [step],
    }


def _store(tmp_path):
    store = operator_preferences.OperatorPreferencesStore(tmp_path / "identity")
    store.replace(FIRST, 0, _preferences("segment"))
    store.replace(SECOND, 0, _preferences("review"))
    return store


def test_online_backup_captures_committed_wal_and_restores_both_accounts(tmp_path):
    store = _store(tmp_path)
    keeper = sqlite3.connect(store.path)
    try:
        assert keeper.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        keeper.execute("BEGIN")
        keeper.execute("SELECT COUNT(*) FROM operator_preferences").fetchone()
        saved = store.replace(FIRST, 1, _preferences("cfd_results"))
        assert Path(str(store.path) + "-wal").is_file()
        backup = tmp_path / "private-backup" / "operator-20261102.sqlite3"
        info = backup_operator_preferences.backup_database(backup)
        assert info == {"schema_version": operator_preferences.SCHEMA_VERSION, "records": 2}
        restored_path = tmp_path / "isolated-restore" / operator_preferences.DATABASE_NAME
        assert backup_operator_preferences.restore_database(backup, restored_path) == info
        restored = operator_preferences.OperatorPreferencesStore(restored_path.parent)
        assert restored.get(FIRST) == saved
        assert restored.get(SECOND) == store.get(SECOND)
        with sqlite3.connect(restored_path) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        keeper.close()


def test_missing_invalid_or_patient_shaped_source_fails_without_creating_target(tmp_path):
    missing_backup = tmp_path / "backup" / "missing.sqlite3"
    with pytest.raises(FileNotFoundError):
        backup_operator_preferences.backup_database(missing_backup)
    assert not missing_backup.exists()

    store = _store(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE operator_preferences SET preferences_json=? WHERE user_id=?",
                           (json.dumps({**_preferences("qc"), "patient_name": "not allowed"}), FIRST))
    rejected = tmp_path / "backup" / "rejected.sqlite3"
    with pytest.raises(ValueError, match="invalid preference record"):
        backup_operator_preferences.backup_database(rejected)
    assert not rejected.exists()

    hidden_duplicate = '{"favorite_steps":["patient name"],' + json.dumps(_preferences("qc"))[1:]
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE operator_preferences SET preferences_json=? WHERE user_id=?",
                           (hidden_duplicate, FIRST))
    with pytest.raises(ValueError, match="invalid preference record"):
        backup_operator_preferences.backup_database(rejected)
    assert not rejected.exists()

    invalid = tmp_path / "invalid.sqlite3"
    with sqlite3.connect(invalid) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER)")
    target = tmp_path / "restore" / operator_preferences.DATABASE_NAME
    with pytest.raises(ValueError, match="schema version"):
        backup_operator_preferences.restore_database(invalid, target)
    assert not target.exists()
    with sqlite3.connect(invalid) as connection:
        connection.execute("PRAGMA user_version=1")
    with pytest.raises(ValueError, match="incompatible table set"):
        backup_operator_preferences.restore_database(invalid, target)
    assert not target.exists()


def test_existing_files_sidecars_and_live_directory_cannot_be_overwritten(tmp_path):
    store = _store(tmp_path)
    backup = tmp_path / "backup" / "operator.sqlite3"
    backup_operator_preferences.backup_database(backup)
    original = backup.read_bytes()
    with pytest.raises(FileExistsError):
        backup_operator_preferences.backup_database(backup)
    assert backup.read_bytes() == original

    restored = tmp_path / "restore" / operator_preferences.DATABASE_NAME
    backup_operator_preferences.restore_database(backup, restored)
    restored_bytes = restored.read_bytes()
    with pytest.raises(FileExistsError):
        backup_operator_preferences.restore_database(backup, restored)
    assert restored.read_bytes() == restored_bytes

    sidecar_target = tmp_path / "another" / operator_preferences.DATABASE_NAME
    sidecar_target.parent.mkdir()
    sidecar = Path(str(sidecar_target) + "-wal")
    sidecar.write_bytes(b"retain this sidecar")
    with pytest.raises(FileExistsError):
        backup_operator_preferences.restore_database(backup, sidecar_target)
    assert sidecar.read_bytes() == b"retain this sidecar"
    assert not sidecar_target.exists()

    with pytest.raises(ValueError, match="isolated directory"):
        backup_operator_preferences.restore_database(backup, store.path)
    with pytest.raises(ValueError, match=operator_preferences.DATABASE_NAME):
        backup_operator_preferences.restore_database(backup, tmp_path / "wrong-name.sqlite3")


def test_cli_reports_counts_only_and_snapshot_does_not_include_identity_db(tmp_path, capsys):
    store = _store(tmp_path)
    identity = store.directory / "identity.sqlite3"
    identity.write_bytes(b"identity sentinel")
    backup = tmp_path / "backup" / "operator.sqlite3"
    assert backup_operator_preferences.main(["backup", str(backup)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {"status": "verified", "schema_version": 1, "records": 2}
    assert FIRST not in json.dumps(output)
    assert identity.read_bytes() == b"identity sentinel"
    with sqlite3.connect(backup) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
    assert tables == {"operator_preferences"}
    restored = tmp_path / "isolated" / operator_preferences.DATABASE_NAME
    assert backup_operator_preferences.main(["restore", str(backup), str(restored)]) == 0
    assert json.loads(capsys.readouterr().out) == output
