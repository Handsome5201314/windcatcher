"""Back up or restore the private, inert operator-recipe database.

Capture committed WAL data with SQLite's online backup API. Restore only to a
new file in an isolated directory while the application is stopped; never
merge recipes or write to the configured live database.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import operator_recipes


_COLUMNS = [
    (0, "recipe_id", "TEXT", 0, None, 1),
    (1, "owner_id", "TEXT", 1, None, 0),
    (2, "version", "INTEGER", 1, None, 0),
    (3, "spec_json", "TEXT", 1, None, 0),
    (4, "revoked", "INTEGER", 1, "0", 0),
]
_TABLE_SQL = """
    CREATE TABLE operator_recipes (
        recipe_id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0),
        spec_json TEXT NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1))
    )
"""
_INDEX_SQL = "CREATE INDEX operator_recipes_owner ON operator_recipes (owner_id)"


def _normalized_sql(statement: str) -> str:
    return " ".join(statement.split()).casefold()


def _live_path() -> Path:
    return operator_recipes.configured_directory() / operator_recipes.DATABASE_NAME


def _snapshot_info(connection: sqlite3.Connection) -> dict[str, int]:
    check = connection.execute("PRAGMA integrity_check").fetchall()
    if check != [("ok",)]:
        raise ValueError("operator recipes database failed SQLite integrity_check")
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != operator_recipes.SCHEMA_VERSION:
        raise ValueError("operator recipes database schema version is unsupported")

    objects = {
        name: (kind, sql)
        for name, kind, sql in connection.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    }
    if set(objects) != {"operator_recipes", "operator_recipes_owner"}:
        raise ValueError("operator recipes database has an incompatible object set")
    for name, kind, expected in (
        ("operator_recipes", "table", _TABLE_SQL),
        ("operator_recipes_owner", "index", _INDEX_SQL),
    ):
        actual_kind, actual_sql = objects[name]
        if (actual_kind != kind or type(actual_sql) is not str
                or _normalized_sql(actual_sql) != _normalized_sql(expected)):
            raise ValueError("operator recipes database has an incompatible schema")
    columns = connection.execute("PRAGMA table_info(operator_recipes)").fetchall()
    if columns != _COLUMNS:
        raise ValueError("operator recipes database has incompatible columns")

    count = 0
    for recipe_id, owner_id, row_version, encoded, revoked in connection.execute(
        "SELECT recipe_id,owner_id,version,spec_json,revoked FROM operator_recipes"
    ):
        if not isinstance(recipe_id, str) or not operator_recipes.RECIPE_ID.fullmatch(recipe_id):
            raise ValueError("operator recipes database has an invalid recipe ID")
        if not isinstance(owner_id, str) or not operator_recipes.OWNER_ID.fullmatch(owner_id):
            raise ValueError("operator recipes database has an invalid owner ID")
        if type(row_version) is not int or not 1 <= row_version < 2**63:
            raise ValueError("operator recipes database has an invalid row version")
        if type(revoked) is not int or revoked not in (0, 1):
            raise ValueError("operator recipes database has an invalid revocation state")
        try:
            operator_recipes.parse_spec_json(encoded)
        except operator_recipes.RecipeError as exc:
            raise ValueError("operator recipes database has an invalid recipe spec") from exc
        count += 1
    return {"schema_version": version, "records": count}


def _snapshot(source: Path, destination: Path) -> dict[str, int]:
    source = source.expanduser().resolve()
    destination = destination.expanduser()
    if destination.is_symlink():
        raise FileExistsError("backup or restore destination already exists")
    destination = destination.resolve()
    if source == destination:
        raise ValueError("source and destination must differ")
    if not source.is_file():
        raise FileNotFoundError("operator recipes database source does not exist")
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(destination) + suffix)
        if candidate.exists() or candidate.is_symlink():
            raise FileExistsError("backup or restore destination already exists")

    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=10)) as origin:
        _snapshot_info(origin)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb"):
            pass
        try:
            if os.name != "nt":
                destination.chmod(0o600)
            with closing(sqlite3.connect(destination, timeout=10)) as target:
                origin.backup(target)
                info = _snapshot_info(target)
            with closing(sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True, timeout=10)) as verified:
                if _snapshot_info(verified) != info:
                    raise ValueError("operator recipes snapshot changed during verification")
        except Exception:
            for suffix in ("-wal", "-shm", ""):
                Path(str(destination) + suffix).unlink(missing_ok=True)
            raise
    return info


def backup_database(destination: str | Path) -> dict[str, int]:
    """Create a new verified snapshot of the configured live recipe database."""
    return _snapshot(_live_path(), Path(destination))


def restore_database(source: str | Path, destination: str | Path) -> dict[str, int]:
    """Restore a verified snapshot to a new file outside the live directory."""
    target = Path(destination).expanduser()
    if target.is_symlink():
        raise FileExistsError("backup or restore destination already exists")
    target = target.resolve()
    if target.name != operator_recipes.DATABASE_NAME:
        raise ValueError("restore destination must end in operator-recipes.sqlite3")
    live_directory = _live_path().expanduser().resolve().parent
    if target.parent == live_directory or live_directory in target.parents:
        raise ValueError("restore destination must be in an isolated directory")
    return _snapshot(Path(source), target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="snapshot the configured live recipe database")
    backup.add_argument("destination", type=Path, help="new backup file")
    restore = commands.add_parser("restore", help="restore while the service is stopped")
    restore.add_argument("source", type=Path, help="verified backup file")
    restore.add_argument("destination", type=Path, help="new operator-recipes.sqlite3 target file")
    args = parser.parse_args(argv)
    try:
        info = (backup_database(args.destination) if args.command == "backup" else
                restore_database(args.source, args.destination))
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(2, str(exc) + chr(10))
    print(json.dumps({"status": "verified", **info}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
