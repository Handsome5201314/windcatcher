"""Private, inert operator recipe drafts.

This module stores suggestions only. It never invokes MCP tools, evaluates
triggers, reads cases, or grants permissions. The caller must supply an owner ID
obtained from a trusted authenticated session, not from a request body.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .operator_preferences import configured_directory as preferences_directory


DATABASE_NAME = "operator-recipes.sqlite3"
DIRECTORY_ENV = "WINDCATCHER_RECIPES_DIR"
SCHEMA_VERSION = 1
OWNER_ID = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
RECIPE_ID = re.compile(r"recipe-[0-9a-f]{32}\Z", re.ASCII)

# Names are the existing read-only Windcatcher MCP tools. Listing a name here
# does not make it callable through this store.
READ_ONLY_TOOLS = frozenset({
    "service_status", "list_cfd_cases", "inspect_cfd_case", "get_cfd_batch",
})
SUGGESTED_STEPS = frozenset({
    "qc", "segment", "endplanes", "surface", "cfd_prep", "cfd_results",
    "report", "review",
})
TRIGGER_CONDITIONS = frozenset({
    "manual", "case_stage_changed", "batch_finished", "numeric_gate_failed",
})
SPEC_FIELDS = frozenset({"suggested_steps", "allowed_tools", "trigger_condition"})


class RecipeError(ValueError):
    """Invalid recipe identifier, version, or draft specification."""


class RecipeNotFound(RecipeError):
    """No recipe is visible to this owner (also used for cross-owner access)."""


class RecipeRevoked(RecipeError):
    """A revoked recipe cannot be edited or restored."""


class RecipeConflict(RecipeError):
    def __init__(self, current_version: int):
        super().__init__("operator recipe changed; reload before saving")
        self.current_version = current_version


def configured_directory() -> Path:
    """Use the identity data directory by default, with a dedicated override."""
    return Path(os.environ.get(DIRECTORY_ENV) or preferences_directory())


def _owner_id(value: object) -> str:
    if not isinstance(value, str) or not OWNER_ID.fullmatch(value):
        raise RecipeError("invalid owner id")
    return value


def _recipe_id(value: object) -> str:
    if not isinstance(value, str) or not RECIPE_ID.fullmatch(value):
        raise RecipeError("invalid recipe id")
    return value


def _expected_version(value: object) -> int:
    if type(value) is not int or not 1 <= value < 2**63 - 1:
        raise RecipeError("invalid expected version")
    return value


def _enum_list(value: object, choices: frozenset[str], *, maximum: int, label: str) -> list[str]:
    if (type(value) is not list or not 1 <= len(value) <= maximum
            or any(type(item) is not str or item not in choices for item in value)
            or len(set(value)) != len(value)):
        raise RecipeError(f"invalid {label}")
    return list(value)


def validate_spec(value: object) -> dict:
    """Canonicalize a bounded draft; reject free text and patient-shaped data."""
    if type(value) is not dict or set(value) != SPEC_FIELDS:
        raise RecipeError("invalid recipe specification fields")
    trigger = value["trigger_condition"]
    if type(trigger) is not str or trigger not in TRIGGER_CONDITIONS:
        raise RecipeError("invalid trigger condition")
    return {
        "suggested_steps": _enum_list(value["suggested_steps"], SUGGESTED_STEPS,
                                      maximum=8, label="suggested steps"),
        "allowed_tools": _enum_list(value["allowed_tools"], READ_ONLY_TOOLS,
                                    maximum=4, label="allowed tools"),
        "trigger_condition": trigger,
    }


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecipeError("recipe JSON contains duplicate keys")
        result[key] = value
    return result


def parse_spec_json(encoded: object) -> dict:
    """Decode stored JSON strictly, including duplicate-key rejection."""
    if type(encoded) is not str:
        raise RecipeError("invalid recipe JSON")
    try:
        value = json.loads(encoded, object_pairs_hook=_unique_object)
    except ValueError as exc:
        raise RecipeError("invalid recipe JSON") from exc
    return validate_spec(value)


class OperatorRecipesStore:
    def __init__(self, directory: Path | str | None = None):
        self.directory = Path(directory) if directory is not None else configured_directory()
        self.path = self.directory / DATABASE_NAME
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError("operator recipes schema is newer than this application")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS operator_recipes (
                    recipe_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK (version > 0),
                    spec_json TEXT NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1))
                )
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS operator_recipes_owner
                ON operator_recipes (owner_id)
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
    def _row(row: sqlite3.Row) -> dict:
        try:
            _recipe_id(row["recipe_id"])
            _owner_id(row["owner_id"])
            spec = parse_spec_json(row["spec_json"])
            if (type(row["version"]) is not int or row["version"] < 1
                    or type(row["revoked"]) is not int or row["revoked"] not in (0, 1)):
                raise RecipeError("invalid stored recipe metadata")
        except (json.JSONDecodeError, RecipeError, TypeError) as exc:
            raise RuntimeError("stored operator recipe is invalid") from exc
        return {
            "recipe_id": row["recipe_id"],
            "owner_id": row["owner_id"],
            "version": row["version"],
            "schema_version": SCHEMA_VERSION,
            "spec": spec,
            "revoked": bool(row["revoked"]),
        }

    def create(self, owner_id: str, spec: object) -> dict:
        owner_id = _owner_id(owner_id)
        validated = validate_spec(spec)
        recipe_id = "recipe-" + secrets.token_hex(16)
        encoded = json.dumps(validated, separators=(",", ":"), allow_nan=False)
        with self._db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                INSERT INTO operator_recipes (recipe_id,owner_id,version,spec_json,revoked)
                VALUES (?,?,1,?,0)
            """, (recipe_id, owner_id, encoded))
            row = connection.execute(
                "SELECT * FROM operator_recipes WHERE recipe_id=? AND owner_id=?",
                (recipe_id, owner_id),
            ).fetchone()
        return self._row(row)

    def list(self, owner_id: str) -> list[dict]:
        owner_id = _owner_id(owner_id)
        with self._db() as connection:
            rows = connection.execute(
                "SELECT * FROM operator_recipes WHERE owner_id=? ORDER BY rowid",
                (owner_id,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def get(self, owner_id: str, recipe_id: str) -> dict | None:
        owner_id, recipe_id = _owner_id(owner_id), _recipe_id(recipe_id)
        with self._db() as connection:
            row = connection.execute(
                "SELECT * FROM operator_recipes WHERE recipe_id=? AND owner_id=?",
                (recipe_id, owner_id),
            ).fetchone()
        return self._row(row) if row is not None else None

    def _change(self, owner_id: str, recipe_id: str, expected_version: int,
                spec: dict | None, *, revoke: bool) -> dict:
        owner_id, recipe_id = _owner_id(owner_id), _recipe_id(recipe_id)
        expected_version = _expected_version(expected_version)
        encoded = None if spec is None else json.dumps(spec, separators=(",", ":"), allow_nan=False)
        with self._db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM operator_recipes WHERE recipe_id=? AND owner_id=?",
                (recipe_id, owner_id),
            ).fetchone()
            if row is None:
                raise RecipeNotFound("recipe not found")
            self._row(row)
            if row["revoked"]:
                raise RecipeRevoked("recipe is revoked")
            if row["version"] != expected_version:
                raise RecipeConflict(row["version"])
            if revoke:
                connection.execute("""
                    UPDATE operator_recipes SET version=version+1,revoked=1
                    WHERE recipe_id=? AND owner_id=?
                """, (recipe_id, owner_id))
            else:
                connection.execute("""
                    UPDATE operator_recipes SET version=version+1,spec_json=?
                    WHERE recipe_id=? AND owner_id=?
                """, (encoded, recipe_id, owner_id))
            updated = connection.execute(
                "SELECT * FROM operator_recipes WHERE recipe_id=? AND owner_id=?",
                (recipe_id, owner_id),
            ).fetchone()
        return self._row(updated)

    def replace(self, owner_id: str, recipe_id: str, expected_version: int, spec: object) -> dict:
        """Edit only an active draft; optimistic-lock conflicts leave it intact."""
        validated = validate_spec(spec)
        return self._change(owner_id, recipe_id, expected_version, validated, revoke=False)

    def revoke(self, owner_id: str, recipe_id: str, expected_version: int) -> dict:
        """Permanently withdraw a draft; revocation is retained for audit."""
        return self._change(owner_id, recipe_id, expected_version, None, revoke=True)
