"""Read-only inventory of activity backups before enabling personal accounts.

Backups are separate SQLite files. Pruning the live database cannot remove an
expired event from a snapshot that was made earlier. This module only reports
counts and timestamps; removal requires an explicit, reviewed procedure.
"""

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

from .activity_monitor import MULTIUSER_RETENTION_DAYS


def audit_directory(directory: str | Path, *, now: datetime | None = None) -> dict:
    root = Path(directory).expanduser()
    if not root.is_dir() or root.is_symlink():
        raise ValueError("activity backup directory must be an existing, direct directory")
    if hasattr(root, "is_junction") and root.is_junction():
        raise ValueError("activity backup directory cannot be a junction")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("audit time must include a timezone")
    cutoff = current.astimezone(timezone.utc) - timedelta(days=MULTIUSER_RETENTION_DAYS)
    files = []
    for path in sorted(root.glob("*.sqlite3")):
        entry = {"file": path.name, "status": "review_required"}
        if path.is_symlink() or not path.is_file():
            entry["reason"] = "not_regular_file"
            files.append(entry)
            continue
        # A SQLite WAL may hold committed rows absent from the main file. Do
        # not open it here: a read-only SQLite connection may still create a
        # shared-memory sidecar on a writable directory. Review the set as one
        # backup before auditing it.
        if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm")):
            entry["reason"] = "sqlite_sidecar_present"
            files.append(entry)
            continue
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as database:
                if database.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("sqlite_integrity")
                cursor = database.execute("SELECT timestamp FROM events")
                total = expired = invalid = future = 0
                earliest = latest = None
                for (raw,) in cursor:
                    total += 1
                    try:
                        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                        if stamp.tzinfo is None:
                            raise ValueError("missing timezone")
                        stamp = stamp.astimezone(timezone.utc)
                    except (AttributeError, TypeError, ValueError):
                        invalid += 1
                        continue
                    expired += stamp < cutoff
                    future += stamp > current.astimezone(timezone.utc) + timedelta(minutes=5)
                    earliest = min(earliest, stamp) if earliest else stamp
                    latest = max(latest, stamp) if latest else stamp
                entry.update(total_events=total, expired_events=expired,
                             invalid_timestamps=invalid, future_events=future,
                             earliest=earliest.isoformat() if earliest else None,
                             latest=latest.isoformat() if latest else None)
                if not expired and not invalid and not future:
                    entry["status"] = "current"
                else:
                    entry["reason"] = "expired_invalid_or_future_events"
        except (sqlite3.DatabaseError, OSError, ValueError) as exc:
            entry["reason"] = type(exc).__name__
        files.append(entry)
    return {"status": "empty" if not files else ("current" if all(
        item["status"] == "current" for item in files) else "review_required"),
            "retention_days": MULTIUSER_RETENTION_DAYS,
            "cutoff_utc": cutoff.isoformat(), "files": files}
