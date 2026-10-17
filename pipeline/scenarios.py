"""鼻塞方案的版本化资产、基准绑定和人工审核记录。

本模块只负责研究资产的保存与追溯，不生成医疗器械建议，也不执行鼻塞 CFD。
原始 STL、毫米规范化 STL、每次摆位版本、审核记录和长期审计事件均落在病例
自己的 ``scenarios/<scenario_id>`` 目录中。
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import time
import zipfile
from datetime import datetime
from pathlib import Path

from . import activity_monitor, cases, plug_models, scenario_store
from .errors import PipelineError

LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 2
SCENARIO_ID_PATTERN = r"^plug-\d{8}-\d{6}-[0-9a-f]{8}$"
_SCENARIO_ID_RE = re.compile(SCENARIO_ID_PATTERN)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _new_scenario_id() -> str:
    return f"plug-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}"


def _validate_scenario_id(scenario_id: str) -> str:
    if not isinstance(scenario_id, str) or not _SCENARIO_ID_RE.fullmatch(scenario_id):
        raise PipelineError("failed", "鼻塞方案编号无效", ["刷新病例页面后重新选择方案"])
    return scenario_id


def scenario_dir(case_id: str, scenario_id: str) -> Path:
    cases.get_case(case_id)
    _validate_scenario_id(scenario_id)
    scenarios_root = cases.case_dir(case_id) / "scenarios"
    if scenarios_root.is_symlink():
        raise PipelineError("failed", "鼻塞方案存储目录无效", ["检查病例目录后重试"])
    root = scenarios_root / scenario_id
    if root.is_symlink():
        raise PipelineError("failed", "鼻塞方案目录无效", ["检查病例目录后重试"])
    try:
        root.resolve(strict=False).relative_to(scenarios_root.resolve())
    except ValueError as exc:
        raise PipelineError("failed", "鼻塞方案目录不在病例存储范围内", ["刷新病例列表后重试"]) from exc
    return root


def _now_file_hash(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return plug_models.sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise PipelineError("failed", "气道基准产物无法读取", ["重新生成气道模型后重试"]) from exc


def _read_json(path: Path, *, label: str) -> dict:
    for attempt in range(3):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            break
        except (PermissionError, FileNotFoundError) as exc:
            if attempt < 2:
                time.sleep(0.01)
                continue
            if isinstance(exc, FileNotFoundError):
                raise PipelineError("failed", f"鼻塞方案{label}缺失", ["刷新病例列表后重试"]) from exc
            raise PipelineError("failed", f"鼻塞方案{label}无法读取", ["检查本机病例目录后重试"]) from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PipelineError("failed", f"鼻塞方案{label}无法读取", ["检查本机病例目录后重试"]) from exc
    if not isinstance(value, dict):
        raise PipelineError("failed", f"鼻塞方案{label}格式无效", ["删除损坏的方案后重新上传"])
    return value


def _base_reference(case_id: str) -> dict:
    """捕获当前气道基准指纹，不写入患者标识。"""
    case = cases.get_case(case_id)
    root = cases.case_dir(case_id)
    mask_version = None
    segmentation_path = root / "segment" / "segmentation.json"
    if segmentation_path.is_file() and not segmentation_path.is_symlink():
        segmentation = _read_json(segmentation_path, label="气道分割记录")
        if type(segmentation.get("mask_version")) is int and segmentation["mask_version"] >= 1:
            mask_version = segmentation["mask_version"]
    surface_stage = (case.get("stages") or {}).get("surface") or {}
    surface_status = surface_stage.get("status") if isinstance(surface_stage, dict) else None
    surface_data = surface_stage.get("data") if isinstance(surface_stage, dict) else {}
    surface_mask_version = surface_data.get("mask_version") if isinstance(surface_data, dict) else None
    surface_stl_sha = _now_file_hash(root / "surface" / "airway.stl")
    surface_json_sha = _now_file_hash(root / "surface" / "surface.json")
    endplanes_sha = _now_file_hash(root / "segment" / "endplanes.json")
    available = bool(
        mask_version is not None and surface_status in {"ok", "warn"} and surface_stl_sha and surface_json_sha
        and surface_mask_version == mask_version
    )
    value = {
        "state": "available" if available else "unavailable",
        "mask_version": mask_version,
        "surface_mask_version": surface_mask_version if type(surface_mask_version) is int else None,
        "surface_status": surface_status if surface_status in {"ok", "warn"} else "unavailable",
        "surface_stl_sha256": surface_stl_sha,
        "surface_json_sha256": surface_json_sha,
        "endplanes_sha256": endplanes_sha,
    }
    value["fingerprint"] = plug_models.sha256_bytes(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return value


def _base_status(reference: dict | None, current: dict) -> str:
    if not isinstance(reference, dict):
        return "stale"
    if reference.get("fingerprint") != current.get("fingerprint"):
        return "stale"
    return "current" if current.get("state") == "available" else "unavailable"


def _check_expected_version(scenario: dict, expected_version: int | None) -> None:
    if expected_version is None:
        return
    if type(expected_version) is not int or expected_version < 1:
        raise PipelineError("failed", "期望版本号无效", ["刷新方案后重新提交当前版本号"])
    if expected_version != scenario.get("current_version"):
        raise PipelineError(
            "blocked", "鼻塞方案版本已变化，当前页面数据已过期",
            ["刷新方案详情后重新确认摆位或审核"],
            {"expected_version": expected_version, "current_version": scenario.get("current_version")},
        )


def _atomic_bytes(path: Path, data: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        try:
            temporary.write_bytes(data)
            temporary.replace(path)
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError as exc:
                    LOGGER.error("Unable to remove scenario temporary file: %s", type(exc).__name__)
    except OSError as exc:
        raise PipelineError("failed", "鼻塞方案文件写入失败", ["检查病例目录权限和磁盘空间后重试"]) from exc


def _read_bytes(path: Path, *, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise PipelineError("failed", f"鼻塞方案{label}缺失", ["重新生成该方案版本或重新上传模型"])
    try:
        return path.read_bytes()
    except OSError as exc:
        raise PipelineError("failed", f"鼻塞方案{label}无法读取", ["检查本机病例目录后重试"]) from exc


def _atomic_json(path: Path, value: dict) -> None:
    _atomic_bytes(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"))


def _version_name(version: int) -> str:
    if type(version) is not int or version < 1:
        raise PipelineError("failed", "鼻塞方案版本号无效", ["刷新方案后重试"])
    return f"v{version:04d}"


def _version_dir(root: Path, version: int) -> Path:
    target = root / "versions" / _version_name(version)
    if target.is_symlink() or not target.is_dir():
        raise PipelineError("failed", f"鼻塞方案版本不存在：{version}", ["刷新方案列表后选择当前版本"])
    return target


def _write_manifest(root: Path, version_root: Path, manifest: dict) -> None:
    _atomic_json(version_root / "manifest.json", manifest)
    _atomic_json(root / "manifest.json", manifest)


def _copy_current(version_root: Path, root: Path, *, review: dict | None = None) -> None:
    placed = version_root / "placed" / "model.stl"
    placement = version_root / "placement.json"
    placed_bytes = _read_bytes(placed, label="摆位模型")
    if placement.is_symlink() or not placement.is_file():
        raise PipelineError("failed", "鼻塞方案版本文件不完整", ["重新保存摆位或重新上传模型"])
    _atomic_bytes(root / "placed" / "model.stl", placed_bytes)
    _atomic_json(root / "placement.json", _read_json(placement, label="摆位记录"))
    if review is not None:
        _atomic_json(root / "review.json", review)


def _write_version_snapshot(version_root: Path, scenario: dict) -> None:
    _atomic_json(version_root / "scenario.json", scenario)


def _assert_version_not_running(root: Path, version: int) -> None:
    """摆位和审核不能覆盖正在读取的 CFD 版本。"""
    task_path = root / "versions" / _version_name(version) / "scenario_task.json"
    if task_path.is_file() and not task_path.is_symlink():
        try:
            task = json.loads(task_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PipelineError("failed", "鼻塞 CFD 任务记录无法读取", ["检查任务状态后重试"]) from exc
        if isinstance(task, dict) and task.get("status") in {"queued", "running"}:
            raise PipelineError("blocked", "当前鼻塞版本正在执行 CFD，不能修改摆位或审核", ["等待任务完成后再创建新的摆位版本"])


def _manifest(*, case_id: str, scenario_id: str, version: int, unit: str,
              original_bytes: bytes, normalized_bytes: bytes, placed_bytes: bytes,
              model_qc: dict, placed_qc: dict, placement: dict, review: dict,
              base_reference: dict) -> dict:
    review_ready = bool(
        review.get("approved") and placed_qc.get("can_use_for_cfd")
        and base_reference.get("state") == "available"
    )
    return {
        "schema_version": SCHEMA_VERSION, "purpose": "research_asset_only",
        "case_id": case_id, "scenario_id": scenario_id, "version": version,
        "created_at": _now(), "unit_input": unit, "coordinate_unit": "mm",
        "files": {"original": "input/original.stl", "normalized": "normalized/model.stl",
                   "placed": "placed/model.stl", "placement": "placement.json", "review": "review.json"},
        "sha256": {"original": plug_models.sha256_bytes(original_bytes),
                   "normalized": plug_models.sha256_bytes(normalized_bytes),
                   "placed": plug_models.sha256_bytes(placed_bytes)},
        "model_qc": model_qc, "placed_qc": placed_qc, "placement": placement,
        "review": review, "base_reference": base_reference,
        "review_ready": review_ready,
        "cfd_ready": False,
        "cfd_blockers": [] if review_ready else ["manual_review_or_current_airway_required"],
    }


def _scenario_response(scenario: dict) -> dict:
    return json.loads(json.dumps(scenario, ensure_ascii=False))


def _with_current_base_status(case_id: str, scenario: dict) -> dict:
    result = _scenario_response(scenario)
    current = _base_reference(case_id)
    result["base_status"] = _base_status(result.get("base_reference"), current)
    result["current_base_reference"] = current
    task_path = scenario_dir(case_id, result["scenario_id"]) / "versions" / _version_name(int(result["current_version"])) / "scenario_task.json"
    if task_path.is_file():
        task = _read_json(task_path, label="CFD 任务")
        result["task"] = task
        if task.get("status") in {"queued", "running"}:
            result["cfd"] = {**(result.get("cfd") or {}), "status": "running"}
        elif task.get("status") in {"failed", "blocked"}:
            result["cfd"] = {**(result.get("cfd") or {}), "status": task["status"], "reason": task.get("reason") or task.get("message")}
    return result


def _record_activity(case_id: str, action: str, scenario_id: str, details: dict) -> None:
    """业务账本和长期 JSONL 必须落盘；短期监控故障单独记录。"""
    root = scenario_dir(case_id, scenario_id)
    event = {"event_id": secrets.token_hex(16), "timestamp": _now(), "action": action, "case_id": case_id,
             "scenario_id": scenario_id, "details": details}
    selected_version = details.get("version")
    record_root = root / "versions" / _version_name(selected_version) if selected_version is not None else root
    scenario = _read_json(record_root / "scenario.json", label="状态")
    manifest = _read_json(record_root / "manifest.json", label="清单")
    scenario_store.record(root, scenario, manifest, event)
    try:
        with (root / "audit.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except (OSError, ValueError, TypeError) as exc:
        raise PipelineError(
            "failed", "鼻塞研究审计文件写入失败；业务记录已入数据库，请刷新核对",
            ["检查病例目录权限和磁盘空间后重试"], {"operation_may_be_saved": True},
        ) from exc
    try:
        activity_monitor.record_event({"kind": "stage", "action": action, "case_id": case_id,
                                       "target": "scenario", "details": {"scenario_id": scenario_id, **details}})
    except Exception as exc:
        LOGGER.error("Scenario short-term monitor failed for %s: %s", scenario_id, type(exc).__name__)


def record_event(case_id: str, scenario_id: str, action: str, details: dict | None = None) -> None:
    """写入不改变方案状态的长期业务事件（例如下载和查看对比）。"""
    _record_activity(case_id, action, scenario_id, details or {})


def create_scenario(case_id: str, data: bytes, unit: str) -> dict:
    cases.get_case(case_id)
    normalized, model_qc, normalized_bytes = plug_models.normalized_mesh(data, unit)
    scenario_id = _new_scenario_id()
    scenarios_root = cases.case_dir(case_id) / "scenarios"
    try:
        scenarios_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PipelineError("failed", "鼻塞方案存储目录不可写", ["检查病例目录权限和磁盘空间后重试"]) from exc
    if scenarios_root.is_symlink():
        raise PipelineError("failed", "鼻塞方案存储目录无效", ["检查病例目录后重试"])
    root = scenarios_root / scenario_id
    staging = scenarios_root / f".{scenario_id}.{secrets.token_hex(4)}.tmp"
    version = 1
    version_root = staging / "versions" / _version_name(version)
    placement = {"version": version, "confirmed": False, "translation_mm": [0.0, 0.0, 0.0],
                 "rotation_deg": [0.0, 0.0, 0.0], "scale": [1.0, 1.0, 1.0], "updated_at": _now()}
    placed_bytes = plug_models.mesh_to_binary_stl(normalized, "Windcatcher placed STL")
    placed_qc = plug_models.inspect_mesh(normalized, unit="mm")
    review = {"status": "pending", "approved": False, "history": []}
    base_reference = _base_reference(case_id)
    status = "uploaded" if model_qc.get("verdict") == "pass" else "geometry_blocked"
    scenario = {"schema_version": SCHEMA_VERSION, "scenario_id": scenario_id, "case_id": case_id,
                "created_at": _now(), "updated_at": _now(), "status": status, "current_version": version,
                "model": {"unit_input": str(unit).strip().lower(), "coordinate_unit": "mm",
                          "original_sha256": plug_models.sha256_bytes(data),
                          "normalized_sha256": plug_models.sha256_bytes(normalized_bytes),
                          "original_bytes": len(data), "qc": model_qc},
                "placement": placement, "review": review, "base_reference": base_reference,
                "base_status": "current" if base_reference.get("state") == "available" else "unavailable",
                "cfd": {"status": "not_started"},
                "versions": [{"version": version, "created_at": placement["updated_at"],
                              "placement_confirmed": False, "review_status": "pending",
                              "placed_qc": placed_qc, "cfd_ready": False, "review_ready": False}]}
    try:
        _atomic_bytes(staging / "input" / "original.stl", bytes(data))
        _atomic_bytes(staging / "normalized" / "model.stl", normalized_bytes)
        _atomic_bytes(version_root / "placed" / "model.stl", placed_bytes)
        _atomic_json(version_root / "placement.json", placement)
        _atomic_json(version_root / "review.json", review)
        manifest = _manifest(case_id=case_id, scenario_id=scenario_id, version=version,
                             unit=str(unit).strip().lower(), original_bytes=data,
                             normalized_bytes=normalized_bytes, placed_bytes=placed_bytes,
                             model_qc=model_qc, placed_qc=placed_qc, placement=placement,
                             review=review, base_reference=base_reference)
        _atomic_json(version_root / "manifest.json", manifest)
        _write_version_snapshot(version_root, scenario)
        _copy_current(version_root, staging, review=review)
        _atomic_json(staging / "manifest.json", manifest)
        _atomic_json(staging / "scenario.json", scenario)
        staging.replace(root)
    except (OSError, PipelineError) as exc:
        if isinstance(exc, PipelineError):
            raise
        raise PipelineError("failed", "鼻塞方案写入失败，未发布不完整方案",
                            ["检查病例目录权限和磁盘空间后重试"]) from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    _record_activity(case_id, "scenario_created", scenario_id,
                     {"version": version, "status": status, "geometry_verdict": model_qc.get("verdict")})
    return _with_current_base_status(case_id, scenario)


def list_scenarios(case_id: str) -> list[dict]:
    cases.get_case(case_id)
    root = cases.case_dir(case_id) / "scenarios"
    if not root.is_dir() or root.is_symlink():
        return []
    output = []
    for path in sorted(root.glob("*/scenario.json")):
        value = _read_json(path, label="状态")
        if (not path.is_symlink() and not path.parent.is_symlink()
                and value.get("case_id") == case_id
                and _SCENARIO_ID_RE.fullmatch(str(value.get("scenario_id", "")))):
            output.append(_with_current_base_status(case_id, value))
    return sorted(output, key=lambda item: item.get("updated_at", ""), reverse=True)


def get_scenario(case_id: str, scenario_id: str) -> dict:
    return _with_current_base_status(case_id, _read_json(scenario_dir(case_id, scenario_id) / "scenario.json", label="状态"))


def update_cfd_state(case_id: str, scenario_id: str, *, version: int, state: str, details: dict | None = None) -> dict:
    """记录某个摆位版本的独立 CFD 适配/求解状态。

    CFD 产物保存在版本目录内；这里仅更新索引和审计，不把方案结果写入
    基准病例的 ``cfd`` 阶段，避免刷新病例时误把两个算例混在一起。
    """
    allowed = {"prepared", "running", "solved", "missing", "failed", "blocked"}
    if state not in allowed:
        raise PipelineError("failed", "鼻塞 CFD 状态无效", [f"合法状态：{', '.join(sorted(allowed))}"])
    root = scenario_dir(case_id, scenario_id)
    scenario = _read_json(root / "scenario.json", label="状态")
    if type(version) is not int or version < 1 or version > scenario.get("current_version", 0):
        raise PipelineError("blocked", "鼻塞方案 CFD 版本无效", ["刷新方案详情后重新选择版本"])
    is_current = version == scenario["current_version"]
    version_root = _version_dir(root, version)
    if not is_current:
        scenario = _read_json(version_root / "scenario.json", label="版本状态")
    now = _now()
    record = {"status": state, "version": version, "updated_at": now, **(details or {})}
    scenario["updated_at"] = now
    scenario["cfd"] = record
    scenario["versions"] = [
        {**item, "cfd_status": state, "cfd_ready": bool(record.get("cfd_ready", item.get("cfd_ready", False)))}
        if item.get("version") == version else item
        for item in scenario.get("versions", [])
    ]
    if is_current:
        _atomic_json(root / "scenario.json", scenario)
    _write_version_snapshot(version_root, scenario)
    _record_activity(case_id, "scenario_cfd_state_updated", scenario_id,
                     {"version": version, "status": state, "numeric_ok": record.get("numeric_ok"),
                      "comparison_status": record.get("comparison_status")})
    return _with_current_base_status(case_id, scenario)


def update_placement(case_id: str, scenario_id: str, placement: dict, *, expected_version: int | None = None) -> dict:
    scenario = get_scenario(case_id, scenario_id)
    _check_expected_version(scenario, expected_version)
    root = scenario_dir(case_id, scenario_id)
    _assert_version_not_running(root, int(scenario.get("current_version", 0)))
    normalized_path = root / "normalized" / "model.stl"
    try:
        normalized = plug_models.parse_stl(_read_bytes(normalized_path, label="规范化模型"))
    except (OSError, PipelineError) as exc:
        if isinstance(exc, PipelineError):
            raise
        raise PipelineError("failed", "规范化鼻塞模型缺失，无法保存摆位", ["重新上传鼻塞模型"]) from exc
    normalized_placement = plug_models.validate_placement(placement)
    transformed = plug_models.transform_mesh(normalized, normalized_placement)
    placed_qc = plug_models.inspect_mesh(transformed, unit="mm")
    next_version = int(scenario.get("current_version", 0)) + 1
    version_root = root / "versions" / _version_name(next_version)
    placement_record = {"version": next_version, "confirmed": True, **normalized_placement, "updated_at": _now()}
    review = {"status": "pending", "approved": False,
              "history": list((scenario.get("review") or {}).get("history") or [])}
    placed_bytes = plug_models.mesh_to_binary_stl(transformed, "Windcatcher placed STL")
    original_bytes = _read_bytes(root / "input" / "original.stl", label="原始模型")
    normalized_bytes = _read_bytes(normalized_path, label="规范化模型")
    # 新摆位版本重新绑定当前气道产物；这样在气道重建后重新摆位即可解除 stale。
    base_reference = _base_reference(case_id)
    manifest = _manifest(case_id=case_id, scenario_id=scenario_id, version=next_version,
                         unit=str(scenario.get("model", {}).get("unit_input", "mm")),
                         original_bytes=original_bytes, normalized_bytes=normalized_bytes,
                         placed_bytes=placed_bytes, model_qc=scenario.get("model", {}).get("qc", {}),
                         placed_qc=placed_qc, placement=placement_record, review=review,
                         base_reference=base_reference)
    _atomic_bytes(version_root / "placed" / "model.stl", placed_bytes)
    _atomic_json(version_root / "placement.json", placement_record)
    _atomic_json(version_root / "review.json", review)
    _write_manifest(root, version_root, manifest)
    status = "placement_pending_review" if placed_qc.get("verdict") == "pass" else "geometry_blocked"
    scenario["updated_at"] = _now(); scenario["status"] = status; scenario["current_version"] = next_version
    scenario["placement"] = placement_record; scenario["review"] = review
    scenario["cfd"] = {"status": "not_started", "version": next_version, "updated_at": scenario["updated_at"]}
    scenario["base_reference"] = base_reference
    scenario["base_status"] = _base_status(base_reference, _base_reference(case_id))
    scenario["versions"] = list(scenario.get("versions") or []) + [{"version": next_version,
        "created_at": placement_record["updated_at"], "placement_confirmed": True,
        "review_status": "pending", "placed_qc": placed_qc, "cfd_ready": False, "review_ready": False}]
    _atomic_json(root / "scenario.json", scenario)
    _write_version_snapshot(version_root, scenario)
    _record_activity(case_id, "scenario_placement_updated", scenario_id,
                     {"version": next_version, "status": status, "geometry_verdict": placed_qc.get("verdict")})
    return _with_current_base_status(case_id, scenario)


def record_review(case_id: str, scenario_id: str, *, approved: bool, reviewer_role: str,
                  notes: str = "", expected_version: int | None = None) -> dict:
    scenario = get_scenario(case_id, scenario_id)
    _check_expected_version(scenario, expected_version)
    if not (scenario.get("placement") or {}).get("confirmed"):
        raise PipelineError("blocked", "鼻塞模型尚未完成摆位确认，不能提交人工审核",
                            ["先保存平移、旋转和缩放后的摆位"])
    current_base = _base_reference(case_id)
    base_status = _base_status(scenario.get("base_reference"), current_base)
    if base_status == "stale":
        raise PipelineError("blocked", "气道基准已变化，不能审核当前鼻塞摆位",
                            ["重新生成当前气道模型，再重新确认鼻塞摆位"], {"base_status": base_status})
    role = str(reviewer_role or "").strip()
    if not 1 <= len(role) <= 80 or "\x00" in role:
        raise PipelineError("failed", "审核角色不能为空且长度不能超过 80 个字符",
                            ["填写医生、工程师或研究人员等角色"])
    notes = str(notes or "").strip()
    if len(notes) > 2000 or "\x00" in notes:
        raise PipelineError("failed", "审核备注长度或格式无效", ["删减备注后重试"])
    root = scenario_dir(case_id, scenario_id)
    version = int(scenario["current_version"])
    _assert_version_not_running(root, version)
    version_root = _version_dir(root, version)
    manifest = _read_json(version_root / "manifest.json", label="清单")
    placed_qc = manifest.get("placed_qc") or {}
    if approved and not placed_qc.get("can_use_for_cfd"):
        raise PipelineError("blocked", "当前摆位未通过几何预检，不能通过人工审核",
                            ["修复 STL 几何后重新上传", "确认摆位没有让模型退化"], {"placed_qc": placed_qc})
    review = {"status": "approved" if approved else "rejected", "approved": bool(approved),
              "reviewer_role": role, "notes": notes, "version": version, "reviewed_at": _now(),
              "history": list((scenario.get("review") or {}).get("history") or [])}
    review["history"].append({"approved": review["approved"], "reviewer_role": role,
                               "notes": notes, "version": version, "reviewed_at": review["reviewed_at"]})
    manifest["review"] = review
    manifest["review_ready"] = bool(approved and placed_qc.get("can_use_for_cfd")
                                     and current_base.get("state") == "available")
    manifest["cfd_ready"] = False
    # 审核备注或状态变化不删除旧 CFD 产物；它们仍按同一摆位版本留存，
    # 便于追溯。未通过审核时，prepare/run 接口仍会阻断使用该结果。
    _atomic_json(version_root / "review.json", review)
    _write_manifest(root, version_root, manifest)
    _atomic_json(root / "review.json", review)
    (root / "exports" / f"scenario-v{version:04d}.zip").unlink(missing_ok=True)
    scenario["updated_at"] = _now(); scenario["review"] = review
    scenario["cfd"] = {"status": "not_started", "version": version, "updated_at": scenario["updated_at"]}
    scenario["status"] = "reviewed" if approved else "rejected"
    scenario["base_status"] = base_status
    scenario["versions"] = [{**item, "review_status": review["status"], "cfd_ready": False,
                              "review_ready": manifest["review_ready"]}
                             if item.get("version") == version else item for item in scenario.get("versions", [])]
    _atomic_json(root / "scenario.json", scenario)
    _write_version_snapshot(version_root, scenario)
    _record_activity(case_id, "scenario_reviewed", scenario_id,
                     {"version": version, "status": review["status"], "cfd_ready": False})
    return _with_current_base_status(case_id, scenario)


def _artifact_for_version(root: Path, version: int, artifact: str) -> tuple[Path, str, str]:
    version_root = _version_dir(root, version)
    mapping = {
        "original": (root / "input" / "original.stl", "model/stl", f"original-v{version:04d}.stl"),
        "normalized": (root / "normalized" / "model.stl", "model/stl", f"normalized-v{version:04d}.stl"),
        "placed": (version_root / "placed" / "model.stl", "model/stl", f"placed-v{version:04d}.stl"),
        "placement": (version_root / "placement.json", "application/json", f"placement-v{version:04d}.json"),
        "review": (version_root / "review.json", "application/json", f"review-v{version:04d}.json"),
        "manifest": (version_root / "manifest.json", "application/json", f"manifest-v{version:04d}.json"),
        "comparison": (version_root / "cfd" / "comparison.json", "application/json", f"comparison-v{version:04d}.json"),
        "cfd-meta": (version_root / "cfd" / "scenario_meta.json", "application/json", f"cfd-meta-v{version:04d}.json"),
        "cfd-status": (version_root / "cfd" / "run_status.json", "application/json", f"cfd-status-v{version:04d}.json"),
    }
    if artifact not in mapping:
        raise PipelineError("failed", f"不支持的鼻塞方案下载类型：{artifact}",
                            ["使用 original、normalized、placed、placement、review、manifest、comparison、cfd-meta、cfd-status 或 bundle"])
    path, media_type, filename = mapping[artifact]
    if path.is_symlink() or not path.is_file():
        raise PipelineError("failed", "请求的鼻塞方案产物不存在", ["重新生成该方案版本或重新上传模型"])
    return path, media_type, filename


def _cfd_bundle(case_id: str, root: Path, version: int) -> tuple[Path, str, str]:
    """下载独立算例的可复核输入和状态，不打包可能很大的网格与场文件。"""
    version_root = _version_dir(root, version)
    cfd_root = version_root / "cfd"
    if not (cfd_root / "scenario_meta.json").is_file():
        raise PipelineError("failed", "该鼻塞版本尚未生成独立 CFD 算例", ["先执行 cfd-prep"])
    entries: list[tuple[Path, str]] = []
    for name in ("scenario_meta.json", "case_meta.json", "comparison.json", "gate.json", "run_status.json"):
        path = cfd_root / name
        if path.is_file() and not path.is_symlink():
            entries.append((path, f"cfd/{name}"))
    for directory in ("system", "0"):
        for path in sorted((cfd_root / directory).glob("*")):
            if path.is_file() and not path.is_symlink():
                entries.append((path, f"cfd/{directory}/{path.name}"))
    for path in sorted((cfd_root / "constant" / "triSurface").glob("*.stl")):
        if path.is_file() and not path.is_symlink():
            entries.append((path, f"cfd/constant/triSurface/{path.name}"))
    if not entries:
        raise PipelineError("failed", "独立 CFD 算例缺少可下载产物", ["重新生成鼻塞方案 CFD 算例"])
    export = root / "exports" / f"scenario-cfd-v{version:04d}.zip"
    export.parent.mkdir(parents=True, exist_ok=True)
    temporary = export.with_name(f".{export.name}.{secrets.token_hex(4)}.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, arcname in entries:
                archive.write(path, arcname)
        temporary.replace(export)
    except (OSError, zipfile.BadZipFile) as exc:
        raise PipelineError("failed", "鼻塞 CFD 下载包写入失败", ["检查病例目录权限和磁盘空间后重试"]) from exc
    finally:
        temporary.unlink(missing_ok=True)
    return export, "application/zip", f"{case_id}-scenario-cfd-v{version:04d}.zip"


def download_artifact(case_id: str, scenario_id: str, *, artifact: str = "bundle",
                      version: int | None = None) -> tuple[Path, str, str]:
    scenario = get_scenario(case_id, scenario_id)
    selected_version = int(scenario.get("current_version", 0)) if version is None else version
    root = scenario_dir(case_id, scenario_id)
    if artifact == "cfd":
        result = _cfd_bundle(case_id, root, selected_version)
        _record_activity(case_id, "scenario_downloaded", scenario_id,
                         {"version": selected_version, "artifact": artifact})
        return result
    if artifact != "bundle":
        result = _artifact_for_version(root, selected_version, artifact)
        _record_activity(case_id, "scenario_downloaded", scenario_id,
                         {"version": selected_version, "artifact": artifact})
        return result
    version_root = _version_dir(root, selected_version)
    export = root / "exports" / f"scenario-v{selected_version:04d}.zip"
    entries = [(root / "input" / "original.stl", "input/original.stl"),
               (root / "normalized" / "model.stl", "normalized/model.stl"),
               (version_root / "placed" / "model.stl", "placed/model.stl"),
               (version_root / "placement.json", "placement.json"),
               (version_root / "review.json", "review.json"),
               (version_root / "manifest.json", "manifest.json"),
               (version_root / "scenario.json", "scenario.json")]
    cfd_root = version_root / "cfd"
    if (cfd_root / "scenario_meta.json").is_file():
        for name in ("scenario_meta.json", "case_meta.json", "comparison.json", "gate.json", "run_status.json"):
            path = cfd_root / name
            if path.is_file() and not path.is_symlink():
                entries.append((path, f"cfd/{name}"))
        plug = cfd_root / "constant" / "triSurface" / "plug.stl"
        if plug.is_file() and not plug.is_symlink():
            entries.append((plug, "cfd/constant/triSurface/plug.stl"))
    for path, _ in entries:
        if path.is_symlink() or not path.is_file():
            raise PipelineError("failed", "鼻塞方案下载包缺少必要产物", ["重新生成该方案版本或重新上传模型"])
    export.parent.mkdir(parents=True, exist_ok=True)
    temporary = export.with_name(f".{export.name}.{secrets.token_hex(4)}.tmp")
    try:
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path, arcname in entries:
                    archive.write(path, arcname)
            temporary.replace(export)
        except (OSError, zipfile.BadZipFile) as exc:
            raise PipelineError("failed", "鼻塞方案下载包写入失败", ["检查病例目录权限和磁盘空间后重试"]) from exc
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError as exc:
                LOGGER.error("Unable to remove scenario ZIP temporary file: %s", type(exc).__name__)
    result = export, "application/zip", f"{scenario_id}-v{selected_version:04d}.zip"
    _record_activity(case_id, "scenario_downloaded", scenario_id,
                     {"version": selected_version, "artifact": artifact})
    return result
