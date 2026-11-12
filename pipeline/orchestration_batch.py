"""Bounded CT-to-CFD orchestration over existing case artifacts and review gates.

The external Harness selects only a server-authorized action. The service
rechecks the case, permission and source fingerprint before every mutation.
Human mask/endplane confirmation and final CFD eligibility are never inferred.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack
from typing import Callable

from . import (activity_monitor, automation, cases, cfd_postprocess_jobs,
               endplane_auto_candidate, endplane_candidates, endplanes, harness_advice,
               harness_projection, openfoam_runner, orchestration, report, scenario_jobs,
               segmentation, segmentation_learning)
from .errors import PipelineError

CASE_ID = harness_projection.CASE_ID
BILATERAL_FORMAT = "oblique_bilateral_local_v3"
MAX_ACTIONS_PER_ITEM = 5
Authorize = Callable[[str, str], bool]
_SOURCES = (
    "case.json", "volume/meta.json", "volume/hu.npy",
    "segment/segmentation.json", "segment/mask.npy",
    "segment/endplanes.json", "surface/airway.stl",
    "surface/surface.json", "surface/geom_qc.json", "cfd/case_meta.json",
    "report/report.json",
)


def _require_access(item: dict, authorize: Authorize) -> None:
    if not isinstance(item, dict) or type(item.get("case_id")) is not str or not CASE_ID.fullmatch(item["case_id"]):
        raise PipelineError("failed", "匿名病例编号无效", ["刷新病例列表"])
    if not isinstance(item.get("actor_id"), str) or not item["actor_id"]:
        raise PipelineError("blocked", "操作者身份不可验证", ["重新登录工作台"])
    if not authorize(item["actor_id"], item["case_id"]):
        raise PipelineError("blocked", "病例编辑权限已变化", ["请有权限的操作者重新提交"])


def _source_fingerprint(case_id: str) -> str:
    root = cases.case_dir(case_id)
    if root.is_symlink() or not root.is_dir():
        raise PipelineError("blocked", "病例目录不可用", ["检查病例目录"])
    digest = hashlib.sha256()
    for relative in _SOURCES:
        path = root / relative
        if path.is_symlink() or path.parent.is_symlink():
            raise PipelineError("blocked", "病例产物不能是符号链接", ["检查病例文件"])
        if path.exists() and not path.is_file():
            raise PipelineError("blocked", "病例产物不是普通文件", ["检查病例文件"])
        digest.update(relative.encode("ascii") + b"\0")
        if path.is_file():
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
        else:
            digest.update(b"<missing>")
    return digest.hexdigest()


def preflight(item: dict, authorize: Authorize) -> dict:
    """Capture an anonymous version digest before a queued item runs."""
    _require_access(item, authorize)
    case_id = item["case_id"]
    case = cases.get_case(case_id)
    if (case.get("job") or {}).get("status") == "running" or any(
        stage.get("status") == "running" for stage in case.get("stages", {}).values()
        if isinstance(stage, dict)
    ):
        raise PipelineError("blocked", "该病例已有阶段正在运行", ["等待当前步骤结束后重新提交"])
    try:
        projection = harness_projection.project_case_state(case_id)
    except harness_projection.ProjectionError as exc:
        raise PipelineError("blocked", "病例阶段摘要不可读取", ["检查病例阶段和 CFD 闸门"]) from exc
    return {"state_sha256": projection["state_sha256"],
            "source_sha256": _source_fingerprint(case_id)}


def submission_snapshot(item: dict, authorize: Authorize) -> dict:
    """Fast enqueue revision; full content hashes still run before each action.

    Owned artifacts are replaced/written by the service. File identity, nanosecond
    timestamps and size detect edits while queued without reading CT/solver data
    or evaluating quality gates inside the submission transaction.
    """
    _require_access(item, authorize)
    root = cases.case_dir(item["case_id"])
    if root.is_symlink() or not root.is_dir():
        raise PipelineError("blocked", "病例目录不可用", ["检查病例目录"])
    entries = []
    for relative in _SOURCES:
        path = root / relative
        if path.is_symlink() or path.parent.is_symlink():
            raise PipelineError("blocked", "病例产物不能是符号链接", ["检查病例文件"])
        try:
            info = path.stat()
        except FileNotFoundError:
            entries.append([relative, None])
            continue
        if not path.is_file():
            raise PipelineError("blocked", "病例产物不是普通文件", ["检查病例文件"])
        entries.append([relative, info.st_dev, info.st_ino, info.st_size,
                        info.st_mtime_ns, info.st_ctime_ns])
    digest = hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode("ascii"))
    return {"enqueue_revision": digest.hexdigest()}


def _stages(projection: dict) -> dict[str, str]:
    return projection["stages"]


def _has_current_bilateral_plan(case_id: str) -> bool:
    try:
        plan, _version = orchestration._plan(case_id, cases.case_dir(case_id))
    except PipelineError:
        return False
    return isinstance(plan, dict) and plan.get("format") == BILATERAL_FORMAT


def _auto_endplane_context(case_id: str) -> int:
    """Require a reviewed mask and an untouched downstream workflow.

    The automated geometry gate does not replace mask review and never
    overwrites a saved manual/stale plan, a failed stage or existing outputs.
    """
    case = cases.get_case(case_id)
    if (case.get("job") or {}).get("status") == "running" or any(
        orchestration._stage(case, name) != "ok"
        for name in ("import", "anonymize", "qc", "segment")
    ):
        raise PipelineError("blocked", "自动三端面生成需要已完成人工复核的当前掩膜",
                            ["先在工作台复核并确认气道掩膜"])
    if any(orchestration._stage(case, name) != "pending"
           for name in ("surface", "cfd_prep", "cfd_run", "report")):
        raise PipelineError("blocked", "已有下游阶段记录，不能自动替换端面",
                            ["在工作台核对既有产物并进行局部修正"])
    root = cases.case_dir(case_id)
    saved = root / "segment" / "endplanes.json"
    if (root.is_symlink() or saved.is_symlink() or saved.exists()
            or endplanes.load_plan(case_id, "main_airway") is not None):
        raise PipelineError("blocked", "已有端面方案，不能自动覆盖",
                            ["在工作台核对并局部修正当前端面"])
    for name in ("surface", "cfd", "report"):
        directory = root / name
        if directory.is_symlink() or (directory.exists() and (
            not directory.is_dir() or any(directory.iterdir())
        )):
            raise PipelineError("blocked", "已有下游产物，不能自动覆盖",
                                ["在工作台核对既有产物并进行局部修正"])
    automation.require_confirmed_mask(case_id)
    segmentation_meta = orchestration._metadata(
        root / "segment" / "segmentation.json", "气道分割记录")
    version = segmentation_meta.get("mask_version")
    if type(version) is not int or version < 1:
        raise PipelineError("blocked", "气道掩膜版本无效", ["重新复核当前掩膜"])
    return version


def _can_auto_endplanes(case_id: str) -> bool:
    try:
        _auto_endplane_context(case_id)
    except PipelineError:
        return False
    return endplanes.bilateral_local_available()


def allowed_actions(projection: dict) -> list[str]:
    """Build the action list locally; no model can broaden it."""
    stages = _stages(projection)
    case_id = projection["case_id"]
    if any(stages[name] != "ok" for name in ("import", "anonymize", "qc")):
        return ["wait_for_review"]
    if stages["segment"] == "pending" and all(
        stages[name] == "pending" for name in ("surface", "cfd_prep", "cfd_run")
    ):
        return ["segment_candidate", "wait_for_review"]
    if stages["segment"] != "ok":
        return ["wait_for_review"]
    if not _has_current_bilateral_plan(case_id):
        if _can_auto_endplanes(case_id):
            return ["propose_endplanes", "wait_for_review"]
        return ["wait_for_review"]
    if stages["cfd_run"] in {"ok", "warn"}:
        if (projection["numeric_ok"] is True and projection["eligible"] is True
                and stages["report"] == "pending"):
            return ["generate_report", "wait_for_review"]
        return ["inspect_numeric_gate", "wait_for_review"]
    if stages["cfd_run"] != "pending":
        return ["wait_for_review"]
    if (projection["baseline_ready"] and stages["surface"] in {"ok", "warn"}
            and stages["cfd_prep"] in {"ok", "warn"}):
        return ["submit_ready_cfd", "wait_for_review"]
    if stages["cfd_prep"] in {"pending", "blocked", "failed"}:
        return ["prepare_surface_cfd", "wait_for_review"]
    return ["wait_for_review"]


def _review_reason(projection: dict) -> tuple[str, list[str]]:
    stages = _stages(projection)
    if any(stages[name] != "ok" for name in ("import", "anonymize", "qc")):
        return "CT 导入、脱敏或质控尚未就绪", ["复核 CT 序列和质控结果"]
    if stages["segment"] != "ok":
        return "气道掩膜等待人工复核", ["在工作台复核并确认当前掩膜"]
    if not _has_current_bilateral_plan(projection["case_id"]):
        return "缺少与双侧 CFD 匹配的三局部端面方案", ["在工作台核对左右鼻孔与鼻咽端面并保存"]
    if stages["cfd_run"] in {"ok", "warn"}:
        return "CFD 数值或工程复核尚未通过", ["检查数值闸门及工程复核"]
    return "当前阶段不能安全自动推进", ["检查病例阶段和阻断详情"]


def _blocked(case_id: str, reason: str, remediation: list[str], events: list[dict],
             *, next_action: str = "human_review", status: str = "blocked") -> dict:
    return {"task_status": status, "case_id": case_id, "reason": reason,
            "remediation": remediation, "next_action": next_action,
            "actions": events, "llm_usage": _usage(events)}


def _usage(events: list[dict]) -> dict:
    """Only sum provider-reported token counts; never estimate a bill."""
    model_events = [event for event in events if event.get("source") == "deepseek-harness"]
    if not model_events:
        return {"status": "unknown", "total_tokens": None, "cost_usd": None}
    meters = [event.get("metering") for event in model_events]
    if any(not isinstance(value, dict) or value.get("status") != "reported"
           or type(value.get("total_tokens")) is not int for value in meters):
        return {"status": "unknown", "total_tokens": None, "cost_usd": None}
    return {"status": "reported", "total_tokens": sum(value["total_tokens"] for value in meters),
            "cost_usd": None}


def _audit(item: dict, action: str, outcome: str) -> None:
    """Persist a minimal action trail before and after every mutation."""
    try:
        activity_monitor.record_event({
            "kind": "stage", "action": f"orchestration_{outcome}",
            "case_id": item["case_id"], "session_id": item["actor_id"],
            "target": action, "details": {"outcome": outcome},
        })
    except Exception as exc:
        raise PipelineError("blocked", "编排操作审计不可写，已停止自动动作",
                            ["检查本机操作日志存储后重试"]) from exc


def _run_segment(case_id: str) -> dict:
    case = cases.get_case(case_id)
    stages = case["stages"]
    if (any(stages[name]["status"] != "ok" for name in ("import", "anonymize", "qc"))
            or stages["segment"]["status"] != "pending"):
        raise PipelineError("blocked", "分割候选的上游阶段已变化", ["刷新病例状态后重试"])
    root = cases.case_dir(case_id)
    if any((root / relative).exists() for relative in (
        "segment/segmentation.json", "segment/endplanes.json",
        "surface/airway.stl", "cfd/case_meta.json",
    )):
        raise PipelineError("blocked", "已有掩膜或下游产物，不能自动覆盖", ["人工核对已有病例产物"])
    cases.update_stage(case_id, "segment", "running", message="Harness 批次生成待复核掩膜候选")
    try:
        result = segmentation.run_segment(case_id)
    except Exception as exc:
        cases.update_stage(case_id, "segment",
                           "blocked" if isinstance(exc, PipelineError) and exc.status == "blocked" else "failed",
                           message=exc.reason if isinstance(exc, PipelineError) else "分割候选生成异常，请检查本机日志")
        raise
    return {"mask_version": result.get("mask_version"), "human_review_required": True}


def _inspect_gate(case_id: str) -> dict:
    gate = openfoam_runner.check_result_gate(case_id)
    if not isinstance(gate, dict) or type(gate.get("numeric_ok")) is not bool or type(gate.get("eligible")) is not bool:
        raise PipelineError("failed", "数值闸门输出无效", ["检查求解产物"])
    return {key: gate.get(key) for key in ("numeric_ok", "eligible", "reasons")}


def _run_auto_endplanes(item: dict, authorize: Authorize, expected: dict) -> dict:
    """Generate locally and save only through the distinct technical gate.

    Reserve the same mutations as HTTP before taking the saver file lock.
    Recheck permission and all input bytes after generation and again inside
    the final save callback; a model supplies no geometry or provenance.
    """
    case_id = item["case_id"]
    with ExitStack() as stack:
        cfd_postprocess_jobs.reserve_mutation(case_id)
        stack.callback(cfd_postprocess_jobs.release_mutation, case_id)
        scenario_jobs.reserve_mutation(case_id)
        stack.callback(scenario_jobs.release_mutation, case_id)
        _require_access(item, authorize)
        version = _auto_endplane_context(case_id)

        def before_persist():
            _require_access(item, authorize)
            if (_auto_endplane_context(case_id) != version
                    or _source_fingerprint(case_id) != expected["source_sha256"]):
                raise PipelineError("blocked", "三端面预检期间病例输入版本已变化",
                                    ["复核当前病例后重新提交"])

        before_persist()
        generated = endplane_auto_candidate.generate_bilateral_candidate(
            case_id, "main_airway", version)
        before_persist()
        if not isinstance(generated, dict) or generated.get("status") != "available":
            raise PipelineError("blocked", "自动三端面候选未通过真实网格预检",
                                ["在工作台局部修正左右鼻孔或鼻咽端面，再预览并保存"])
        candidates = generated.get("candidates")
        if (generated.get("scope") != "main_airway"
                or type(generated.get("mask_version")) is not int
                or generated["mask_version"] != version
                or generated.get("generator_version") != endplane_auto_candidate.BILATERAL_GENERATOR_VERSION
                or not isinstance(candidates, list) or len(candidates) != 1
                or not isinstance(candidates[0], dict)):
            raise PipelineError("blocked", "自动三端面候选来源或版本无效",
                                ["检查当前候选生成器后重试"])
        try:
            plan = endplane_candidates.validate_candidate_plan(
                "main_airway", version, candidates[0].get("plan"))
        except endplane_candidates.CandidateInputError as exc:
            raise PipelineError("blocked", "自动三端面候选结构无效",
                                ["在工作台核对并局部修正端面"]) from exc
        if plan.get("format") != BILATERAL_FORMAT:
            raise PipelineError("blocked", "自动候选不是双侧三局部端面",
                                ["在工作台核对三个开口"])
        saved = endplanes.save_automated_bilateral_endplanes(
            case_id, "main_airway",
            plan["left_inlet_plane"], plan["right_inlet_plane"], plan["posterior_plane"],
            plan["left_inlet_seed_lps_mm"], plan["right_inlet_seed_lps_mm"],
            plan["posterior_seed_lps_mm"], version,
            generator_version=endplane_auto_candidate.BILATERAL_GENERATOR_VERSION,
            before_persist=before_persist,
        )
        if (not isinstance(saved, dict) or saved.get("confirmation_source") != "automated_technical_gate"
                or saved.get("human_confirmed") is not False):
            raise PipelineError("failed", "自动端面保存来源记录无效",
                                ["核对已保存端面来源后再继续"])
        return {"mask_version": version, "format": BILATERAL_FORMAT,
                "confirmation_source": "automated_technical_gate", "human_confirmed": False,
                "generator_version": endplane_auto_candidate.BILATERAL_GENERATOR_VERSION}


def execute(item: dict, authorize: Authorize, progress=None) -> dict:
    """Advance one case until a human gate, terminal quality result or failure."""
    case_id = item["case_id"]
    events: list[dict] = []
    def publish(action):
        if progress is not None:
            progress({"current_action": action, "actions": list(events), "llm_usage": _usage(events)})
    for _ in range(MAX_ACTIONS_PER_ITEM):
        publish("preflight")
        before = preflight(item, authorize)
        projection = harness_projection.project_case_state(case_id)
        # Deterministic local tools are part of the Harness loop, but need no
        # paid model decision. A comparison never confirms or replaces a mask.
        if (projection['stages']['segment'] in {'ok', 'warn'} and all(
                projection['stages'][name] == 'ok' for name in ('import', 'anonymize', 'qc'))):
            try:
                def comparison_progress(action):
                    _require_access(item, authorize)
                    _audit(item, action, 'running')
                    publish(action)
                comparison = segmentation_learning.compare_current_if_pending(
                    case_id, progress=comparison_progress)
                if comparison is not None:
                    _audit(item, 'compare_segmentation_candidates', 'completed')
                    events.append({'source': 'server_rules', 'action': 'compare_segmentation_candidates',
                        'result': 'completed', 'comparison': comparison})
                    publish('compare_segmentation_candidates')
            except segmentation_learning.LearningError as exc:
                return _blocked(case_id, str(exc), ['检查分割学习记录后重试'], events,
                    next_action='review_segmentation_learning')
        actions = allowed_actions(projection)
        if actions == ["wait_for_review"]:
            # A model has no decision to make here. Avoid a paid call for each
            # case when the only safe outcome is human review.
            reason, remediation = _review_reason(projection)
            _audit(item, "wait_for_review", "blocked")
            events.append({"source": "server_rules", "action": "wait_for_review",
                           "state_sha256": projection["state_sha256"], "result": "paused"})
            return _blocked(case_id, reason, remediation, events)
        if item.get("use_harness", True):
            if not harness_advice.actions_enabled():
                return _blocked(case_id, "DeepSeek Harness 动作执行尚未启用",
                                ["检查本机 Harness 配置与隔离容器"], events, next_action="configure_harness")
            try:
                publish("harness_selection")
                selection = harness_advice.request_action(projection, actions, item["actor_id"])
            except harness_advice.AdviceUnavailable as exc:
                return _blocked(case_id, exc.reason, ["检查 Harness 隔离运行环境后重试"], events,
                                next_action="retry_harness")
            action = selection["action"]
            event = {"source": "deepseek-harness", "action": action,
                     "state_sha256": selection["state_sha256"],
                     "input_sha256": selection["input_sha256"],
                     "metering": selection["metering"]}
        else:
            action = next((name for name in actions if name != "wait_for_review"), "wait_for_review")
            event = {"source": "server_rules", "action": action,
                     "state_sha256": projection["state_sha256"]}
        # Model selection is untrusted, and may take up to a minute. Check the
        # authorized source and server allowlist again before changing files.
        try:
            after = preflight(item, authorize)
        except PipelineError as exc:
            events.append({**event, "result": "rejected_after_selection"})
            return _blocked(case_id, exc.reason, exc.remediation, events,
                            next_action="resubmit_current_version")
        if after != before:
            events.append({**event, "result": "rejected_stale"})
            return _blocked(case_id, "模型选择期间病例版本已变化", ["复核当前病例并重新提交"], events,
                            next_action="resubmit_current_version")
        current = harness_projection.project_case_state(case_id)
        if action not in allowed_actions(current) or current["state_sha256"] != projection["state_sha256"]:
            events.append({**event, "result": "rejected_stage"})
            return _blocked(case_id, "模型动作与当前病例阶段不一致", ["刷新并重新提交"], events,
                            next_action="resubmit_current_version")
        if action == "wait_for_review":
            reason, remediation = _review_reason(current)
            _audit(item, action, "blocked")
            events.append({**event, "result": "paused"})
            return _blocked(case_id, reason, remediation, events)
        _audit(item, action, "running")
        publish(action)
        try:
            if action == "segment_candidate":
                outcome = _run_segment(case_id)
            elif action == "propose_endplanes":
                outcome = _run_auto_endplanes(item, authorize, after)
            elif action == "prepare_surface_cfd":
                outcome = orchestration.advance_case(case_id, retry=item.get("retry_failed", False))
                if outcome["status"] != "prepared":
                    _audit(item, action, "blocked")
                    events.append({**event, "result": outcome["status"]})
                    return _blocked(case_id, outcome.get("reason", "表面或算例尚未就绪"),
                                    outcome.get("remediation", ["检查几何和边界条件"]), events,
                                    next_action=outcome.get("next_action", "review_geometry"),
                                    status="failed" if outcome["status"] == "failed" else "blocked")
            elif action == "submit_ready_cfd":
                solver_item = {"case_id": case_id, "scenario_id": None, "version": None,
                               "backend": None, "timeout_s": 7200}
                snapshot = automation.preflight(solver_item)
                if automation.preflight(solver_item) != snapshot:
                    raise PipelineError("blocked", "求解输入版本已变化", ["复核当前算例后重试"])
                outcome = automation.execute(solver_item)
                if outcome.get("task_status") != "solved":
                    _audit(item, action, "blocked")
                    events.append({**event, "result": outcome.get("task_status", "failed")})
                    return _blocked(case_id, outcome.get("reason") or "求解未成功完成",
                                    ["检查本机求解日志与算例"], events, next_action="review_solver",
                                    status="failed" if outcome.get("task_status") == "failed" else "blocked")
            elif action == "inspect_numeric_gate":
                outcome = _inspect_gate(case_id)
                if outcome["numeric_ok"] and outcome["eligible"] and not all(
                    (cases.case_dir(case_id) / relative).is_file()
                    for relative in ("report/report.md", "report/report.json")
                ):
                    raise PipelineError("blocked", "合格报告产物缺失", ["核对报告阶段后重新生成"])
                _audit(item, action, "completed" if outcome["numeric_ok"] and outcome["eligible"] else "blocked")
                events.append({**event, "result": "checked"})
                if outcome["numeric_ok"] and outcome["eligible"]:
                    return {"task_status": "solved", "case_id": case_id,
                            "gate": outcome, "actions": events, "llm_usage": _usage(events),
                            "next_action": "review_report"}
                return _blocked(case_id, "CFD 数值或工程复核未通过",
                                ["检查数值闸门与工程复核"], events, next_action="review_numeric_gate")
            elif action == "generate_report":
                if cases.get_case(case_id)["stages"]["report"]["status"] != "pending":
                    raise PipelineError("blocked", "报告阶段已变化，不能自动覆盖", ["检查当前报告"])
                built = report.build_report(case_id)
                if built.get("cfd_eligible") is not True:
                    raise PipelineError("blocked", "报告生成后 CFD 合格状态已变化", ["检查数值闸门"])
                outcome = {"markdown_path": built.get("markdown_path"),
                           "json_path": built.get("json_path"), "human_review_required": True}
            else:
                raise PipelineError("blocked", "动作不在服务端执行白名单", ["刷新任务"])
        except PipelineError as exc:
            _audit(item, action, "blocked" if exc.status == "blocked" else "failed")
            events.append({**event, "result": exc.status})
            return _blocked(case_id, exc.reason, exc.remediation, events,
                            next_action="review_endplanes" if action == "propose_endplanes" else "review_action_failure",
                            status="failed" if exc.status == "failed" else "blocked")
        except Exception:
            _audit(item, action, "failed")
            events.append({**event, "result": "failed"})
            return _blocked(case_id, "自动动作执行异常，请检查本机服务日志",
                            ["核对已有产物并显式重试"], events,
                            next_action="review_endplanes" if action == "propose_endplanes" else "review_action_failure",
                            status="failed")
        _audit(item, action, "completed")
        events.append({**event, "result": "completed",
                       "output": outcome if action != "submit_ready_cfd" else {"task_status": outcome.get("task_status")}})
    return _blocked(case_id, "单例自动步骤达到本轮上限", ["检查阶段后用新请求继续"], events,
                    next_action="resubmit_current_version")
