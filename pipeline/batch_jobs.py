"""Single-process, durable CFD queue. The HTTP service owns all computation.

SQLite is the source of truth for submissions, snapshots, outcomes and events.
Queued work survives restart; interrupted running work requires explicit retry.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .errors import PipelineError

LOGGER = logging.getLogger(__name__)


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class BatchQueue:
    def __init__(self, path: Path, preflight, execute):
        self.path, self.preflight, self.execute = path, preflight, execute
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.reserved: set[str] = set()
        self.thread: threading.Thread | None = None
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
                    request_json TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL, position INTEGER NOT NULL,
                    case_id TEXT NOT NULL, input_json TEXT NOT NULL, snapshot_json TEXT,
                    status TEXT NOT NULL, result_json TEXT, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL);
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def mutation(self, case_id):
        """Shared by HTTP mutations and the worker, including non-solver writes."""
        with self.lock:
            if case_id in self.reserved:
                raise PipelineError("blocked", "该病例正在执行批量任务或保存操作", ["等待当前任务结束后重试"])
            self.reserved.add(case_id)
        try:
            yield
        finally:
            with self.lock:
                self.reserved.discard(case_id)

    def _set(self, db, item_id, status, result):
        db.execute("UPDATE items SET status=?, result_json=?, updated_at=? WHERE id=?",
                   (status, _json(result), _now(), item_id))
        db.execute("INSERT INTO events(item_id,status,created_at) VALUES(?,?,?)", (item_id, status, _now()))

    def submit(self, request_key: str, items: list[dict]):
        request = _json(items)
        with self.lock, self._db() as db:
            previous = db.execute("SELECT * FROM batches WHERE request_key=?", (request_key,)).fetchone()
            if previous:
                if previous["request_json"] != request:
                    raise PipelineError("blocked", "同一幂等键已用于不同批量参数", ["重试原请求，或为新请求指定新幂等键"])
                return self.get(previous["id"])
            batch_id = "batch-" + secrets.token_hex(8)
            db.execute("INSERT INTO batches VALUES(?,?,?,?)", (batch_id, request_key, request, _now()))
            for position, item in enumerate(items):
                snapshot, status, result = None, "queued", None
                try:
                    if item["case_id"] in self.reserved:
                        raise PipelineError("blocked", "该病例正在执行其他操作", ["等待完成后重新提交"])
                    snapshot = self.preflight(item)
                except PipelineError as exc:
                    status, result = "blocked" if exc.status == "blocked" else "failed", exc.to_dict()
                cursor = db.execute("INSERT INTO items(batch_id,position,case_id,input_json,snapshot_json,status,result_json,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                                    (batch_id, position, item["case_id"], _json(item), _json(snapshot), status, _json(result), _now()))
                db.execute("INSERT INTO events(item_id,status,created_at) VALUES(?,?,?)", (cursor.lastrowid, status, _now()))
        self.wake.set()
        return self.get(batch_id)

    def get(self, batch_id):
        with self.lock, self._db() as db:
            batch = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if not batch:
                raise PipelineError("failed", "批量任务不存在", ["检查 batch_id"])
            rows = db.execute("SELECT * FROM items WHERE batch_id=? ORDER BY position", (batch_id,)).fetchall()
            items = [{"item_id": row["id"], "input": json.loads(row["input_json"]), "status": row["status"],
                      "result": json.loads(row["result_json"]), "updated_at": row["updated_at"]} for row in rows]
            counts = {status: sum(i["status"] == status for i in items)
                      for status in ("queued", "running", "solved", "blocked", "failed", "cancelled")}
            status = "running" if counts["running"] else "queued" if counts["queued"] else "completed"
            return {"batch_id": batch_id, "status": status, "counts": counts, "items": items,
                    "created_at": batch["created_at"], "poll_after_seconds": 60 if status != "completed" else None}

    def cancel(self, batch_id):
        """Cancel only unstarted work; never pretend a live solver has stopped."""
        self.get(batch_id)
        with self.lock, self._db() as db:
            for row in db.execute("SELECT id FROM items WHERE batch_id=? AND status='queued'", (batch_id,)).fetchall():
                self._set(db, row["id"], "cancelled", {"reason": "用户取消了尚未开始的任务"})
        return self.get(batch_id)

    def start(self):
        with self.lock, self._db() as db:
            if self.thread and self.thread.is_alive():
                return
            for row in db.execute("SELECT id FROM items WHERE status='running'").fetchall():
                self._set(db, row["id"], "failed", {"reason": "服务中断，任务未完成；请检查求解日志后用新幂等键重试", "stage": "interrupted"})
            self.stopping.clear()
            self.thread = threading.Thread(target=self._worker, name="batch-cfd", daemon=True)
            self.thread.start()

    def stop(self):
        self.stopping.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=2)

    def _run_one(self):
        with self.lock, self._db() as db:
            row = db.execute("SELECT * FROM items WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not row:
                return False
            case_id = row["case_id"]
            if case_id in self.reserved:
                return False
            self.reserved.add(case_id)
            self._set(db, row["id"], "running", None)
        try:
            item = json.loads(row["input_json"])
            if self.preflight(item) != json.loads(row["snapshot_json"]):
                raise PipelineError("blocked", "排队期间输入或审核版本已变化，未执行求解", ["复核当前输入，再用新幂等键提交"])
            result = self.execute(item)
            status = result.pop("task_status")
            if status not in {"solved", "blocked", "failed"}:
                raise ValueError("invalid task outcome")
        except PipelineError as exc:
            status, result = "blocked" if exc.status == "blocked" else "failed", exc.to_dict()
        except Exception as exc:
            LOGGER.exception("Batch item %s failed", row["id"])
            status, result = "failed", {"reason": "批量计算发生异常，请检查服务日志", "error_type": type(exc).__name__}
        try:
            with self.lock, self._db() as db:
                self._set(db, row["id"], status, result)
        finally:
            with self.lock:
                self.reserved.discard(case_id)
        return True

    def _worker(self):
        try:
            while not self.stopping.is_set():
                if not self._run_one():
                    self.wake.wait(1)
                    self.wake.clear()
        except Exception:
            # Stop visibly on persistence failure rather than launching untracked work.
            LOGGER.exception("Batch queue stopped; inspect its database and restart the service")

    def health(self):
        return {"worker_alive": bool(self.thread and self.thread.is_alive()), "concurrency": 1}
