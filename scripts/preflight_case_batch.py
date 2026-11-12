"""Read-only, anonymous preflight for a sequential Windcatcher case batch.

Usage::

    .venv/Scripts/python.exe scripts/preflight_case_batch.py --created-on 2026-11-02
    .venv/Scripts/python.exe scripts/preflight_case_batch.py case-20261102-122120-0aca

This reports existing stage states and the established CFD preflight. It does
not read CT images, change case artifacts, confirm masks/endplanes, call a
model, or submit a solver job. A ready result is not a numeric quality pass.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Callable, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import automation, cases, endplanes
from pipeline.errors import PipelineError


CASE_ID = re.compile(r"case-\d{8}-\d{6}-[0-9a-f]{4}\Z", re.ASCII)
UPSTREAM = ("import", "qc", "anonymize")


def select_case_ids(
    explicit: Sequence[str], created_on: str | None,
    list_fn: Callable[[], list[dict]] = cases.list_cases,
) -> list[str]:
    """Select the named cases or one date's existing anonymous IDs only."""
    if bool(explicit) == bool(created_on):
        raise ValueError("Provide case IDs or --created-on, but not both")
    if not created_on:
        return list(explicit)
    try:
        parsed = date.fromisoformat(created_on)
    except ValueError as exc:
        raise ValueError("--created-on must be a valid YYYY-MM-DD date") from exc
    prefix = "case-" + parsed.strftime("%Y%m%d") + "-"
    selected = sorted(row["case_id"] for row in list_fn()
                      if isinstance(row.get("case_id"), str)
                      and CASE_ID.fullmatch(row["case_id"])
                      and row["case_id"].startswith(prefix))
    if not selected:
        raise ValueError("No existing anonymous cases match --created-on")
    return selected


def next_step(stages: dict, baseline: dict) -> tuple[str, bool, list[str]]:
    """Describe the next gate; only CFD preflight grants a machine action."""
    if baseline.get("ready"):
        if stages.get("cfd_run") in {"ok", "warn"}:
            return "review_existing_numeric_gate", True, []
        if stages.get("cfd_run") in {"failed", "blocked"}:
            return "review_failed_cfd", True, []
        return "submit_cfd_batch", False, ["submit_cfd_batch"]
    if any(stages.get(name) not in {"ok", "skipped"} for name in UPSTREAM):
        return "review_upstream", True, []
    if stages.get("segment") != "ok":
        return "review_mask", True, []
    if stages.get("surface") != "ok":
        return "review_endplanes_and_surface", True, []
    if stages.get("cfd_prep") != "ok":
        return "prepare_cfd_case", False, []
    return "resolve_cfd_preflight", True, []


def scan_cases(
    case_ids: Sequence[str],
    inspect_fn: Callable[[str], dict] = automation.inspect_case,
    plan_fn: Callable[[str, str], dict | None] = endplanes.load_plan,
) -> dict:
    """Inspect every case in order; an invalid or failed case does not stop the rest."""
    rows: list[dict] = []
    seen: set[str] = set()
    for case_id in case_ids:
        if not isinstance(case_id, str) or not CASE_ID.fullmatch(case_id):
            rows.append({"case_id": str(case_id), "status": "invalid_id",
                         "reason": "Expected an anonymous case ID"})
            continue
        if case_id in seen:
            rows.append({"case_id": case_id, "status": "duplicate_id",
                         "reason": "Case ID already appeared in this batch"})
            continue
        seen.add(case_id)
        try:
            inspected = inspect_fn(case_id)
        except PipelineError as exc:
            rows.append({"case_id": case_id, "status": "inspection_error",
                         "reason": exc.reason, "remediation": exc.remediation})
            continue
        except Exception as exc:
            rows.append({"case_id": case_id, "status": "inspection_error",
                         "reason": "Case inspection failed; see local service logs",
                         "error_type": type(exc).__name__})
            continue
        if not isinstance(inspected, dict) or not isinstance(inspected.get("stages"), dict) or not isinstance(inspected.get("baseline"), dict):
            rows.append({"case_id": case_id, "status": "inspection_error",
                         "reason": "Case inspection returned an invalid contract"})
            continue
        stages = inspected["stages"]
        baseline = inspected["baseline"]
        step, human, actions = next_step(stages, baseline)
        if step == "prepare_cfd_case":
            try:
                plan = plan_fn(case_id, "main_airway")
            except (PipelineError, OSError, ValueError) as exc:
                rows.append({"case_id": case_id, "status": "inspection_error",
                             "reason": "Cannot inspect saved endplane plan",
                             "error_type": type(exc).__name__})
                continue
            if isinstance(plan, dict) and plan.get("format") == "oblique_planes_v1":
                step, human = "review_legacy_global_endplanes", True
        rows.append({"case_id": case_id, "status": "ready" if actions else "paused",
                     "stages": stages, "baseline": baseline,
                     "next_step": step, "requires_human_review": human,
                     "preflight_eligible_actions": actions})
    return {"schema_version": 1, "case_count": len(rows),
            "summary": {name: sum(row["status"] == name for row in rows)
                        for name in ("ready", "paused", "invalid_id", "duplicate_id", "inspection_error")},
            "items": rows}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_ids", nargs="*", help="Existing anonymous case IDs, in execution order")
    parser.add_argument("--created-on", help="Select existing cases by case-ID date (YYYY-MM-DD)")
    args = parser.parse_args(argv)
    try:
        selected = select_case_ids(args.case_ids, args.created_on)
    except ValueError as exc:
        parser.error(str(exc))
    report = scan_cases(selected)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 2 if any(report["summary"][name] for name in ("invalid_id", "duplicate_id", "inspection_error")) else 0


if __name__ == "__main__":
    sys.exit(main())
