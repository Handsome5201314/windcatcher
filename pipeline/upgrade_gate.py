"""Offline, human-gated upgrade decisions for anonymous technical evaluations.

This module never executes an algorithm, dispatches CFD, or grants case access.
Its canary assignment is a routing decision for a separately reviewed adapter.
"""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class UpgradeError(ValueError):
    """A report or state transition failed closed."""


def segmentation_readiness(report: dict) -> dict:
    """Separate schema adapter; never turns a segmentation score into CFD approval."""
    from .segmentation_learning import digest
    if (not isinstance(report, dict) or report.get('schema') != 'segmentation-evaluation-v1'
            or report.get('report_sha256') != digest({k: v for k, v in report.items() if k != 'report_sha256'})
            or report.get('auto_promotion') is not False
            or report.get('reference_provenance_independently_verified') is not False
            or report.get('clinical_validation') is not False):
        raise UpgradeError('invalid segmentation evaluation report')
    held = report.get('holdout')
    if (not isinstance(held, dict) or set(held) != {'samples', 'subjects', 'regressions', 'improved_samples'}
            or any(type(v) is not int or v < 0 for v in held.values())
            or held['regressions'] + held['improved_samples'] > held['samples']
            or held['subjects'] > held['samples']):
        raise UpgradeError('invalid segmentation holdout summary')
    reasons = []
    if not held['samples']:
        reasons.append('no_independent_holdout')
    if held['regressions']:
        reasons.append('holdout_regression')
    if not held['improved_samples']:
        reasons.append('no_holdout_improvement')
    if report.get('synthetic_only') is True:
        reasons.append('synthetic_only')
    return {'schema': 'segmentation-upgrade-readiness-v1',
        'report_sha256': report['report_sha256'], 'dataset_sha256': report['dataset_sha256'],
        'status': 'blocked' if reasons else 'ready_for_independent_review',
        'reasons': reasons, 'auto_promotion': False, 'production_eligible': False,
        'required_before_release': ['independent_reference_provenance_review',
            'clinically_representative_holdout', 'critical_airway_region_evaluation',
            'downstream_cfd_validation', 'explicit_versioned_release']}


_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_ID = re.compile(r"[a-z0-9][a-z0-9._-]{1,63}\Z", re.ASCII)
_REFERENCE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,99}\Z", re.ASCII)
_CASE = re.compile(r"(?:case-\d{8}-\d{6}-[0-9a-f]{4}|synthetic(?:-[A-Za-z0-9_-]{1,64})?)\Z", re.ASCII)
_METRICS = {
    "endplane_miscut_rate": "lower",
    "manual_seconds_mean": "lower",
    "numeric_gate_pass_rate": "higher",
    "tokens_per_eligible_case": "lower",
    "cost_usd_per_eligible_case": "lower",
    "mobile_completion_rate": "higher",
}
_RATES = {"endplane_miscut_rate", "numeric_gate_pass_rate", "mobile_completion_rate"}


def _digest(value: object, name: str) -> str:
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise UpgradeError(f"invalid {name}")
    return value


def _identifier(value: object, name: str, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or not pattern.fullmatch(value):
        raise UpgradeError(f"invalid {name}")
    return value


def _nonnegative(value: object, name: str) -> float:
    if type(value) not in (int, float):
        raise UpgradeError(f"invalid {name}")
    try:
        number = float(value)
    except OverflowError as exc:
        raise UpgradeError(f"invalid {name}") from exc
    if not math.isfinite(number) or number < 0 or number > 1e12:
        raise UpgradeError(f"invalid {name}")
    return number


def validate_report(report: object) -> dict:
    """Reject incomplete coverage, synthetic cohorts, and nonfinite metrics."""
    if (type(report) is not dict or type(report.get("schema_version")) is not int
            or report["schema_version"] != 1):
        raise UpgradeError("invalid learning report version")
    if set(report) != {"schema_version", "data_kind", "cohort_sha256",
                       "endplane_report_sha256", "dataset_sha256",
                       "evaluator_source_sha256", "source_references_verified",
                       "total", "metrics"}:
        raise UpgradeError("unexpected learning report fields")
    if report.get("data_kind") != "curated_anonymous":
        raise UpgradeError("curated anonymous evaluation required")
    _digest(report["evaluator_source_sha256"], "evaluator source digest")
    if report["source_references_verified"] is not False:
        raise UpgradeError("source verification must be separately attested")
    total = report.get("total")
    if type(total) is not int or not 1 <= total <= 1000:
        raise UpgradeError("invalid report case count")
    cohort = _digest(report.get("cohort_sha256"), "frozen cohort digest")
    dataset = _digest(report.get("dataset_sha256"), "metric dataset digest")
    endplane = _digest(report.get("endplane_report_sha256"), "endplane report digest")
    metrics = report.get("metrics")
    if type(metrics) is not dict or set(metrics) != set(_METRICS):
        raise UpgradeError("incomplete or extra learning metrics")
    values = {}
    for name in _METRICS:
        metric = metrics[name]
        if type(metric) is not dict or set(metric) != {"value", "numerator", "denominator"}:
            raise UpgradeError(f"invalid {name} metric shape")
        numerator = _nonnegative(metric["numerator"], name + " numerator")
        denominator = metric["denominator"]
        if type(denominator) is not int or not 1 <= denominator <= total:
            raise UpgradeError(f"missing or invalid {name} denominator")
        value = _nonnegative(metric["value"], name + " value")
        if not math.isclose(value, numerator / denominator, rel_tol=1e-8, abs_tol=1e-9):
            raise UpgradeError(f"inconsistent {name} metric")
        if name in _RATES and (numerator > denominator or value > 1):
            raise UpgradeError(f"invalid {name} rate")
        if name in {"manual_seconds_mean", "endplane_miscut_rate", "numeric_gate_pass_rate"} and denominator != total:
            raise UpgradeError(f"incomplete {name} coverage")
        values[name] = value
    return {"cohort_sha256": cohort, "dataset_sha256": dataset,
            "endplane_report_sha256": endplane, "total": total, "values": values}


def compare_reports(baseline: object, candidate: object) -> dict:
    """Compare runs on the same frozen references, using all six metrics."""
    left, right = validate_report(baseline), validate_report(candidate)
    if left["cohort_sha256"] != right["cohort_sha256"] or left["total"] != right["total"]:
        raise UpgradeError("reports use different frozen cohorts")
    regressions, improvements = [], []
    for name, direction in _METRICS.items():
        old, new = left["values"][name], right["values"][name]
        if math.isclose(old, new, rel_tol=1e-10, abs_tol=1e-12):
            continue
        if (new < old) == (direction == "lower"):
            improvements.append(name)
        else:
            regressions.append(name)
    return {"cohort_sha256": left["cohort_sha256"], "total": left["total"],
            "baseline_dataset_sha256": left["dataset_sha256"],
            "candidate_dataset_sha256": right["dataset_sha256"],
            "regressions": regressions, "improvements": improvements}


class UpgradeLedger:
    """Durable local gate storing only versions, hashes, and approval references."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS upgrades (
                candidate_id TEXT PRIMARY KEY, baseline_version TEXT NOT NULL,
                candidate_version TEXT NOT NULL, cohort_sha256 TEXT NOT NULL,
                baseline_dataset_sha256 TEXT NOT NULL, candidate_dataset_sha256 TEXT NOT NULL,
                status TEXT NOT NULL, canary_percent INTEGER NOT NULL DEFAULT 0,
                approval_ref TEXT, authorization_ref TEXT, source_review_ref TEXT,
                reviewer_id TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS upgrade_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT NOT NULL,
                event TEXT NOT NULL, evidence_sha256 TEXT, reference_id TEXT,
                created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))""")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=10000")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _event(db: sqlite3.Connection, candidate_id: str, event: str,
               digest: str | None = None, reference: str | None = None) -> None:
        db.execute("""INSERT INTO upgrade_events
            (candidate_id,event,evidence_sha256,reference_id) VALUES (?,?,?,?)""",
            (candidate_id, event, digest, reference))

    @staticmethod
    def _locked(db: sqlite3.Connection, candidate_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM upgrades WHERE candidate_id=?", (candidate_id,)).fetchone()
        if row is None:
            raise UpgradeError("candidate not found")
        return row

    def register(self, candidate_id: str, baseline_version: str, candidate_version: str,
                 baseline: object, candidate: object) -> dict:
        candidate_id = _identifier(candidate_id, "candidate id", _ID)
        baseline_version = _identifier(baseline_version, "baseline version", _ID)
        candidate_version = _identifier(candidate_version, "candidate version", _ID)
        if baseline_version == candidate_version:
            raise UpgradeError("candidate must have a distinct version")
        result = compare_reports(baseline, candidate)
        status = "evaluated" if result["improvements"] and not result["regressions"] else "rejected"
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("""INSERT INTO upgrades
                    (candidate_id,baseline_version,candidate_version,cohort_sha256,
                     baseline_dataset_sha256,candidate_dataset_sha256,status)
                    VALUES (?,?,?,?,?,?,?)""",
                    (candidate_id, baseline_version, candidate_version, result["cohort_sha256"],
                     result["baseline_dataset_sha256"], result["candidate_dataset_sha256"], status))
            except sqlite3.IntegrityError as exc:
                raise UpgradeError("candidate id already registered") from exc
            self._event(db, candidate_id, status, result["candidate_dataset_sha256"])
        return {"candidate_id": candidate_id, "status": status, **result}

    def approve(self, candidate_id: str, reviewer_id: str, approval_ref: str,
                authorization_ref: str, source_review_ref: str) -> dict:
        candidate_id = _identifier(candidate_id, "candidate id", _ID)
        reviewer_id = _identifier(reviewer_id, "reviewer id", _REFERENCE)
        approval_ref = _identifier(approval_ref, "approval reference", _REFERENCE)
        authorization_ref = _identifier(authorization_ref, "authorization reference", _REFERENCE)
        source_review_ref = _identifier(source_review_ref, "source review reference", _REFERENCE)
        if len({approval_ref, authorization_ref, source_review_ref}) != 3:
            raise UpgradeError("approval, authorization and source review need separate references")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._locked(db, candidate_id)
            if row["status"] != "evaluated":
                raise UpgradeError("only non-regressing evaluations can be approved")
            db.execute("""UPDATE upgrades SET status='approved',reviewer_id=?,approval_ref=?,
                authorization_ref=?,source_review_ref=? WHERE candidate_id=?""",
                (reviewer_id, approval_ref, authorization_ref, source_review_ref, candidate_id))
            self._event(db, candidate_id, "approved", reference=approval_ref)
        return self.status(candidate_id)

    def start_canary(self, candidate_id: str, percent: int) -> dict:
        candidate_id = _identifier(candidate_id, "candidate id", _ID)
        if type(percent) is not int or not 1 <= percent <= 10:
            raise UpgradeError("canary must be between 1 and 10 percent")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._locked(db, candidate_id)
            if (row["status"] != "approved" or not row["approval_ref"]
                    or not row["authorization_ref"] or not row["source_review_ref"]):
                raise UpgradeError("authorization, source review and human approval required before canary")
            if db.execute("SELECT 1 FROM upgrades WHERE status='canary' LIMIT 1").fetchone():
                raise UpgradeError("another candidate is already in canary")
            db.execute("UPDATE upgrades SET status='canary',canary_percent=? WHERE candidate_id=?",
                       (percent, candidate_id))
            self._event(db, candidate_id, "canary_started", reference=row["approval_ref"])
        return self.status(candidate_id)

    def _rollback_locked(self, db: sqlite3.Connection, candidate_id: str,
                         reason: str, digest: str | None = None,
                         reference: str | None = None) -> None:
        db.execute("UPDATE upgrades SET status='rolled_back',canary_percent=0 WHERE candidate_id=?",
                   (candidate_id,))
        self._event(db, candidate_id, "rolled_back_" + reason, digest, reference)

    def observe(self, candidate_id: str, baseline: object, candidate: object,
                *, permission_errors: int = 0, quality_errors: int = 0) -> dict:
        candidate_id = _identifier(candidate_id, "candidate id", _ID)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._locked(db, candidate_id)
            if row["status"] != "canary":
                raise UpgradeError("candidate is not in canary")
            try:
                if (type(permission_errors) is not int or permission_errors < 0
                        or type(quality_errors) is not int or quality_errors < 0):
                    raise UpgradeError("invalid incident counts")
                result = compare_reports(baseline, candidate)
                if (result["cohort_sha256"] == row["cohort_sha256"]
                        or result["baseline_dataset_sha256"] == row["baseline_dataset_sha256"]
                        or result["candidate_dataset_sha256"] == row["candidate_dataset_sha256"]):
                    raise UpgradeError("monitor report reuses frozen evaluation")
                if db.execute("""SELECT 1 FROM upgrade_events
                    WHERE candidate_id=? AND event='canary_observed' AND evidence_sha256=? LIMIT 1""",
                    (candidate_id, result["candidate_dataset_sha256"])).fetchone():
                    raise UpgradeError("monitor report was already observed")
            except UpgradeError:
                self._rollback_locked(db, candidate_id, "invalid_monitor_report")
                return {"candidate_id": candidate_id, "status": "rolled_back",
                        "reason": "invalid_monitor_report"}
            if permission_errors or quality_errors or result["regressions"]:
                reason = ("permission_error" if permission_errors else
                          "quality_error" if quality_errors else "metric_regression")
                self._rollback_locked(db, candidate_id, reason,
                                      result["candidate_dataset_sha256"])
                return {"candidate_id": candidate_id, "status": "rolled_back",
                        "reason": reason, **result}
            self._event(db, candidate_id, "canary_observed",
                        result["candidate_dataset_sha256"])
            return {"candidate_id": candidate_id, "status": "canary", **result}

    def rollback(self, candidate_id: str, reference_id: str) -> dict:
        candidate_id = _identifier(candidate_id, "candidate id", _ID)
        reference_id = _identifier(reference_id, "rollback reference", _REFERENCE)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._locked(db, candidate_id)
            if row["status"] not in {"approved", "canary"}:
                raise UpgradeError("candidate cannot be rolled back from this state")
            self._rollback_locked(db, candidate_id, "manual", reference=reference_id)
        return self.status(candidate_id)

    def incident(self, candidate_id: str, kind: str, reference_id: str) -> dict:
        """Revert immediately when permission or quality errors are observed."""
        candidate_id = _identifier(candidate_id, "candidate id", _ID)
        reference_id = _identifier(reference_id, "incident reference", _REFERENCE)
        if kind not in {"permission_error", "quality_error"}:
            raise UpgradeError("unsupported incident kind")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._locked(db, candidate_id)
            if row["status"] != "canary":
                raise UpgradeError("candidate is not in canary")
            self._rollback_locked(db, candidate_id, kind, reference=reference_id)
        return self.status(candidate_id)

    def assignment(self, candidate_id: str, case_id: str) -> str:
        """Return a stable route without reading or persisting the case."""
        candidate_id = _identifier(candidate_id, "candidate id", _ID)
        case_id = _identifier(case_id, "anonymous case id", _CASE)
        with self._db() as db:
            row = self._locked(db, candidate_id)
        if row["status"] != "canary":
            return row["baseline_version"]
        bucket = int.from_bytes(hashlib.sha256(
            (candidate_id + "\0" + case_id).encode("ascii")).digest()[:8], "big") % 10000
        return (row["candidate_version"] if bucket < row["canary_percent"] * 100
                else row["baseline_version"])

    def status(self, candidate_id: str) -> dict:
        candidate_id = _identifier(candidate_id, "candidate id", _ID)
        with self._db() as db:
            row = self._locked(db, candidate_id)
            events = db.execute("""SELECT event,evidence_sha256,reference_id,created_at_utc
                FROM upgrade_events WHERE candidate_id=? ORDER BY id""", (candidate_id,)).fetchall()
        return {**dict(row), "events": [dict(event) for event in events]}
