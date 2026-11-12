"""Candidate and human-feedback ledger stays anonymous, structured, and actor-scoped."""

import json
import sqlite3

import pytest

from pipeline.endplane_learning import (
    EndplaneLearningStore,
    LearningError,
    validate_plan,
)


CASE_ID = "case-20261104-123456-abcd"
ACTOR_A = "a" * 32
ACTOR_B = "b" * 32


@pytest.fixture
def store(tmp_path):
    return EndplaneLearningStore(tmp_path / "learning")


def plan(index=20):
    return {
        "format": "oblique_planes_v1",
        "scope": "main_airway",
        "mask_version": 3,
        "anterior_plane": {"point_lps_mm": [0, index, 0], "normal_lps": [0, 1, 0]},
        "posterior_plane": {"point_lps_mm": [0, 8, 0], "normal_lps": [0, -1, 0]},
    }


def test_proposal_creation_is_idempotent_and_hashes_canonical_plan(store):
    first = store.create_proposal(
        CASE_ID, "main_airway", 3, plan(),
        source="reference_knn", source_version="v1", actor_id=ACTOR_A,
    )
    retried = store.create_proposal(
        CASE_ID, "main_airway", 3, dict(reversed(list(plan().items()))),
        source="reference_knn", source_version="v1", actor_id=ACTOR_A,
    )

    assert retried["proposal_id"] == first["proposal_id"]
    assert first["status"] == "proposed"
    assert first["plan"] == plan()
    assert len(first["plan_hash"]) == 64
    assert store.list_proposals(actor_id=ACTOR_A) == [first]
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'" )} >= {"proposals", "feedback"}


def test_two_local_candidates_commit_atomically_and_retry_idempotently(store):
    candidates = [
        ("local_mask_geometry_left", plan(20)),
        ("local_mask_geometry_right", plan(21)),
    ]
    with sqlite3.connect(store.path) as db:
        db.execute("""
            CREATE TRIGGER fail_right BEFORE INSERT ON proposals
            WHEN NEW.source = 'local_mask_geometry_right'
            BEGIN SELECT RAISE(ABORT, 'forced batch failure'); END
        """)
    with pytest.raises(sqlite3.IntegrityError, match="forced batch failure"):
        store.create_proposals(CASE_ID, "main_airway", 3, candidates,
                               source_version="v1", actor_id=ACTOR_A)
    assert store.list_proposals(actor_id=ACTOR_A) == []
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TRIGGER fail_right")
    created = store.create_proposals(CASE_ID, "main_airway", 3, candidates,
                                     source_version="v1", actor_id=ACTOR_A)
    retried = store.create_proposals(CASE_ID, "main_airway", 3, candidates,
                                     source_version="v1", actor_id=ACTOR_A)
    assert len(created) == 2
    assert [item["proposal_id"] for item in retried] == [item["proposal_id"] for item in created]
    assert {item["source"] for item in created} == {source for source, _ in candidates}


def test_feedback_transitions_are_structured_and_auditable(store):
    proposal = store.create_proposal(
        CASE_ID, "main_airway", 3, plan(),
        source="reference_knn", source_version="v1", actor_id=ACTOR_A,
    )
    adjusted = store.record_feedback(
        proposal["proposal_id"], "adjusted", actor_id=ACTOR_A,
        feedback={"adjusted_plan": plan(21), "reason_code": "geometry_mismatch"},
    )
    assert adjusted["status"] == "adjusted"
    assert adjusted["previous_status"] == "proposed"
    assert store.get_proposal(proposal["proposal_id"], actor_id=ACTOR_A)["status"] == "adjusted"
    assert store.list_feedback(proposal["proposal_id"], actor_id=ACTOR_A)[0]["status"] == "adjusted"

    with pytest.raises(LearningError, match="terminal"):
        store.record_feedback(proposal["proposal_id"], "accepted", actor_id=ACTOR_A)


@pytest.mark.parametrize("payload", [
    {"patient_name": "张三"},
    {"path": "C:/patient/ct"},
    {"comment": "looks good"},
    {"unknown": 1},
    {"adjusted_plan": {**plan(), "notes": "free text"}},
])
def test_patient_paths_free_text_and_unknown_fields_are_rejected(store, payload):
    with pytest.raises(LearningError):
        store.create_proposal(
            CASE_ID, "main_airway", 3,
            {**plan(), **payload},
            source="reference_knn", source_version="v1", actor_id=ACTOR_A,
        )

    proposal = store.create_proposal(
        CASE_ID, "main_airway", 3, plan(),
        source="reference_knn", source_version="v1", actor_id=ACTOR_A,
    )
    with pytest.raises(LearningError):
        store.record_feedback(proposal["proposal_id"], "adjusted",
                              actor_id=ACTOR_A, feedback=payload)


def test_actor_isolation_hides_and_blocks_other_actor(store):
    proposal = store.create_proposal(
        CASE_ID, "main_airway", 3, plan(),
        source="reference_knn", source_version="v1", actor_id=ACTOR_A,
    )
    assert store.list_proposals(actor_id=ACTOR_B) == []
    assert store.get_proposal(proposal["proposal_id"], actor_id=ACTOR_B) is None
    assert store.list_proposals() == []
    assert store.get_proposal(proposal["proposal_id"]) is None
    assert store.list_feedback(proposal["proposal_id"]) == []
    with pytest.raises(LearningError, match="actor"):
        store.record_feedback(proposal["proposal_id"], "accepted", actor_id=ACTOR_B)


def test_feedback_status_is_limited_and_rows_are_append_only(store):
    proposal = store.create_proposal(
        CASE_ID, "main_airway", 3, plan(),
        source="reference_knn", source_version="v1", actor_id=ACTOR_A,
    )
    with pytest.raises(LearningError):
        store.record_feedback(proposal["proposal_id"], "pending", actor_id=ACTOR_A)
    store.record_feedback(proposal["proposal_id"], "rejected", actor_id=ACTOR_A,
                          feedback={"reason_code": "insufficient_evidence"})
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 1
        stored = db.execute("SELECT feedback_json FROM feedback").fetchone()[0]
    assert json.loads(stored) == {"reason_code": "insufficient_evidence"}


def test_feedback_cannot_be_updated_or_deleted_in_database(store):
    proposal = store.create_proposal(
        CASE_ID, "main_airway", 3, plan(),
        source="reference_knn", source_version="v1", actor_id=ACTOR_A,
    )
    store.record_feedback(proposal["proposal_id"], "accepted", actor_id=ACTOR_A)
    with sqlite3.connect(store.path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("UPDATE feedback SET status='rejected'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM feedback")


def test_stored_proposal_hash_mismatch_fails_closed(store):
    proposal = store.create_proposal(
        CASE_ID, "main_airway", 3, plan(),
        source="reference_knn", source_version="v1", actor_id=ACTOR_A,
    )
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE proposals SET plan_hash=?", ("0" * 64,))
    with pytest.raises(RuntimeError, match="invalid"):
        store.get_proposal(proposal["proposal_id"], actor_id=ACTOR_A)


def test_case_review_bridges_service_proposal_without_expanding_actor_access(store):
    proposal = store.create_proposal(
        CASE_ID, "main_airway", 3, plan(), source="mcp_candidate",
        source_version="v1", actor_id=ACTOR_A,
    )
    other_case = "synthetic-another-case"
    assert store.get_case_proposal(other_case, proposal["proposal_id"]) is None
    assert store.list_case_proposals(other_case) == []
    assert store.list_case_feedback(other_case, proposal["proposal_id"]) == []
    assert store.get_case_proposal(CASE_ID, proposal["proposal_id"]) == proposal
    assert store.list_case_proposals(CASE_ID, "main_airway") == [proposal]
    with pytest.raises(LearningError, match="not found"):
        store.record_case_feedback(other_case, proposal["proposal_id"], "accepted",
                                   reviewer_id=ACTOR_B)
    reviewed = store.record_case_feedback(CASE_ID, proposal["proposal_id"], "accepted",
                                          reviewer_id=ACTOR_B)
    assert reviewed["status"] == "accepted"
    assert reviewed["actor_id"] == ACTOR_A
    assert store.list_case_feedback(CASE_ID, proposal["proposal_id"])[0]["actor_id"] == ACTOR_B
    assert store.list_proposals(actor_id=ACTOR_B) == []
    assert store.get_proposal(proposal["proposal_id"], actor_id=ACTOR_B) is None
    assert store.list_feedback(proposal["proposal_id"], actor_id=ACTOR_B) == []
    with pytest.raises(LearningError, match="terminal"):
        store.record_case_feedback(CASE_ID, proposal["proposal_id"], "rejected",
                                   reviewer_id=ACTOR_B)


def test_case_feedback_rejects_tampered_plan_atomically(store):
    proposal = store.create_proposal(
        CASE_ID, "main_airway", 3, plan(), source="mcp_candidate",
        source_version="v1", actor_id=ACTOR_A,
    )
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE proposals SET plan_hash=? WHERE proposal_id=?",
                   ("0" * 64, proposal["proposal_id"]))
    with pytest.raises(RuntimeError, match="invalid"):
        store.record_case_feedback(CASE_ID, proposal["proposal_id"], "accepted",
                                   reviewer_id=ACTOR_B)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT status FROM proposals").fetchone()[0] == "proposed"
        assert db.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0


def test_case_feedback_requires_trusted_reviewer_and_rejects_actor_override(store):
    proposal = store.create_proposal(
        CASE_ID, "main_airway", 3, plan(), source="mcp_candidate",
        source_version="v1", actor_id=ACTOR_A,
    )
    with pytest.raises(LearningError, match="reviewer"):
        store.record_case_feedback(CASE_ID, proposal["proposal_id"], "accepted", reviewer_id=None)
    with pytest.raises(TypeError):
        store.record_case_feedback(CASE_ID, proposal["proposal_id"], "accepted",
                                   reviewer_id=ACTOR_B, actor_id=ACTOR_A)


@pytest.mark.parametrize("change", [
    {"anterior_plane": None},
    {"posterior_plane": {"point_lps_mm": [0, 0, 0], "normal_lps": [0, 0, 0]}},
    {"anterior_seed_lps_mm": [0, 0, 0]},
    {"anterior_index": 5},
    {"format": "oblique_local_inlet_v2"},
    {"scope": "full_candidate"},
    {"mask_version": 4},
])
def test_candidate_plan_must_be_complete_consistent_geometry(change):
    with pytest.raises(LearningError):
        validate_plan(plan() | change, scope="main_airway", mask_version=3)
