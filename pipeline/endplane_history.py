"""Versioned saved-endplane evidence, separate from activity telemetry.

Committed means the plan was saved. It does not mean clinical review, training
consent, or a valid CFD result. It stores a pseudonymous operator ID when the
gateway authenticates the save, but no CT, mask, name, credential, path, or note.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from .config import APP_DIR


DIRECTORY_ENV = "WINDCATCHER_LEARNING_DIR"
DATABASE_NAME = "learning.sqlite3"
SCHEMA_VERSION = 1
_CASE_ID = re.compile(r"(?:case-\d{8}-\d{6}-[0-9a-f]{4}|synthetic(?:-[A-Za-z0-9_-]{1,64})?)\Z", re.ASCII)
_IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,99}\Z", re.ASCII)
_SCOPES = {"main_airway", "full_candidate"}
_ENUMS = {
    "format": {"oblique_planes_v1", "oblique_local_inlet_v2", "oblique_bilateral_local_v3"},
    "scope": _SCOPES,
    "view": {"axial", "coronal", "sagittal"},
}
_INTS = {
    "axis", "anterior_sign", "anterior_index", "posterior_index", "mask_version",
    "source_voxels", "retained_voxels", "retained_components",
    "anterior_deleted_voxels", "anterior_candidate_components",
    "anterior_opening_components", "anterior_terminal_components",
    "anterior_unselected_positive_voxels", "anterior_unselected_deleted_voxels",
    "unselected_region_changed_voxels",
    "left_deleted_voxels", "right_deleted_voxels", "posterior_deleted_voxels",
}
_REALS = {
    "anterior_area_mm2", "posterior_area_mm2", "retained_volume_ml",
    "anterior_area_estimate_mm2", "posterior_area_estimate_mm2",
    "left_inlet_area_estimate_mm2", "right_inlet_area_estimate_mm2",
}
_MESH_INTS = {
    "retained_surface_faces", "selected_delete_surface_fragments",
    "anterior_deleted_voxels", "inlet_loops", "outlet_loops",
    "inlet_cap_faces", "outlet_cap_faces",
    "left_inlet_loops", "right_inlet_loops", "left_inlet_cap_faces",
    "right_inlet_cap_faces",
}
_MESH_REALS = {
    "inlet_area_mm2", "outlet_area_mm2",
    "left_inlet_area_mm2", "right_inlet_area_mm2",
}

# Only the authenticated gateway sets this request context. Local/offline saves
# have no individual account and deliberately keep actor_id absent.
CURRENT_ACTOR_ID: ContextVar[str | None] = ContextVar("windcatcher_endplane_actor_id", default=None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _path() -> Path:
    directory = os.environ.get(DIRECTORY_ENV)
    return (Path(directory) if directory else APP_DIR / ".local-learning") / DATABASE_NAME


def _migrate(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS endplane_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id TEXT NOT NULL,
            scope TEXT NOT NULL,
            mask_version INTEGER NOT NULL,
            previous_plan_json TEXT,
            confirmed_plan_json TEXT NOT NULL,
            plan_hash TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'interactive_save',
            status TEXT NOT NULL CHECK (status IN ('pending', 'committed', 'aborted')),
            actor_id TEXT,
            recorded_at TEXT NOT NULL,
            committed_at TEXT
        )
    """)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(endplane_revisions)")}
    for name, definition in {
        "source": "TEXT NOT NULL DEFAULT 'interactive_save'",
        "actor_id": "TEXT",
        "committed_at": "TEXT",
    }.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE endplane_revisions ADD COLUMN {name} {definition}")
    connection.execute("CREATE INDEX IF NOT EXISTS endplane_revisions_case ON endplane_revisions(case_id, scope, id)")
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


@contextmanager
def _db():
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout=10000")
        # Multiple workers may initialize a fresh ledger simultaneously.
        # Switching journal mode needs an exclusive lock and can fail before
        # busy_timeout has a chance to wait, so retry only that transition.
        deadline = time.monotonic() + 10
        while True:
            try:
                mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
                if mode.lower() != "wal":
                    mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if mode.lower() == "wal":
                    break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                if time.monotonic() >= deadline:
                    raise
            if time.monotonic() >= deadline:
                raise RuntimeError("endplane ledger WAL initialization timed out")
            time.sleep(0.05)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            _migrate(connection)
        yield connection
    finally:
        connection.close()


def _validate(case_id: str, scope: str, mask_version: int | None = None) -> None:
    if not isinstance(case_id, str) or not _CASE_ID.fullmatch(case_id):
        raise ValueError("invalid anonymous case id")
    if scope not in _SCOPES:
        raise ValueError("invalid endplane scope")
    if mask_version is not None and (type(mask_version) is not int or mask_version < 0):
        raise ValueError("invalid mask version")


def _number(value, *, integer: bool):
    if integer:
        if type(value) is not int or abs(value) > 1_000_000_000_000:
            raise ValueError("invalid endplane integer")
    elif type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1_000_000_000_000:
        raise ValueError("invalid endplane number")
    return value


def _vector(value):
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("invalid endplane vector")
    return [_number(part, integer=False) for part in value]


def _plane(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid endplane geometry")
    if not all(key in value for key in ("point_lps_mm", "normal_lps")):
        raise ValueError("incomplete endplane geometry")
    # A plane has exactly two evidence vectors. Unknown nested fields, including
    # names and file paths, are never retained in the learning store.
    return {key: _vector(value[key]) for key in ("point_lps_mm", "normal_lps")}


def _mesh(value):
    if not isinstance(value, dict) or type(value.get("format")) is not str or value["format"] not in {
        "oblique_mesh_clip_v1", "oblique_mesh_clip_local_v2",
        "oblique_mesh_clip_bilateral_local_v3",
    }:
        raise ValueError("invalid endplane mesh preflight")
    result = {"format": value["format"]}
    for key in _MESH_INTS:
        if key in value:
            result[key] = _number(value[key], integer=True)
    for key in _MESH_REALS:
        if key in value:
            result[key] = _number(value[key], integer=False)
    if "closed_caps" in value:
        if type(value["closed_caps"]) is not bool:
            raise ValueError("invalid endplane mesh cap flag")
        result["closed_caps"] = value["closed_caps"]
    return result


def _project_plan(value: dict) -> dict:
    """Keep only known geometric/numeric fields from the current three formats.

    Unknown fields in older saved plans are omitted, not copied or interpreted.
    The live endplanes.json remains authoritative for the full legacy plan.
    """
    result = {}
    for key, allowed in _ENUMS.items():
        if key in value:
            if type(value[key]) is not str or value[key] not in allowed:
                raise ValueError(f"invalid endplane {key}")
            result[key] = value[key]
    for key in _INTS:
        if key in value:
            result[key] = _number(value[key], integer=True)
    for key in _REALS:
        if key in value:
            result[key] = _number(value[key], integer=False)
    for key in ("anterior_plane", "posterior_plane", "left_inlet_plane", "right_inlet_plane"):
        if key in value:
            result[key] = _plane(value[key])
    for key in ("anterior_seed_lps_mm", "left_inlet_seed_lps_mm",
                "right_inlet_seed_lps_mm", "posterior_seed_lps_mm"):
        if key in value:
            result[key] = _vector(value[key])
    if "mesh_preflight" in value:
        result["mesh_preflight"] = _mesh(value["mesh_preflight"])
    if "complete" in value:
        if type(value["complete"]) is not bool:
            raise ValueError("invalid endplane completeness flag")
        result["complete"] = value["complete"]
    return result


def _plan_json(value: dict | None, *, optional: bool) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, dict):
        raise ValueError("endplane plan must be an object")
    # Partial saves may carry localized validation_error. The explicit
    # projection also omits all other unknown and nested free-text fields.
    evidence = _project_plan(value)
    encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    if len(encoded) > 65536:
        raise ValueError("endplane plan is too large")
    return encoded


def plan_hash(plan: dict) -> str:
    """Stable SHA-256 independent of dict key order."""
    return hashlib.sha256(_plan_json(plan, optional=False).encode("utf-8")).hexdigest()


def begin_revision(
    case_id: str, scope: str, mask_version: int,
    previous_plan: dict | None, confirmed_plan: dict, *,
    source: str = "interactive_save", actor_id: str | None = None,
) -> int:
    """Stage a revision; pending rows are never returned as saved evidence."""
    _validate(case_id, scope, mask_version)
    if not isinstance(source, str) or not _IDENTIFIER.fullmatch(source):
        raise ValueError("invalid source")
    if actor_id is not None and (not isinstance(actor_id, str) or not _IDENTIFIER.fullmatch(actor_id)):
        raise ValueError("invalid actor id")
    prior = _plan_json(previous_plan, optional=True)
    current = _plan_json(confirmed_plan, optional=False)
    evidence = json.loads(current)
    if evidence.get("scope") != scope or evidence.get("mask_version") != mask_version:
        raise ValueError("endplane plan scope or mask version does not match revision")
    digest = hashlib.sha256(current.encode("utf-8")).hexdigest()
    with _db() as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("""
            SELECT id FROM endplane_revisions
            WHERE case_id=? AND scope=? AND mask_version=? AND status='pending'
              AND previous_plan_json IS ? AND confirmed_plan_json=? AND actor_id IS ?
            ORDER BY id DESC LIMIT 1
        """, (case_id, scope, mask_version, prior, current, actor_id)).fetchone()
        if existing:
            return int(existing["id"])
        cursor = connection.execute("""
            INSERT INTO endplane_revisions
                (case_id, scope, mask_version, previous_plan_json,
                 confirmed_plan_json, plan_hash, source, status, actor_id, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """, (case_id, scope, mask_version, prior, current, digest, source, actor_id, _now()))
        return int(cursor.lastrowid)


def _change_status(revision_id: int, target: str) -> None:
    if type(revision_id) is not int or revision_id < 1:
        raise ValueError("invalid revision id")
    with _db() as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT status FROM endplane_revisions WHERE id=?", (revision_id,)).fetchone()
        if row is None:
            raise ValueError("endplane revision not found")
        if row["status"] == target:
            return
        if row["status"] != "pending":
            raise ValueError("endplane revision status cannot be changed")
        connection.execute("UPDATE endplane_revisions SET status=?, committed_at=? WHERE id=?",
                           (target, _now() if target == "committed" else None, revision_id))


def commit_revision(revision_id: int) -> None:
    """Confirm a saved plan, not its clinical quality or training eligibility."""
    _change_status(revision_id, "committed")


def abort_revision(revision_id: int) -> None:
    """Keep failed attempts auditable without labeling them as saved plans."""
    _change_status(revision_id, "aborted")


def reconcile_pending(case_id: str, scope: str, current_plan: dict | None) -> dict:
    """Resolve interrupted writes against the actual saved plan.

    Only the newest pending revision can be committed, and only if its entire
    canonical plan matches the current saved plan. All older pending attempts
    are marked aborted. A missing or different current plan commits nothing.
    """
    _validate(case_id, scope)
    current = _plan_json(current_plan, optional=True)
    digest = hashlib.sha256(current.encode("utf-8")).hexdigest() if current is not None else None
    with _db() as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        pending = connection.execute("""
            SELECT id, confirmed_plan_json, plan_hash FROM endplane_revisions
            WHERE case_id=? AND scope=? AND status='pending' ORDER BY id
        """, (case_id, scope)).fetchall()
        latest = pending[-1] if pending else None
        committed = (int(latest["id"]) if latest is not None and current is not None
                     and latest["confirmed_plan_json"] == current and latest["plan_hash"] == digest
                     else None)
        aborted = []
        for row in pending:
            revision_id = int(row["id"])
            if revision_id == committed:
                connection.execute("""
                    UPDATE endplane_revisions SET status='committed', committed_at=? WHERE id=?
                """, (_now(), revision_id))
            else:
                connection.execute("UPDATE endplane_revisions SET status='aborted' WHERE id=?",
                                   (revision_id,))
                aborted.append(revision_id)
    return {"committed": committed, "aborted": aborted}


def list_revisions(case_id: str, scope: str | None = None, *, include_pending: bool = False) -> list[dict]:
    """Return saved evidence only by default, never a training data set."""
    if scope is None:
        if not isinstance(case_id, str) or not _CASE_ID.fullmatch(case_id):
            raise ValueError("invalid anonymous case id")
    else:
        _validate(case_id, scope)
    query = "SELECT * FROM endplane_revisions WHERE case_id=?"
    params: list[object] = [case_id]
    if scope is not None:
        query += " AND scope=?"
        params.append(scope)
    if not include_pending:
        query += " AND status='committed'"
    query += " ORDER BY id"
    with _db() as connection:
        rows = connection.execute(query, params).fetchall()
    result = []
    for row in rows:
        data = dict(row)
        previous = data.pop("previous_plan_json")
        current = data.pop("confirmed_plan_json")
        data["previous_plan"] = json.loads(previous) if previous is not None else None
        data["confirmed_plan"] = json.loads(current)
        result.append(data)
    return result
