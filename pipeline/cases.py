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
    # The ID has only four random hex digits. Reserve the directory atomically:
    # reusing an existing directory would overwrite another operator's case.
    for _ in range(32):
        case_id = _new_case_id()
        root = case_dir(case_id)
        # A partially deleted case has no directory but still owns its ID.
        # Never reuse that ID while its deletion marker is awaiting cleanup.
        marker = _deletion_marker(case_id)
        if marker.exists() or marker.is_symlink():
            continue
        try:
            root.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        break
    else:
        raise PipelineError("failed", "无法分配唯一病例编号", ["请稍后重试创建病例"])

    data = {
        "case_id": case_id,
        "created_at": _now(),
        "updated_at": _now(),
        "source_type": source_type,          # local_path / direct_link / synthetic
        # 不把用户输入的绝对路径写入病例状态；路径可能包含患者姓名/住院号。
        "source_path": {
            "browser_upload": "<浏览器上传的 DICOM 文件夹>",
            "direct_link": "<授权直链下载的 DICOM 文件夹>",
            "synthetic": "<合成测试数据>",
        }.get(source_type, "<本机 DICOM 文件夹>"),
        "synthetic": bool(synthetic),
        "stages": {
            stage: {"status": "pending", "message": "", "artifacts": [], "data": {}, "updated_at": None}
            for stage in STAGES
        },
    }
    data["stages"]["import"]["status"] = "ok"
    data["stages"]["import"]["message"] = f"已登记输入来源：{source_type}"
    data["stages"]["import"]["updated_at"] = _now()
    try:
        for sub in ("import", "anonymized", "volume", "segment", "surface", "cfd", "report", "preview"):
            (root / sub).mkdir()
        _write_case(case_id, data)
    except Exception:
        shutil.rmtree(root)
        raise
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
        marker = CASES_DIR / f".windcatcher-deleting-{path.parent.name}.json"
        if path.parent.name.startswith(".") or marker.exists() or marker.is_symlink():
            continue
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


def check_case_deletable(case_id: str) -> Path:
    """预检本地病例是否已停止处理且位于允许删除的目录。

    只返回经核验的 ``cases/<case_id>`` 路径，不执行删除。
    """
    root = case_dir(case_id)
    try:
        case = get_case(case_id)
    except PipelineError:
        raise

    from .segmentation_model_jobs import case_busy
    if case_busy(case_id):
        raise PipelineError('blocked', 'AI 分割候选正在处理，请先取消任务', ['等待候选完成或取消后再删除病例'])
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

    return resolved


def _deletion_marker(case_id: str) -> Path:
    return CASES_DIR / f".windcatcher-deleting-{case_dir(case_id).name}.json"


def _read_deletion_marker(case_id: str) -> dict | None:
    marker = _deletion_marker(case_id)
    if marker.is_symlink():
        raise PipelineError("failed", "病例删除标记无效", ["联系管理员检查病例存储"])
    if not marker.exists():
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8")) if marker.is_file() else None
    except (OSError, UnicodeError, ValueError):
        data = None
    if (not isinstance(data, dict) or data.get("case_id") != case_id
            or not (data.get("owner_id") is None or isinstance(data.get("owner_id"), str))):
        raise PipelineError("failed", "病例删除标记损坏", ["联系管理员检查病例存储"])
    return data


def deletion_pending(case_id: str) -> bool:
    """A marker permits retry after a partial file delete or identity failure."""
    return _read_deletion_marker(case_id) is not None


def deletion_owner(case_id: str) -> str | None:
    marker = _read_deletion_marker(case_id)
    return marker["owner_id"] if marker is not None else None


def begin_case_deletion(case_id: str, owner_id: str | None = None) -> None:
    """Atomically mark an authorized, idle case before revoking public views."""
    marker_data = _read_deletion_marker(case_id)
    if marker_data is not None:
        if marker_data["owner_id"] != owner_id:
            raise PipelineError("failed", "病例删除由另一位所有者启动", ["由原所有者继续删除"])
        return
    check_case_deletable(case_id)
    marker = _deletion_marker(case_id)
    temporary = marker.with_name(marker.name + f".{secrets.token_hex(4)}.tmp")
    try:
        temporary.write_text(json.dumps({"case_id": case_id, "owner_id": owner_id}), encoding="utf-8")
        temporary.replace(marker)
    finally:
        temporary.unlink(missing_ok=True)


def finish_case_deletion(case_id: str) -> None:
    marker = _deletion_marker(case_id)
    if deletion_pending(case_id):
        marker.unlink()


def delete_case(case_id: str, *, keep_marker: bool = False, owner_id: str | None = None) -> None:
    """Delete a case after its durable marker; retain it until ACL retirement."""
    begin_case_deletion(case_id, owner_id)
    root = case_dir(case_id)
    if root.is_symlink():
        raise PipelineError("failed", "病例目录不在本地病例存储范围内", ["联系管理员检查病例存储"])
    if root.exists():
        resolved = root.resolve(strict=True)
        if resolved.parent != CASES_DIR.resolve() or not resolved.is_dir():
            raise PipelineError("failed", "病例目录不在本地病例存储范围内", ["联系管理员检查病例存储"])
        try:
            shutil.rmtree(resolved)
        except OSError as exc:
            raise PipelineError("failed", f"删除病例失败：{exc}", ["确认风见没有正在读取该病例，再重试"]) from exc
    if not keep_marker:
        finish_case_deletion(case_id)


def artifact_path(case_id: str, rel: str) -> Path:
    """把病例内的相对路径解析成绝对路径，并阻断目录穿越。"""
    root = case_dir(case_id).resolve()
    target = (root / rel).resolve()
    try:
        relative = target.relative_to(root)
    except ValueError:
        raise PipelineError("failed", "请求的文件不在该病例目录内", ["检查相对路径是否正确"])
    if relative.parts and relative.parts[0].lower() == 'learning':
        raise PipelineError('blocked', '学习快照不通过通用产物接口下载', ['使用学习摘要接口'])
    return target
