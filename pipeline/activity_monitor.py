"""仅保存白名单操作元数据的本机活动日志，不保存输入内容或原始错误文本。"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import APP_DIR, STAGES

DIRECTORY_ENV = "WINDCATCHER_MONITOR_DIR"
RETENTION_DAYS = 14
MAX_EVENTS = 50_000
_DATABASE_NAME = "activity.sqlite3"
_KINDS = {"lifecycle", "ui", "navigation", "viewer", "request", "error", "stage"}
_ROUTES = {"work", "cases", "env", "method", "sample", "results"}
_IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_:.-]{0,99}\Z", re.ASCII)
_CASE_ID = re.compile(r"(?:case-\d{8}-\d{6}-[0-9a-f]{4}|synthetic(?:-[A-Za-z0-9_-]{1,64})?)\Z", re.ASCII)
_ENUM_DETAILS = {
    "method": {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"},
    "outcome": {"ok", "success", "completed", "failed", "error", "blocked", "cancelled", "aborted", "pending", "running", "warn", "unknown", "invalid", "network_error"},
    "stage": set(STAGES) | {"preview", "cfd_postprocess", "environment", "install", "ai_report", "selection", "endplanes", "viewer"},
    "status": {"pending", "running", "ok", "warn", "failed", "blocked", "idle", "missing", "ready", "loading", "error", "saving", "cancelled", "completed", "recording", "stopped", "stale", "unavailable", "available"},
    "view": {"axial", "coronal", "sagittal", "3d", "none", "x", "y", "z"},
    "tool": {"paint", "erase", "translate", "rotate", "orbit", "pan", "zoom", "select", "add", "none"},
    "mode": {"3d", "slices", "anterior", "posterior", "translate", "rotate", "paint", "erase", "velocity", "pressure", "none", "mask", "model", "segment", "endplanes", "full_candidate", "main_airway", "complete_candidate", "main_nasal_airway"},
    "gesture": {"click", "double_click", "dblclick", "drag", "wheel", "pointer", "pinch", "pan", "zoom", "rotate", "keyboard"},
    "source": {"app.js", "activity-monitor.js", "cfd-viewer.js", "three.min.js", "STLLoader.js", "OrbitControls.js", "TransformControls.js", "server.py", "activity_monitor.py", "unknown", "inline"},
    "error_type": {"Error", "TypeError", "SyntaxError", "ReferenceError", "RangeError", "URIError", "EvalError", "NetworkError", "AbortError", "SecurityError", "QuotaExceededError", "DOMException", "UnhandledRejection", "UnknownError", "Exception", "RuntimeError", "ValueError", "KeyError", "OSError", "HTTPException", "PipelineError", "ValidationError", "RequestValidationError", "TimeoutError", "ConnectionError", "DatabaseError", "OperationalError", "PermissionError", "FileNotFoundError"},
}
_BOOL_DETAILS = {"overlay", "dirty", "checked"}
_INTEGER_DETAILS = {"status_code", "index", "anterior_index", "posterior_index", "operation_count", "point_count", "button", "line", "column", "count", "sequence"}
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


@contextmanager
def _connect():
    path = _database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=0.5)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=500")
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
        yield connection
    finally:
        connection.close()


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
        elif key == "value":
            if type(item) is bool or _number(item):
                result[key] = item
        elif key == "controls":
            if type(item) is bool or _number(item):
                result[key] = item
            elif isinstance(item, (list, tuple)) and len(item) <= 16 and all(_number(part) for part in item):
                result[key] = list(item)
    return result


def _prune(connection: sqlite3.Connection, now: datetime) -> None:
    connection.execute("DELETE FROM events WHERE timestamp < ?", (_timestamp(now - timedelta(days=RETENTION_DAYS)),))
    connection.execute("""
        DELETE FROM events WHERE id <= (
            SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?
        )
    """, (MAX_EVENTS,))


def record_event(event: dict) -> int:
    """保存一个操作事件并返回本机递增编号；存储异常交由调用方处理。"""
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
    now = _now()
    with _connect() as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute("""
            INSERT INTO events (timestamp, kind, action, route, case_id, session_id, request_id, target, details, is_error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            _timestamp(now), kind, action, route, case_id,
            session_id, request_id,
            _identifier(event.get("target")), json.dumps(details, ensure_ascii=True, allow_nan=False), int(is_error),
        ))
        event_id = int(cursor.lastrowid)
        _prune(connection, now)
    return event_id


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


def get_status() -> dict:
    with _connect() as connection, connection:
        _prune(connection, _now())
        total = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        latest = connection.execute("SELECT id, timestamp FROM events ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "status": "recording", "total_events": total,
        "last_event_at": latest[1] if latest else None,
        "last_event_id": int(latest[0]) if latest else 0,
        "retention_days": RETENTION_DAYS, "max_events": MAX_EVENTS,
        "database_path": ".local-monitor/activity.sqlite3",
    }
