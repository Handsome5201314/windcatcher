"""Back up or restore the personal display-preference database.

Use SQLite's online backup API to capture committed WAL data. Restore only to
an unused, isolated directory while the application is stopped; this tool never
merges account preferences or writes to the configured live database.
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

from pipeline import operator_preferences


_COLUMNS = {
    "user_id": ("TEXT", 0, 1),
    "version": ("INTEGER", 1, 0),
    "preferences_json": ("TEXT", 1, 0),
}


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("operator preferences JSON contains duplicate keys")
        result[key] = value
    return result


def _live_path() -> Path:
    return operator_preferences.configured_directory() / operator_preferences.DATABASE_NAME


def _snapshot_info(connection: sqlite3.Connection) -> dict[str, int]:
    check = connection.execute("PRAGMA integrity_check").fetchall()
    if check != [("ok",)]:
        raise ValueError("operator preferences database failed SQLite integrity_check")
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != operator_preferences.SCHEMA_VERSION:
        raise ValueError("operator preferences database schema version is unsupported")
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}
    if tables != {"operator_preferences"}:
        raise ValueError("operator preferences database has an incompatible table set")
    objects = connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('trigger', 'view')"
    ).fetchall()
    if objects:
        raise ValueError("operator preferences database contains unexpected triggers or views")
    columns = {row[1]: (row[2].upper(), row[3], row[5])
               for row in connection.execute("PRAGMA table_info(operator_preferences)")}
    if columns != _COLUMNS:
        raise ValueError("operator preferences database has incompatible columns")
    count = 0
    for user_id, row_version, encoded in connection.execute(
        "SELECT user_id,version,preferences_json FROM operator_preferences"
    ):
        if not isinstance(user_id, str) or not operator_preferences.USER_ID.fullmatch(user_id):
            raise ValueError("operator preferences database has an invalid account ID")
        if type(row_version) is not int or row_version <= 0:
            raise ValueError("operator preferences database has an invalid row version")
        try:
            operator_preferences.validate_preferences(json.loads(encoded, object_pairs_hook=_unique_object))
        except (TypeError, ValueError) as exc:
            raise ValueError("operator preferences database has an invalid preference record") from exc
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
        raise FileNotFoundError("operator preferences database source does not exist")
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
                    raise ValueError("operator preferences snapshot changed during verification")
        except Exception:
            for suffix in ("-wal", "-shm", ""):
                Path(str(destination) + suffix).unlink(missing_ok=True)
            raise
    return info


def backup_database(destination: str | Path) -> dict[str, int]:
    """Create a new verified snapshot of the configured live preferences DB."""
    return _snapshot(_live_path(), Path(destination))


def restore_database(source: str | Path, destination: str | Path) -> dict[str, int]:
    """Restore a verified snapshot to a new file outside the live directory."""
    target = Path(destination).expanduser()
    if target.is_symlink():
        raise FileExistsError("backup or restore destination already exists")
    target = target.resolve()
    if target.name != operator_preferences.DATABASE_NAME:
        raise ValueError("restore destination must end in operator-preferences.sqlite3")
    if target.parent == _live_path().expanduser().resolve().parent:
        raise ValueError("restore destination must be in an isolated directory")
    return _snapshot(Path(source), target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="snapshot the configured live preferences DB")
    backup.add_argument("destination", type=Path, help="new backup file")
    restore = commands.add_parser("restore", help="restore while the service is stopped")
    restore.add_argument("source", type=Path, help="verified backup file")
    restore.add_argument("destination", type=Path, help="new operator-preferences.sqlite3 target file")
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
