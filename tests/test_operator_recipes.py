"""Recipe drafts stay private, inert, versioned and revocable."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from pipeline.operator_recipes import (
    OperatorRecipesStore,
    RecipeConflict,
    RecipeError,
    RecipeNotFound,
    RecipeRevoked,
)


FIRST = "a" * 32
SECOND = "b" * 32


@pytest.fixture
def store(tmp_path):
    return OperatorRecipesStore(tmp_path / "identity")


def spec():
    return {
        "suggested_steps": ["qc", "endplanes", "review"],
        "allowed_tools": ["service_status", "inspect_cfd_case"],
        "trigger_condition": "manual",
    }


def test_create_list_and_restart_keep_only_bounded_fields(store):
    created = store.create(FIRST, spec())
    assert created == {
        "recipe_id": created["recipe_id"], "owner_id": FIRST, "version": 1,
        "schema_version": 1, "spec": spec(), "revoked": False,
    }
    assert created["recipe_id"].startswith("recipe-")
    created["spec"]["suggested_steps"].append("surface")
    assert store.get(FIRST, created["recipe_id"])["spec"] == spec()
    assert OperatorRecipesStore(store.directory).list(FIRST) == [store.get(FIRST, created["recipe_id"])]
    assert store.list(SECOND) == []
    with sqlite3.connect(store.path) as connection:
        stored = connection.execute(
            "SELECT owner_id,version,spec_json,revoked FROM operator_recipes"
        ).fetchone()
    assert stored[:2] == (FIRST, 1)
    assert json.loads(stored[2]) == spec()
    assert stored[3] == 0


def test_owner_scoping_hides_foreign_recipe_and_denies_writes(store):
    created = store.create(FIRST, spec())
    recipe_id = created["recipe_id"]
    missing_id = "recipe-" + "0" * 32
    assert store.get(SECOND, recipe_id) is None
    assert store.get(SECOND, missing_id) is None
    for target in (recipe_id, missing_id):
        with pytest.raises(RecipeNotFound, match="recipe not found"):
            store.replace(SECOND, target, 1, spec())
        with pytest.raises(RecipeNotFound, match="recipe not found"):
            store.revoke(SECOND, target, 1)
    assert store.get(FIRST, recipe_id) == created


def test_replace_requires_current_version_and_preserves_owner(store):
    created = store.create(FIRST, spec())
    updated_spec = {
        "suggested_steps": ["cfd_results", "review"],
        "allowed_tools": ["get_cfd_batch"],
        "trigger_condition": "numeric_gate_failed",
    }
    updated = store.replace(FIRST, created["recipe_id"], 1, updated_spec)
    assert updated["version"] == 2
    assert updated["spec"] == updated_spec
    with pytest.raises(RecipeConflict) as exc:
        store.replace(FIRST, created["recipe_id"], 1, spec())
    assert exc.value.current_version == 2
    assert store.get(FIRST, created["recipe_id"]) == updated


def test_concurrent_replace_has_one_winner_and_one_conflict(store):
    created = store.create(FIRST, spec())
    barrier = Barrier(2)

    def update(trigger):
        next_spec = spec()
        next_spec["trigger_condition"] = trigger
        barrier.wait(timeout=5)
        try:
            return store.replace(FIRST, created["recipe_id"], 1, next_spec)["version"]
        except RecipeConflict as exc:
            return f"conflict:{exc.current_version}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(update, ("batch_finished", "case_stage_changed")))
    assert sorted(map(str, outcomes)) == ["2", "conflict:2"]
    assert store.get(FIRST, created["recipe_id"])["version"] == 2


def test_revoke_increments_version_and_cannot_restore(store):
    created = store.create(FIRST, spec())
    revoked = store.revoke(FIRST, created["recipe_id"], 1)
    assert revoked["version"] == 2
    assert revoked["revoked"] is True
    assert store.get(FIRST, created["recipe_id"]) == revoked
    assert store.list(FIRST) == [revoked]
    with pytest.raises(RecipeRevoked):
        store.revoke(FIRST, created["recipe_id"], 2)
    with pytest.raises(RecipeRevoked):
        store.replace(FIRST, created["recipe_id"], 2, spec())
    assert store.get(FIRST, created["recipe_id"]) == revoked


@pytest.mark.parametrize("bad", [
    {**spec(), "patient_name": "patient"},
    {**spec(), "case_id": "case-20261102-000000-0001"},
    {**spec(), "notes": "give medical advice"},
    {**spec(), "allowed_tools": ["submit_cfd_batch"]},
    {**spec(), "allowed_tools": ["cancel_queued_cfd"]},
    {**spec(), "allowed_tools": ["inspect_cfd_case", "inspect_cfd_case"]},
    {**spec(), "allowed_tools": []},
    {**spec(), "suggested_steps": ["share"]},
    {**spec(), "suggested_steps": ["cfd_run"]},
    {**spec(), "suggested_steps": ["qc"] * 9},
    {**spec(), "trigger_condition": "auto_publish"},
    {**spec(), "trigger_condition": True},
])
def test_rejects_free_text_patient_data_write_tools_and_unknown_enums(store, bad):
    with pytest.raises(RecipeError):
        store.create(FIRST, bad)
    assert store.list(FIRST) == []


@pytest.mark.parametrize("owner", ["", "alice", "../../cases", "a" * 31, "A" * 32])
def test_rejects_untrusted_owner_ids(store, owner):
    with pytest.raises(RecipeError):
        store.create(owner, spec())
    with pytest.raises(RecipeError):
        store.list(owner)


@pytest.mark.parametrize("version", [-1, 0, True, "1", 1.0, 2**63])
def test_rejects_invalid_expected_version(store, version):
    created = store.create(FIRST, spec())
    with pytest.raises(RecipeError):
        store.replace(FIRST, created["recipe_id"], version, spec())
    with pytest.raises(RecipeError):
        store.revoke(FIRST, created["recipe_id"], version)
    assert store.get(FIRST, created["recipe_id"]) == created


def test_rejects_invalid_recipe_ids(store):
    for recipe_id in ("", "../recipe", "recipe-" + "A" * 32):
        with pytest.raises(RecipeError):
            store.get(FIRST, recipe_id)
        with pytest.raises(RecipeError):
            store.revoke(FIRST, recipe_id, 1)


def test_corrupt_stored_spec_fails_closed(store):
    created = store.create(FIRST, spec())
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE operator_recipes SET spec_json=? WHERE recipe_id=?",
            ('{"patient_name":"must not return"}', created["recipe_id"]),
        )
    with pytest.raises(RuntimeError, match="stored operator recipe"):
        store.get(FIRST, created["recipe_id"])


def test_duplicate_json_keys_fail_closed_on_reads_and_updates(store):
    created = store.create(FIRST, spec())
    duplicate = ('{"suggested_steps":["qc"],"suggested_steps":["review"],'
                 '"allowed_tools":["inspect_cfd_case"],"trigger_condition":"manual"}')
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE operator_recipes SET spec_json=? WHERE recipe_id=?",
                           (duplicate, created["recipe_id"]))
    with pytest.raises(RuntimeError, match="stored operator recipe"):
        store.get(FIRST, created["recipe_id"])
    with pytest.raises(RuntimeError, match="stored operator recipe"):
        store.list(FIRST)
    with pytest.raises(RuntimeError, match="stored operator recipe"):
        store.replace(FIRST, created["recipe_id"], 1, spec())
    with pytest.raises(RuntimeError, match="stored operator recipe"):
        store.revoke(FIRST, created["recipe_id"], 1)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT version FROM operator_recipes").fetchone()[0] == 1


def test_newer_schema_is_not_downgraded(store):
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA user_version=2")
    with pytest.raises(RuntimeError, match="newer"):
        OperatorRecipesStore(store.directory)
