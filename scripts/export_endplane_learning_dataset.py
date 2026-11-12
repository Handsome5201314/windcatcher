"""Export an explicitly allow-listed, reviewed endplane learning dataset.

This command reads only the anonymous endplane-learning ledger.  It never
scans committed endplane history and never reads CT, masks, case directories,
patient shares, or free text.  A proposal becomes an export record only after
one terminal human feedback event: ``accepted`` keeps the proposed plan and
``adjusted`` keeps the structured plan supplied in that feedback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import endplane_learning


class ExportError(ValueError):
    """The explicit export request or a ledger record is invalid."""


_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_PROPOSAL_ID = re.compile(r"proposal-[0-9a-f]{32}\Z", re.ASCII)
_IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,99}\Z", re.ASCII)
_FEEDBACK_KEYS = {"feedback_id", "proposal_id", "actor_id", "status", "previous_status",
                  "feedback", "created_at", "schema_version"}
_REVIEW_KEYS = {"evaluation_authorization_ref", "reference_confirmation_ref",
                "reviewer_id", "reviewed_at_utc"}


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _validate_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ExportError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _validate_proposal_ids(proposal_ids: Iterable[str]) -> list[str]:
    if isinstance(proposal_ids, (str, bytes, bytearray)):
        raise ExportError("proposal_ids must be an explicit sequence")
    try:
        values = list(proposal_ids)
    except TypeError as exc:
        raise ExportError("proposal_ids must be an explicit sequence") from exc
    if not values:
        raise ExportError("at least one proposal id is required")
    if any(not isinstance(value, str) or not _PROPOSAL_ID.fullmatch(value) for value in values):
        raise ExportError("proposal_ids contains an invalid proposal id")
    if len(set(values)) != len(values):
        raise ExportError("proposal_ids must not contain duplicates")
    # Sorting makes the manifest and its hash independent of command-line order.
    return sorted(values)


def _feedback_hash(feedback: dict) -> str:
    # Keep the audit identity bounded to the stored structured event.  The
    # timestamp and numeric event id make two events distinguishable without
    # exporting actor/account data.
    payload = {
        "proposal_id": feedback["proposal_id"],
        "actor_id": feedback["actor_id"],
        "feedback_id": feedback["feedback_id"],
        "status": feedback["status"],
        "previous_status": feedback["previous_status"],
        "feedback": feedback["feedback"],
        "created_at": feedback["created_at"],
    }
    return _digest(payload)


def _record_for_proposal(proposal: dict, feedback: dict) -> dict:
    if (not isinstance(feedback, dict) or set(feedback) != _FEEDBACK_KEYS
            or feedback["schema_version"] != 1 or type(feedback["schema_version"]) is not int):
        raise ExportError("stored feedback event is invalid")
    feedback_actor = feedback["actor_id"]
    if feedback_actor is not None and (not isinstance(feedback_actor, str)
                                       or not _IDENTIFIER.fullmatch(feedback_actor)):
        raise ExportError("stored feedback actor is invalid")
    status = feedback.get("status")
    if status not in {"accepted", "adjusted"}:
        raise ExportError("only accepted or adjusted feedback can be exported")
    if proposal.get("status") != status:
        raise ExportError("proposal status does not match its feedback status")
    payload = feedback.get("feedback")
    if (not isinstance(payload, dict)
            or set(payload) - {"reason_code", "adjusted_plan", "adjusted_plan_hash"}):
        raise ExportError("stored feedback payload is invalid")
    reason = payload.get("reason_code")
    if reason is not None and (not isinstance(reason, str) or not _IDENTIFIER.fullmatch(reason)):
        raise ExportError("stored feedback reason code is invalid")
    if (feedback.get("proposal_id") != proposal["proposal_id"]
            or feedback.get("previous_status") != "proposed"
            or type(feedback.get("feedback_id")) is not int
            or feedback["feedback_id"] <= 0):
        raise ExportError("stored feedback event does not match proposal")
    try:
        created_at = datetime.fromisoformat(feedback["created_at"].replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ExportError("stored feedback event time is invalid") from exc
    if created_at.tzinfo is None or created_at.utcoffset() != timezone.utc.utcoffset(created_at):
        raise ExportError("stored feedback event time must be UTC")

    if status == "accepted":
        if "adjusted_plan" in payload or "adjusted_plan_hash" in payload:
            raise ExportError("accepted feedback must not contain an adjusted plan")
        effective_plan = proposal["plan"]
    else:
        effective_plan = payload.get("adjusted_plan")
        if not isinstance(effective_plan, dict):
            raise ExportError("adjusted feedback has no structured adjusted plan")

    # Revalidate the effective plan before it leaves the ledger.  This also
    # guarantees that a tampered adjusted payload fails closed.
    effective_plan = endplane_learning.validate_plan(
        effective_plan,
        scope=proposal["scope"],
        mask_version=proposal["mask_version"],
    )
    effective_hash = endplane_learning.plan_hash(
        effective_plan,
        scope=proposal["scope"],
        mask_version=proposal["mask_version"],
    )
    expected_adjusted_hash = payload.get("adjusted_plan_hash")
    if status == "adjusted" and expected_adjusted_hash != effective_hash:
        raise ExportError("adjusted plan hash does not match the plan")

    return {
        "proposal_id": proposal["proposal_id"],
        "case_id": proposal["case_id"],
        "scope": proposal["scope"],
        "mask_version": proposal["mask_version"],
        "source": proposal["source"],
        "source_version": proposal["source_version"],
        "feedback_status": status,
        "proposal_plan_sha256": proposal["plan_hash"],
        "effective_plan_sha256": effective_hash,
        "feedback_sha256": _feedback_hash(feedback),
        "plan": effective_plan,
    }


def _reviews(value: object, ids: list[str]) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - set(ids):
        raise ExportError("review references must match the explicit proposal allowlist")
    result = {}
    for proposal_id, review in value.items():
        if not isinstance(review, dict) or set(review) != _REVIEW_KEYS:
            raise ExportError("review reference fields are invalid")
        for field in _REVIEW_KEYS - {"reviewed_at_utc"}:
            if not isinstance(review[field], str) or not _IDENTIFIER.fullmatch(review[field]):
                raise ExportError("review references must be bounded identifiers")
        try:
            parsed = datetime.strptime(review["reviewed_at_utc"], "%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError) as exc:
            raise ExportError("review reference time must be UTC") from exc
        if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != review["reviewed_at_utc"]:
            raise ExportError("review reference time must be canonical UTC")
        result[proposal_id] = dict(review)
    return result


def export_dataset(
    proposal_ids: Iterable[str],
    *,
    source_cohort_sha256: str,
    store: endplane_learning.EndplaneLearningStore | None = None,
    actor_id: str | None = None,
    review_references: dict | None = None,
) -> dict:
    """Build a frozen anonymous manifest from an explicit proposal allowlist."""

    ids = _validate_proposal_ids(proposal_ids)
    source_cohort_sha256 = _validate_digest(source_cohort_sha256, "source_cohort_sha256")
    reviews = _reviews(review_references, ids)
    if store is None:
        store = endplane_learning.EndplaneLearningStore()

    records: list[dict] = []
    for proposal_id in ids:
        proposal = store.get_proposal(proposal_id, actor_id=actor_id)
        if proposal is None:
            raise ExportError(f"proposal is not available to this actor: {proposal_id}")
        feedback_rows = store.list_feedback(proposal_id, actor_id=actor_id)
        if len(feedback_rows) != 1:
            raise ExportError(f"proposal must have exactly one terminal feedback event: {proposal_id}")
        feedback = feedback_rows[0]
        if feedback.get("status") not in {"accepted", "adjusted"}:
            raise ExportError(f"proposal is not approved for export: {proposal_id}")
        record = _record_for_proposal(proposal, feedback)
        if not record["case_id"].startswith("synthetic") and proposal_id not in reviews:
            raise ExportError("real cases require explicit evaluation and reference review records")
        record["review_references_sha256"] = (_digest(reviews[proposal_id])
                                               if proposal_id in reviews else None)
        records.append(record)

    identities = [(row["case_id"], row["scope"], row["mask_version"]) for row in records]
    if len(set(identities)) != len(identities):
        raise ExportError("only one reviewed reference is allowed per case, scope, and mask version")

    allowlist_hash = _digest(ids)
    body = {
        "schema_version": 1,
        "dataset_kind": "endplane_learning_reviewed",
        "source_cohort_sha256": source_cohort_sha256,
        "cohort_sha256": _digest([{
            "case_id": row["case_id"], "scope": row["scope"],
            "mask_version": row["mask_version"], "plan": row["plan"],
        } for row in sorted(records, key=lambda row: (row["case_id"], row["scope"],
                                                      row["mask_version"], row["proposal_id"]))]),
        "proposal_allowlist_sha256": allowlist_hash,
        "source_references_verified": False,
        "proposal_count": len(records),
        "records": records,
    }
    manifest_sha256 = _digest(body)
    return body | {"manifest_sha256": manifest_sha256}


def _json_load_ids(values: list[str] | None, path: Path | None) -> list[str]:
    result = list(values or [])
    if path is not None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ExportError("proposal id file is unreadable") from exc
        if not isinstance(data, list):
            raise ExportError("proposal id file must contain a JSON array")
        result.extend(data)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal-id", action="append", help="explicit proposal id; repeatable")
    parser.add_argument("--proposal-ids-file", type=Path,
                        help="JSON array of explicit proposal ids")
    parser.add_argument("--source-cohort-sha256", required=True,
                        help="source cohort digest; the exported cohort digest is calculated")
    parser.add_argument("--review-references-file", type=Path,
                        help="proposal-id keyed structured authorization/reference review records")
    parser.add_argument("--actor-id")
    parser.add_argument("--learning-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        proposal_ids = _validate_proposal_ids(_json_load_ids(args.proposal_id, args.proposal_ids_file))
        _validate_digest(args.source_cohort_sha256, "source_cohort_sha256")
        reviews = None
        if args.review_references_file is not None:
            reviews = json.loads(args.review_references_file.read_text(encoding="utf-8"))
        reviews = _reviews(reviews, proposal_ids)
        if args.output is not None and args.output.exists():
            raise ExportError("output already exists")
        store = endplane_learning.EndplaneLearningStore(args.learning_dir)
        result = export_dataset(
            proposal_ids,
            source_cohort_sha256=args.source_cohort_sha256,
            store=store,
            actor_id=args.actor_id,
            review_references=reviews,
        )
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"))
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as handle:
                handle.write(encoded + "\n")
    except (OSError, UnicodeError, json.JSONDecodeError, ExportError,
            endplane_learning.LearningError):
        print(json.dumps({"error": "invalid_or_unavailable_learning_export"}), file=sys.stderr)
        return 2
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
