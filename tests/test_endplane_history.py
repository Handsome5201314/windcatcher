"""Endplane saves are versioned evidence, never automatic training labels."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from pipeline import endplane_history as history
from pipeline import endplanes


CASE_ID = "case-20261102-123456-abcd"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(history.DIRECTORY_ENV, str(tmp_path / "learning"))
    return tmp_path / "learning" / history.DATABASE_NAME


def plan(index):
    return {"scope": "main_airway", "mask_version": 3,
            "format": "oblique_planes_v1", "anterior_index": index,
            "posterior_index": 8}


def test_commit_is_saved_evidence_not_a_training_label(isolated):
    revision_id = history.begin_revision(CASE_ID, "main_airway", 3, None, plan(20))
    assert history.list_revisions(CASE_ID) == []
    pending = history.list_revisions(CASE_ID, include_pending=True)
    assert pending[0]["status"] == "pending"
    assert pending[0]["actor_id"] is None
    history.commit_revision(revision_id)
    history.commit_revision(revision_id)
    records = history.list_revisions(CASE_ID)
    assert len(records) == 1
    assert records[0]["status"] == "committed"
    assert records[0]["previous_plan"] is None
    assert records[0]["confirmed_plan"] == plan(20)
    assert records[0]["source"] == "interactive_save"
    assert records[0]["plan_hash"] == history.plan_hash(plan(20))
    assert "training_eligible" not in records[0]
    with sqlite3.connect(isolated) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_persisted_revision_uses_trusted_actor_context_only(tmp_path, monkeypatch):
    root = tmp_path / "case"
    (root / "segment").mkdir(parents=True)
    (root / "segment" / "segmentation.json").write_text(
        json.dumps({"mask_version": 3}), encoding="utf-8")
    monkeypatch.setattr(endplanes, "invalidate_downstream", lambda *_args: None)

    actor_id = "a" * 32
    token = history.CURRENT_ACTOR_ID.set(actor_id)
    try:
        endplanes._persist_plan(CASE_ID, root, "main_airway", plan(20), 3)
    finally:
        history.CURRENT_ACTOR_ID.reset(token)
    assert history.list_revisions(CASE_ID)[0]["actor_id"] == actor_id

    # Offline saves do not inherit a previous HTTP request's actor.
    endplanes._persist_plan(CASE_ID, root, "main_airway", plan(21), 3)
    assert [row["actor_id"] for row in history.list_revisions(CASE_ID)] == [actor_id, None]


def test_pending_retry_does_not_relabel_revision_to_another_actor():
    first = history.begin_revision(CASE_ID, "main_airway", 3, None, plan(20), actor_id="first")
    second = history.begin_revision(CASE_ID, "main_airway", 3, None, plan(20), actor_id="second")
    assert first != second
    assert [row["actor_id"] for row in history.list_revisions(CASE_ID, include_pending=True)] == [
        "first", "second"]


def test_prior_and_confirmed_plan_are_immutable_after_later_save():
    first = history.begin_revision(CASE_ID, "main_airway", 3, None, plan(20))
    history.commit_revision(first)
    second = history.begin_revision(CASE_ID, "main_airway", 3, plan(20), plan(21),
                                    source="user_confirmed_oblique_planes")
    history.commit_revision(second)
    records = history.list_revisions(CASE_ID, "main_airway")
    assert [row["id"] for row in records] == [first, second]
    assert records[0]["confirmed_plan"] == plan(20)
    assert records[1]["previous_plan"] == plan(20)
    assert records[1]["confirmed_plan"] == plan(21)
    assert records[1]["plan_hash"] != records[0]["plan_hash"]


def test_localized_partial_save_error_is_not_copied_to_learning_store(isolated):
    partial = {**plan(20), "validation_error": {
        "status": "blocked", "reason": "鼻孔端和鼻咽端尚未连通",
        "remediation": ["请检查病例后重试"]}}
    revision_id = history.begin_revision(CASE_ID, "main_airway", 3, None, partial)
    history.commit_revision(revision_id)
    row = history.list_revisions(CASE_ID)[0]
    assert row["confirmed_plan"] == plan(20)
    with sqlite3.connect(isolated) as connection:
        stored = connection.execute("SELECT confirmed_plan_json FROM endplane_revisions").fetchone()[0]
    assert "validation_error" not in stored
    assert "鼻孔" not in stored


def test_pending_retry_is_idempotent_even_with_key_order_change():
    reversed_plan = dict(reversed(list(plan(20).items())))
    first = history.begin_revision(CASE_ID, "main_airway", 3, None, plan(20))
    retried = history.begin_revision(CASE_ID, "main_airway", 3, None, reversed_plan)
    assert retried == first
    assert len(history.list_revisions(CASE_ID, include_pending=True)) == 1
    assert history.plan_hash(reversed_plan) == history.plan_hash(plan(20))


def test_failed_save_can_abort_but_cannot_later_commit():
    revision_id = history.begin_revision(CASE_ID, "main_airway", 3, None, plan(20))
    history.abort_revision(revision_id)
    history.abort_revision(revision_id)
    assert history.list_revisions(CASE_ID) == []
    assert history.list_revisions(CASE_ID, include_pending=True)[0]["status"] == "aborted"
    with pytest.raises(ValueError, match="cannot be changed"):
        history.commit_revision(revision_id)


def test_reconcile_commits_only_latest_pending_matching_saved_plan():
    older = history.begin_revision(CASE_ID, "main_airway", 3, None, plan(20))
    latest = history.begin_revision(CASE_ID, "main_airway", 3, plan(20), plan(21))
    outcome = history.reconcile_pending(CASE_ID, "main_airway", plan(21))
    assert outcome == {"committed": latest, "aborted": [older]}
    assert [row["id"] for row in history.list_revisions(CASE_ID)] == [latest]
    assert history.reconcile_pending(CASE_ID, "main_airway", plan(21)) == {
        "committed": None, "aborted": []}


def test_reconcile_never_commits_older_match_when_latest_differs():
    older = history.begin_revision(CASE_ID, "main_airway", 3, None, plan(20))
    latest = history.begin_revision(CASE_ID, "main_airway", 3, plan(20), plan(21))
    outcome = history.reconcile_pending(CASE_ID, "main_airway", plan(20))
    assert outcome == {"committed": None, "aborted": [older, latest]}
    assert history.list_revisions(CASE_ID) == []


def test_reconcile_missing_saved_plan_aborts_pending():
    revision_id = history.begin_revision(CASE_ID, "main_airway", 3, None, plan(20))
    assert history.reconcile_pending(CASE_ID, "main_airway", None) == {
        "committed": None, "aborted": [revision_id]}
    assert history.list_revisions(CASE_ID) == []


def test_parallel_same_pending_transition_creates_one_revision():
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _index: history.begin_revision(
            CASE_ID, "main_airway", 3, None, plan(20)), range(8)))
    assert len(set(ids)) == 1
    assert len(history.list_revisions(CASE_ID, include_pending=True)) == 1


def test_missing_database_is_empty_and_optional_columns_migrate(isolated):
    assert history.list_revisions(CASE_ID) == []
    isolated.unlink()
    with sqlite3.connect(isolated) as connection:
        connection.execute("""
            CREATE TABLE endplane_revisions (
                id INTEGER PRIMARY KEY, case_id TEXT NOT NULL, scope TEXT NOT NULL,
                mask_version INTEGER NOT NULL, previous_plan_json TEXT,
                confirmed_plan_json TEXT NOT NULL, plan_hash TEXT NOT NULL,
                status TEXT NOT NULL, recorded_at TEXT NOT NULL)
        """)
        encoded = json.dumps(plan(20), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        connection.execute("""
            INSERT INTO endplane_revisions
            (case_id, scope, mask_version, previous_plan_json, confirmed_plan_json,
             plan_hash, status, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (CASE_ID, "main_airway", 3, None, encoded,
              hashlib.sha256(encoded.encode()).hexdigest(), "committed", "2026-01-01T00:00:00Z"))
    records = history.list_revisions(CASE_ID)
    assert len(records) == 1
    assert records[0]["confirmed_plan"] == plan(20)
    assert records[0]["source"] == "interactive_save"
    assert records[0]["actor_id"] is None


@pytest.mark.parametrize("bad", [
    {"patient_name": "Alice"}, {"source_path": "C:/private/scan.dcm"},
    {"measure": float("nan")},
])
def test_unknown_only_plans_are_rejected(bad):
    with pytest.raises(ValueError):
        history.begin_revision(CASE_ID, "main_airway", 3, None, bad)
    assert history.list_revisions(CASE_ID, include_pending=True) == []


def test_unknown_identity_fields_are_dropped_from_all_nested_evidence(isolated):
    safe = {**plan(20),
            "anterior_plane": {"point_lps_mm": [1, 2, 3], "normal_lps": [0, 1, 0]},
            "mesh_preflight": {"format": "oblique_mesh_clip_v1",
                               "inlet_loops": 2, "closed_caps": True}}
    unsafe = {**safe, "patient_name": "Alice", "source_path": "C:/private/scan.dcm",
              "anterior_plane": {**safe["anterior_plane"], "patient_name": "Alice"},
              "mesh_preflight": {**safe["mesh_preflight"],
                                 "source_path": "C:/private/scan.dcm"}}
    revision_id = history.begin_revision(CASE_ID, "main_airway", 3, unsafe, unsafe)
    history.commit_revision(revision_id)
    record = history.list_revisions(CASE_ID)[0]
    assert record["previous_plan"] == safe
    assert record["confirmed_plan"] == safe
    with sqlite3.connect(isolated) as connection:
        stored = connection.execute(
            "SELECT previous_plan_json, confirmed_plan_json FROM endplane_revisions"
        ).fetchone()
    assert all("Alice" not in part and "private" not in part for part in stored)


def test_all_saved_plan_formats_preserve_known_geometric_evidence():
    indexed = plan(20)
    paired = {"format": "oblique_planes_v1", "scope": "main_airway",
              "mask_version": 3, "axis": 1, "view": "coronal",
              "anterior_sign": -1, "anterior_plane": {"point_lps_mm": [1, 2, 3],
              "normal_lps": [0, -1, 0]}, "posterior_plane": {"point_lps_mm": [4, 5, 6],
              "normal_lps": [0, 1, 0]}, "source_voxels": 320, "retained_voxels": 280,
              "retained_components": 1, "retained_volume_ml": 1.23,
              "anterior_area_estimate_mm2": 34.5, "posterior_area_estimate_mm2": 45.6}
    local = {**paired, "format": "oblique_local_inlet_v2",
             "anterior_seed_lps_mm": [1, 3, 4], "complete": True,
             "anterior_deleted_voxels": 12, "anterior_candidate_components": 2,
             "anterior_opening_components": 1, "anterior_terminal_components": 1,
             "anterior_unselected_positive_voxels": 8,
             "anterior_unselected_deleted_voxels": 0,
             "unselected_region_changed_voxels": 0,
             "mesh_preflight": {"format": "oblique_mesh_clip_local_v2",
                                "inlet_loops": 2, "outlet_loops": 1,
                                "inlet_area_mm2": 34.5, "closed_caps": True}}
    for candidate in (indexed, paired, local):
        revision_id = history.begin_revision(CASE_ID, "main_airway", 3, None, candidate)
        history.commit_revision(revision_id)
    assert [record["confirmed_plan"] for record in history.list_revisions(CASE_ID)] == [
        indexed, paired, local]


@pytest.mark.parametrize("field,value", [
    ("scope", []), ("format", {}), ("view", []),
    ("mesh_preflight", {"format": []}),
])
def test_malformed_enum_types_raise_value_error(field, value):
    candidate = {**plan(20), field: value}
    with pytest.raises(ValueError):
        history.begin_revision(CASE_ID, "main_airway", 3, None, candidate)


def test_case_and_scope_reject_uncontrolled_keys():
    with pytest.raises(ValueError):
        history.begin_revision("../private", "main_airway", 3, None, plan(20))
    with pytest.raises(ValueError):
        history.begin_revision(CASE_ID, "other", 3, None, plan(20))
