"""Personal UI preferences never store case data or execute favored steps."""

from __future__ import annotations

import json
import math
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from pipeline.operator_preferences import (
    DEFAULT_PREFERENCES,
    OperatorPreferencesStore,
    PreferenceConflict,
    PreferenceError,
)


FIRST = "a" * 32
SECOND = "b" * 32


@pytest.fixture
def store(tmp_path):
    return OperatorPreferencesStore(tmp_path / "identity")


def _preferences():
    return {
        "view_pose": {"azimuth": 0.3, "elevation": -0.2, "zoom": 1.25},
        "display_ranges": {
            "velocity": {"min": 0, "max": 12.5, "unit": "m/s"},
            "pressure": {"min": -120, "max": 80, "unit": "Pa"},
        },
        "favorite_steps": ["segment", "endplanes", "cfd_results"],
    }


def test_defaults_replace_and_restart_preserve_only_whitelisted_preferences(store):
    first = store.get(FIRST)
    assert first == {"user_id": FIRST, "version": 0, "schema_version": 1,
                     "preferences": DEFAULT_PREFERENCES}
    first["preferences"]["favorite_steps"].append("share")
    assert store.get(FIRST)["preferences"]["favorite_steps"] == []

    saved = store.replace(FIRST, 0, _preferences())
    assert saved["version"] == 1
    assert saved["preferences"]["display_ranges"]["velocity"]["max"] == 12.5
    assert OperatorPreferencesStore(store.directory).get(FIRST) == saved
    with sqlite3.connect(store.path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(operator_preferences)")]
        stored = connection.execute(
            "SELECT user_id,version,preferences_json FROM operator_preferences"
        ).fetchone()
    assert columns == ["user_id", "version", "preferences_json"]
    assert stored[:2] == (FIRST, 1)
    assert json.loads(stored[2]) == saved["preferences"]


def test_accounts_are_isolated_and_stale_update_cannot_overwrite(store):
    first = store.replace(FIRST, 0, _preferences())
    assert store.get(SECOND)["version"] == 0
    second_preferences = _preferences()
    second_preferences["favorite_steps"] = ["qc", "report"]
    second = store.replace(SECOND, 0, second_preferences)
    assert second["version"] == 1
    assert store.get(FIRST) == first
    with pytest.raises(PreferenceConflict) as exc:
        store.replace(FIRST, 0, second_preferences)
    assert exc.value.current_version == 1
    assert store.get(FIRST) == first


def test_concurrent_updates_are_serialized_by_version(store):
    barrier = Barrier(2)

    def update(step):
        preferences = _preferences()
        preferences["favorite_steps"] = [step]
        barrier.wait(timeout=5)
        try:
            return store.replace(FIRST, 0, preferences)["version"]
        except PreferenceConflict as exc:
            return f"conflict:{exc.current_version}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(update, ("qc", "share")))
    assert sorted(map(str, outcomes)) == ["1", "conflict:1"]
    assert store.get(FIRST)["version"] == 1


@pytest.mark.parametrize("value", [
    {**_preferences(), "patient_name": "patient"},
    {**_preferences(), "case_id": "case-20261102-000000-0001"},
    {**_preferences(), "notes": "recommend treatment"},
    {**_preferences(), "view_pose": {"azimuth": math.nan, "elevation": 0, "zoom": 1}},
    {**_preferences(), "view_pose": {"azimuth": True, "elevation": 0, "zoom": 1}},
    {**_preferences(), "view_pose": {"azimuth": 10**1000, "elevation": 0, "zoom": 1}},
    {**_preferences(), "display_ranges": {"velocity": {"min": 0, "max": 12,
                                                   "unit": "Pa"}, "pressure": None}},
    {**_preferences(), "display_ranges": {"velocity": {"min": 12, "max": 0,
                                                   "unit": "m/s"}, "pressure": None}},
    {**_preferences(), "display_ranges": {"velocity": None,
                                              "pressure": {"min": 0, "max": 10, "unit": []}}},
    {**_preferences(), "favorite_steps": ["segment", "segment"]},
    {**_preferences(), "favorite_steps": ["auto-diagnose"]},
    {**_preferences(), "favorite_steps": ["qc"] * 9},
])
def test_rejects_patient_fields_invalid_ranges_and_unapproved_steps(store, value):
    with pytest.raises(PreferenceError):
        store.replace(FIRST, 0, value)
    assert store.get(FIRST)["version"] == 0


@pytest.mark.parametrize("user_id", ["", "alice", "../../cases", "a" * 31, "A" * 32])
def test_rejects_untrusted_account_identifiers(store, user_id):
    with pytest.raises(PreferenceError):
        store.get(user_id)
    with pytest.raises(PreferenceError):
        store.replace(user_id, 0, _preferences())


@pytest.mark.parametrize("version", [-1, True, "0", 0.0, 2**63])
def test_rejects_invalid_expected_versions(store, version):
    with pytest.raises(PreferenceError):
        store.replace(FIRST, version, _preferences())


def test_corrupted_stored_payload_fails_closed(store):
    with sqlite3.connect(store.path) as connection:
        connection.execute("INSERT INTO operator_preferences VALUES (?,?,?)",
                           (FIRST, 1, '{"patient_name":"must not return"}'))
    with pytest.raises(RuntimeError, match="stored operator preferences"):
        store.get(FIRST)


def test_newer_database_schema_is_not_downgraded(store):
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA user_version=2")
    with pytest.raises(RuntimeError, match="newer"):
        OperatorPreferencesStore(store.directory)
