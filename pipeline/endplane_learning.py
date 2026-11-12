"""Anonymous, append-audited endplane proposal and feedback ledger.

This store is deliberately separate from ``endplane_history``.  A proposal is
only a candidate; feedback never writes a case plan or marks a CFD result as
valid.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import APP_DIR


DATABASE_NAME = "endplane-learning.sqlite3"
DIRECTORY_ENV = "WINDCATCHER_ENDPLANE_LEARNING_DIR"
FALLBACK_DIRECTORY_ENV = "WINDCATCHER_LEARNING_DIR"
SCHEMA_VERSION = 1

_CASE_ID = re.compile(
    r"(?:case-\d{8}-\d{6}-[0-9a-f]{4}|synthetic(?:-[A-Za-z0-9_-]{1,64})?)\Z",
    re.ASCII,
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,99}\Z", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_SCOPES = frozenset({"main_airway", "full_candidate"})
_FORMATS = frozenset({"oblique_planes_v1", "oblique_local_inlet_v2", "oblique_bilateral_local_v3"})
_VIEWS = frozenset({"axial", "coronal", "sagittal"})
_PLAN_ENUMS = {"format": _FORMATS, "scope": _SCOPES, "view": _VIEWS}
_PLAN_INTS = frozenset({
    "axis", "anterior_sign", "anterior_index", "posterior_index", "mask_version",
    "source_voxels", "retained_voxels", "retained_components",
    "anterior_deleted_voxels", "anterior_candidate_components",
    "anterior_opening_components", "anterior_terminal_components",
    "anterior_unselected_positive_voxels", "anterior_unselected_deleted_voxels",
    "unselected_region_changed_voxels", "retained_surface_faces",
    "selected_delete_surface_fragments", "inlet_loops", "outlet_loops",
    "inlet_cap_faces", "outlet_cap_faces",
    "left_deleted_voxels", "right_deleted_voxels", "posterior_deleted_voxels",
    "left_inlet_loops", "right_inlet_loops", "left_inlet_cap_faces", "right_inlet_cap_faces",
})
_PLAN_REALS = frozenset({
    "anterior_area_mm2", "posterior_area_mm2", "retained_volume_ml",
    "anterior_area_estimate_mm2", "posterior_area_estimate_mm2",
    "inlet_area_mm2", "outlet_area_mm2",
    "left_inlet_area_estimate_mm2", "right_inlet_area_estimate_mm2",
    "left_inlet_area_mm2", "right_inlet_area_mm2",
})
_PLAN_TOP_LEVEL = frozenset(_PLAN_ENUMS) | _PLAN_INTS | _PLAN_REALS | frozenset({
    "anterior_plane", "posterior_plane", "anterior_seed_lps_mm",
    "left_inlet_plane", "right_inlet_plane", "left_inlet_seed_lps_mm",
    "right_inlet_seed_lps_mm", "posterior_seed_lps_mm", "mesh_preflight", "complete",
})
_MESH_INTS = frozenset({
    "retained_surface_faces", "selected_delete_surface_fragments", "anterior_deleted_voxels",
    "inlet_loops", "outlet_loops", "inlet_cap_faces", "outlet_cap_faces",
    "left_inlet_loops", "right_inlet_loops", "left_inlet_cap_faces", "right_inlet_cap_faces",
})
_MESH_REALS = frozenset({"inlet_area_mm2", "outlet_area_mm2", "left_inlet_area_mm2", "right_inlet_area_mm2"})
_MESH_FIELDS = frozenset({"format", "closed_caps"}) | _MESH_INTS | _MESH_REALS
_FEEDBACK_FIELDS = frozenset({"reason_code", "adjusted_plan", "adjusted_plan_hash"})
_FEEDBACK_STATUSES = frozenset({"accepted", "adjusted", "rejected"})


class LearningError(ValueError):
    """Input, access, or state transition was rejected."""


def configured_directory(directory: Path | str | None = None) -> Path:
    if directory is not None:
        return Path(directory)
    return Path(os.environ.get(DIRECTORY_ENV)
                or os.environ.get(FALLBACK_DIRECTORY_ENV)
                or APP_DIR / ".local-learning")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validate_case_id(value: object) -> str:
    if not isinstance(value, str) or not _CASE_ID.fullmatch(value):
        raise LearningError("invalid anonymous case id")
    return value


def _validate_scope(value: object) -> str:
    if not isinstance(value, str) or value not in _SCOPES:
        raise LearningError("invalid endplane scope")
    return value


def _validate_mask_version(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 2**31 - 1:
        raise LearningError("invalid mask version")
    return value


def _validate_identifier(value: object, label: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise LearningError(f"invalid {label}")
    return value


def _number(value: object, *, integer: bool) -> int | float:
    if integer:
        if type(value) is not int or abs(value) > 1_000_000_000_000:
            raise LearningError("invalid structured plan integer")
        return value
    if type(value) not in (int, float):
        raise LearningError("invalid structured plan number")
    if not (-1_000_000_000_000 <= value <= 1_000_000_000_000):
        raise LearningError("invalid structured plan number")
    return value


def _vector(value: object) -> list[int | float]:
    if type(value) is not list or len(value) != 3:
        raise LearningError("invalid structured plan vector")
    return [_number(item, integer=False) for item in value]


def _plane(value: object) -> dict:
    if type(value) is not dict or set(value) != {"point_lps_mm", "normal_lps"}:
        raise LearningError("invalid structured plan plane")
    result = {key: _vector(value[key]) for key in ("point_lps_mm", "normal_lps")}
    if not any(result["normal_lps"]):
        raise LearningError("structured plan plane normal must be nonzero")
    return result


def _mesh(value: object) -> dict:
    if type(value) is not dict or set(value) - _MESH_FIELDS:
        raise LearningError("invalid structured plan mesh fields")
    if type(value.get("format")) is not str or value["format"] not in {
        "oblique_mesh_clip_v1", "oblique_mesh_clip_local_v2", "oblique_mesh_clip_bilateral_local_v3"
    }:
        raise LearningError("invalid structured plan mesh format")
    result = {"format": value["format"]}
    for key in _MESH_INTS:
        if key in value:
            result[key] = _number(value[key], integer=True)
    for key in _MESH_REALS:
        if key in value:
            result[key] = _number(value[key], integer=False)
    if "closed_caps" in value:
        if type(value["closed_caps"]) is not bool:
            raise LearningError("invalid structured plan cap flag")
        result["closed_caps"] = value["closed_caps"]
    return result


def validate_plan(plan: object, *, scope: str | None = None, mask_version: int | None = None) -> dict:
    """Validate a complete candidate, including envelope and format consistency."""
    if type(plan) is not dict or not plan or set(plan) - _PLAN_TOP_LEVEL:
        raise LearningError("plan contains unknown or unsupported fields")
    if not {"format", "scope", "mask_version", "posterior_plane"} <= set(plan):
        raise LearningError("candidate plan requires scope, mask version and complete planes")
    _validate_scope(plan["scope"])
    _validate_mask_version(plan["mask_version"])
    if {"axis", "anterior_sign", "anterior_index", "posterior_index"} & set(plan):
        raise LearningError("candidate plane formats cannot include legacy indices")
    if plan["format"] == "oblique_bilateral_local_v3":
        required = {"left_inlet_plane", "right_inlet_plane", "left_inlet_seed_lps_mm",
                    "right_inlet_seed_lps_mm", "posterior_seed_lps_mm"}
        if plan["scope"] != "main_airway" or not required <= set(plan):
            raise LearningError("bilateral candidate requires three planes and seeds in main_airway")
        if {"anterior_plane", "anterior_seed_lps_mm"} & set(plan):
            raise LearningError("bilateral candidate cannot include legacy anterior fields")
    elif plan["format"] == "oblique_local_inlet_v2":
        if "anterior_plane" not in plan:
            raise LearningError("local inlet candidate requires an anterior plane")
        if plan["scope"] != "main_airway" or "anterior_seed_lps_mm" not in plan:
            raise LearningError("local inlet candidate requires main_airway and seed")
    elif "anterior_plane" not in plan or "anterior_seed_lps_mm" in plan:
        raise LearningError("ordinary oblique planes require an anterior plane without a seed")
    if plan["format"] != "oblique_bilateral_local_v3" and ({
        "left_inlet_plane", "right_inlet_plane", "left_inlet_seed_lps_mm",
        "right_inlet_seed_lps_mm", "posterior_seed_lps_mm"} & set(plan)):
        raise LearningError("bilateral fields require bilateral format")
    result: dict = {}
    for key, allowed in _PLAN_ENUMS.items():
        if key in plan:
            if type(plan[key]) is not str or plan[key] not in allowed:
                raise LearningError(f"invalid structured plan {key}")
            result[key] = plan[key]
    for key in _PLAN_INTS:
        if key in plan:
            result[key] = _number(plan[key], integer=True)
    for key in _PLAN_REALS:
        if key in plan:
            result[key] = _number(plan[key], integer=False)
    for key in ("anterior_plane", "posterior_plane", "left_inlet_plane", "right_inlet_plane"):
        if key in plan:
            result[key] = _plane(plan[key])
    for key in ("anterior_seed_lps_mm", "left_inlet_seed_lps_mm",
                "right_inlet_seed_lps_mm", "posterior_seed_lps_mm"):
        if key in plan:
            result[key] = _vector(plan[key])
    if "mesh_preflight" in plan:
        result["mesh_preflight"] = _mesh(plan["mesh_preflight"])
        if plan["format"] == "oblique_bilateral_local_v3" and result["mesh_preflight"]["format"] != "oblique_mesh_clip_bilateral_local_v3":
            raise LearningError("bilateral mesh preflight format mismatch")
    if "complete" in plan:
        if type(plan["complete"]) is not bool:
            raise LearningError("invalid structured plan completeness")
        result["complete"] = plan["complete"]
    if scope is not None and result["scope"] != _validate_scope(scope):
        raise LearningError("plan scope does not match proposal scope")
    if mask_version is not None and result["mask_version"] != _validate_mask_version(mask_version):
        raise LearningError("plan mask version does not match proposal mask version")
    return result


def plan_hash(plan: object, *, scope: str | None = None, mask_version: int | None = None) -> str:
    encoded = json.dumps(validate_plan(plan, scope=scope, mask_version=mask_version),
                         sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _feedback_payload(value: object, status: str, *, scope: str, mask_version: int) -> dict:
    if value is None:
        value = {}
    if type(value) is not dict or set(value) - _FEEDBACK_FIELDS:
        raise LearningError("feedback must contain only structured fields")
    result = {}
    if "reason_code" in value:
        reason = value["reason_code"]
        if not isinstance(reason, str) or not _IDENTIFIER.fullmatch(reason):
            raise LearningError("feedback reason_code must be a bounded identifier")
        result["reason_code"] = reason
    if "adjusted_plan" in value:
        if status != "adjusted":
            raise LearningError("adjusted_plan is only valid for adjusted feedback")
        result["adjusted_plan"] = validate_plan(value["adjusted_plan"], scope=scope,
                                                  mask_version=mask_version)
    if "adjusted_plan_hash" in value:
        digest = value["adjusted_plan_hash"]
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise LearningError("invalid adjusted plan hash")
        result["adjusted_plan_hash"] = digest
    if status == "adjusted" and "adjusted_plan" not in result:
        raise LearningError("adjusted feedback requires adjusted_plan")
    if "adjusted_plan" in result:
        digest = plan_hash(result["adjusted_plan"], scope=scope, mask_version=mask_version)
        if "adjusted_plan_hash" in result and result["adjusted_plan_hash"] != digest:
            raise LearningError("adjusted plan hash does not match plan")
        result.setdefault("adjusted_plan_hash", digest)
    return result


class EndplaneLearningStore:
    """Independent SQLite store for anonymous proposals and feedback.

    Actor methods are private to the proposal actor.  Explicit ``*_case_*``
    methods bridge service-created proposals to human case review; their caller
    MUST first authorize view/review access to the exact case and obtain the
    reviewer ID from its trusted authentication context.  This store does not
    grant case permissions or read/write case files.
    """

    def __init__(self, directory: Path | str | None = None):
        self.directory = configured_directory(directory)
        self.path = self.directory / DATABASE_NAME
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError("endplane learning schema is newer than this application")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS proposals (
                    proposal_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    case_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    mask_version INTEGER NOT NULL CHECK (mask_version >= 0),
                    plan_json TEXT NOT NULL,
                    plan_hash TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_version TEXT NOT NULL,
                    actor_id TEXT,
                    status TEXT NOT NULL CHECK (status IN ('proposed', 'accepted', 'adjusted', 'rejected')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS feedback (
                    feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
                    actor_id TEXT,
                    status TEXT NOT NULL CHECK (status IN ('accepted', 'adjusted', 'rejected')),
                    previous_status TEXT NOT NULL,
                    feedback_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS proposals_actor ON proposals(actor_id, created_at)")
            connection.execute("CREATE INDEX IF NOT EXISTS proposals_case ON proposals(case_id, scope, created_at)")
            connection.execute("CREATE INDEX IF NOT EXISTS feedback_proposal ON feedback(proposal_id, feedback_id)")
            connection.execute("""
                CREATE TRIGGER IF NOT EXISTS feedback_append_only_update
                BEFORE UPDATE ON feedback BEGIN
                    SELECT RAISE(ABORT, 'feedback rows are append-only');
                END
            """)
            connection.execute("""
                CREATE TRIGGER IF NOT EXISTS feedback_append_only_delete
                BEFORE DELETE ON feedback BEGIN
                    SELECT RAISE(ABORT, 'feedback rows are append-only');
                END
            """)
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def _db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("PRAGMA foreign_keys=ON")
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _decode_proposal(row: sqlite3.Row) -> dict:
        try:
            plan = json.loads(row["plan_json"])
            canonical = validate_plan(plan, scope=row["scope"], mask_version=row["mask_version"])
            encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=True, allow_nan=False)
            if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != row["plan_hash"]:
                raise LearningError("stored proposal hash does not match plan")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("stored endplane proposal is invalid") from exc
        return {
            "proposal_id": row["proposal_id"],
            "case_id": row["case_id"],
            "scope": row["scope"],
            "mask_version": row["mask_version"],
            "plan": plan,
            "plan_hash": row["plan_hash"],
            "source": row["source"],
            "source_version": row["source_version"],
            "version": row["source_version"],
            "actor_id": row["actor_id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "schema_version": SCHEMA_VERSION,
        }

    @staticmethod
    def _decode_feedback(row: sqlite3.Row) -> dict:
        try:
            feedback = json.loads(row["feedback_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("stored endplane feedback is invalid") from exc
        return {
            "feedback_id": row["feedback_id"],
            "proposal_id": row["proposal_id"],
            "actor_id": row["actor_id"],
            "status": row["status"],
            "previous_status": row["previous_status"],
            "feedback": feedback,
            "created_at": row["created_at"],
            "schema_version": SCHEMA_VERSION,
        }

    def create_proposal(
        self,
        case_id: str,
        scope: str,
        mask_version: int,
        plan: dict,
        source: str,
        version: str | None = None,
        *,
        source_version: str | None = None,
        actor_id: str | None = None,
    ) -> dict:
        case_id = _validate_case_id(case_id)
        scope = _validate_scope(scope)
        mask_version = _validate_mask_version(mask_version)
        canonical = validate_plan(plan, scope=scope, mask_version=mask_version)
        source = _validate_identifier(source, "proposal source")
        if version is not None and source_version is not None and version != source_version:
            raise LearningError("version and source_version disagree")
        source_version = _validate_identifier(source_version if source_version is not None else version,
                                              "proposal source version")
        if source_version is None:
            raise LearningError("proposal source version is required")
        actor_id = _validate_identifier(actor_id, "actor id", allow_none=True)
        encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=False)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        identity = json.dumps([case_id, scope, mask_version, digest, source, source_version, actor_id],
                              separators=(",", ":"), ensure_ascii=True)
        idempotency_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        now = _now()
        with self._db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM proposals WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                return self._decode_proposal(existing)
            proposal_id = "proposal-" + secrets.token_hex(16)
            connection.execute("""
                INSERT INTO proposals (
                    proposal_id,idempotency_key,case_id,scope,mask_version,plan_json,plan_hash,
                    source,source_version,actor_id,status,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,'proposed',?,?)
            """, (proposal_id, idempotency_key, case_id, scope, mask_version, encoded, digest,
                   source, source_version, actor_id, now, now))
            return self._decode_proposal(connection.execute(
                "SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone())

    def create_proposals(
        self,
        case_id: str,
        scope: str,
        mask_version: int,
        candidates: list[tuple[str, dict]],
        *,
        source_version: str,
        actor_id: str | None = None,
    ) -> list[dict]:
        """Register one or two candidates together, or leave the ledger unchanged."""
        case_id = _validate_case_id(case_id)
        scope = _validate_scope(scope)
        mask_version = _validate_mask_version(mask_version)
        source_version = _validate_identifier(source_version, "proposal source version")
        actor_id = _validate_identifier(actor_id, "actor id", allow_none=True)
        if type(candidates) is not list or not 1 <= len(candidates) <= 2:
            raise LearningError("invalid proposal batch size")
        prepared = []
        for source, plan in candidates:
            source = _validate_identifier(source, "proposal source")
            canonical = validate_plan(plan, scope=scope, mask_version=mask_version)
            encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=True, allow_nan=False)
            digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            identity = json.dumps([case_id, scope, mask_version, digest, source,
                                   source_version, actor_id], separators=(",", ":"), ensure_ascii=True)
            key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            prepared.append((source, encoded, digest, key))
        if len({item[3] for item in prepared}) != len(prepared):
            raise LearningError("duplicate proposal in batch")
        now = _now()
        with self._db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            proposals = []
            for source, encoded, digest, key in prepared:
                existing = connection.execute(
                    "SELECT * FROM proposals WHERE idempotency_key=?", (key,)
                ).fetchone()
                if existing is None:
                    proposal_id = "proposal-" + secrets.token_hex(16)
                    connection.execute("""
                        INSERT INTO proposals (
                            proposal_id,idempotency_key,case_id,scope,mask_version,plan_json,plan_hash,
                            source,source_version,actor_id,status,created_at,updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,'proposed',?,?)
                    """, (proposal_id, key, case_id, scope, mask_version, encoded, digest,
                           source, source_version, actor_id, now, now))
                    existing = connection.execute(
                        "SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)
                    ).fetchone()
                proposals.append(self._decode_proposal(existing))
            return proposals

    def get_proposal(self, proposal_id: str, *, actor_id: str | None = None) -> dict | None:
        proposal_id = _validate_identifier(proposal_id, "proposal id")
        actor_id = _validate_identifier(actor_id, "actor id", allow_none=True)
        with self._db() as connection:
            row = connection.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        if row is None or row["actor_id"] != actor_id:
            return None
        return self._decode_proposal(row)

    def list_proposals(self, *, actor_id: str | None = None, case_id: str | None = None,
                       scope: str | None = None) -> list[dict]:
        actor_id = _validate_identifier(actor_id, "actor id", allow_none=True)
        if case_id is not None:
            case_id = _validate_case_id(case_id)
        if scope is not None:
            scope = _validate_scope(scope)
        query = "SELECT * FROM proposals WHERE actor_id IS ?"
        params: list[object] = [actor_id]
        if case_id is not None:
            query += " AND case_id=?"
            params.append(case_id)
        if scope is not None:
            query += " AND scope=?"
            params.append(scope)
        query += " ORDER BY created_at, proposal_id"
        with self._db() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._decode_proposal(row) for row in rows]

    def record_feedback(self, proposal_id: str, status: str, *, actor_id: str | None = None,
                        feedback: dict | None = None) -> dict:
        proposal_id = _validate_identifier(proposal_id, "proposal id")
        if not isinstance(status, str) or status not in _FEEDBACK_STATUSES:
            raise LearningError("feedback status must be accepted, adjusted, or rejected")
        actor_id = _validate_identifier(actor_id, "actor id", allow_none=True)
        now = _now()
        with self._db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            if row is None:
                raise LearningError("proposal not found")
            if actor_id != row["actor_id"]:
                raise LearningError("actor is not allowed to modify this proposal")
            return self._record_feedback_locked(connection, row, status, actor_id, feedback, now)

    def _record_feedback_locked(self, connection: sqlite3.Connection, row: sqlite3.Row,
                                status: str, reviewer_id: str | None,
                                feedback: dict | None, now: str) -> dict:
        # Validate the complete stored plan and its hash before any transition;
        # the enclosing BEGIN IMMEDIATE keeps validation and audit atomic.
        self._decode_proposal(row)
        if row["status"] != "proposed":
            raise LearningError("proposal is already in a terminal feedback state")
        payload = _feedback_payload(feedback, status, scope=row["scope"],
                                    mask_version=row["mask_version"])
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=False)
        connection.execute("""
            INSERT INTO feedback
                (proposal_id,actor_id,status,previous_status,feedback_json,created_at)
            VALUES (?,?,?,?,?,?)
        """, (row["proposal_id"], reviewer_id, status, row["status"], encoded, now))
        connection.execute("UPDATE proposals SET status=?,updated_at=? WHERE proposal_id=?",
                           (status, now, row["proposal_id"]))
        updated = connection.execute("SELECT * FROM proposals WHERE proposal_id=?",
                                     (row["proposal_id"],)).fetchone()
        return {**self._decode_proposal(updated), "previous_status": row["status"]}

    def get_case_proposal(self, case_id: str, proposal_id: str) -> dict | None:
        """Read across actors only after the caller authorizes this exact case."""
        case_id = _validate_case_id(case_id)
        proposal_id = _validate_identifier(proposal_id, "proposal id")
        with self._db() as connection:
            row = connection.execute(
                "SELECT * FROM proposals WHERE case_id=? AND proposal_id=?",
                (case_id, proposal_id),
            ).fetchone()
        return self._decode_proposal(row) if row is not None else None

    def list_case_proposals(self, case_id: str, scope: str | None = None) -> list[dict]:
        """List one authorized case; this method performs no authorization itself."""
        case_id = _validate_case_id(case_id)
        query = "SELECT * FROM proposals WHERE case_id=?"
        params: list[object] = [case_id]
        if scope is not None:
            query += " AND scope=?"
            params.append(_validate_scope(scope))
        query += " ORDER BY created_at, proposal_id"
        with self._db() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._decode_proposal(row) for row in rows]

    def record_case_feedback(self, case_id: str, proposal_id: str, status: str, *,
                             reviewer_id: str, feedback: dict | None = None) -> dict:
        """Review after caller checks case review permission and trusted reviewer.

        ``reviewer_id`` is the authenticated human, never a request-supplied
        proposal actor.  It is recorded on the audit row even when the proposal
        was created by a separate service identity.
        """
        case_id = _validate_case_id(case_id)
        proposal_id = _validate_identifier(proposal_id, "proposal id")
        reviewer_id = _validate_identifier(reviewer_id, "reviewer id")
        if type(status) is not str or status not in _FEEDBACK_STATUSES:
            raise LearningError("feedback status must be accepted, adjusted, or rejected")
        with self._db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM proposals WHERE case_id=? AND proposal_id=?",
                (case_id, proposal_id),
            ).fetchone()
            if row is None:
                raise LearningError("proposal not found")
            return self._record_feedback_locked(connection, row, status, reviewer_id, feedback, _now())

    def list_case_feedback(self, case_id: str, proposal_id: str) -> list[dict]:
        """Read feedback only after caller authorizes view access to this case."""
        case_id = _validate_case_id(case_id)
        proposal_id = _validate_identifier(proposal_id, "proposal id")
        with self._db() as connection:
            rows = connection.execute("""
                SELECT feedback.* FROM feedback JOIN proposals USING (proposal_id)
                WHERE proposals.case_id=? AND proposals.proposal_id=?
                ORDER BY feedback.feedback_id
            """, (case_id, proposal_id)).fetchall()
        return [self._decode_feedback(row) for row in rows]

    def list_feedback(self, proposal_id: str, *, actor_id: str | None = None) -> list[dict]:
        proposal_id = _validate_identifier(proposal_id, "proposal id")
        actor_id = _validate_identifier(actor_id, "actor id", allow_none=True)
        with self._db() as connection:
            proposal = connection.execute("SELECT actor_id FROM proposals WHERE proposal_id=?",
                                          (proposal_id,)).fetchone()
            if proposal is None or proposal["actor_id"] != actor_id:
                return []
            rows = connection.execute(
                "SELECT * FROM feedback WHERE proposal_id=? ORDER BY feedback_id", (proposal_id,)
            ).fetchall()
        return [self._decode_feedback(row) for row in rows]


_DEFAULT_STORE: EndplaneLearningStore | None = None
_DEFAULT_STORE_PATH: Path | None = None


def _default_store() -> EndplaneLearningStore:
    global _DEFAULT_STORE, _DEFAULT_STORE_PATH
    path = configured_directory() / DATABASE_NAME
    if _DEFAULT_STORE is None or _DEFAULT_STORE_PATH != path:
        _DEFAULT_STORE = EndplaneLearningStore()
        _DEFAULT_STORE_PATH = path
    return _DEFAULT_STORE


def create_proposal(*args, **kwargs) -> dict:
    return _default_store().create_proposal(*args, **kwargs)


def create_proposals(*args, **kwargs) -> list[dict]:
    return _default_store().create_proposals(*args, **kwargs)


def get_proposal(*args, **kwargs) -> dict | None:
    return _default_store().get_proposal(*args, **kwargs)


def list_proposals(*args, **kwargs) -> list[dict]:
    return _default_store().list_proposals(*args, **kwargs)


def record_feedback(*args, **kwargs) -> dict:
    return _default_store().record_feedback(*args, **kwargs)


def list_feedback(*args, **kwargs) -> list[dict]:
    return _default_store().list_feedback(*args, **kwargs)


def get_case_proposal(case_id: str, proposal_id: str) -> dict | None:
    """Caller must authorize view access to the exact case before calling."""
    return _default_store().get_case_proposal(case_id, proposal_id)


def list_case_proposals(case_id: str, scope: str | None = None) -> list[dict]:
    """Caller must authorize view access to the exact case before calling."""
    return _default_store().list_case_proposals(case_id, scope)


def record_case_feedback(case_id: str, proposal_id: str, status: str, *,
                         reviewer_id: str, feedback: dict | None = None) -> dict:
    """Caller must authorize case review and supply its authenticated reviewer."""
    return _default_store().record_case_feedback(
        case_id, proposal_id, status, reviewer_id=reviewer_id, feedback=feedback)


def list_case_feedback(case_id: str, proposal_id: str) -> list[dict]:
    """Caller must authorize view access to the exact case before calling."""
    return _default_store().list_case_feedback(case_id, proposal_id)
