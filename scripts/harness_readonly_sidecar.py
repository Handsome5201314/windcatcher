"""Offline, read-only advice contract for a future restricted Harness process.

The process accepts one bounded JSON summary on stdin and writes one JSON advice
object on stdout. It has no imports from the case store, MCP adapter, network or
agent runtime. A live model must not be connected until an independently scoped
Harness profile and process isolation have been verified.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys

MAX_INPUT_BYTES = 4096
SCHEMA_VERSION = 1
CASE_ID = re.compile(r"(?:case-\d{8}-\d{6}-[0-9a-f]{4}|synthetic(?:-[A-Za-z0-9_-]{1,64})?)\Z", re.ASCII)
STAGES = ("import", "anonymize", "qc", "segment", "surface", "cfd_prep", "cfd_run", "report")
STAGE_STATUSES = frozenset({"pending", "running", "ok", "warn", "failed", "blocked"})
READ_ONLY_TOOLS = ("service_status", "list_cfd_cases", "inspect_cfd_case", "get_cfd_batch")
OUTPUT_STEPS = frozenset({"qc", "segment", "surface", "cfd_prep", "cfd_results", "report", "review"})
_FIELDS = frozenset({"schema_version", "case_id", "state_sha256", "stages",
                     "baseline_ready", "numeric_ok", "eligible"})


class SummaryError(ValueError):
    """Invalid or potentially sensitive input; safe to show without echoing it."""


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SummaryError("重复的 JSON 字段")
        result[key] = value
    return result


def _validate(raw: bytes) -> dict:
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise SummaryError("摘要长度无效")
    try:
        summary = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SummaryError("摘要不是有效 JSON") from exc
    if not isinstance(summary, dict) or set(summary) != _FIELDS:
        raise SummaryError("摘要字段不符合白名单")
    if type(summary["schema_version"]) is not int or summary["schema_version"] != SCHEMA_VERSION:
        raise SummaryError("摘要版本无效")
    if not isinstance(summary["case_id"], str) or not CASE_ID.fullmatch(summary["case_id"]):
        raise SummaryError("匿名病例编号无效")
    if not isinstance(summary["state_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", summary["state_sha256"]):
        raise SummaryError("状态指纹无效")
    stages = summary["stages"]
    if not isinstance(stages, dict) or set(stages) != set(STAGES):
        raise SummaryError("阶段摘要不完整")
    if any(not isinstance(value, str) or value not in STAGE_STATUSES for value in stages.values()):
        raise SummaryError("阶段状态无效")
    for name in ("baseline_ready", "numeric_ok", "eligible"):
        if summary[name] is not None and type(summary[name]) is not bool:
            raise SummaryError("质量状态无效")
    if summary["eligible"] is True and (summary["numeric_ok"] is not True
                                         or summary["baseline_ready"] is not True):
        raise SummaryError("质量状态相互矛盾")
    return summary


def advise(raw: bytes) -> dict:
    """Validate a trusted, anonymous status projection and propose a review step."""
    summary = _validate(raw)
    stages = summary["stages"]
    failed = [stage for stage in STAGES if stages[stage] in {"failed", "blocked"}]
    warnings = [stage for stage in STAGES if stages[stage] == "warn"]
    recap = []
    if failed:
        recap.append("stage_failure")
    if warnings:
        recap.append("stage_warning")
    if summary["baseline_ready"] is False:
        recap.append("baseline_blocked")
    if summary["numeric_ok"] is False:
        recap.append("numeric_gate_failed")
    if summary["eligible"] is False:
        recap.append("eligibility_not_met")
    if failed:
        stage = failed[0]
        recommendation = "review_failed_stage"
        trigger = "manual"
    elif summary["numeric_ok"] is False:
        stage = "cfd_run"
        recommendation = "review_numeric_gate"
        trigger = "numeric_gate_failed"
    elif warnings:
        stage = warnings[0]
        recommendation = "review_warning_stage"
        trigger = "manual"
    elif summary["baseline_ready"] is False:
        stage = "cfd_prep"
        recommendation = "review_preparation"
        trigger = "manual"
    elif summary["eligible"] is False:
        stage = "cfd_run"
        recommendation = "review_eligibility"
        trigger = "manual"
    else:
        stage = next((name for name in STAGES if stages[name] != "ok"), "report")
        recommendation = "review_next_stage" if stage != "report" else "review_report"
        trigger = "manual"
    step = stage if stage in OUTPUT_STEPS else "review"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "advisory_only",
        "case_id": summary["case_id"],
        "state_sha256": summary["state_sha256"],
        "input_sha256": hashlib.sha256(json.dumps(summary, sort_keys=True, separators=(",", ":"),
                                                  ensure_ascii=True).encode("ascii")).hexdigest(),
        "next_step": {"code": recommendation, "stage": stage},
        "failure_recap": recap,
        "skill_draft": {"trigger_condition": trigger, "suggested_steps": [step],
                        "allowed_tools": ["inspect_cfd_case"]},
        "human_review_required": True,
        "automated_action": False,
    }


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        result = advise(raw)
    except SummaryError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
