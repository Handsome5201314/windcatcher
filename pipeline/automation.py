"""Small, deidentified automation contract over the existing CFD pipeline."""

from __future__ import annotations

import hashlib
import json
import re
import time
from contextlib import ExitStack
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import cases, cfd_postprocess_jobs, mask_review, openfoam_runner, scenario_cfd, scenario_jobs, scenarios
from .errors import PipelineError


class BatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(pattern=r"^case-\d{8}-\d{6}-[0-9a-f]{4}$")
    scenario_id: str | None = Field(default=None, pattern=scenarios.SCENARIO_ID_PATTERN)
    version: int | None = Field(default=None, ge=1)
    backend: Literal["native", "wsl", "docker"] | None = None
    timeout_s: int = Field(default=7200, ge=60, le=86400)

    @model_validator(mode="after")
    def scenario_version(self):
        if (self.scenario_id is None) != (self.version is None):
            raise ValueError("鼻塞任务必须同时提供 scenario_id 和 version；基准任务两者留空")
        return self


class BatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_key: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    items: list[BatchItem] = Field(min_length=1, max_length=50)


def require_confirmed_mask(case_id):
    path = cases.case_dir(case_id) / "segment" / "segmentation.json"
    try:
        seg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PipelineError("blocked", "当前病例还没有可确认的气道掩膜", ["先完成分割并在 CT 视窗人工复核"]) from exc
    if not isinstance(seg, dict):
        raise PipelineError("failed", "气道分割记录格式无效", ["检查分割记录"])
    if seg.get("need_human_confirm", True) or (seg.get("review_flags") or {}).get("external_air_suspected"):
        raise PipelineError("blocked", "气道掩膜尚未完成人工确认", ["在 CT 视窗复核并确认气道范围"])
    if not mask_review.has_current_external_air_validation(seg):
        raise PipelineError("blocked", "掩膜缺少当前版本的体外空气复核", ["先去除体外空气并逐层复核后确认"])


def _hash(path):
    if path.is_symlink():
        raise PipelineError("blocked", "自动求解输入不能是符号链接", ["使用病例目录内的原始产物"])
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_hash(path):
    if path.name == "decomposeParDict" and path.parent.name == "system":
        default = ('FoamFile\n{\n    version 2.0;\n    format ascii;\n    class dictionary;\n'
                   '    object decomposeParDict;\n}\n'
                   'numberOfSubdomains 1;\nmethod scotch;\ndistributed no;\nroots ();\n')
        text = path.read_text(encoding="utf-8") if path.is_file() else default
        if len(re.findall(r"(?m)^\s*numberOfSubdomains\s+\d+\s*;", text)) == 1:
            text = re.sub(r"(?m)^(\s*numberOfSubdomains\s+)\d+(\s*;)", r"\g<1>1\2", text)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    return _hash(path)


def preflight(item):
    case_id = item["case_id"]
    case = cases.get_case(case_id)
    if case.get("job", {}).get("status") == "running" or any(s.get("status") == "running" for s in case.get("stages", {}).values()):
        raise PipelineError("blocked", "该病例已有处理步骤在运行", ["等待当前步骤结束"])
    # Reuse the same mutation guards as HTTP; no untracked concurrent solver.
    with ExitStack() as stack:
        cfd_postprocess_jobs.reserve_mutation(case_id)
        stack.callback(cfd_postprocess_jobs.release_mutation, case_id)
        scenario_jobs.reserve_mutation(case_id)
        stack.callback(scenario_jobs.release_mutation, case_id)
        require_confirmed_mask(case_id)
        root = cases.case_dir(case_id)
        if case.get("stages", {}).get("cfd_prep", {}).get("status") not in {"ok", "warn"}:
            raise PipelineError("blocked", "基准 CFD 算例尚未准备就绪", ["先生成人工确认后的 STL 和 CFD 算例"])
        if not (root / "cfd" / "system" / "controlDict").is_file() or not (root / "cfd" / "case_meta.json").is_file():
            raise PipelineError("blocked", "CFD 算例文件缺失", ["重新生成 CFD 算例"])
        paths = [root / p for p in ("segment/segmentation.json", "segment/endplanes.json", "surface/airway.stl", "surface/surface.json", "cfd/case_meta.json")]
        for folder in ("0", "system", "constant"):
            paths.extend(p for p in (root / "cfd" / folder).rglob("*") if p.is_file() and "polyMesh" not in p.parts)
        paths.append(root / "cfd" / "system" / "decomposeParDict")
        snapshot = {str(p.relative_to(root)).replace("\\", "/"): _snapshot_hash(p) for p in sorted(set(paths))}
        if item.get("scenario_id"):
            scenario = scenarios.get_scenario(case_id, item["scenario_id"])
            if scenario.get("current_version") != item["version"]:
                raise PipelineError("blocked", "鼻塞方案版本已变化", ["刷新方案，选择当前版本"])
            review = scenario.get("review") or {}
            if review.get("status") != "approved" or not review.get("approved") or scenario.get("base_status") != "current":
                raise PipelineError("blocked", "鼻塞方案尚未审核通过或气道基准已变化", ["人工核对摆位并审核当前版本"])
            snapshot["scenario"] = {key: scenario.get(key) for key in ("current_version", "base_reference", "placement", "review")}
        return snapshot


def _gate_summary(gate):
    return {key: gate.get(key) for key in ("eligible", "numeric_ok", "reasons", "metrics")}


def execute(item):
    case_id = item["case_id"]
    with ExitStack() as stack:
        cfd_postprocess_jobs.reserve_mutation(case_id)
        stack.callback(cfd_postprocess_jobs.release_mutation, case_id)
        if item.get("scenario_id"):
            task = scenario_jobs.start(case_id, item["scenario_id"], version=item["version"], backend=item.get("backend"), timeout_s=item["timeout_s"])
            while task["status"] in {"queued", "running"}:
                time.sleep(0.25)  # Internal polling does not call a language model.
                task = scenario_jobs.get_task(case_id, item["scenario_id"], version=item["version"])
            if task["status"] != "solved" or task.get("state_persistence_error"):
                return {"task_status": "blocked" if task["status"] == "blocked" else "failed", "reason": task.get("message"), "task_id": task["job_id"]}
            status = scenario_cfd.get_status(case_id, item["scenario_id"], version=item["version"])
            comparison = scenario_cfd.get_comparison(case_id, item["scenario_id"], version=item["version"])
            return {"task_status": "solved", "gate": _gate_summary(status.get("gate") or {}),
                    "comparison": {key: comparison.get(key) for key in ("status", "reason", "baseline", "scenario", "delta", "eligibility")}}
        scenario_jobs.reserve_mutation(case_id)
        stack.callback(scenario_jobs.release_mutation, case_id)
        try:
            result = openfoam_runner.run_case(case_id, backend=item.get("backend"), timeout_s=item["timeout_s"])
            gate = openfoam_runner.check_result_gate(case_id)
        except Exception as exc:
            cases.update_stage(case_id, "cfd_run", "blocked" if isinstance(exc, PipelineError) and exc.status == "blocked" else "failed",
                               message=exc.reason if isinstance(exc, PipelineError) else "批量 CFD 执行异常，请检查服务日志")
            raise
        status = "solved" if result.get("status") == "ran" else "blocked" if result.get("status") == "missing" else "failed"
        return {"task_status": status, "run_status": result.get("status"), "reason": result.get("message"), "gate": _gate_summary(gate)}


def inspect_case(case_id):
    case = cases.get_case(case_id)
    item = BatchItem(case_id=case_id).model_dump()
    try:
        preflight(item)
        readiness = {"ready": True}
    except PipelineError as exc:
        readiness = {"ready": False, "reason": exc.reason, "remediation": exc.remediation}
    return {"case_id": case_id, "synthetic": case.get("synthetic", False), "stages": {k: v["status"] for k, v in case["stages"].items()},
            "baseline": readiness, "scenarios": [{"scenario_id": s["scenario_id"], "version": s["current_version"], "base_status": s["base_status"],
                                                "review_status": (s.get("review") or {}).get("status"), "cfd_status": (s.get("cfd") or {}).get("status")}
                                               for s in scenarios.list_scenarios(case_id)],
            "llm_usage": {"tokens": None, "source": "由 Codex/WorkBuddy 调用端提供；MCP 不掌握宿主 token 用量"}}
