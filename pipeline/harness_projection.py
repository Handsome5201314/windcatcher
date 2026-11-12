"""Strict, read-only case status projection for an advisory Harness process.

The caller must authorize case_id for the current human account before calling
this module. No case record, paths, free text, geometry, or gate detail is
returned. The fingerprint identifies projected status, not a monotonic case
revision and not the underlying CT/CFD artifacts.
"""

from __future__ import annotations

import hashlib
import json

from . import automation, cases, openfoam_runner
from .access_identity import CASE_ID
from .config import STAGES
from .errors import PipelineError


SCHEMA_VERSION = 1
STAGE_STATUSES = frozenset({"pending", "running", "ok", "warn", "failed", "blocked"})


class ProjectionError(ValueError):
    """A case cannot safely provide a complete advisory status summary."""


def _stage_statuses(case: object, case_id: str) -> dict[str, str]:
    if not isinstance(case, dict) or case.get("case_id") != case_id:
        raise ProjectionError("病例状态与匿名编号不一致")
    entries = case.get("stages")
    if not isinstance(entries, dict):
        raise ProjectionError("病例阶段状态不完整")
    statuses = {}
    for stage in STAGES:
        entry = entries.get(stage)
        if not isinstance(entry, dict) or type(entry.get("status")) is not str:
            raise ProjectionError("病例阶段状态不完整")
        status = entry["status"]
        if status not in STAGE_STATUSES:
            raise ProjectionError("病例阶段状态无效")
        statuses[stage] = status
    return statuses


def _baseline_ready(case_id: str, stages: dict[str, str]) -> bool:
    if stages["cfd_prep"] not in {"ok", "warn"}:
        return False
    try:
        snapshot = automation.preflight({"case_id": case_id})
    except PipelineError as exc:
        if exc.status == "blocked":
            return False
        raise ProjectionError("基准算例准备状态不可读取") from exc
    except (OSError, ValueError) as exc:
        raise ProjectionError("基准算例准备状态不可读取") from exc
    if not isinstance(snapshot, dict) or not snapshot:
        raise ProjectionError("基准算例准备状态无效")
    return True


def _quality_gate(case_id: str, stages: dict[str, str], baseline_ready: bool) -> tuple[bool | None, bool | None]:
    if stages["cfd_run"] not in {"ok", "warn"}:
        return None, None
    try:
        gate = openfoam_runner.check_result_gate(case_id)
    except (PipelineError, OSError, ValueError) as exc:
        raise ProjectionError("数值质量状态不可读取") from exc
    if (not isinstance(gate, dict) or type(gate.get("numeric_ok")) is not bool
            or type(gate.get("eligible")) is not bool):
        raise ProjectionError("数值质量状态无效")
    numeric_ok, eligible = gate["numeric_ok"], gate["eligible"]
    if eligible and (not numeric_ok or not baseline_ready):
        raise ProjectionError("数值质量状态相互矛盾")
    return numeric_ok, eligible


def project_case_state(case_id: str) -> dict:
    """Return only fixed status codes, trusted booleans, and a state digest.

    Call after the gateway has checked case/view permission. A fabricated
    integer case_version is never supplied.
    """
    if not isinstance(case_id, str) or not CASE_ID.fullmatch(case_id):
        raise ProjectionError("匿名病例编号无效")
    try:
        case = cases.get_case(case_id)
    except (PipelineError, OSError, ValueError) as exc:
        raise ProjectionError("病例状态不可读取") from exc
    stages = _stage_statuses(case, case_id)
    baseline_ready = _baseline_ready(case_id, stages)
    numeric_ok, eligible = _quality_gate(case_id, stages, baseline_ready)
    projection = {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "stages": stages,
        "baseline_ready": baseline_ready,
        "numeric_ok": numeric_ok,
        "eligible": eligible,
    }
    canonical = json.dumps(projection, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True, allow_nan=False).encode("ascii")
    return {**projection, "state_sha256": hashlib.sha256(canonical).hexdigest()}
