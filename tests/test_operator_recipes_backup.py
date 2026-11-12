"""Private recipe snapshots capture WAL and restore only into isolation."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pipeline import operator_recipes
from scripts import backup_operator_recipes


FIRST = "a" * 32
SECOND = "b" * 32


@pytest.fixture(autouse=True)
def isolated_recipe_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(operator_recipes.DIRECTORY_ENV, str(tmp_path / "identity"))


def _spec(step: str) -> dict:
    return {
        "suggested_steps": [step],
        "allowed_tools": ["inspect_cfd_case"],
        "trigger_condition": "manual",
    }


def _store(tmp_path):
    store = operator_recipes.OperatorRecipesStore(tmp_path / "identity")
    first = store.create(FIRST, _spec("qc"))
    second = store.create(SECOND, _spec("review"))
    store.revoke(SECOND, second["recipe_id"], 1)
    return store, first, second


def test_online_backup_captures_wal_and_restores_versions_and_revocations(tmp_path):
    store, first, second = _store(tmp_path)
    keeper = sqlite3.connect(store.path)
    try:
        assert keeper.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        keeper.execute("BEGIN")
        keeper.execute("SELECT COUNT(*) FROM operator_recipes").fetchone()
        saved = store.replace(FIRST, first["recipe_id"], 1, _spec("endplanes"))
        assert Path(str(store.path) + "-wal").is_file()

        backup = tmp_path / "private-backup" / "recipes-20261102.sqlite3"
        info = backup_operator_recipes.backup_database(backup)
        assert info == {"schema_version": operator_recipes.SCHEMA_VERSION, "records": 2}
        restored_path = tmp_path / "isolated-restore" / operator_recipes.DATABASE_NAME
        assert backup_operator_recipes.restore_database(backup, restored_path) == info
        restored = operator_recipes.OperatorRecipesStore(restored_path.parent)
        assert restored.get(FIRST, first["recipe_id"]) == saved
        assert restored.get(SECOND, second["recipe_id"]) == store.get(SECOND, second["recipe_id"])
        assert restored.get(SECOND, second["recipe_id"])["revoked"] is True
        with sqlite3.connect(restored_path) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        keeper.close()


def test_missing_source_invalid_spec_and_duplicate_keys_never_create_target(tmp_path):
    missing_backup = tmp_path / "backup" / "missing.sqlite3"
    with pytest.raises(FileNotFoundError):
        backup_operator_recipes.backup_database(missing_backup)
    assert not missing_backup.exists()

    store, first, _ = _store(tmp_path)
    rejected = tmp_path / "backup" / "rejected.sqlite3"
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE operator_recipes SET spec_json=? WHERE recipe_id=?",
                           (json.dumps({**_spec("qc"), "patient_name": "not allowed"}),
                            first["recipe_id"]))
    with pytest.raises(ValueError, match="invalid recipe spec"):
        backup_operator_recipes.backup_database(rejected)
    assert not rejected.exists()

    duplicate = ('{"suggested_steps":["qc"],"suggested_steps":["review"],'
                 '"allowed_tools":["inspect_cfd_case"],"trigger_condition":"manual"}')
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE operator_recipes SET spec_json=? WHERE recipe_id=?",
                           (duplicate, first["recipe_id"]))
    with pytest.raises(ValueError, match="invalid recipe spec"):
        backup_operator_recipes.backup_database(rejected)
    assert not rejected.exists()


def test_invalid_schema_and_record_metadata_are_rejected(tmp_path):
    invalid = tmp_path / "invalid.sqlite3"
    with sqlite3.connect(invalid) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER)")
    target = tmp_path / "restore" / operator_recipes.DATABASE_NAME
    with pytest.raises(ValueError, match="schema version"):
        backup_operator_recipes.restore_database(invalid, target)
    assert not target.exists()
    with sqlite3.connect(invalid) as connection:
        connection.execute("PRAGMA user_version=1")
    with pytest.raises(ValueError, match="incompatible object set"):
        backup_operator_recipes.restore_database(invalid, target)
    assert not target.exists()

    store, first, _ = _store(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE operator_recipes SET owner_id=? WHERE recipe_id=?",
                           ("foreign owner", first["recipe_id"]))
    rejected = tmp_path / "backup" / "bad-owner.sqlite3"
    with pytest.raises(ValueError, match="invalid owner ID"):
        backup_operator_recipes.backup_database(rejected)
    assert not rejected.exists()


@pytest.mark.parametrize("column,bad,reason", [
    ("recipe_id", "bad-id", "invalid recipe ID"),
    ("owner_id", "bad-owner", "invalid owner ID"),
    ("version", 0, "invalid row version"),
    ("revoked", 2, "invalid revocation state"),
])
def test_invalid_row_fields_are_rejected_before_backup(tmp_path, column, bad, reason):
    store, first, _ = _store(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(f"UPDATE operator_recipes SET {column}=? WHERE recipe_id=?",
                           (bad, first["recipe_id"]))
    rejected = tmp_path / "backup" / "invalid-row.sqlite3"
    with pytest.raises(ValueError, match=reason):
        backup_operator_recipes.backup_database(rejected)
    assert not rejected.exists()


def test_incompatible_table_definition_or_extra_objects_are_rejected(tmp_path):
    store, _, _ = _store(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute("CREATE INDEX unexpected_idx ON operator_recipes(version)")
    rejected = tmp_path / "backup" / "extra-index.sqlite3"
    with pytest.raises(ValueError, match="incompatible object set"):
        backup_operator_recipes.backup_database(rejected)
    assert not rejected.exists()


def test_existing_files_sidecars_and_live_directory_cannot_be_overwritten(tmp_path):
    store, _, _ = _store(tmp_path)
    backup = tmp_path / "backup" / "recipes.sqlite3"
    backup_operator_recipes.backup_database(backup)
    original = backup.read_bytes()
    with pytest.raises(FileExistsError):
        backup_operator_recipes.backup_database(backup)
    assert backup.read_bytes() == original

    restored = tmp_path / "restore" / operator_recipes.DATABASE_NAME
    backup_operator_recipes.restore_database(backup, restored)
    restored_bytes = restored.read_bytes()
    with pytest.raises(FileExistsError):
        backup_operator_recipes.restore_database(backup, restored)
    assert restored.read_bytes() == restored_bytes

    sidecar_target = tmp_path / "another" / operator_recipes.DATABASE_NAME
    sidecar_target.parent.mkdir()
    sidecar = Path(str(sidecar_target) + "-wal")
    sidecar.write_bytes(b"retain this sidecar")
    with pytest.raises(FileExistsError):
        backup_operator_recipes.restore_database(backup, sidecar_target)
    assert sidecar.read_bytes() == b"retain this sidecar"
    assert not sidecar_target.exists()

    with pytest.raises(ValueError, match="isolated directory"):
        backup_operator_recipes.restore_database(backup, store.path)
    with pytest.raises(ValueError, match="isolated directory"):
        backup_operator_recipes.restore_database(
            backup, store.directory / "nested" / operator_recipes.DATABASE_NAME)
    with pytest.raises(ValueError, match=operator_recipes.DATABASE_NAME):
        backup_operator_recipes.restore_database(backup, tmp_path / "wrong-name.sqlite3")


def test_cli_reports_counts_only_and_never_copies_identity_database(tmp_path, capsys):
    store, _, _ = _store(tmp_path)
    identity = store.directory / "identity.sqlite3"
    identity.write_bytes(b"identity sentinel")
    backup = tmp_path / "backup" / "recipes.sqlite3"
    assert backup_operator_recipes.main(["backup", str(backup)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {"status": "verified", "schema_version": 1, "records": 2}
    assert FIRST not in json.dumps(output)
    assert identity.read_bytes() == b"identity sentinel"
    with sqlite3.connect(backup) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
    assert tables == {"operator_recipes"}
    restored = tmp_path / "isolated" / operator_recipes.DATABASE_NAME
    assert backup_operator_recipes.main(["restore", str(backup), str(restored)]) == 0
    assert json.loads(capsys.readouterr().out) == output
