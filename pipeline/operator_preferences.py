"""Per-operator UI preferences, separate from case data and identity secrets.

Only a trusted gateway may supply the authenticated account ID. Values are
strictly whitelisted presentation choices; they never trigger a workflow,
change a case, or contain patient identifiers or free-form text.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from .config import APP_DIR


DATABASE_NAME = "operator-preferences.sqlite3"
DIRECTORY_ENV = "WINDCATCHER_PREFERENCES_DIR"
SCHEMA_VERSION = 1
USER_ID = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
FAVORITE_STEPS = frozenset({
    "qc", "segment", "endplanes", "surface", "cfd_prep", "cfd_run",
    "cfd_results", "report", "review", "share",
})
PRESSURE_UNITS = frozenset({"Pa", "m2/s2", "m²/s²"})
DEFAULT_PREFERENCES = {
    "view_pose": None,
    "display_ranges": {"velocity": None, "pressure": None},
    "favorite_steps": [],
}


def configured_directory() -> Path:
    """Resolve the live preference directory without creating database files."""
    return Path(os.environ.get(DIRECTORY_ENV) or os.environ.get("WINDCATCHER_IDENTITY_DIR")
                or APP_DIR / ".local-identity")


class PreferenceError(ValueError):
    """Rejected preference value or optimistic-concurrency update."""


class PreferenceConflict(PreferenceError):
    def __init__(self, current_version: int):
        super().__init__("operator preferences changed; reload before saving")
        self.current_version = current_version


def _user_id(value: str) -> str:
    if not isinstance(value, str) or not USER_ID.fullmatch(value):
        raise PreferenceError("invalid account id")
    return value


def _number(value: object, minimum: float, maximum: float, label: str) -> float:
    if type(value) not in {int, float}:
        raise PreferenceError(f"invalid {label}")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise PreferenceError(f"invalid {label}") from exc
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise PreferenceError(f"invalid {label}")
    return number


def _color_range(value: object, field: str) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"min", "max", "unit"}:
        raise PreferenceError(f"invalid {field} display range")
    unit = value["unit"]
    if field == "velocity":
        if unit != "m/s":
            raise PreferenceError("velocity display range must use m/s")
        minimum = _number(value["min"], 0, 500, "velocity minimum")
        maximum = _number(value["max"], 0, 500, "velocity maximum")
    else:
        if not isinstance(unit, str) or unit not in PRESSURE_UNITS:
            raise PreferenceError("invalid pressure unit")
        minimum = _number(value["min"], -1_000_000, 1_000_000, "pressure minimum")
        maximum = _number(value["max"], -1_000_000, 1_000_000, "pressure maximum")
    if minimum >= maximum:
        raise PreferenceError(f"{field} display range must increase")
    return {"min": minimum, "max": maximum, "unit": unit}


def validate_preferences(value: object) -> dict:
    """Return a bounded canonical copy; reject unknown or patient-shaped data."""
    if not isinstance(value, dict) or set(value) != set(DEFAULT_PREFERENCES):
        raise PreferenceError("preferences must contain only view_pose, display_ranges and favorite_steps")
    pose = value["view_pose"]
    if pose is not None:
        if not isinstance(pose, dict) or set(pose) != {"azimuth", "elevation", "zoom"}:
            raise PreferenceError("invalid view pose")
        pose = {
            "azimuth": _number(pose["azimuth"], -math.pi, math.pi, "azimuth"),
            "elevation": _number(pose["elevation"], -math.pi / 2, math.pi / 2, "elevation"),
            "zoom": _number(pose["zoom"], 0.7, 5, "zoom"),
        }
    ranges = value["display_ranges"]
    if not isinstance(ranges, dict) or set(ranges) != {"velocity", "pressure"}:
        raise PreferenceError("invalid display ranges")
    steps = value["favorite_steps"]
    if (not isinstance(steps, list) or len(steps) > 8
            or any(not isinstance(step, str) or step not in FAVORITE_STEPS for step in steps)
            or len(set(steps)) != len(steps)):
        raise PreferenceError("invalid favorite steps")
    return {
        "view_pose": pose,
        "display_ranges": {
            "velocity": _color_range(ranges["velocity"], "velocity"),
            "pressure": _color_range(ranges["pressure"], "pressure"),
        },
        "favorite_steps": list(steps),
    }


class OperatorPreferencesStore:
    def __init__(self, directory: Path | str | None = None):
        root = directory if directory is not None else configured_directory()
        self.directory = Path(root)
        self.path = self.directory / DATABASE_NAME
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError("operator preferences schema is newer than this application")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS operator_preferences (
                    user_id TEXT PRIMARY KEY,
                    version INTEGER NOT NULL CHECK (version > 0),
                    preferences_json TEXT NOT NULL
                )
            """)
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def _db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout=10000")
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _row(user_id: str, version: int, preferences: dict) -> dict:
        return {"user_id": user_id, "version": version,
                "schema_version": SCHEMA_VERSION, "preferences": preferences}

    def get(self, user_id: str) -> dict:
        user_id = _user_id(user_id)
        with self._db() as connection:
            row = connection.execute(
                "SELECT version,preferences_json FROM operator_preferences WHERE user_id=?", (user_id,)
            ).fetchone()
        if row is None:
            return self._row(user_id, 0, deepcopy(DEFAULT_PREFERENCES))
        try:
            preferences = validate_preferences(json.loads(row["preferences_json"]))
        except (json.JSONDecodeError, PreferenceError) as exc:
            raise RuntimeError("stored operator preferences are invalid") from exc
        return self._row(user_id, row["version"], preferences)

    def replace(self, user_id: str, expected_version: int, preferences: object) -> dict:
        """Replace one account's preferences; stale updates fail without writes."""
        user_id = _user_id(user_id)
        if type(expected_version) is not int or not 0 <= expected_version < 2**63 - 1:
            raise PreferenceError("invalid expected version")
        validated = validate_preferences(preferences)
        encoded = json.dumps(validated, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        with self._db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM operator_preferences WHERE user_id=?", (user_id,)
            ).fetchone()
            current_version = row["version"] if row is not None else 0
            if expected_version != current_version:
                raise PreferenceConflict(current_version)
            next_version = current_version + 1
            connection.execute("""
                INSERT INTO operator_preferences (user_id,version,preferences_json) VALUES (?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                    version=excluded.version,preferences_json=excluded.preferences_json
            """, (user_id, next_version, encoded))
        return self._row(user_id, next_version, validated)
