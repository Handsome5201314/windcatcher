"""Compare explicit saved endplane revisions with separately curated references.

This is an offline technical comparison, not clinical validation or training
authorization. A committed revision means only that an operator saved a plan.
The manifest must explicitly list each anonymous case and its reference plan;
the tool never scans cases, CT images, masks, or patient-share data.
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

from pipeline import endplane_history


class ManifestError(ValueError):
    """The curated evaluation manifest is missing or malformed."""


_CASE_ID = re.compile(r"(?:case-\d{8}-\d{6}-[0-9a-f]{4}|synthetic(?:-[A-Za-z0-9_-]{1,64})?)\Z", re.ASCII)
_SCOPES = {"main_airway", "full_candidate"}
_FORMATS = {"ct_indices_v1", "oblique_planes_v1", "oblique_local_inlet_v2"}
_ENTRY_KEYS = {"case_id", "scope", "revision_id", "reference_plan", "tolerances"}
_PROVENANCE_KEYS = {"schema_version", "manifest_sha256", "records"}
_RECORD_KEYS = {
    "case_id", "scope", "revision_id", "revision_plan_sha256",
    "evaluation_authorization_ref", "reference_confirmation_ref",
    "reviewer_id", "reviewed_at_utc",
}
_RECORD_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,99}\Z", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


def _finite_number(value: object, label: str) -> float:
    if type(value) not in (int, float):
        raise ManifestError(f"{label} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ManifestError(f"{label} must be a finite number") from exc
    if not math.isfinite(number) or abs(number) > 1_000_000_000:
        raise ManifestError(f"{label} must be a finite number")
    return number


def _vector(value: object, label: str, *, normal: bool = False) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ManifestError(f"{label} must be a three-number vector")
    parts = tuple(_finite_number(part, label) for part in value)
    if normal and math.hypot(*parts) < 1e-12:
        raise ManifestError(f"{label} must be a nonzero vector")
    return parts


def _plan_format(plan: object) -> str:
    if not isinstance(plan, dict):
        raise ManifestError("plan must be an object")
    name = plan.get("format", "ct_indices_v1")
    if not isinstance(name, str) or name not in _FORMATS:
        raise ManifestError("unsupported plan format")
    return name


def _validate_plan(plan: object, *, expected_scope: str) -> str:
    name = _plan_format(plan)
    if "scope" in plan and plan["scope"] != expected_scope:
        raise ManifestError("plan scope does not match case scope")
    if "mask_version" in plan and (type(plan["mask_version"]) is not int or plan["mask_version"] < 0):
        raise ManifestError("mask_version must be a nonnegative integer")
    if name == "ct_indices_v1":
        for side in ("anterior", "posterior"):
            value = plan.get(f"{side}_index")
            if type(value) is not int or value < 0:
                raise ManifestError(f"{side}_index must be a nonnegative integer")
    else:
        for side in ("anterior", "posterior"):
            plane = plan.get(f"{side}_plane")
            if not isinstance(plane, dict):
                raise ManifestError(f"{side}_plane must be an object")
            _vector(plane.get("point_lps_mm"), f"{side}_plane.point_lps_mm")
            _vector(plane.get("normal_lps"), f"{side}_plane.normal_lps", normal=True)
        if name == "oblique_local_inlet_v2":
            _vector(plan.get("anterior_seed_lps_mm"), "anterior_seed_lps_mm")
    return name


def _validate_reference_fields(plan: dict, name: str) -> None:
    common = {"format", "scope", "mask_version"}
    geometry = ({"anterior_index", "posterior_index"} if name == "ct_indices_v1" else
                {"anterior_plane", "posterior_plane"})
    if name == "oblique_local_inlet_v2":
        geometry.add("anterior_seed_lps_mm")
    if set(plan) - common - geometry:
        raise ManifestError("reference plan contains unsupported fields")
    if name != "ct_indices_v1":
        for side in ("anterior", "posterior"):
            if set(plan[f"{side}_plane"]) != {"point_lps_mm", "normal_lps"}:
                raise ManifestError("reference plane contains unsupported fields")


def _validate_tolerances(value: object, name: str) -> dict[str, float]:
    required = ({"max_index_delta"} if name == "ct_indices_v1" else
                {"max_plane_offset_mm", "max_normal_deg"})
    if name == "oblique_local_inlet_v2":
        required.add("max_seed_mm")
    if not isinstance(value, dict) or set(value) != required:
        raise ManifestError("tolerances must specify exactly " + ", ".join(sorted(required)))
    result = {key: _finite_number(value[key], f"tolerances.{key}") for key in sorted(required)}
    if any(number < 0 for number in result.values()):
        raise ManifestError("tolerances cannot be negative")
    if result.get("max_normal_deg", 0) > 180:
        raise ManifestError("max_normal_deg cannot exceed 180")
    return result


def _validate_manifest(manifest: object) -> list[dict]:
    if (not isinstance(manifest, dict) or set(manifest) != {"schema_version", "cases"}
            or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1):
        raise ManifestError("schema_version must be 1")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ManifestError("cases must be a nonempty list")
    if len(cases) > 1000:
        raise ManifestError("at most 1000 explicitly listed cases are accepted")
    seen = set()
    validated = []
    for entry in cases:
        if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
            raise ManifestError("each case must contain exactly the required fields")
        case_id, scope, revision_id = entry["case_id"], entry["scope"], entry["revision_id"]
        if not isinstance(case_id, str) or not _CASE_ID.fullmatch(case_id):
            raise ManifestError("case_id must be an anonymous case identifier")
        if not isinstance(scope, str) or scope not in _SCOPES:
            raise ManifestError("unsupported scope")
        if type(revision_id) is not int or revision_id <= 0:
            raise ManifestError("revision_id must be a positive integer")
        key = (case_id, scope)
        if key in seen:
            raise ManifestError("duplicate case and scope in evaluation manifest")
        seen.add(key)
        name = _validate_plan(entry["reference_plan"], expected_scope=scope)
        _validate_reference_fields(entry["reference_plan"], name)
        validated.append({**entry, "format": name,
                          "tolerances": _validate_tolerances(entry["tolerances"], name)})
    return validated


def _validate_provenance(entries: list[dict], provenance: object,
                         manifest_sha256: str | None) -> dict[tuple[str, str, int], str]:
    """Bind manually curated source references to the exact manifest bytes."""
    real_entries = [entry for entry in entries if not entry["case_id"].startswith("synthetic")]
    if not real_entries:
        if provenance is not None:
            raise ManifestError("synthetic-only manifests do not need a provenance sidecar")
        return {}
    if len(real_entries) != len(entries):
        raise ManifestError("synthetic and real cases must use separate manifests")
    if not isinstance(provenance, dict) or set(provenance) != _PROVENANCE_KEYS:
        raise ManifestError("real-case evaluation requires a provenance sidecar")
    if type(provenance["schema_version"]) is not int or provenance["schema_version"] != 1:
        raise ManifestError("provenance schema_version must be 1")
    digest = provenance["manifest_sha256"]
    if (not isinstance(digest, str) or not _SHA256.fullmatch(digest)
            or not isinstance(manifest_sha256, str) or digest != manifest_sha256):
        raise ManifestError("provenance manifest SHA-256 mismatch")
    records = provenance["records"]
    if not isinstance(records, list) or len(records) != len(entries):
        raise ManifestError("provenance records must match manifest cases")
    expected = {(entry["case_id"], entry["scope"], entry["revision_id"]) for entry in entries}
    seen: dict[tuple[str, str, int], str] = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != _RECORD_KEYS:
            raise ManifestError("provenance record fields are invalid")
        if (not isinstance(record["case_id"], str)
                or not isinstance(record["scope"], str)
                or type(record["revision_id"]) is not int):
            raise ManifestError("provenance record identity is invalid")
        key = (record["case_id"], record["scope"], record["revision_id"])
        if key not in expected or key in seen:
            raise ManifestError("provenance record does not match a unique manifest case")
        for field in ("evaluation_authorization_ref", "reference_confirmation_ref", "reviewer_id"):
            value = record[field]
            if not isinstance(value, str) or not _RECORD_ID.fullmatch(value):
                raise ManifestError(f"invalid provenance {field}")
        reviewed_at = record["reviewed_at_utc"]
        if not isinstance(reviewed_at, str):
            raise ManifestError("invalid provenance review time")
        try:
            parsed = datetime.strptime(reviewed_at, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError as exc:
            raise ManifestError("invalid provenance review time") from exc
        if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != reviewed_at:
            raise ManifestError("invalid provenance review time")
        plan_digest = record["revision_plan_sha256"]
        if not isinstance(plan_digest, str) or not _SHA256.fullmatch(plan_digest):
            raise ManifestError("invalid provenance revision plan SHA-256")
        seen[key] = plan_digest
    if set(seen) != expected:
        raise ManifestError("provenance records are incomplete")
    return seen


def _angle_degrees(reference: tuple[float, ...], candidate: tuple[float, ...]) -> float:
    denominator = math.dist(reference, (0.0, 0.0, 0.0)) * math.dist(candidate, (0.0, 0.0, 0.0))
    cosine = max(-1.0, min(1.0, sum(a * b for a, b in zip(reference, candidate)) / denominator))
    return math.degrees(math.acos(cosine))


def _plane_offset_mm(
    reference_point: tuple[float, ...], reference_normal: tuple[float, ...],
    candidate_point: tuple[float, ...], candidate_normal: tuple[float, ...],
) -> float:
    """Symmetric point-to-plane distance; tangent-plane point choice is immaterial."""
    delta = tuple(candidate - reference for reference, candidate in zip(reference_point, candidate_point))
    reference_length = math.hypot(*reference_normal)
    candidate_length = math.hypot(*candidate_normal)
    return max(
        abs(sum(part * normal for part, normal in zip(delta, reference_normal))) / reference_length,
        abs(sum(part * normal for part, normal in zip(delta, candidate_normal))) / candidate_length,
    )


def _compare(reference: dict, candidate: dict, name: str, tolerance: dict[str, float]) -> tuple[dict, list[str]]:
    metrics: dict[str, int | float] = {}
    failures = []
    if name == "ct_indices_v1":
        for side in ("anterior", "posterior"):
            key = f"{side}_index_delta"
            metrics[key] = abs(reference[f"{side}_index"] - candidate[f"{side}_index"])
            if metrics[key] > tolerance["max_index_delta"]:
                failures.append(f"{key}_exceeds_tolerance")
    else:
        for side in ("anterior", "posterior"):
            ref_plane, candidate_plane = reference[f"{side}_plane"], candidate[f"{side}_plane"]
            offset_key, normal_key = f"{side}_plane_offset_mm", f"{side}_normal_deg"
            ref_point = _vector(ref_plane["point_lps_mm"], offset_key)
            candidate_point = _vector(candidate_plane["point_lps_mm"], offset_key)
            ref_normal = _vector(ref_plane["normal_lps"], normal_key, normal=True)
            candidate_normal = _vector(candidate_plane["normal_lps"], normal_key, normal=True)
            plane_offset = _plane_offset_mm(ref_point, ref_normal, candidate_point, candidate_normal)
            normal_error = _angle_degrees(ref_normal, candidate_normal)
            metrics[offset_key] = round(plane_offset, 6)
            metrics[normal_key] = round(normal_error, 6)
            if plane_offset > tolerance["max_plane_offset_mm"]:
                failures.append(f"{offset_key}_exceeds_tolerance")
            if normal_error > tolerance["max_normal_deg"]:
                failures.append(f"{normal_key}_exceeds_tolerance")
        if name == "oblique_local_inlet_v2":
            seed_error = math.dist(
                _vector(reference["anterior_seed_lps_mm"], "reference seed"),
                _vector(candidate["anterior_seed_lps_mm"], "candidate seed"))
            metrics["anterior_seed_mm"] = round(seed_error, 6)
            if seed_error > tolerance["max_seed_mm"]:
                failures.append("anterior_seed_mm_exceeds_tolerance")
    return metrics, failures


def _evaluator_source_sha256() -> str:
    digest = hashlib.sha256()
    for path in (Path(__file__), Path(endplane_history.__file__)):
        digest.update(path.name.encode("ascii"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _cohort_sha256(entries: list[dict]) -> str:
    """Identify the frozen references independently of candidate revision IDs."""
    cohort = [{
        "case_id": entry["case_id"], "scope": entry["scope"],
        "reference_plan": entry["reference_plan"],
        "tolerances": entry["tolerances"],
    } for entry in sorted(entries, key=lambda row: (row["case_id"], row["scope"]))]
    payload = json.dumps(cohort, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def evaluate_manifest(manifest: object, *, provenance: object = None,
                      manifest_sha256: str | None = None,
                      provenance_sha256: str | None = None) -> dict:
    """Return reproducible technical errors; never return reference or candidate geometry."""
    entries = _validate_manifest(manifest)
    expected_hashes = _validate_provenance(entries, provenance, manifest_sha256)
    report = {
        "schema_version": 1, "data_kind": "curated_anonymous" if expected_hashes else "synthetic_only",
        "manifest_sha256": manifest_sha256,
        "cohort_sha256": _cohort_sha256(entries),
        "provenance_sha256": provenance_sha256 if expected_hashes else None,
        "evaluator_source_sha256": _evaluator_source_sha256(),
        "source_references_verified": False,
        "total": len(entries), "passed": 0, "failed": 0, "cases": [],
    }
    for entry in entries:
        case_id, scope, revision_id = entry["case_id"], entry["scope"], entry["revision_id"]
        item = {"case_id": case_id, "scope": scope, "revision_id": revision_id,
                "passed": False, "metrics": {}, "failures": []}
        revisions = endplane_history.list_revisions(case_id, scope)
        revision = next((row for row in revisions if row.get("id") == revision_id
                         and row.get("status") == "committed"), None)
        if revision is None:
            item["failures"].append("committed_revision_not_found")
        else:
            candidate = revision.get("confirmed_plan")
            try:
                expected_hash = expected_hashes.get((case_id, scope, revision_id))
                if expected_hash and (revision.get("plan_hash") != expected_hash
                                      or endplane_history.plan_hash(candidate) != expected_hash):
                    item["failures"].append("revision_plan_sha256_mismatch")
                elif (candidate_format := _validate_plan(candidate, expected_scope=scope)) != entry["format"]:
                    item["failures"].append("plan_format_mismatch")
                elif ("mask_version" in entry["reference_plan"] and
                      candidate.get("mask_version") != entry["reference_plan"]["mask_version"]):
                    item["failures"].append("mask_version_mismatch")
                else:
                    item["metrics"], item["failures"] = _compare(
                        entry["reference_plan"], candidate, entry["format"], entry["tolerances"])
            except (ManifestError, ValueError, TypeError):
                item["failures"].append("invalid_confirmed_plan")
        item["passed"] = not item["failures"]
        report["passed" if item["passed"] else "failed"] += 1
        report["cases"].append(item)
    return report


def _unique_json_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError("duplicate JSON key")
        result[key] = value
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="explicit curated JSON manifest; never scanned automatically")
    parser.add_argument("--provenance", type=Path, help="required source sidecar for real cases")
    args = parser.parse_args(argv)
    try:
        manifest_bytes = args.manifest.read_bytes()
        manifest = json.loads(manifest_bytes, object_pairs_hook=_unique_json_pairs)
        provenance_bytes = args.provenance.read_bytes() if args.provenance else None
        provenance = (json.loads(provenance_bytes, object_pairs_hook=_unique_json_pairs)
                      if provenance_bytes is not None else None)
        report = evaluate_manifest(
            manifest, provenance=provenance,
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            provenance_sha256=(hashlib.sha256(provenance_bytes).hexdigest()
                               if provenance_bytes is not None else None),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ManifestError):
        print(json.dumps({"error": "invalid_or_unreadable_manifest"}), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
