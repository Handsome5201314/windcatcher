"""鼻塞方案 CFD 的持久后台任务。

鼻塞算例可能要经历划网格、检查网格和求解器迭代，不能把整个过程绑在
HTTP 请求上。任务状态写在对应版本目录内，服务重启后可以把未完成任务
标记为中断；原始基准病例和其它摆位版本不会被覆盖。
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from datetime import datetime
from pathlib import Path

from . import cases, scenarios
from .errors import PipelineError

LOGGER = logging.getLogger(__name__)
_LOCK = threading.RLock()
_ACTIVE: dict[tuple[str, str, int], dict] = {}
_MUTATIONS: set[str] = set()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _version_root(case_id: str, scenario_id: str, version: int | None = None) -> tuple[dict, Path, int]:
    scenario = scenarios.get_scenario(case_id, scenario_id)
    selected = int(scenario.get("current_version", 0)) if version is None else int(version)
    current = int(scenario.get("current_version", 0))
    if selected < 1 or selected > current:
        raise PipelineError("failed", "鼻塞方案版本无效", ["刷新方案详情后重新选择当前版本"])
    root = scenarios.scenario_dir(case_id, scenario_id)
    version_root = root / "versions" / f"v{selected:04d}"
    if version_root.is_symlink() or not version_root.is_dir():
        raise PipelineError("failed", f"鼻塞方案版本不存在：{selected}", ["刷新方案列表后重试"])
    return scenario, version_root, selected


def _task_path(case_id: str, scenario_id: str, version: int) -> Path:
    _scenario, version_root, _selected = _version_root(case_id, scenario_id, version)
    return version_root / "scenario_task.json"


def _read_task(path: Path) -> dict | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PipelineError("failed", "鼻塞 CFD 任务记录无法读取", ["检查该方案版本的任务记录后重试"]) from exc
    if not isinstance(value, dict):
        raise PipelineError("failed", "鼻塞 CFD 任务记录格式无效", ["重新提交该版本的独立 CFD"])
    return value


def _write_task(path: Path, task: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        temporary.write_text(json.dumps(task, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(path)
    except (OSError, TypeError, ValueError) as exc:
        raise PipelineError("failed", "鼻塞 CFD 任务状态写入失败", ["检查病例目录权限和磁盘空间后重试"]) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _update_task(path: Path, **changes) -> dict:
    current = _read_task(path) or {}
    current.update(changes)
    current["updated_at"] = _now()
    _write_task(path, current)
    return current


def _task_is_active(task: dict | None) -> bool:
    return isinstance(task, dict) and task.get("status") in {"queued", "running"}


def _iter_case_task_paths(case_id: str):
    root = cases.case_dir(case_id) / "scenarios"
    if not root.is_dir() or root.is_symlink():
        return []
    return root.glob("*/versions/v*/scenario_task.json")


def reserve_mutation(case_id: str) -> None:
    """阻止摆位、审核等写操作与方案 CFD 并发。"""
    with _LOCK:
        if case_id in _MUTATIONS:
            raise PipelineError("blocked", "该病例正在保存另一项操作", ["等待当前操作结束后重试"])
        if any(key[0] == case_id for key in _ACTIVE):
            raise PipelineError("blocked", "该病例正在执行鼻塞 CFD", ["等待鼻塞 CFD 完成后再修改摆位或审核"])
        for path in _iter_case_task_paths(case_id):
            if _task_is_active(_read_task(path)):
                raise PipelineError("blocked", "该病例正在执行鼻塞 CFD", ["等待鼻塞 CFD 完成后再修改摆位或审核"])
        _MUTATIONS.add(case_id)


def release_mutation(case_id: str) -> None:
    with _LOCK:
        _MUTATIONS.discard(case_id)


def get_task(case_id: str, scenario_id: str, *, version: int | None = None) -> dict | None:
    _scenario, version_root, selected = _version_root(case_id, scenario_id, version)
    with _LOCK:
        return _read_task(version_root / "scenario_task.json")


def recover_interrupted() -> int:
    """服务启动时把上一次进程留下的 running/queued 任务标为中断。"""
    recovered = 0
    for case_path in cases.CASES_DIR.glob("*/scenarios/*/versions/v*/scenario_task.json"):
        if case_path.is_symlink():
            continue
        task = _read_task(case_path)
        if not _task_is_active(task):
            continue
        task.update(
            status="failed",
            stage="interrupted",
            message="服务中断了鼻塞 CFD 任务，原始输入和基准算例保持不变，可重新提交",
            updated_at=_now(),
        )
        _write_task(case_path, task)
        try:
            scenario_id = case_path.parents[2].name
            case_id = case_path.parents[4].name
            version = int(case_path.parent.name.removeprefix("v"))
            _set_scenario_state(case_id, scenario_id, version, "failed", task_id=task.get("job_id"), reason="interrupted")
        except Exception as exc:
            task["state_persistence_error"] = type(exc).__name__
            task["state_persistence_message"] = "任务已标为中断，但长期方案索引同步失败；请刷新并检查研究账本"
            _write_task(case_path, task)
            LOGGER.exception("Unable to persist interrupted scenario task state for %s", case_path)
        recovered += 1
    return recovered


def _set_scenario_state(case_id: str, scenario_id: str, version: int, state: str, **details) -> None:
    scenarios.update_cfd_state(case_id, scenario_id, version=version, state=state, details=details)


def start(
    case_id: str,
    scenario_id: str,
    *,
    backend: str | None = None,
    timeout_s: int = 7200,
    version: int | None = None,
) -> dict:
    """登记并启动一个不会阻塞 HTTP 请求的鼻塞 CFD 任务。"""
    scenario, version_root, selected = _version_root(case_id, scenario_id, version)
    if selected != int(scenario.get("current_version", 0)):
        raise PipelineError("blocked", "只能提交当前摆位版本的鼻塞 CFD", ["刷新方案后选择当前版本"])
    review = scenario.get("review") or {}
    if review.get("status") != "approved" or not review.get("approved"):
        raise PipelineError("blocked", "鼻塞方案尚未完成人工审核，不能提交 CFD", ["先完成摆位确认和人工审核"])
    if scenario.get("base_status") != "current":
        raise PipelineError("blocked", "当前鼻塞方案绑定的气道基准已变化", ["重新生成气道模型并重新确认摆位"])
    key = (case_id, scenario_id, selected)
    path = version_root / "scenario_task.json"
    with _LOCK:
        if any(item[0] == case_id for item in _ACTIVE):
            raise PipelineError("blocked", "该病例已有鼻塞 CFD 正在执行", ["等待当前鼻塞 CFD 完成后再提交另一方案"])
        for other_path in _iter_case_task_paths(case_id):
            if other_path == path:
                continue
            if _task_is_active(_read_task(other_path)):
                raise PipelineError("blocked", "该病例已有鼻塞 CFD 正在执行", ["等待当前鼻塞 CFD 完成后再提交另一方案"])
        if key in _ACTIVE:
            raise PipelineError("blocked", "该鼻塞版本正在求解", ["等待当前任务完成后查看状态"])
        previous = _read_task(path)
        if _task_is_active(previous):
            raise PipelineError("blocked", "该鼻塞版本已有运行中的 CFD 任务", ["等待当前任务完成后查看状态"])
        task = {
            "schema_version": 1,
            "job_id": secrets.token_hex(8),
            "case_id": case_id,
            "scenario_id": scenario_id,
            "version": selected,
            "status": "queued",
            "stage": "queued",
            "message": "已提交鼻塞 CFD，等待后台准备算例",
            "backend": backend,
            "timeout_s": int(timeout_s),
            "created_at": _now(),
            "updated_at": _now(),
        }
        _write_task(path, task)
        _ACTIVE[key] = {"task_path": path, "cancelled": False}
        try:
            scenarios.update_cfd_state(
                case_id,
                scenario_id,
                version=selected,
                state="running",
                details={"task_id": task["job_id"], "stage": "queued"},
            )
        except Exception:
            _ACTIVE.pop(key, None)
            task.update(status="failed", stage="failed", message="无法登记鼻塞 CFD 运行状态", updated_at=_now())
            _write_task(path, task)
            raise
        threading.Thread(
            target=_worker,
            args=(key, backend, int(timeout_s)),
            name=f"scenario-cfd-{scenario_id}-v{selected}",
            daemon=True,
        ).start()
        return task


def _worker(key: tuple[str, str, int], backend: str | None, timeout_s: int) -> None:
    case_id, scenario_id, version = key
    with _LOCK:
        active = _ACTIVE.get(key)
        path = active.get("task_path") if active else None
    if path is None:
        return
    try:
        _update_task(path, status="running", stage="preparing", message="正在检查摆位并生成鼻塞独立 CFD 算例；完成后继续 OpenFOAM 求解")
        from . import scenario_cfd

        result = scenario_cfd.run_scenario(
            case_id,
            scenario_id,
            backend=backend,
            timeout_s=timeout_s,
            version=version,
        )
        run_status = (result.get("run") or {}).get("status")
        final = "solved" if run_status == "ran" else "failed"
        changes = {
            "status": final,
            "stage": "completed" if final == "solved" else "failed",
            "message": "鼻塞 CFD 已完成，可查看前后对比" if final == "solved" else "鼻塞 CFD 未完成，请查看求解记录",
            "run_status": run_status,
            "comparison_status": (result.get("comparison") or {}).get("status"),
        }
        details = {"run_status": run_status, "comparison_status": changes["comparison_status"]}
    except PipelineError as exc:
        final = exc.status if exc.status in {"blocked", "failed"} else "failed"
        changes = {"status": final, "stage": final, "message": exc.reason, "reason": exc.reason,
                   "remediation": exc.remediation, "detail": exc.detail}
        details = {"reason": exc.reason}
    except Exception as exc:  # pragma: no cover - 由真实 OpenFOAM 环境触发
        LOGGER.exception("Scenario CFD failed for %s/%s/v%s", case_id, scenario_id, version)
        changes = {"status": "failed", "stage": "failed", "message": "鼻塞 CFD 执行失败，请查看任务记录和日志",
                   "reason": type(exc).__name__}
        details = {"reason": type(exc).__name__}
    # 状态查询与写操作共用此锁，避免在长期账本提交前发布完成状态。
    with _LOCK:
        try:
            task = _update_task(path, **changes)
            try:
                _set_scenario_state(case_id, scenario_id, version, task["status"], task_id=task.get("job_id"), **details)
            except Exception as state_exc:
                LOGGER.exception("Unable to persist final scenario CFD state for %s/%s/v%s", case_id, scenario_id, version)
                _update_task(path, state_persistence_error=type(state_exc).__name__, state_persistence_message="任务文件已写入，但长期方案索引同步失败；请刷新并检查研究账本")
        finally:
            _ACTIVE.pop(key, None)
