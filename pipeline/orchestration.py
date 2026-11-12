"""Conservative, one-case-at-a-time progression to a prepared CFD case.

This module never confirms a mask or an endplane and never starts a solver.
The existing surface and CFD-preparation functions remain the geometry and
boundary-condition authorities. One call may run surface/QC and CFD prep for
one case; callers must run the established CFD preflight before submission.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path

from . import automation, cases, cfd_postprocess_jobs, cfd_prep, endplanes, geom_qc, scenario_jobs, surface
from .errors import PipelineError


_READY = {"ok", "warn"}
_RETRYABLE = {"failed", "blocked"}
_ENDPLANE_SOURCES = {
    "oblique_planes_v1": "user_confirmed_oblique_planes",
    endplanes.LOCAL_INLET_FORMAT: "user_confirmed_local_oblique_planes",
    endplanes.BILATERAL_LOCAL_FORMAT: "user_confirmed_bilateral_local_oblique_planes",
    None: "user_confirmed_ct_planes",
}


def _metadata(path: Path, description: str) -> dict:
    if path.is_symlink() or not path.is_file():
        raise PipelineError("blocked", f"{description}缺失或不是病例内的普通文件", ["检查该阶段产物后重试"])
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise PipelineError("failed", f"{description}无法读取", ["检查该阶段产物后重试"]) from exc
    if not isinstance(value, dict):
        raise PipelineError("failed", f"{description}格式无效", ["检查该阶段产物后重试"])
    return value


def _stage(case: dict, name: str) -> str:
    entry = (case.get("stages") or {}).get(name)
    if not isinstance(entry, dict) or entry.get("status") not in cases.STAGE_STATUS:
        raise PipelineError("failed", "病例阶段记录格式无效", ["检查病例状态文件"])
    return entry["status"]


def _plan(case_id: str, root: Path) -> tuple[dict, int]:
    automation.require_confirmed_mask(case_id)
    segmentation = _metadata(root / "segment" / "segmentation.json", "气道分割记录")
    version = segmentation.get("mask_version")
    if type(version) is not int or version < 1:
        raise PipelineError("blocked", "气道掩膜版本无效", ["重新复核并确认当前掩膜"])
    plan = endplanes.load_plan(case_id, "main_airway")
    if plan is None:
        raise PipelineError("blocked", "主气道缺少人工保存的鼻孔端和鼻咽端面", ["在工作台选择并保存两端面"])
    if plan.get("mask_version") != version:
        raise PipelineError("blocked", "端面方案与当前掩膜版本不一致", ["重新预览并保存当前掩膜的两端面"])
    format_name = plan.get("format")
    if not any(format_name == supported for supported in _ENDPLANE_SOURCES):
        raise PipelineError("blocked", "端面方案格式不受支持", ["在工作台重新选择并保存两端面"])
    if format_name is None:
        complete = type(plan.get("anterior_index")) is int and type(plan.get("posterior_index")) is int
    else:
        complete = (plan.get("complete", True) is not False
                    and isinstance(plan.get("anterior_plane"), dict)
                    and isinstance(plan.get("posterior_plane"), dict))
        if format_name == endplanes.LOCAL_INLET_FORMAT:
            complete = complete and isinstance(plan.get("anterior_seed_lps_mm"), list)
        elif format_name == endplanes.BILATERAL_LOCAL_FORMAT:
            complete = (plan.get("complete") is True
                        and all(isinstance(plan.get(name), dict) for name in (
                            "left_inlet_plane", "right_inlet_plane", "posterior_plane"))
                        and all(isinstance(plan.get(name), list) for name in (
                            "left_inlet_seed_lps_mm", "right_inlet_seed_lps_mm",
                            "posterior_seed_lps_mm")))
    if not complete:
        raise PipelineError("blocked", "鼻孔端和鼻咽端面尚未完整保存", ["分别预览并保存两端面"])
    if (plan.get("confirmation_source") is not None
            or plan.get("human_confirmed") is False):
        if not endplanes.automated_plan_is_valid(plan):
            raise PipelineError("blocked", "自动端面技术来源不完整", ["重新生成并验证三端口"])
    if format_name == "oblique_planes_v1" and not cases.get_case(case_id).get("synthetic", False):
        # The current editor marks global oblique planes as a legacy read-only
        # plan: they may open adjacent nasal branches. A saved plan alone is
        # not sufficient evidence for automated CFD preparation.
        raise PipelineError(
            "blocked", "旧版贯穿全模型端面不能自动推进 CFD",
            ["在工作台重新核对鼻孔端和鼻咽端；先完成与 CFD 工况兼容的局部入口设计及验证"],
        )
    return plan, version


def _surface_current(root: Path, plan: dict, version: int) -> dict:
    stl = root / "surface" / "airway.stl"
    if stl.is_symlink() or not stl.is_file():
        raise PipelineError("blocked", "STL 产物缺失", ["检查并重新生成表面模型"])
    meta = _metadata(root / "surface" / "surface.json", "表面模型记录")
    endplane_meta = meta.get("endplanes")
    params = meta.get("params")
    expected_source = (
        "automated_bilateral_local_oblique_planes"
        if plan.get("confirmation_source") == "automated_technical_gate"
        else _ENDPLANE_SOURCES[plan.get("format")]
    )
    if (meta.get("mask_version") != version
            or not isinstance(endplane_meta, dict)
            or endplane_meta.get("source") != expected_source
            or {key: value for key, value in endplane_meta.items() if key != "source"} != plan
            or not isinstance(params, dict) or params.get("model_scope") != "main_airway"):
        raise PipelineError("blocked", "表面模型与当前掩膜或端面方案不一致", ["重新生成 STL 并复核几何质检"])
    qc = _metadata(root / "surface" / "geom_qc.json", "几何质检记录")
    if qc.get("verdict") not in {"pass", "warn"}:
        raise PipelineError("blocked", "几何质检未通过", ["检查并修复开口与网格后重新质检"])
    return meta


def _execute_stage(case_id: str, stage: str, operation) -> None:
    cases.update_stage(case_id, stage, "running", message="逐例编排正在执行当前步骤")
    try:
        operation()
    except Exception as exc:
        status = exc.status if isinstance(exc, PipelineError) else "failed"
        reason = exc.reason if isinstance(exc, PipelineError) else f"{stage} 执行异常，请检查本机日志"
        cases.update_stage(case_id, stage, status, message=reason)
        raise


def advance_case(case_id: str, *, retry: bool = False) -> dict:
    """Advance one existing case through surface/QC and CFD prep, never CFD run.

    Failed or blocked stages require an explicit retry. A successful stage is
    not regenerated; stale successful artifacts block instead of being silently
    overwritten. No CT or patient data is returned in this result.
    """
    completed: list[str] = []
    step = "preconditions"
    try:
        root = cases.case_dir(case_id)
        if root.is_symlink():
            raise PipelineError("blocked", "病例目录不能是符号链接", ["检查病例目录后重试"])
        # Match HTTP mutation ordering: reserve in-process mutations before
        # taking the cross-process endplane lock. Reversing this order can
        # deadlock with an endplane save that waits for the file lock.
        with ExitStack() as stack:
            cfd_postprocess_jobs.reserve_mutation(case_id)
            stack.callback(cfd_postprocess_jobs.release_mutation, case_id)
            scenario_jobs.reserve_mutation(case_id)
            stack.callback(scenario_jobs.release_mutation, case_id)
            # Endplane saves invalidate downstream artifacts; hold this lock
            # from plan verification through the surface and prep writes.
            stack.enter_context(endplanes._case_save_lock(case_id))
            case = cases.get_case(case_id)
            if case.get("job", {}).get("status") == "running" or any(
                _stage(case, name) == "running" for name in case["stages"]
            ):
                raise PipelineError("blocked", "该病例已有步骤正在运行", ["等待现有步骤结束后重试"])
            if any(_stage(case, name) != "ok" for name in ("import", "anonymize", "qc")):
                raise PipelineError("blocked", "导入、脱敏或 CT 质控尚未完成", ["先复核上游阶段"])
            if _stage(case, "segment") != "ok":
                raise PipelineError("blocked", "气道分割尚未完成人工复核", ["在 CT 视窗复核并确认掩膜"])
            if _stage(case, "cfd_run") != "pending":
                raise PipelineError("blocked", "已有 CFD 求解记录，不能覆盖其输入算例", ["先核对现有求解与数值闸门"])
            plan, version = _plan(case_id, root)

            surface_status = _stage(case, "surface")
            prep_initial = _stage(case, "cfd_prep")
            if surface_status in _RETRYABLE and not retry:
                raise PipelineError("blocked", "表面建模上次未完成", ["检查失败原因，修正后显式重试"])
            if surface_status == "pending" or (retry and surface_status in _RETRYABLE):
                if prep_initial in _READY:
                    raise PipelineError("blocked", "旧 CFD 算例仍标记为就绪，不能重建其表面输入", ["先核对并失效旧算例，再重新建模"])
                step = "surface"

                def build_surface():
                    surface.run_surface(case_id, {"model_scope": "main_airway"})
                    qc = geom_qc.run_geom_qc(case_id)
                    if qc.get("verdict") not in {"pass", "warn"}:
                        raise PipelineError("blocked", "几何质检未通过", ["修正几何开口后显式重试"])

                _execute_stage(case_id, "surface", build_surface)
                completed.append("surface")
            _surface_current(root, plan, version)

            # A local single-inlet cut has no approved CFD boundary condition.
            # Keep its model but do not build a bilateral-nasal CFD case.
            step = "cfd_prep"
            if plan.get("format") == endplanes.LOCAL_INLET_FORMAT:
                raise PipelineError("blocked", "局部单支鼻孔入口尚未定义 CFD 工况", ["先人工确定入口边界条件和流量"])

            case = cases.get_case(case_id)
            prep_status = _stage(case, "cfd_prep")
            if "surface" in completed and prep_status in _READY:
                raise PipelineError("blocked", "表面模型已重建，但旧 CFD 算例仍标记为就绪", ["先核对并失效旧算例，再重新准备 CFD"])
            if prep_status in _RETRYABLE and not retry:
                raise PipelineError("blocked", "CFD 算例上次未准备完成", ["检查失败原因，修正后显式重试"])
            if prep_status == "pending" or (retry and prep_status in _RETRYABLE):
                step = "cfd_prep"
                _execute_stage(case_id, "cfd_prep", lambda: cfd_prep.prepare_case(case_id))
                completed.append("cfd_prep")
            if _stage(cases.get_case(case_id), "cfd_prep") not in _READY:
                raise PipelineError("blocked", "CFD 算例阶段未就绪", ["检查算例生成结果"])
            for relative in ("cfd/case_meta.json", "cfd/system/controlDict"):
                artifact = root / relative
                if artifact.is_symlink() or not artifact.is_file():
                    raise PipelineError("blocked", "CFD 算例产物缺失", ["检查并重新生成 CFD 算例"])
            return {"case_id": case_id, "status": "prepared", "completed_steps": completed,
                    "next_action": "run_authoritative_cfd_preflight"}
    except PipelineError as exc:
        return {"case_id": case_id, "status": exc.status, "completed_steps": completed,
                "next_action": "review_" + step, "reason": exc.reason, "remediation": exc.remediation}
    except Exception as exc:
        return {"case_id": case_id, "status": "failed", "completed_steps": completed,
                "next_action": "review_" + step,
                "reason": "逐例编排发生未处理错误，请检查本机日志", "error_type": type(exc).__name__}
