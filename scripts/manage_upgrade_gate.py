"""Local-only operator CLI for frozen evaluation, canary decisions, and rollback.

No command executes algorithms or changes the running Windcatcher application.
The chosen database must be stored in a restricted, backed-up data directory.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.upgrade_gate import UpgradeError, UpgradeLedger


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise UpgradeError("duplicate report JSON key")
        result[key] = value
    return result


def _report(path: Path) -> dict:
    with path.open("rb") as source:
        raw = source.read(1_000_001)
    if len(raw) > 1_000_000:
        raise UpgradeError("report exceeds 1 MB")
    try:
        return json.loads(raw, object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError) as exc:
        raise UpgradeError("invalid report JSON") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="restricted local upgrade-ledger.sqlite3 path")
    actions = parser.add_subparsers(dest="action", required=True)
    register = actions.add_parser("register", help="compare a candidate on the frozen cohort")
    register.add_argument("candidate_id")
    register.add_argument("baseline_version")
    register.add_argument("candidate_version")
    register.add_argument("baseline_report", type=Path)
    register.add_argument("candidate_report", type=Path)
    approve = actions.add_parser("approve", help="attest separate human review")
    approve.add_argument("candidate_id")
    approve.add_argument("reviewer_id")
    approve.add_argument("approval_ref")
    approve.add_argument("authorization_ref")
    approve.add_argument("source_review_ref")
    canary = actions.add_parser("start-canary", help="record a 1-10% routing plan")
    canary.add_argument("candidate_id")
    canary.add_argument("percent", type=int)
    observe = actions.add_parser("observe", help="compare new canary observations")
    observe.add_argument("candidate_id")
    observe.add_argument("baseline_report", type=Path)
    observe.add_argument("candidate_report", type=Path)
    observe.add_argument("--permission-errors", type=int, default=0)
    observe.add_argument("--quality-errors", type=int, default=0)
    incident = actions.add_parser("incident", help="immediately roll back on a safety error")
    incident.add_argument("candidate_id")
    incident.add_argument("kind", choices=("permission_error", "quality_error"))
    incident.add_argument("reference_id")
    rollback = actions.add_parser("rollback", help="manually revert to baseline")
    rollback.add_argument("candidate_id")
    rollback.add_argument("reference_id")
    status = actions.add_parser("status", help="show the candidate's gate and event history")
    status.add_argument("candidate_id")
    assignment = actions.add_parser("assignment", help="show a routing decision only")
    assignment.add_argument("candidate_id")
    assignment.add_argument("anonymous_case_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.db.resolve().is_relative_to(ROOT):
            raise UpgradeError("upgrade ledger must be outside the repository and public directory")
        ledger = UpgradeLedger(args.db)
        if args.action == "register":
            result = ledger.register(args.candidate_id, args.baseline_version,
                                     args.candidate_version, _report(args.baseline_report),
                                     _report(args.candidate_report))
        elif args.action == "approve":
            result = ledger.approve(args.candidate_id, args.reviewer_id, args.approval_ref,
                                    args.authorization_ref, args.source_review_ref)
        elif args.action == "start-canary":
            result = ledger.start_canary(args.candidate_id, args.percent)
        elif args.action == "observe":
            try:
                baseline_report = _report(args.baseline_report)
                candidate_report = _report(args.candidate_report)
            except (OSError, UpgradeError):
                baseline_report, candidate_report = {}, {}
            result = ledger.observe(args.candidate_id, baseline_report,
                                    candidate_report,
                                    permission_errors=args.permission_errors,
                                    quality_errors=args.quality_errors)
        elif args.action == "incident":
            result = ledger.incident(args.candidate_id, args.kind, args.reference_id)
        elif args.action == "rollback":
            result = ledger.rollback(args.candidate_id, args.reference_id)
        elif args.action == "status":
            result = ledger.status(args.candidate_id)
        else:
            result = {"candidate_id": args.candidate_id,
                      "version": ledger.assignment(args.candidate_id, args.anonymous_case_id)}
    except (OSError, sqlite3.Error, UpgradeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 1 if result.get("status") in {"rejected", "rolled_back"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
