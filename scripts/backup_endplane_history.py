"""Back up or restore the saved-endplane ledger with SQLite's online backup API.

The ledger is process evidence, not a clinical label or a training dataset.
Backups include every revision status and never inspect CT or case files.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import endplane_history


_REQUIRED_COLUMNS = {
    "id", "case_id", "scope", "mask_version", "previous_plan_json",
    "confirmed_plan_json", "plan_hash", "source", "status", "actor_id",
    "recorded_at", "committed_at",
}


def _read_snapshot_info(connection: sqlite3.Connection) -> dict[str, int]:
    result = connection.execute("PRAGMA quick_check").fetchone()
    if result is None or result[0] != "ok":
        raise ValueError("learning database failed SQLite quick_check")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(endplane_revisions)")}
    if not _REQUIRED_COLUMNS.issubset(columns):
        raise ValueError("learning database has an incompatible revision table")
    schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if schema_version < 1 or schema_version > endplane_history.SCHEMA_VERSION:
        raise ValueError("learning database schema version is unsupported")
    total = int(connection.execute("SELECT COUNT(*) FROM endplane_revisions").fetchone()[0])
    committed = int(connection.execute(
        "SELECT COUNT(*) FROM endplane_revisions WHERE status='committed'"
    ).fetchone()[0])
    return {"schema_version": schema_version, "revisions": total, "committed": committed}


def _snapshot(source: Path, destination: Path) -> dict[str, int]:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if source == destination:
        raise ValueError("source and destination must differ")
    if not source.is_file():
        raise FileNotFoundError("learning database source does not exist")
    for suffix in ("", "-wal", "-shm"):
        if Path(str(destination) + suffix).exists():
            raise FileExistsError("backup or restore destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)

    # URI mode=ro prevents a missing source from silently becoming an empty DB.
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=10) as origin:
        _read_snapshot_info(origin)
        try:
            with destination.open("xb"):
                pass
        except FileExistsError:
            raise FileExistsError("backup or restore destination already exists") from None
        try:
            with sqlite3.connect(destination, timeout=10) as target:
                origin.backup(target)
                info = _read_snapshot_info(target)
            # Verify the closed file is independently readable before success.
            with sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True) as verified:
                if _read_snapshot_info(verified) != info:
                    raise ValueError("learning database snapshot changed during verification")
        except Exception:
            for suffix in ("-wal", "-shm", ""):
                Path(str(destination) + suffix).unlink(missing_ok=True)
            raise
    return info


def backup_database(destination: str | Path) -> dict[str, int]:
    """Create a new, verified snapshot of the configured live learning ledger."""
    return _snapshot(endplane_history._path(), Path(destination))


def restore_database(source: str | Path, destination: str | Path) -> dict[str, int]:
    """Restore to an explicitly named, nonexistent target; stop the app first."""
    target = Path(destination)
    if target.name != endplane_history.DATABASE_NAME:
        raise ValueError("restore destination must end in learning.sqlite3")
    return _snapshot(Path(source), target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    backup = subcommands.add_parser("backup", help="snapshot the configured live ledger")
    backup.add_argument("destination", type=Path, help="new backup file")
    restore = subcommands.add_parser("restore", help="restore while the service is stopped")
    restore.add_argument("source", type=Path, help="verified backup file")
    restore.add_argument("destination", type=Path, help="new learning.sqlite3 target file")
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            info = backup_database(args.destination)
        else:
            info = restore_database(args.source, args.destination)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(2, str(exc) + chr(10))
    print(json.dumps({"status": "verified", **info}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
