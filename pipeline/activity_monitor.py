"""仅保存白名单操作元数据的本机活动日志，不保存输入内容或原始错误文本。"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import APP_DIR, STAGES

DIRECTORY_ENV = "WINDCATCHER_MONITOR_DIR"
# Legacy/local history keeps its established behavior. Personal-account mode
# expires operation metadata after a bounded window.
RETENTION_DAYS = None
MULTIUSER_RETENTION_DAYS = 180
MAX_EVENTS = None
SCHEMA_VERSION = 1
APP_VERSION = "2026.11.01.activity1"
RETENTION_LAST_ERROR: str | None = None
_DATABASE_NAME = "activity.sqlite3"
_KINDS = {"lifecycle", "ui", "navigation", "viewer", "request", "error", "stage"}
_ROUTES = {"work", "cases", "env", "method", "sample", "results", "activity", "share", "cfd-review"}
_BROWSER_ACTIONS = {
    "session_started", "queue_dropped", "click", "submit", "change", "input",
    "pointer", "wheel", "route", "page_opened", "feedback", "api_outcome",
    "response", "network_error", "javascript", "unhandled_rejection",
    "action_started", "action_finished", "action_failed", "mask_edit",
    "plane_selected", "plane_adjusted", "ct_view_changed",
}
_IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_:.-]{0,99}\Z", re.ASCII)
_CASE_ID = re.compile(r"(?:case-\d{8}-\d{6}-[0-9a-f]{4}|synthetic(?:-[A-Za-z0-9_-]{1,64})?)\Z", re.ASCII)
_ENUM_DETAILS = {
    "method": {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"},
    "outcome": {"ok", "success", "completed", "failed", "error", "blocked", "cancelled", "aborted", "pending", "running", "warn", "unknown", "invalid", "network_error"},
    "stage": set(STAGES) | {"preview", "cfd_postprocess", "environment", "install", "ai_report", "selection", "endplanes", "viewer", "scenario"},
    "status": {"pending", "running", "ok", "warn", "failed", "blocked", "idle", "missing", "ready", "loading", "error", "saving", "cancelled", "completed", "recording", "stopped", "stale", "unavailable", "available", "uploaded", "geometry_blocked", "placement_pending_review", "reviewed", "rejected"},
    "view": {"axial", "coronal", "sagittal", "3d", "none", "x", "y", "z"},
    "tool": {"paint", "erase", "translate", "rotate", "orbit", "pan", "zoom", "select", "add", "none"},
    "mode": {"3d", "slices", "anterior", "posterior", "translate", "rotate", "paint", "erase", "velocity", "pressure", "none", "mask", "model", "segment", "endplanes", "full_candidate", "main_airway", "complete_candidate", "main_nasal_airway"},
    "gesture": {"click", "double_click", "dblclick", "drag", "wheel", "pointer", "pinch", "pan", "zoom", "rotate", "keyboard"},
    "source": {"app.js", "activity-monitor.js", "cfd-viewer.js", "three.min.js", "STLLoader.js", "OrbitControls.js", "TransformControls.js", "server.py", "activity_monitor.py", "unknown", "inline"},
    "error_type": {"Error", "TypeError", "SyntaxError", "ReferenceError", "RangeError", "URIError", "EvalError", "NetworkError", "AbortError", "SecurityError", "QuotaExceededError", "DOMException", "UnhandledRejection", "UnknownError", "Exception", "RuntimeError", "ValueError", "KeyError", "OSError", "HTTPException", "PipelineError", "ValidationError", "RequestValidationError", "TimeoutError", "ConnectionError", "DatabaseError", "OperationalError", "PermissionError", "FileNotFoundError"},
}
_BOOL_DETAILS = {"overlay", "dirty", "checked", "cfd_ready"}
_INTEGER_DETAILS = {"status_code", "index", "anterior_index", "posterior_index", "operation_count", "point_count", "button", "line", "column", "count", "sequence", "version"}
_NUMBER_DETAILS = {"duration_ms", "radius", "level", "width", "delta_x", "delta_y", "delta"}

# 服务层可在请求/后台任务边界设置此上下文；事件自身显式提供的字段仍优先。
CURRENT_CONTEXT: ContextVar[dict | None] = ContextVar("windcatcher_activity_context", default=None)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _database_path() -> Path:
    directory = os.environ.get(DIRECTORY_ENV)
    return (Path(directory) if directory else APP_DIR / ".local-monitor") / _DATABASE_NAME


def _ensure_wal(connection: sqlite3.Connection) -> None:
    """Another fresh connection can hold the lock needed to enter WAL mode."""
    deadline = time.monotonic() + 10
    while True:
        try:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            if mode.lower() == "wal":
                return
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.lower() == "wal":
                return
        except sqlite3.OperationalError as exc:
            code = getattr(exc, "sqlite_errorcode", None)
            if code is None or code & 0xff not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                raise
        if time.monotonic() >= deadline:
            raise sqlite3.OperationalError("activity database could not enter WAL mode")
        time.sleep(0.025)


@contextmanager
def _connect():
    path = _database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        # Setting WAL on every request can race with another connection's
        # first write or migration. Wait before acquiring locks, and only
        # change journal mode if this database has not entered WAL yet.
        connection.execute("PRAGMA busy_timeout=10000")
        _ensure_wal(connection)
        if _retention_days() is not None:
            # Overwrite deleted SQLite cells where possible; WAL and older backups
            # still require separate lifecycle controls.
            connection.execute("PRAGMA secure_delete=ON")
        # Serialize the one-time migration, including when processes start together.
        if connection.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                _migrate(connection)
            connection.commit()
        yield connection
    finally:
        connection.close()


def _migrate(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            kind TEXT NOT NULL,
            action TEXT NOT NULL,
            route TEXT,
            case_id TEXT,
            session_id TEXT,
            request_id TEXT,
            target TEXT,
            details TEXT NOT NULL,
            is_error INTEGER NOT NULL
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS events_timestamp ON events(timestamp)")
    connection.execute("CREATE INDEX IF NOT EXISTS events_case_id ON events(case_id, id)")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
    for name, definition in {
        "event_id": "TEXT",
        "schema_version": "INTEGER NOT NULL DEFAULT 0",
        "app_version": "TEXT NOT NULL DEFAULT 'legacy'",
    }.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE events ADD COLUMN {name} {definition}")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS events_event_id ON events(event_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS events_session ON events(session_id, id)")
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def _identifier(value) -> str | None:
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


def _number(value) -> bool:
    return type(value) in {int, float} and abs(value) <= 1_000_000_000_000 and math.isfinite(value)


def _details(value) -> dict:
    """逐字段重建数据，禁止原对象、未知键或嵌套自由文本进入数据库。"""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in value.items():
        if key in _ENUM_DETAILS:
            if isinstance(item, str) and item in _ENUM_DETAILS[key]:
                result[key] = item
            elif key == "error_type" and isinstance(item, str):
                result[key] = "UnknownError"
        elif key in _BOOL_DETAILS:
            if type(item) is bool:
                result[key] = item
        elif key in _INTEGER_DETAILS:
            if type(item) is int and -1 <= item <= 1_000_000_000:
                if key != "status_code" or 100 <= item <= 599:
                    result[key] = item
        elif key in _NUMBER_DETAILS:
            if _number(item):
                result[key] = item
        elif key == "client_time_ms":
            if type(item) in {int, float} and 0 <= item <= 10_000_000_000_000 and math.isfinite(item):
                result[key] = item
        elif key == "scenario_id":
            identifier = _identifier(item)
            if identifier:
                result[key] = identifier
        elif key == "geometry_verdict":
            if isinstance(item, str) and item in {"pass", "warn", "failed"}:
                result[key] = item
        elif key == "value":
            if type(item) is bool or _number(item):
                result[key] = item
        elif key == "controls":
            if type(item) is bool or _number(item):
                result[key] = item
            elif isinstance(item, (list, tuple)) and len(item) <= 16 and all(_number(part) for part in item):
                result[key] = list(item)
    return result


def _retention_days() -> int | None:
    return MULTIUSER_RETENTION_DAYS if os.environ.get("WINDCATCHER_MULTIUSER") == "1" else RETENTION_DAYS


def _prune(connection: sqlite3.Connection, now: datetime) -> None:
    retention_days = _retention_days()
    if retention_days is not None:
        connection.execute("DELETE FROM events WHERE timestamp < ?", (_timestamp(now - timedelta(days=retention_days)),))
    if MAX_EVENTS is not None:
        connection.execute("""
        DELETE FROM events WHERE id <= (
            SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?
        )
        """, (MAX_EVENTS,))


def prune_expired_events() -> int:
    """Remove expired personal metadata and truncate committed WAL frames.

    This is safe to run while the service is idle. Historical backup files are
    managed separately and are never modified by this operation.
    """
    global RETENTION_LAST_ERROR
    if _retention_days() is None:
        return 0
    try:
        with _connect() as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                previous_changes = connection.total_changes
                _prune(connection, _now())
                removed = connection.total_changes - previous_changes
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is None or checkpoint[0]:
                raise sqlite3.OperationalError("activity retention WAL checkpoint is busy")
    except Exception as exc:
        RETENTION_LAST_ERROR = type(exc).__name__
        raise
    RETENTION_LAST_ERROR = None
    return removed


def _prepare_event(event: dict) -> tuple:
    if not isinstance(event, dict):
        raise ValueError("活动事件必须是对象")
    kind = event.get("kind")
    if not isinstance(kind, str) or kind not in _KINDS:
        raise ValueError("活动事件类型无效")
    action = _identifier(event.get("action"))
    if not action:
        raise ValueError("活动事件操作标识无效")
    route = event.get("route")
    route = route if isinstance(route, str) and route in _ROUTES else None
    case_id = event.get("case_id")
    case_id = case_id if isinstance(case_id, str) and _CASE_ID.fullmatch(case_id) else None
    details = _details(event.get("details"))
    context = CURRENT_CONTEXT.get()
    if not isinstance(context, dict):
        context = {}
    session_id = _identifier(event.get("session_id", context.get("session_id")))
    request_id = _identifier(event.get("request_id", context.get("request_id")))
    is_error = kind == "error" or details.get("outcome") in {"failed", "error", "blocked", "network_error"} or details.get("status") in {"failed", "error", "blocked"} or details.get("status_code", 0) >= 400
    event_id = event.get("event_id")
    if event_id is not None:
        try:
            event_id = str(uuid.UUID(event_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("活动事件去重编号无效") from exc
    return (
        _timestamp(_now()), kind, action, route, case_id, session_id, request_id,
        _identifier(event.get("target")), json.dumps(details, ensure_ascii=True, allow_nan=False),
        int(is_error), event_id, SCHEMA_VERSION, APP_VERSION,
    )


def record_event(event: dict) -> int:
    """保存事件；同一 UUID 重送返回原编号，不覆盖首次收到的内容。"""
    return record_events([event])[0]


def record_events(events: list[dict]) -> list[int]:
    """整批验证、原子提交，防止部分写入后重试造成数据重复。"""
    prepared = [_prepare_event(event) for event in events]
    ids = []
    with _connect() as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        for values in prepared:
            cursor = connection.execute("""
                INSERT INTO events (timestamp, kind, action, route, case_id, session_id,
                    request_id, target, details, is_error, event_id, schema_version, app_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO NOTHING
            """, values)
            row_id = cursor.lastrowid if cursor.rowcount else connection.execute(
                "SELECT id FROM events WHERE event_id = ?", (values[10],)).fetchone()[0]
            ids.append(int(row_id))
        _prune(connection, _now())
    return ids


def validate_browser_events(events: list[dict]) -> list[dict]:
    """浏览器只能提交固定操作枚举；上下文与 details 仍按白名单重建。"""
    for event in events:
        if event.get("kind") not in _KINDS or event.get("action") not in _BROWSER_ACTIONS:
            raise ValueError("操作事件格式无效")
        _prepare_event(event)
    return events


def query_events(
    after: int = 0,
    limit: int = 100,
    case_id: str | None = None,
    kind: str | None = None,
    session_id: str | None = None,
    errors_only: bool = False,
    latest: bool = False,
) -> dict:
    """按递增编号查询，游标允许编号间隔。

    latest=True 仅用于读取日志尾部；has_more 表示更早仍有记录，不能用于前向翻页。
    """
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("活动查询游标或条数无效")
    if case_id is not None and (not isinstance(case_id, str) or not _CASE_ID.fullmatch(case_id)):
        raise ValueError("活动查询病例编号无效")
    if kind is not None and (not isinstance(kind, str) or kind not in _KINDS):
        raise ValueError("活动查询类型无效")
    if session_id is not None and not _identifier(session_id):
        raise ValueError("活动查询会话编号无效")
    if type(errors_only) is not bool:
        raise ValueError("活动查询错误筛选无效")
    if type(latest) is not bool:
        raise ValueError("活动查询尾部筛选无效")
    conditions, parameters = ["id > ?"], [after]
    for key, value in (("case_id", case_id), ("kind", kind), ("session_id", session_id)):
        if value is not None:
            conditions.append(f"{key} = ?")
            parameters.append(value)
    if errors_only:
        conditions.append("is_error = 1")
    with _connect() as connection, connection:
        _prune(connection, _now())
        order = "DESC" if latest else "ASC"
        rows = connection.execute(
            f"SELECT * FROM events WHERE {' AND '.join(conditions)} ORDER BY id {order} LIMIT ?",
            (*parameters, limit + 1),
        ).fetchall()
    has_more = len(rows) > limit
    if latest:
        rows = list(reversed(rows[:limit]))
    else:
        rows = rows[:limit]
    events = []
    for row in rows:
        entry = dict(row)
        entry.pop("is_error")
        entry["details"] = json.loads(entry["details"])
        events.append(entry)
    return {
        "events": events,
        "next_after": events[-1]["id"] if events else after,
        "has_more": has_more,
    }


def query_recipe_signals(owner_id: str) -> list[dict]:
    """Read only recent successful stage hints bound to a personal account.

    The public gateway binds browser event session_id to the authenticated
    principal before ingestion. This query is available only in personal mode.
    """
    if _retention_days() != MULTIUSER_RETENTION_DAYS:
        raise ValueError("personal operation history is not enabled")
    if not isinstance(owner_id, str) or not re.fullmatch(r"[0-9a-f]{32}", owner_id, re.ASCII):
        raise ValueError("invalid operation history owner")
    with _connect() as connection, connection:
        now = _now()
        _prune(connection, now)
        rows = connection.execute("""
            SELECT id,timestamp,kind,action,case_id,session_id,details
            FROM events
            WHERE session_id=? AND kind='stage' AND action='action_finished'
              AND case_id IS NOT NULL AND is_error=0
              AND timestamp >= ? AND timestamp <= ?
            ORDER BY id DESC LIMIT 2000
        """, (owner_id, _timestamp(now - timedelta(days=MULTIUSER_RETENTION_DAYS)),
              _timestamp(now))).fetchall()
    signals = []
    for row in reversed(rows):
        try:
            details = json.loads(row["details"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("stored operation metadata is invalid") from exc
        if not isinstance(details, dict):
            raise RuntimeError("stored operation metadata is invalid")
        signals.append({**dict(row), "details": details})
    return signals


def get_status() -> dict:
    with _connect() as connection, connection:
        _prune(connection, _now())
        total = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        latest = connection.execute("SELECT id, timestamp FROM events ORDER BY id DESC LIMIT 1").fetchone()
    status = {
        "status": "recording", "total_events": total,
        "last_event_at": latest[1] if latest else None,
        "last_event_id": int(latest[0]) if latest else 0,
        "retention_days": _retention_days(), "max_events": MAX_EVENTS,
        "schema_version": SCHEMA_VERSION, "app_version": APP_VERSION,
        "database_path": ".local-monitor/activity.sqlite3",
    }
    if RETENTION_LAST_ERROR is not None:
        status.update(status="unavailable", retention_error_type=RETENTION_LAST_ERROR)
    return status


def backup_database(destination: str | Path) -> Path:
    """以 SQLite 在线备份 API 创建新快照，包含 WAL 中已提交的数据。"""
    source_path = _database_path().resolve()
    destination_path = Path(destination).expanduser().resolve()
    if source_path == destination_path:
        raise ValueError("backup destination cannot replace the live database")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as source:
        try:
            with destination_path.open("xb"):
                pass
        except FileExistsError:
            raise FileExistsError("backup destination already exists") from None
        try:
            if _retention_days() is not None:
                # No read may have occurred since these events expired. Exclude
                # them from this new snapshot and commit before SQLite backup.
                with source:
                    source.execute("BEGIN IMMEDIATE")
                    _prune(source, _now())
            target = sqlite3.connect(destination_path, timeout=0.5)
            try:
                source.backup(target)
            finally:
                target.close()
        except Exception:
            destination_path.unlink(missing_ok=True)
            raise
    return destination_path
