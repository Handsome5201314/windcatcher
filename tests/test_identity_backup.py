"""Identity snapshots preserve grants and tombstones, and restore revokes sessions."""

from __future__ import annotations

import json
import sqlite3

import pytest

from pipeline import access_identity
from scripts import backup_identity


OLD = "case-20261102-123456-aaaa"
RETIRED = "case-20261102-123457-bbbb"
PASSWORD = "test-admin-password-long-enough"


@pytest.fixture(autouse=True)
def isolated_identity_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(access_identity.DIRECTORY_ENV, str(tmp_path / "identity"))


def _populated_store(tmp_path):
    store = access_identity.IdentityStore(tmp_path / "identity")
    admin = store.bootstrap_admin("backup-admin", PASSWORD, [OLD])["id"]
    member = store.create_user(admin, "backup-member", "test-member-password-long-enough")["id"]
    store.bind_case_owner(RETIRED, member)
    store.retire_case(member, RETIRED)
    token = store.issue_session(admin)
    return store, admin, member, token


def test_wal_snapshot_and_restoration_preserve_acl_but_revoke_sessions(tmp_path):
    store, admin, member, token = _populated_store(tmp_path)
    assert store.resolve_session(token)["id"] == admin
    backup_path = tmp_path / "private-backup" / "identity-20261102.sqlite3"
    info = backup_identity.backup_database(backup_path)
    assert info == {"schema_version": access_identity.SCHEMA_VERSION,
                    "users": 3, "grants": 1, "tombstones": 1}
    assert backup_path.is_file()

    restored_path = tmp_path / "restore" / "identity.sqlite3"
    assert backup_identity.restore_database(backup_path, restored_path) == info
    restored = access_identity.IdentityStore(restored_path.parent)
    assert restored.authenticate("backup-admin", PASSWORD)["id"] == admin
    assert restored.authorize_case(admin, OLD, "manage")
    assert not restored.authorize_case(member, RETIRED, "view")
    assert restored.resolve_session(token) is None
    with pytest.raises(PermissionError, match="retired"):
        restored.bind_case_owner(RETIRED, member)
    with sqlite3.connect(restored_path) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_missing_or_incompatible_source_and_existing_targets_fail_closed(tmp_path):
    missing = tmp_path / "identity" / "identity.sqlite3"
    backup_path = tmp_path / "private-backup" / "snapshot.sqlite3"
    with pytest.raises(FileNotFoundError):
        backup_identity.backup_database(backup_path)
    assert not missing.exists() and not backup_path.exists()

    _populated_store(tmp_path)
    backup_identity.backup_database(backup_path)
    original = backup_path.read_bytes()
    with pytest.raises(FileExistsError):
        backup_identity.backup_database(backup_path)
    assert backup_path.read_bytes() == original

    restored_path = tmp_path / "restore" / "identity.sqlite3"
    backup_identity.restore_database(backup_path, restored_path)
    restored_bytes = restored_path.read_bytes()
    with pytest.raises(FileExistsError):
        backup_identity.restore_database(backup_path, restored_path)
    assert restored_path.read_bytes() == restored_bytes

    invalid = tmp_path / "wrong.sqlite3"
    with sqlite3.connect(invalid) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER)")
    wrong_target = tmp_path / "other" / "identity.sqlite3"
    with pytest.raises(ValueError, match="schema version"):
        backup_identity.restore_database(invalid, wrong_target)
    assert not wrong_target.exists()
    with pytest.raises(ValueError, match="identity.sqlite3"):
        backup_identity.restore_database(backup_path, tmp_path / "other" / "wrong.sqlite3")


def test_restore_rejects_existing_sidecar_and_cli_prints_counts_only(tmp_path, capsys):
    _populated_store(tmp_path)
    backup_path = tmp_path / "snapshot.sqlite3"
    assert backup_identity.main(["backup", str(backup_path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {"status": "verified", "schema_version": access_identity.SCHEMA_VERSION,
                      "users": 3, "grants": 1, "tombstones": 1}
    assert "backup-admin" not in json.dumps(output)

    target = tmp_path / "restore" / "identity.sqlite3"
    target.parent.mkdir()
    sidecar = target.with_name(target.name + "-wal")
    sidecar.write_bytes(b"old sidecar")
    with pytest.raises(FileExistsError):
        backup_identity.restore_database(backup_path, target)
    assert sidecar.read_bytes() == b"old sidecar"
    assert not target.exists()
