"""Summarize a manually frozen, anonymous quality dataset for offline upgrades.

This tool never reads patient feedback, browser events, CT, or live CFD data.
Source references in the input are declarations; their originals require human
verification outside this script. The output is not clinical validation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.evaluate_endplanes import ManifestError, _unique_json_pairs


_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_CASE_ID = re.compile(r"(?:case-\d{8}-\d{6}-[0-9a-f]{4}|synthetic(?:-[A-Za-z0-9_-]{1,64})?)\Z", re.ASCII)
_REFERENCE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,99}\Z", re.ASCII)
_SCOPES = {"main_airway", "full_candidate"}
_DATASET_KEYS = {"schema_version", "endplane_report_sha256", "cases"}
_CASE_KEYS = {
    "case_id", "scope", "revision_id", "miscut", "manual_seconds",
    "numeric_gate_ok", "cfd_eligible", "tokens", "cost_usd",
    "mobile_attempted", "mobile_completed", "quality_review_ref",
    "manual_time_ref", "numeric_gate_ref", "usage_ref", "mobile_ref",
    "reviewer_id", "reviewed_at_utc",
}
_SOURCE_FIELDS = (
    "quality_review_ref", "manual_time_ref", "numeric_gate_ref",
    "usage_ref", "mobile_ref", "reviewer_id",
)


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ManifestError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _nonnegative_number(value: object, label: str) -> int | float:
    if type(value) not in (int, float):
        raise ManifestError(f"{label} must be a nonnegative finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ManifestError(f"{label} must be a nonnegative finite number") from exc
    if not math.isfinite(number) or number < 0 or number > 1_000_000_000:
        raise ManifestError(f"{label} must be a nonnegative finite number")
    return value


def _identity(row: object) -> tuple[str, str, int]:
    if not isinstance(row, dict):
        raise ManifestError("case record must be an object")
    case_id, scope, revision_id = (row.get(key) for key in ("case_id", "scope", "revision_id"))
    if not isinstance(case_id, str) or not _CASE_ID.fullmatch(case_id):
        raise ManifestError("case_id must be an anonymous case identifier")
    if not isinstance(scope, str) or scope not in _SCOPES:
        raise ManifestError("unsupported scope")
    if type(revision_id) is not int or revision_id <= 0:
        raise ManifestError("revision_id must be a positive integer")
    return case_id, scope, revision_id


def _report_cases(report: object) -> dict[tuple[str, str, int], dict]:
    if (not isinstance(report, dict) or type(report.get("schema_version")) is not int
            or report["schema_version"] != 1):
        raise ManifestError("endplane report schema_version must be 1")
    if (not isinstance(report.get("data_kind"), str)
            or report["data_kind"] not in {"curated_anonymous", "synthetic_only"}):
        raise ManifestError("unsupported endplane report data kind")
    _digest(report.get("cohort_sha256"), "cohort_sha256")
    _digest(report.get("evaluator_source_sha256"), "evaluator_source_sha256")
    if report["data_kind"] == "curated_anonymous":
        _digest(report.get("manifest_sha256"), "manifest_sha256")
        _digest(report.get("provenance_sha256"), "provenance_sha256")
    cases = report.get("cases")
    if not isinstance(cases, list) or not cases or len(cases) > 1000:
        raise ManifestError("endplane report needs 1 to 1000 cases")
    found = {}
    passed_count = 0
    for row in cases:
        key = _identity(row)
        if key in found or type(row.get("passed")) is not bool or not isinstance(row.get("failures"), list):
            raise ManifestError("invalid or duplicate endplane report case")
        failures = row["failures"]
        if any(not isinstance(reason, str) or not reason.endswith("_exceeds_tolerance")
               for reason in failures):
            raise ManifestError("endplane report contains an unresolved revision or integrity failure")
        if row["passed"] != (not failures):
            raise ManifestError("endplane report pass count is inconsistent")
        passed_count += row["passed"]
        found[key] = row
    if (type(report.get("total")) is not int or report["total"] != len(cases)
            or type(report.get("passed")) is not int or report["passed"] != passed_count
            or type(report.get("failed")) is not int or report["failed"] != len(cases) - passed_count):
        raise ManifestError("endplane report counts are inconsistent")
    real = [not key[0].startswith("synthetic") for key in found]
    if (any(real) != (report["data_kind"] == "curated_anonymous")) or (any(real) and not all(real)):
        raise ManifestError("endplane report data kind does not match cases")
    return found


def _metric_cases(dataset: object, report_keys: set[tuple[str, str, int]]) -> dict[tuple[str, str, int], dict]:
    if (not isinstance(dataset, dict) or set(dataset) != _DATASET_KEYS
            or type(dataset.get("schema_version")) is not int or dataset["schema_version"] != 1):
        raise ManifestError("quality dataset schema_version must be 1")
    cases = dataset["cases"]
    if not isinstance(cases, list) or len(cases) != len(report_keys):
        raise ManifestError("quality dataset must match every endplane case")
    found = {}
    for row in cases:
        if not isinstance(row, dict) or set(row) != _CASE_KEYS:
            raise ManifestError("quality record fields are invalid; feedback is separate")
        key = _identity(row)
        if key in found or key not in report_keys:
            raise ManifestError("quality record does not match a unique endplane case")
        for field in ("miscut", "numeric_gate_ok", "cfd_eligible", "mobile_attempted", "mobile_completed"):
            if type(row[field]) is not bool:
                raise ManifestError(f"{field} must be a boolean")
        if row["cfd_eligible"] and not row["numeric_gate_ok"]:
            raise ManifestError("eligible CFD must pass its numeric gate")
        if row["mobile_completed"] and not row["mobile_attempted"]:
            raise ManifestError("mobile completion requires an attempt")
        _nonnegative_number(row["manual_seconds"], "manual_seconds")
        _nonnegative_number(row["cost_usd"], "cost_usd")
        if type(row["tokens"]) is not int:
            raise ManifestError("tokens must be a nonnegative integer")
        _nonnegative_number(row["tokens"], "tokens")
        for field in _SOURCE_FIELDS:
            value = row[field]
            if not isinstance(value, str) or not _REFERENCE.fullmatch(value):
                raise ManifestError(f"{field} must be a structured source reference")
        reviewed_at = row["reviewed_at_utc"]
        if not isinstance(reviewed_at, str):
            raise ManifestError("invalid review time")
        try:
            parsed = datetime.strptime(reviewed_at, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError as exc:
            raise ManifestError("invalid review time") from exc
        if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != reviewed_at:
            raise ManifestError("invalid review time")
        found[key] = row
    if set(found) != report_keys:
        raise ManifestError("quality dataset cases are incomplete")
    return found


def _ratio(numerator: int | float, denominator: int) -> dict:
    return {"value": numerator / denominator if denominator else None,
            "numerator": numerator, "denominator": denominator}


def evaluate_metrics(report: object, dataset: object, *, report_sha256: str,
                     dataset_sha256: str) -> dict:
    """Aggregate frozen technical/process measures without echoing source IDs."""
    report_sha256 = _digest(report_sha256, "endplane report SHA-256")
    dataset_sha256 = _digest(dataset_sha256, "quality dataset SHA-256")
    report_cases = _report_cases(report)
    if not isinstance(dataset, dict) or dataset.get("endplane_report_sha256") != report_sha256:
        raise ManifestError("quality dataset is not bound to the exact endplane report")
    metrics_by_case = _metric_cases(dataset, set(report_cases))
    rows = list(metrics_by_case.values())
    total = len(rows)
    eligible = sum(row["cfd_eligible"] for row in rows)
    mobile_attempts = sum(row["mobile_attempted"] for row in rows)
    result = {
        "schema_version": 1,
        "data_kind": report["data_kind"],
        "cohort_sha256": report["cohort_sha256"],
        "endplane_report_sha256": report_sha256,
        "dataset_sha256": dataset_sha256,
        "evaluator_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_references_verified": False,
        "total": total,
        "metrics": {
            "endplane_miscut_rate": _ratio(sum(row["miscut"] for row in rows), total),
            "manual_seconds_mean": _ratio(sum(row["manual_seconds"] for row in rows), total),
            "numeric_gate_pass_rate": _ratio(sum(row["numeric_gate_ok"] for row in rows), total),
            "tokens_per_eligible_case": _ratio(sum(row["tokens"] for row in rows), eligible),
            "cost_usd_per_eligible_case": _ratio(sum(row["cost_usd"] for row in rows), eligible),
            "mobile_completion_rate": _ratio(sum(row["mobile_completed"] for row in rows), mobile_attempts),
        },
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("endplane_report", type=Path)
    parser.add_argument("quality_dataset", type=Path)
    args = parser.parse_args(argv)
    try:
        report_bytes = args.endplane_report.read_bytes()
        dataset_bytes = args.quality_dataset.read_bytes()
        report = json.loads(report_bytes, object_pairs_hook=_unique_json_pairs)
        dataset = json.loads(dataset_bytes, object_pairs_hook=_unique_json_pairs)
        result = evaluate_metrics(
            report, dataset,
            report_sha256=hashlib.sha256(report_bytes).hexdigest(),
            dataset_sha256=hashlib.sha256(dataset_bytes).hexdigest(),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ManifestError):
        print(json.dumps({"error": "invalid_or_unreadable_frozen_evaluation"}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
