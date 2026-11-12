"""Back up or restore the personal-account database without exposing credentials.

The SQLite snapshot includes password hashes, session hashes, case grants and
tombstones. Keep it in controlled storage and restore only while the app is off.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import access_identity
from pipeline.config import APP_DIR


_REQUIRED_COLUMNS = {
    "users": {"id", "username", "password_salt", "password_digest", "kind", "global_role", "active"},
    "sessions": {"token_hash", "user_id", "expires_at", "revoked_at"},
    "case_grants": {"case_id", "user_id", "role", "granted_by"},
    "upload_owners": {"upload_id", "user_id", "case_id"},
    "case_tombstones": {"case_id", "retired_by", "retired_at"},
    "settings": {"key", "value"},
}


def _live_path() -> Path:
    directory = os.environ.get(access_identity.DIRECTORY_ENV)
    return (Path(directory) if directory else APP_DIR / ".local-identity") / access_identity.DATABASE_NAME


def _read_snapshot_info(connection: sqlite3.Connection) -> dict[str, int]:
    result = connection.execute("PRAGMA quick_check").fetchone()
    if result is None or result[0] != "ok":
        raise ValueError("identity database failed SQLite quick_check")
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != access_identity.SCHEMA_VERSION:
        raise ValueError("identity database schema version is unsupported")
    for table, required in _REQUIRED_COLUMNS.items():
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if not required.issubset(columns):
            raise ValueError("identity database has an incompatible table: " + table)
    markers = {row[0] for row in connection.execute(
        "SELECT key FROM settings WHERE key IN ('bootstrap_user_id', 'mcp_user_id')"
    )}
    if markers != {"bootstrap_user_id", "mcp_user_id"}:
        raise ValueError("identity database has not completed bootstrap")
    return {
        "schema_version": version,
        "users": int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]),
        "grants": int(connection.execute("SELECT COUNT(*) FROM case_grants").fetchone()[0]),
        "tombstones": int(connection.execute("SELECT COUNT(*) FROM case_tombstones").fetchone()[0]),
    }


def _snapshot(source: Path, destination: Path, *, revoke_sessions: bool = False) -> dict[str, int]:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if source == destination:
        raise ValueError("source and destination must differ")
    if not source.is_file():
        raise FileNotFoundError("identity database source does not exist")
    for suffix in ("", "-wal", "-shm"):
        if Path(str(destination) + suffix).exists():
            raise FileExistsError("backup or restore destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)

    # mode=ro cannot silently create an empty source when the configured path is wrong.
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=10) as origin:
        _read_snapshot_info(origin)
        with destination.open("xb"):
            pass
        try:
            if os.name != "nt":
                destination.chmod(0o600)
            with sqlite3.connect(destination, timeout=10) as target:
                origin.backup(target)
                if revoke_sessions:
                    target.execute("UPDATE sessions SET revoked_at=strftime('%s','now') WHERE revoked_at IS NULL")
                    target.commit()
                info = _read_snapshot_info(target)
            with sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True, timeout=10) as verified:
                if _read_snapshot_info(verified) != info:
                    raise ValueError("identity snapshot changed during verification")
        except Exception:
            for suffix in ("-wal", "-shm", ""):
                Path(str(destination) + suffix).unlink(missing_ok=True)
            raise
    return info


def backup_database(destination: str | Path) -> dict[str, int]:
    """Create a new, verified snapshot of the configured live identity DB."""
    return _snapshot(_live_path(), Path(destination))


def restore_database(source: str | Path, destination: str | Path) -> dict[str, int]:
    """Restore to an explicitly named, nonexistent target; stop the app first."""
    target = Path(destination)
    if target.name != access_identity.DATABASE_NAME:
        raise ValueError("restore destination must end in identity.sqlite3")
    return _snapshot(Path(source), target, revoke_sessions=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    backup = subcommands.add_parser("backup", help="snapshot the configured live identity DB")
    backup.add_argument("destination", type=Path, help="new backup file")
    restore = subcommands.add_parser("restore", help="restore while the service is stopped")
    restore.add_argument("source", type=Path, help="verified backup file")
    restore.add_argument("destination", type=Path, help="new identity.sqlite3 target file")
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
