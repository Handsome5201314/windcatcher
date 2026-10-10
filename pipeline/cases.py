"""本地病例存储与阶段状态机。

病例产物全部落在 `cases/<case_id>/` 下，禁止提交版本库、禁止外传。
`case.json` 只记录状态、数值与相对路径，不记录任何患者标识信息的值。
"""

from __future__ import annotations

import json
import logging
import secrets
import shutil
from datetime import datetime
from pathlib import Path

from . import activity_monitor
from .config import CASES_DIR, STAGES, ensure_dirs
from .errors import PipelineError

CASE_FILE = "case.json"
STAGE_STATUS = ("pending", "running", "ok", "warn", "failed", "blocked")
LOGGER = logging.getLogger(__name__)
INTERRUPTED_MESSAGE = "上次服务在处理期间中断，结果未完成；请检查已有日志后重新执行"


def _record_activity(kind: str, action: str, case_id: str, *, target: str | None = None, details: dict | None = None) -> None:
    try:
        activity_monitor.record_event({"kind": kind, "action": action, "case_id": case_id, "target": target, "details": details or {}})
    except Exception as exc:
        # 病例写盘已经完成；日志故障不得把成功的业务操作报告为失败。
        LOGGER.error("Activity monitor write failed: %s", type(exc).__name__)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _new_case_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"case-{stamp}-{secrets.token_hex(2)}"


def case_dir(case_id: str) -> Path:
    if not case_id or Path(case_id).name != case_id or case_id in {".", ".."}:
        raise PipelineError("failed", "病例编号无效", ["刷新病例列表后重试"])
    return CASES_DIR / case_id


def create_case(source_type: str, source_path: str, synthetic: bool = False) -> str:
    """新建病例，返回 case_id，并建立产物子目录。"""
    ensure_dirs()
    case_id = _new_case_id()
    root = case_dir(case_id)
    for sub in ("import", "anonymized", "volume", "segment", "surface", "cfd", "report", "preview"):
        (root / sub).mkdir(parents=True, exist_ok=True)

    data = {
        "case_id": case_id,
        "created_at": _now(),
        "updated_at": _now(),
        "source_type": source_type,          # local_path / direct_link / synthetic
        # 不把用户输入的绝对路径写入病例状态；路径可能包含患者姓名/住院号。
        "source_path": "<本机 DICOM 文件夹>",
        "synthetic": bool(synthetic),
        "stages": {
            stage: {"status": "pending", "message": "", "artifacts": [], "data": {}, "updated_at": None}
            for stage in STAGES
        },
    }
    data["stages"]["import"]["status"] = "ok"
    data["stages"]["import"]["message"] = f"已登记输入来源：{source_type}"
    data["stages"]["import"]["updated_at"] = _now()
    _write_case(case_id, data)
    _record_activity("lifecycle", "case_created", case_id)
    return case_id


def _write_case(case_id: str, data: dict) -> None:
    path = case_dir(case_id) / CASE_FILE
    # 轮询接口可能在后台阶段写入时同时读取；先写同目录临时文件，再原子替换，避免读到半截 JSON。
    temp_path = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def get_case(case_id: str) -> dict:
    path = case_dir(case_id) / CASE_FILE
    if not path.exists():
        raise PipelineError("failed", f"病例不存在：{case_id}", ["确认 case_id 是否正确"])
    return json.loads(path.read_text(encoding="utf-8"))


def update_stage(
    case_id: str,
    stage: str,
    status: str,
    message: str | None = None,
    artifacts: list[str] | None = None,
    data: dict | None = None,
) -> dict:
    """更新某个阶段的状态、说明与产物相对路径。"""
    if stage not in STAGES:
        raise PipelineError("failed", f"未知阶段：{stage}", [f"合法阶段：{', '.join(STAGES)}"])
    if status not in STAGE_STATUS:
        raise PipelineError("failed", f"未知状态：{status}", [f"合法状态：{', '.join(STAGE_STATUS)}"])

    case = get_case(case_id)
    entry = case["stages"][stage]
    entry["status"] = status
    if message is not None:
        entry["message"] = message
    if artifacts is not None:
        entry["artifacts"] = list(artifacts)
    if data is not None:
        entry["data"].update(data)
    entry["updated_at"] = _now()
    case["updated_at"] = _now()
    _write_case(case_id, case)
    _record_activity("stage", "updated", case_id, target="stage", details={"stage": stage, "status": status})
    return entry


def invalidate_downstream(case_id: str, reason: str = "上游气道掩膜已变化，需要重新生成") -> None:
    """使依赖旧掩膜的几何、CFD 和报告产物失效。

    掩膜确认或替换后，旧 STL/算例不能继续以“当前结果”对外提供；同时清理
    对应目录，避免旧文件被直接 URL 或报告接口误取。
    """
    root = case_dir(case_id)
    case = get_case(case_id)
    invalidated = []
    for stage in ("surface", "cfd_prep", "cfd_run", "report"):
        entry = case["stages"].get(stage)
        if not entry:
            continue
        entry["status"] = "pending"
        entry["message"] = reason
        entry["artifacts"] = []
        entry["data"] = {}
        entry["updated_at"] = _now()
        invalidated.append(stage)
    for name in ("surface", "cfd", "report"):
        target = root / name
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
    case["updated_at"] = _now()
    _write_case(case_id, case)
    for stage in invalidated:
        _record_activity("stage", "invalidated", case_id, target="stage", details={"stage": stage, "status": "pending"})


def update_job(
    case_id: str,
    status: str,
    stage: str | None = None,
    message: str | None = None,
) -> dict:
    """记录跨阶段后台任务的当前子步骤，供页面刷新后恢复进度。"""
    case = get_case(case_id)
    job = case.setdefault("job", {})
    job["status"] = status
    job["stage"] = stage
    if message is not None:
        job["message"] = message
    job["updated_at"] = _now()
    case["updated_at"] = _now()
    _write_case(case_id, case)
    _record_activity("stage", "updated", case_id, target="job", details={"stage": stage, "status": status})
    return job


def recover_interrupted_cases() -> int:
    """服务启动时收尾上次进程留下的 running 状态。

    仅在接收新请求前调用；当前服务的任务此时尚未开始。非运行病例不写盘。
    返回实际恢复的病例数，供启动日志和测试核对。
    """
    ensure_dirs()
    base = CASES_DIR.resolve()
    recovered = 0
    for path in sorted(CASES_DIR.glob(f"*/{CASE_FILE}")):
        root = path.parent
        if root.is_symlink() or path.is_symlink() or root.resolve().parent != base:
            LOGGER.warning("跳过病例目录范围外的状态文件：%s", root.name)
            continue
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            LOGGER.warning("无法读取病例状态，未执行中断恢复：%s", root.name)
            continue
        if not isinstance(case, dict) or not isinstance(case.get("stages"), dict):
            LOGGER.warning("病例状态格式无效，未执行中断恢复：%s", root.name)
            continue

        now = _now()
        changed = False
        for entry in case["stages"].values():
            if isinstance(entry, dict) and entry.get("status") == "running":
                entry.update(status="failed", message=INTERRUPTED_MESSAGE, updated_at=now)
                changed = True
        job = case.get("job")
        if isinstance(job, dict) and job.get("status") == "running":
            job.update(status="failed", message=INTERRUPTED_MESSAGE, updated_at=now)
            changed = True
        if changed:
            case["updated_at"] = now
            _write_case(root.name, case)
            recovered += 1
    return recovered


def require_stage(case_id: str, stage: str) -> dict:
    """前序闸门：要求某阶段已经 ok/warn，否则抛 blocked。"""
    case = get_case(case_id)
    entry = case["stages"][stage]
    if entry["status"] not in ("ok", "warn"):
        raise PipelineError(
            "blocked",
            f"阶段 {stage} 尚未通过（当前状态：{entry['status']}），后续步骤不执行",
            [f"先完成 {stage} 阶段并确认其状态为 ok"],
            {"stage": stage, "status": entry["status"], "message": entry["message"]},
        )
    return entry


def list_cases() -> list[dict]:
    ensure_dirs()
    out = []
    for path in sorted(CASES_DIR.glob("*/case.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        stage_statuses = {
            key: (
                "skipped"
                if key == "anonymize" and value.get("data", {}).get("skipped")
                else value["status"]
            )
            for key, value in data.get("stages", {}).items()
        }
        out.append(
            {
                "case_id": data.get("case_id"),
                "created_at": data.get("created_at"),
                "source_type": data.get("source_type"),
                "synthetic": data.get("synthetic", False),
                "stages": stage_statuses,
                "job": data.get("job") or {"status": "idle", "stage": None, "message": ""},
            }
        )
    return out


def delete_case(case_id: str) -> None:
    """删除一个已经停止处理的本地病例及其全部产物。

    删除边界固定在 ``cases/<case_id>``，并拒绝删除仍有后台阶段运行的病例。
    """
    root = case_dir(case_id)
    try:
        case = get_case(case_id)
    except PipelineError:
        raise

    job = case.get("job") or {}
    running = job.get("status") == "running" or any(
        entry.get("status") == "running" for entry in case.get("stages", {}).values()
    )
    if running:
        raise PipelineError(
            "blocked",
            "病例正在处理中，暂不能删除",
            ["等待当前阶段完成或失败后，再回到病例页删除"],
            {"case_id": case_id, "stage": job.get("stage")},
        )

    base = CASES_DIR.resolve()
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise PipelineError("failed", f"无法定位病例目录：{case_id}", ["刷新病例列表后重试"]) from exc
    if root.is_symlink() or resolved.parent != base:
        raise PipelineError("failed", "病例目录不在本地病例存储范围内", ["刷新病例列表后重试"])
    if not (resolved / CASE_FILE).is_file():
        raise PipelineError("failed", f"病例不存在：{case_id}", ["刷新病例列表后重试"])

    try:
        shutil.rmtree(resolved)
    except OSError as exc:
        raise PipelineError("failed", f"删除病例失败：{exc}", ["确认风见没有正在读取该病例，再重试"]) from exc


def artifact_path(case_id: str, rel: str) -> Path:
    """把病例内的相对路径解析成绝对路径，并阻断目录穿越。"""
    root = case_dir(case_id).resolve()
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise PipelineError("failed", "请求的文件不在该病例目录内", ["检查相对路径是否正确"])
    return target
