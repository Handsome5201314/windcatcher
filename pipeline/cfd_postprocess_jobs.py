"""Persistent local postprocessing jobs, isolated from the solver process."""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import sys
import threading
from datetime import datetime

from .cases import case_dir, get_case
from .config import APP_DIR, CASES_DIR
from .errors import PipelineError

LOGGER = logging.getLogger(__name__)
_LOCK = threading.RLock()
_ACTIVE: dict[str, dict] = {}
_MUTATIONS: set[str] = set()
TIMEOUT_SECONDS = 900


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _path(case_id: str):
    return case_dir(case_id) / "cfd" / "results" / "task.json"


def _write(case_id: str, task: dict) -> None:
    path = _path(case_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"task.{secrets.token_hex(4)}.tmp")
    try:
        temporary.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def get_task(case_id: str) -> dict | None:
    with _LOCK:
        path = _path(case_id)
        if not path.exists():
            return None
        try:
            task = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(task, dict):
                raise ValueError("task is not an object")
            return task
        except (OSError, UnicodeError, ValueError) as exc:
            raise PipelineError("failed", f"无法读取后处理任务记录：{exc}", ["检查 cfd/results/task.json"]) from exc


def reserve_mutation(case_id: str) -> None:
    with _LOCK:
        if case_id in _ACTIVE or case_id in _MUTATIONS:
            raise PipelineError("blocked", "该病例正在处理，请等待完成或先取消后处理", ["在病例结果页查看当前进度"])
        _MUTATIONS.add(case_id)


def release_mutation(case_id: str) -> None:
    with _LOCK:
        _MUTATIONS.discard(case_id)


def recover_interrupted() -> int:
    recovered = 0
    for path in CASES_DIR.glob("*/cfd/results/task.json"):
        case_id = path.parents[2].name
        with _LOCK:
            if case_id in _ACTIVE:
                continue
            try:
                task = get_task(case_id)
            except PipelineError:
                LOGGER.exception("Could not recover postprocessing task for %s", case_id)
                continue
            if task and task.get("status") == "running":
                task.update(status="failed", message="服务中断了后处理，可重新生成；原始求解场保持不变", updated_at=_now())
                _write(case_id, task)
                recovered += 1
    return recovered


def start(case_id: str) -> dict:
    with _LOCK:
        if case_id in _ACTIVE:
            return get_task(case_id)
        case = get_case(case_id)
        if case_id in _MUTATIONS or case.get("job", {}).get("status") == "running" or any(
            stage.get("status") == "running" for stage in case.get("stages", {}).values()
        ):
            raise PipelineError("blocked", "病例的上游步骤正在运行，暂不能后处理", ["等待当前步骤结束后重试"])
        if not (case_dir(case_id) / "cfd" / "system" / "controlDict").is_file():
            raise PipelineError("blocked", "还没有可读取的 CFD 算例", ["先生成算例并完成求解"])
        task = {
            "job_id": secrets.token_hex(8), "case_id": case_id, "status": "running",
            "stage": "queued", "message": "正在准备读取已有求解场", "created_at": _now(),
            "updated_at": _now(), "log": "cfd/logs/cfd_postprocess.log",
        }
        _write(case_id, task)
        _ACTIVE[case_id] = {"process": None, "cancelled": False, "timed_out": False}
        threading.Thread(target=_worker, args=(case_id,), name=f"cfd-results-{case_id}", daemon=True).start()
        return task


def cancel(case_id: str) -> dict | None:
    with _LOCK:
        active = _ACTIVE.get(case_id)
        if active:
            active["cancelled"] = True
            process = active["process"]
            if process and process.poll() is None:
                process.terminate()
            task = get_task(case_id)
            task.update(message="正在停止后处理", updated_at=_now())
            _write(case_id, task)
        return get_task(case_id)


def _worker(case_id: str) -> None:
    timer = None
    try:
        root = case_dir(case_id)
        log_path = root / "cfd" / "logs" / "cfd_postprocess.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        environment = dict(os.environ, PYTHONIOENCODING="utf-8", VTK_SMP_MAX_THREADS="4")
        with _LOCK:
            if _ACTIVE[case_id]["cancelled"]:
                _finish(case_id, "cancelled", "后处理已取消，原始求解场保持不变")
                return
            process = subprocess.Popen(
                [sys.executable, "-u", "-m", "pipeline.cfd_results", "--case", case_id],
                cwd=APP_DIR, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            _ACTIVE[case_id]["process"] = process

        def timeout():
            with _LOCK:
                active = _ACTIVE.get(case_id)
                if active and process.poll() is None:
                    active["timed_out"] = True
                    process.terminate()

        timer = threading.Timer(TIMEOUT_SECONDS, timeout)
        timer.daemon = True
        timer.start()
        with log_path.open("w", encoding="utf-8") as log:
            for line in process.stdout:
                log.write(line)
                log.flush()
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("event") in {"progress", "error"}:
                    with _LOCK:
                        task = get_task(case_id)
                        task.update(stage=event.get("stage", task["stage"]), message=event.get("message", event.get("reason", task["message"])), updated_at=_now())
                        _write(case_id, task)
            returncode = process.wait()
        with _LOCK:
            active = _ACTIVE[case_id]
            if active["cancelled"]:
                _finish(case_id, "cancelled", "后处理已取消，原始求解场保持不变")
            elif active["timed_out"]:
                _finish(case_id, "failed", "后处理超时，已停止；请查看日志后单独重试后处理")
            elif returncode != 0:
                task = get_task(case_id)
                _finish(case_id, "failed", task["message"] if task["stage"] != "queued" else f"后处理进程退出码 {returncode}，请查看后处理日志")
            else:
                from .cfd_results import get_results
                result = get_results(case_id)
                if result.get("status") != "ready":
                    _finish(case_id, "failed", result.get("reason", "后处理结束但没有可用的结果清单"))
                else:
                    _finish(case_id, "ready", "真实流场可查看；数值验证状态请参阅结果检查")
    except Exception as exc:
        LOGGER.exception("Postprocessing failed for %s", case_id)
        with _LOCK:
            _finish(case_id, "failed", f"后处理失败：{exc}")
    finally:
        if timer:
            timer.cancel()
        with _LOCK:
            _ACTIVE.pop(case_id, None)


def _finish(case_id: str, status: str, message: str) -> None:
    task = get_task(case_id)
    task.update(status=status, message=message, updated_at=_now())
    _write(case_id, task)
