"""Persistent operator identity and case grants for the hosted workbench.

The gateway, not browser-supplied session or reviewer fields, supplies actor IDs.
This store contains no CT, case geometry, patient name, or raw session token.
Global administrators manage accounts; case grants remain explicit and separate.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from .config import APP_DIR


DIRECTORY_ENV = "WINDCATCHER_IDENTITY_DIR"
DATABASE_NAME = "identity.sqlite3"
SCHEMA_VERSION = 2
SESSION_SECONDS = 8 * 60 * 60
CASE_ID = re.compile(r"(?:case-\d{8}-\d{6}-[0-9a-f]{4}|synthetic(?:-[A-Za-z0-9_-]{1,64})?)\Z", re.ASCII)
UPLOAD_ID = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
SESSION_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z", re.ASCII)
USERNAME = re.compile(r"[\w.@-]{1,80}\Z", re.UNICODE)
CASE_ROLES = {"viewer", "editor", "reviewer", "owner"}
CAPABILITIES = {
    "view": CASE_ROLES,
    "edit": {"editor", "owner"},
    "review": {"reviewer", "owner"},
    "share": {"reviewer", "owner"},
    "manage": {"owner"},
}


class IdentityError(ValueError):
    """Invalid identity input or a rejected authorization transition."""


def _case_id(value: str) -> str:
    if not isinstance(value, str) or not CASE_ID.fullmatch(value):
        raise IdentityError("invalid anonymous case id")
    return value


def _upload_id(value: str) -> str:
    if not isinstance(value, str) or not UPLOAD_ID.fullmatch(value):
        raise IdentityError("invalid upload id")
    return value


def _username(value: str) -> str:
    if not isinstance(value, str) or not USERNAME.fullmatch(value):
        raise IdentityError("invalid username")
    return value


def _password(value: str) -> str:
    if not isinstance(value, str) or not 16 <= len(value.encode("utf-8")) <= 1024:
        raise IdentityError("password must contain 16 to 1024 UTF-8 bytes")
    return value


def _password_digest(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)


def _public_user(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "username": row["username"],
        "global_role": row["global_role"], "kind": row["kind"],
        "active": bool(row["active"]),
    }


class IdentityStore:
    def __init__(self, directory: Path | str | None = None):
        root = directory if directory is not None else os.environ.get(DIRECTORY_ENV, "")
        self.directory = Path(root) if root else APP_DIR / ".local-identity"
        self.path = self.directory / DATABASE_NAME
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._db() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError("identity database schema is newer than this application")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_salt BLOB,
                    password_digest BLOB,
                    kind TEXT NOT NULL CHECK (kind IN ('human', 'service')),
                    global_role TEXT NOT NULL CHECK (global_role IN ('admin', 'member', 'service')),
                    active INTEGER NOT NULL CHECK (active IN (0, 1)),
                    created_at INTEGER NOT NULL,
                    disabled_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    revoked_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id, expires_at);
                CREATE TABLE IF NOT EXISTS case_grants (
                    case_id TEXT NOT NULL,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    role TEXT NOT NULL CHECK (role IN ('viewer','editor','reviewer','owner')),
                    granted_by TEXT REFERENCES users(id),
                    granted_at INTEGER NOT NULL,
                    PRIMARY KEY (case_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS case_grants_user ON case_grants(user_id, case_id);
                CREATE TABLE IF NOT EXISTS upload_owners (
                    upload_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    case_id TEXT,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS case_tombstones (
                    case_id TEXT PRIMARY KEY,
                    retired_by TEXT NOT NULL REFERENCES users(id),
                    retired_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def _db(self, *, write: bool = False):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=10000")
            with connection:
                if write:
                    connection.execute("BEGIN IMMEDIATE")
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _active(connection: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM users WHERE id=? AND active=1", (user_id,)
        ).fetchone()

    @classmethod
    def _admin(cls, connection: sqlite3.Connection, actor_id: str) -> None:
        actor = cls._active(connection, actor_id)
        if actor is None or actor["kind"] != "human" or actor["global_role"] != "admin":
            raise PermissionError("account administrator required")

    @classmethod
    def _owner(cls, connection: sqlite3.Connection, actor_id: str, case_id: str) -> None:
        if cls._active(connection, actor_id) is None:
            raise PermissionError("active case owner required")
        row = connection.execute(
            "SELECT role FROM case_grants WHERE case_id=? AND user_id=?",
            (case_id, actor_id),
        ).fetchone()
        if row is None or row["role"] != "owner":
            raise PermissionError("case owner required")

    def bootstrap_admin(self, username: str, password: str, legacy_case_ids: Iterable[str]) -> dict:
        """Seed the transitional admin and only the cases present on first init.

        Later calls never grant newly discovered cases. An explicit change to the
        configured bootstrap password rotates its hash and revokes its sessions.
        """
        username, password = _username(username), _password(password)
        legacy = sorted({_case_id(case_id) for case_id in legacy_case_ids})
        now = int(time.time())
        with self._db(write=True) as connection:
            bootstrap = connection.execute(
                "SELECT value FROM settings WHERE key='bootstrap_user_id'"
            ).fetchone()
            if bootstrap is None:
                if connection.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None:
                    raise RuntimeError("identity database has users but no bootstrap marker")
                admin_id = secrets.token_hex(16)
                salt = secrets.token_bytes(16)
                connection.execute("""
                    INSERT INTO users (id,username,password_salt,password_digest,kind,global_role,active,created_at)
                    VALUES (?,?,?,?,'human','admin',1,?)
                """, (admin_id, username, salt, _password_digest(password, salt), now))
                service_id = secrets.token_hex(16)
                connection.execute("""
                    INSERT INTO users (id,username,kind,global_role,active,created_at)
                    VALUES (?,?,'service','service',1,?)
                """, (service_id, "service-mcp", now))
                for case_id in legacy:
                    connection.execute("""
                        INSERT INTO case_grants (case_id,user_id,role,granted_by,granted_at)
                        VALUES (?,?,'owner',?,?)
                    """, (case_id, admin_id, admin_id, now))
                connection.execute(
                    "INSERT INTO settings (key,value) VALUES ('bootstrap_user_id',?)", (admin_id,)
                )
                connection.execute(
                    "INSERT INTO settings (key,value) VALUES ('mcp_user_id',?)", (service_id,)
                )
            else:
                admin_id = bootstrap["value"]
                row = connection.execute("SELECT * FROM users WHERE id=?", (admin_id,)).fetchone()
                if row is None or row["username"] != username or row["global_role"] != "admin":
                    raise RuntimeError("configured bootstrap username differs from persistent admin")
                if not hmac.compare_digest(_password_digest(password, row["password_salt"]),
                                           row["password_digest"]):
                    salt = secrets.token_bytes(16)
                    connection.execute("""
                        UPDATE users SET password_salt=?,password_digest=? WHERE id=?
                    """, (salt, _password_digest(password, salt), admin_id))
                    connection.execute(
                        "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                        (now, admin_id),
                    )
            row = connection.execute("SELECT * FROM users WHERE id=?", (admin_id,)).fetchone()
            return _public_user(row)

    def mcp_principal(self) -> dict:
        with self._db() as connection:
            row = connection.execute("""
                SELECT users.* FROM users JOIN settings ON settings.value=users.id
                WHERE settings.key='mcp_user_id' AND users.active=1
            """).fetchone()
            if row is None:
                raise RuntimeError("MCP service identity has not been bootstrapped")
            return _public_user(row)

    def authenticate(self, username: str, password: str) -> dict | None:
        if not isinstance(username, str) or not isinstance(password, str):
            return None
        with self._db() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username=? AND active=1 AND kind='human'", (username,)
            ).fetchone()
            salt = row["password_salt"] if row is not None else bytes(16)
            digest = row["password_digest"] if row is not None else bytes(32)
            if len(password.encode("utf-8")) > 1024:
                return None
            valid = hmac.compare_digest(_password_digest(password, salt), digest)
            return _public_user(row) if valid and row is not None else None

    def create_user(self, actor_id: str, username: str, password: str,
                    global_role: str = "member") -> dict:
        username, password = _username(username), _password(password)
        if global_role not in {"admin", "member"}:
            raise IdentityError("invalid global role")
        salt = secrets.token_bytes(16)
        digest = _password_digest(password, salt)
        user_id, now = secrets.token_hex(16), int(time.time())
        with self._db(write=True) as connection:
            self._admin(connection, actor_id)
            try:
                connection.execute("""
                    INSERT INTO users (id,username,password_salt,password_digest,kind,global_role,active,created_at)
                    VALUES (?,?,?,?,'human',?,1,?)
                """, (user_id, username, salt, digest, global_role, now))
            except sqlite3.IntegrityError as exc:
                raise IdentityError("username already exists") from exc
            return _public_user(connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())

    def list_users(self, actor_id: str) -> list[dict]:
        with self._db() as connection:
            self._admin(connection, actor_id)
            return [_public_user(row) for row in connection.execute(
                "SELECT * FROM users ORDER BY username"
            )]

    def disable_user(self, actor_id: str, user_id: str) -> None:
        now = int(time.time())
        with self._db(write=True) as connection:
            self._admin(connection, actor_id)
            row = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if row is None or row["kind"] != "human":
                raise IdentityError("user not found")
            if actor_id == user_id:
                raise IdentityError("cannot disable own account")
            if row["global_role"] == "admin" and row["active"]:
                count = connection.execute(
                    "SELECT COUNT(*) FROM users WHERE global_role='admin' AND active=1"
                ).fetchone()[0]
                if count <= 1:
                    raise IdentityError("cannot disable last administrator")
            connection.execute("UPDATE users SET active=0,disabled_at=? WHERE id=?", (now, user_id))
            connection.execute(
                "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                (now, user_id),
            )

    def issue_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with self._db(write=True) as connection:
            row = self._active(connection, user_id)
            if row is None or row["kind"] != "human":
                raise PermissionError("active human account required")
            connection.execute("""
                INSERT INTO sessions (token_hash,user_id,created_at,expires_at)
                VALUES (?,?,?,?)
            """, (hashlib.sha256(token.encode("ascii")).hexdigest(), user_id,
                  now, now + SESSION_SECONDS))
        return token

    def resolve_session(self, token: str | None) -> dict | None:
        if not isinstance(token, str) or not SESSION_TOKEN.fullmatch(token):
            return None
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self._db() as connection:
            row = connection.execute("""
                SELECT users.* FROM sessions JOIN users ON users.id=sessions.user_id
                WHERE sessions.token_hash=? AND sessions.revoked_at IS NULL
                  AND sessions.expires_at>? AND users.active=1 AND users.kind='human'
            """, (digest, int(time.time()))).fetchone()
            return _public_user(row) if row is not None else None

    def revoke_session(self, token: str | None) -> None:
        if not isinstance(token, str) or not SESSION_TOKEN.fullmatch(token):
            return
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self._db(write=True) as connection:
            connection.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=?",
                               (int(time.time()), digest))

    @staticmethod
    def _bind_case(connection: sqlite3.Connection, case_id: str, user_id: str) -> None:
        if connection.execute(
            "SELECT 1 FROM case_tombstones WHERE case_id=?", (case_id,)
        ).fetchone() is not None:
            raise PermissionError("retired case id cannot be reused")
        existing = connection.execute(
            "SELECT user_id,role FROM case_grants WHERE case_id=?", (case_id,)
        ).fetchall()
        if existing:
            owners = [row["user_id"] for row in existing if row["role"] == "owner"]
            if owners == [user_id]:
                return
            raise PermissionError("case already has an owner or grants")
        connection.execute("""
            INSERT INTO case_grants (case_id,user_id,role,granted_by,granted_at)
            VALUES (?,?,'owner',?,?)
        """, (case_id, user_id, user_id, int(time.time())))

    def bind_case_owner(self, case_id: str, user_id: str) -> None:
        case_id = _case_id(case_id)
        with self._db(write=True) as connection:
            actor = self._active(connection, user_id)
            if actor is None or actor["kind"] != "human":
                raise PermissionError("active human account required")
            self._bind_case(connection, case_id, user_id)

    def grant_case(self, actor_id: str, case_id: str, user_id: str, role: str) -> None:
        case_id = _case_id(case_id)
        if role not in CASE_ROLES:
            raise IdentityError("invalid case role")
        with self._db(write=True) as connection:
            self._owner(connection, actor_id, case_id)
            if self._active(connection, user_id) is None:
                raise IdentityError("target user is unavailable")
            if actor_id == user_id and role != "owner":
                raise IdentityError("owner cannot downgrade own grant")
            connection.execute("""
                INSERT INTO case_grants (case_id,user_id,role,granted_by,granted_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(case_id,user_id) DO UPDATE SET
                    role=excluded.role,granted_by=excluded.granted_by,granted_at=excluded.granted_at
            """, (case_id, user_id, role, actor_id, int(time.time())))

    def revoke_case_grant(self, actor_id: str, case_id: str, user_id: str) -> None:
        case_id = _case_id(case_id)
        with self._db(write=True) as connection:
            self._owner(connection, actor_id, case_id)
            if actor_id == user_id:
                raise IdentityError("owner cannot revoke own grant")
            connection.execute("DELETE FROM case_grants WHERE case_id=? AND user_id=?",
                               (case_id, user_id))

    def retire_case(self, actor_id: str, case_id: str) -> None:
        """Remove every grant after case deletion and permanently retire its ID.

        The gateway must first delete the case's files, then call this method
        with the authenticated owner ID. A restored case cannot inherit old
        grants or claim the same ID as a newly uploaded case.
        """
        case_id = _case_id(case_id)
        with self._db(write=True) as connection:
            self._owner(connection, actor_id, case_id)
            connection.execute(
                "INSERT INTO case_tombstones (case_id,retired_by,retired_at) VALUES (?,?,?)",
                (case_id, actor_id, int(time.time())),
            )
            connection.execute("DELETE FROM upload_owners WHERE case_id=?", (case_id,))
            connection.execute("DELETE FROM case_grants WHERE case_id=?", (case_id,))

    def revoke_case_collaborators(self, actor_id: str, case_id: str) -> None:
        """Remove every other grant while retaining the owner for delete retry."""
        case_id = _case_id(case_id)
        with self._db(write=True) as connection:
            self._owner(connection, actor_id, case_id)
            connection.execute(
                "DELETE FROM case_grants WHERE case_id=? AND user_id<>?", (case_id, actor_id),
            )

    def list_case_grants(self, actor_id: str, case_id: str) -> list[dict]:
        """Return a case's grants to its active owner, without credentials."""
        case_id = _case_id(case_id)
        with self._db() as connection:
            self._owner(connection, actor_id, case_id)
            return [
                {
                    "id": row["id"],
                    "username": row["username"],
                    "role": row["role"],
                    "active": bool(row["active"]),
                }
                for row in connection.execute("""
                    SELECT users.id, users.username, users.active, case_grants.role
                    FROM case_grants JOIN users ON users.id=case_grants.user_id
                    WHERE case_grants.case_id=? ORDER BY users.username
                """, (case_id,))
            ]

    def case_role(self, user_id: str, case_id: str) -> str | None:
        case_id = _case_id(case_id)
        with self._db() as connection:
            if self._active(connection, user_id) is None:
                return None
            row = connection.execute("""
                SELECT role FROM case_grants WHERE case_id=? AND user_id=?
            """, (case_id, user_id)).fetchone()
            return row["role"] if row is not None else None

    def authorize_case(self, user_id: str, case_id: str, capability: str) -> bool:
        if capability not in CAPABILITIES:
            raise IdentityError("invalid case capability")
        return self.case_role(user_id, case_id) in CAPABILITIES[capability]

    def allowed_case_ids(self, user_id: str, case_ids: Iterable[str],
                         capability: str = "view") -> list[str]:
        if capability not in CAPABILITIES:
            raise IdentityError("invalid case capability")
        requested = [_case_id(case_id) for case_id in case_ids]
        with self._db() as connection:
            if self._active(connection, user_id) is None:
                return []
            roles = {row["case_id"]: row["role"] for row in connection.execute(
                "SELECT case_id,role FROM case_grants WHERE user_id=?", (user_id,)
            )}
        return [case_id for case_id in requested if roles.get(case_id) in CAPABILITIES[capability]]

    def bind_upload_owner(self, upload_id: str, user_id: str) -> None:
        upload_id = _upload_id(upload_id)
        with self._db(write=True) as connection:
            actor = self._active(connection, user_id)
            if actor is None or actor["kind"] != "human":
                raise PermissionError("active human account required")
            existing = connection.execute(
                "SELECT user_id FROM upload_owners WHERE upload_id=?", (upload_id,)
            ).fetchone()
            if existing is not None:
                if existing["user_id"] != user_id:
                    raise PermissionError("upload belongs to another user")
                return
            connection.execute("""
                INSERT INTO upload_owners (upload_id,user_id,created_at) VALUES (?,?,?)
            """, (upload_id, user_id, int(time.time())))

    def authorize_upload(self, user_id: str, upload_id: str) -> bool:
        upload_id = _upload_id(upload_id)
        with self._db() as connection:
            if self._active(connection, user_id) is None:
                return False
            row = connection.execute(
                "SELECT user_id FROM upload_owners WHERE upload_id=?", (upload_id,)
            ).fetchone()
            return row is not None and row["user_id"] == user_id

    def forget_unfinished_upload(self, upload_id: str, user_id: str) -> bool:
        """Release a cancelled/expired session without losing a committed case receipt."""
        upload_id = _upload_id(upload_id)
        with self._db(write=True) as connection:
            deleted = connection.execute(
                "DELETE FROM upload_owners WHERE upload_id=? AND user_id=? AND case_id IS NULL",
                (upload_id, user_id),
            )
            return deleted.rowcount > 0

    def prune_missing_unfinished_uploads(self, root: Path) -> int:
        """Release unfinished receipts whose staging was reaped before a retry."""
        if not root.is_dir():
            raise OSError("upload staging root unavailable")
        with self._db() as connection:
            upload_ids = [row["upload_id"] for row in connection.execute(
                "SELECT upload_id FROM upload_owners WHERE case_id IS NULL"
            )]
        missing = [upload_id for upload_id in upload_ids
                   if not (root / (".windcatcher-chunks-" + upload_id)).is_dir()]
        if not missing:
            return 0
        with self._db(write=True) as connection:
            return sum(connection.execute(
                "DELETE FROM upload_owners WHERE upload_id=? AND case_id IS NULL",
                (upload_id,),
            ).rowcount for upload_id in missing)

    def finish_upload(self, upload_id: str, case_id: str, user_id: str) -> None:
        """Atomically connect an owned upload to a newly registered case."""
        upload_id, case_id = _upload_id(upload_id), _case_id(case_id)
        with self._db(write=True) as connection:
            if self._active(connection, user_id) is None:
                raise PermissionError("active user required")
            row = connection.execute(
                "SELECT user_id,case_id FROM upload_owners WHERE upload_id=?", (upload_id,)
            ).fetchone()
            if row is None or row["user_id"] != user_id:
                raise PermissionError("upload belongs to another user")
            if row["case_id"] is not None and row["case_id"] != case_id:
                raise PermissionError("upload already completed for another case")
            self._bind_case(connection, case_id, user_id)
            connection.execute("UPDATE upload_owners SET case_id=? WHERE upload_id=?",
                               (case_id, upload_id))

    def upload_case_id(self, user_id: str, upload_id: str) -> str | None:
        upload_id = _upload_id(upload_id)
        with self._db() as connection:
            if self._active(connection, user_id) is None:
                return None
            row = connection.execute(
                "SELECT case_id FROM upload_owners WHERE upload_id=? AND user_id=?",
                (upload_id, user_id),
            ).fetchone()
            return row["case_id"] if row is not None else None
