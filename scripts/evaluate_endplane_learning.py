"""Compare an explicit independent candidate snapshot with frozen reviewed plans.

Only the two supplied JSON files are read.  This is an offline geometry
comparison; a pass never promotes a candidate or changes a case or CFD gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import endplane_learning
from scripts import evaluate_endplanes
from scripts import export_endplane_learning_dataset as exporter


class EvaluationError(ValueError):
    """A frozen reference, candidate snapshot, or tolerance is invalid."""


_CASE_ID = re.compile(r"(?:case-\d{8}-\d{6}-[0-9a-f]{4}|synthetic(?:-[A-Za-z0-9_-]{1,64})?)\Z", re.ASCII)
_IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,99}\Z", re.ASCII)
_FROZEN_KEYS = {"schema_version", "dataset_kind", "source_cohort_sha256", "cohort_sha256",
                "proposal_allowlist_sha256", "source_references_verified", "proposal_count",
                "records", "manifest_sha256"}
_RECORD_KEYS = {"proposal_id", "case_id", "scope", "mask_version", "source", "source_version",
                "feedback_status", "proposal_plan_sha256", "effective_plan_sha256",
                "feedback_sha256", "plan", "review_references_sha256"}
_CANDIDATE_KEYS = {"schema_version", "frozen_manifest_sha256", "candidates"}
_ROW_KEYS = {"case_id", "scope", "mask_version", "plan"}
_FORMATS = {"oblique_planes_v1", "oblique_local_inlet_v2"}


def _digest(value: object) -> str:
    try:
        return exporter._digest(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EvaluationError("value is not canonical JSON") from exc


def _hash(value: object) -> str:
    try:
        return exporter._validate_digest(value, "hash")
    except exporter.ExportError as exc:
        raise EvaluationError("invalid SHA-256 digest") from exc


def _identity(row: dict) -> tuple[str, str, int]:
    case_id, scope, version = row.get("case_id"), row.get("scope"), row.get("mask_version")
    if (not isinstance(case_id, str) or not _CASE_ID.fullmatch(case_id)
            or not isinstance(scope, str) or scope not in {"main_airway", "full_candidate"}
            or type(version) is not int or not 0 <= version <= 2**31 - 1):
        raise EvaluationError("invalid anonymous case identity")
    return case_id, scope, version


def _plan(value: object, identity: tuple[str, str, int]) -> dict:
    try:
        plan = endplane_learning.validate_plan(value, scope=identity[1], mask_version=identity[2])
        if (plan.get("format") not in _FORMATS or plan.get("scope") != identity[1]
                or plan.get("mask_version") != identity[2]):
            raise EvaluationError("plan format, scope, and mask version are required")
        evaluate_endplanes._validate_plan(plan, expected_scope=identity[1])
        return plan
    except (endplane_learning.LearningError, evaluate_endplanes.ManifestError) as exc:
        raise EvaluationError("invalid structured endplane plan") from exc


def _frozen(value: object) -> dict[tuple[str, str, int], dict]:
    if (not isinstance(value, dict) or set(value) != _FROZEN_KEYS
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or value["dataset_kind"] != "endplane_learning_reviewed"
            or value["source_references_verified"] is not False):
        raise EvaluationError("invalid frozen learning manifest")
    for field in ("source_cohort_sha256", "cohort_sha256", "proposal_allowlist_sha256", "manifest_sha256"):
        _hash(value[field])
    body = {key: item for key, item in value.items() if key != "manifest_sha256"}
    if _digest(body) != value["manifest_sha256"]:
        raise EvaluationError("frozen manifest hash mismatch")
    records = value["records"]
    if (not isinstance(records, list) or not records
            or type(value["proposal_count"]) is not int or value["proposal_count"] != len(records)):
        raise EvaluationError("frozen record count mismatch")
    by_case, ids = {}, []
    for row in records:
        if not isinstance(row, dict) or set(row) != _RECORD_KEYS:
            raise EvaluationError("frozen record fields are invalid")
        identity = _identity(row)
        if identity in by_case:
            raise EvaluationError("duplicate frozen case")
        try:
            exporter._validate_proposal_ids([row["proposal_id"]])
        except exporter.ExportError as exc:
            raise EvaluationError("invalid frozen proposal id") from exc
        ids.append(row["proposal_id"])
        if (not isinstance(row["feedback_status"], str)
                or row["feedback_status"] not in {"accepted", "adjusted"}):
            raise EvaluationError("frozen reference must be accepted or adjusted")
        for field in ("source", "source_version"):
            if not isinstance(row[field], str) or not _IDENTIFIER.fullmatch(row[field]):
                raise EvaluationError("invalid frozen source identifier")
        for field in ("proposal_plan_sha256", "effective_plan_sha256", "feedback_sha256"):
            _hash(row[field])
        review_hash = row["review_references_sha256"]
        if review_hash is not None:
            _hash(review_hash)
        elif not identity[0].startswith("synthetic"):
            raise EvaluationError("real references require an explicit review hash")
        plan = _plan(row["plan"], identity)
        actual_hash = endplane_learning.plan_hash(plan, scope=identity[1], mask_version=identity[2])
        if actual_hash != row["effective_plan_sha256"]:
            raise EvaluationError("effective plan hash mismatch")
        if row["feedback_status"] == "accepted" and actual_hash != row["proposal_plan_sha256"]:
            raise EvaluationError("accepted proposal hash mismatch")
        by_case[identity] = row | {"plan": plan}
    if len(set(ids)) != len(ids) or ids != sorted(ids):
        raise EvaluationError("frozen proposal allowlist must be unique and sorted")
    if _digest(ids) != value["proposal_allowlist_sha256"]:
        raise EvaluationError("proposal allowlist hash mismatch")
    cohort = [{"case_id": row["case_id"], "scope": row["scope"],
               "mask_version": row["mask_version"], "plan": row["plan"]}
              for row in sorted(records, key=lambda row: (row["case_id"], row["scope"],
                                                          row["mask_version"], row["proposal_id"]))]
    if _digest(cohort) != value["cohort_sha256"]:
        raise EvaluationError("frozen reference cohort hash mismatch")
    real_count = sum(not identity[0].startswith("synthetic") for identity in by_case)
    if real_count not in {0, len(by_case)}:
        raise EvaluationError("synthetic and real references must be evaluated separately")
    return by_case


def _candidates(value: object, frozen_hash: str, references: dict) -> dict:
    if (not isinstance(value, dict) or set(value) != _CANDIDATE_KEYS
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or _hash(value["frozen_manifest_sha256"]) != frozen_hash
            or not isinstance(value["candidates"], list)):
        raise EvaluationError("candidate snapshot does not bind the frozen manifest")
    result = {}
    for row in value["candidates"]:
        if not isinstance(row, dict) or set(row) != _ROW_KEYS:
            raise EvaluationError("invalid candidate row fields")
        identity = _identity(row)
        if identity in result or identity not in references:
            raise EvaluationError("candidate cohort or version does not match frozen references")
        plan = _plan(row["plan"], identity)
        if plan["format"] != references[identity]["plan"]["format"]:
            raise EvaluationError("candidate format does not match frozen reference")
        result[identity] = plan
    if set(result) != set(references):
        raise EvaluationError("candidate snapshot must cover every frozen reference exactly once")
    return result


def evaluate_learning(frozen: object, candidates: object, *, tolerances: object) -> dict:
    """Evaluate explicit snapshots without opening a case directory or ledger."""
    references = _frozen(frozen)
    candidate_plans = _candidates(candidates, frozen["manifest_sha256"], references)
    formats = {row["plan"]["format"] for row in references.values()}
    required = {"max_plane_offset_mm", "max_normal_deg"}
    if "oblique_local_inlet_v2" in formats:
        required.add("max_seed_mm")
    if not isinstance(tolerances, dict) or set(tolerances) != required:
        raise EvaluationError("tolerances must exactly match the frozen plan formats")
    try:
        checked = {key: evaluate_endplanes._finite_number(value, key)
                   for key, value in tolerances.items()}
    except evaluate_endplanes.ManifestError as exc:
        raise EvaluationError("invalid tolerance") from exc
    if any(value < 0 for value in checked.values()) or checked["max_normal_deg"] > 180:
        raise EvaluationError("tolerance is outside its valid range")
    ordered = sorted(references)
    evaluation_cohort = [{"case_id": identity[0], "scope": identity[1], "mask_version": identity[2],
                          "reference_plan": references[identity]["plan"], "tolerances": checked}
                         for identity in ordered]
    report = {
        "schema_version": 1,
        "data_kind": "synthetic_only" if ordered[0][0].startswith("synthetic") else "curated_anonymous",
        "source_references_verified": False,
        "auto_promotion": False,
        "frozen_manifest_sha256": frozen["manifest_sha256"],
        "frozen_cohort_sha256": frozen["cohort_sha256"],
        "source_cohort_sha256": frozen["source_cohort_sha256"],
        "candidate_snapshot_sha256": _digest(candidates),
        "evaluation_cohort_sha256": _digest(evaluation_cohort),
        "evaluator_source_sha256": hashlib.sha256(
            Path(__file__).read_bytes() + Path(evaluate_endplanes.__file__).read_bytes()
            + Path(endplane_learning.__file__).read_bytes()
            + Path(exporter.__file__).read_bytes()).hexdigest(),
        "total": len(ordered), "passed": 0, "failed": 0, "cases": [],
    }
    for identity in ordered:
        reference = references[identity]["plan"]
        metrics, failures = evaluate_endplanes._compare(reference, candidate_plans[identity],
                                                        reference["format"], checked)
        passed = not failures
        report["cases"].append({"case_id": identity[0], "scope": identity[1], "mask_version": identity[2],
                                "passed": passed, "metrics": metrics, "failures": failures})
        report["passed" if passed else "failed"] += 1
    return report


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvaluationError("duplicate JSON key")
        result[key] = value
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("frozen_manifest", type=Path)
    parser.add_argument("candidate_snapshot", type=Path)
    parser.add_argument("--max-plane-offset-mm", required=True, type=float)
    parser.add_argument("--max-normal-deg", required=True, type=float)
    parser.add_argument("--max-seed-mm", type=float)
    args = parser.parse_args(argv)
    try:
        if args.frozen_manifest.resolve() == args.candidate_snapshot.resolve():
            raise EvaluationError("candidate snapshot must be a separate input file")
        frozen_bytes = args.frozen_manifest.read_bytes()
        candidate_bytes = args.candidate_snapshot.read_bytes()
        frozen = json.loads(frozen_bytes, object_pairs_hook=_unique_pairs)
        candidates = json.loads(candidate_bytes, object_pairs_hook=_unique_pairs)
        tolerances = {"max_plane_offset_mm": args.max_plane_offset_mm,
                      "max_normal_deg": args.max_normal_deg}
        if args.max_seed_mm is not None:
            tolerances["max_seed_mm"] = args.max_seed_mm
        result = evaluate_learning(frozen, candidates, tolerances=tolerances)
        result["frozen_file_sha256"] = hashlib.sha256(frozen_bytes).hexdigest()
        result["candidate_file_sha256"] = hashlib.sha256(candidate_bytes).hexdigest()
    except (OSError, UnicodeError, json.JSONDecodeError, EvaluationError):
        print(json.dumps({"error": "invalid_or_unreadable_learning_evaluation"}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
