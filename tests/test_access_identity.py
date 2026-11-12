"""Independent account, session and case authorization storage tests."""

from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from pipeline import access_identity


ADMIN_PASSWORD = "test-admin-password-over-16"
MEMBER_PASSWORD = "test-member-password-over-16"
OLD_CASE = "case-20261101-120000-abcd"
NEW_CASE = "case-20261102-120000-abcd"
UPLOAD = "a" * 32


@pytest.fixture
def store(tmp_path):
    identity = access_identity.IdentityStore(tmp_path / "identity")
    admin = identity.bootstrap_admin("researcher", ADMIN_PASSWORD, [OLD_CASE])
    return identity, admin


def test_bootstrap_owns_only_first_inventory_and_mcp_starts_without_grants(store):
    identity, admin = store
    assert admin["global_role"] == "admin"
    assert identity.authorize_case(admin["id"], OLD_CASE, "manage")
    assert not identity.authorize_case(admin["id"], NEW_CASE, "view")
    service = identity.mcp_principal()
    assert service["kind"] == "service"
    assert not identity.authorize_case(service["id"], OLD_CASE, "view")
    assert identity.bootstrap_admin("researcher", ADMIN_PASSWORD, [NEW_CASE]) == admin
    assert not identity.authorize_case(admin["id"], NEW_CASE, "view")


def test_password_hash_sessions_and_restart_do_not_store_raw_secrets(store):
    identity, admin = store
    assert identity.authenticate("researcher", ADMIN_PASSWORD) == admin
    assert identity.authenticate("researcher", "wrong-password") is None
    token = identity.issue_session(admin["id"])
    assert identity.resolve_session(token) == admin
    with sqlite3.connect(identity.path) as connection:
        password_hash, salt = connection.execute(
            "SELECT password_digest,password_salt FROM users WHERE id=?", (admin["id"],)
        ).fetchone()
        session_hash = connection.execute("SELECT token_hash FROM sessions").fetchone()[0]
        assert password_hash != ADMIN_PASSWORD.encode()
        assert len(salt) == 16 and len(password_hash) == 32
        assert session_hash == hashlib.sha256(token.encode()).hexdigest()
        assert token not in identity.path.read_bytes().decode("latin-1")
    restarted = access_identity.IdentityStore(identity.directory)
    assert restarted.resolve_session(token) == admin
    restarted.revoke_session(token)
    assert identity.resolve_session(token) is None


def test_bootstrap_password_rotation_revokes_previous_sessions(store):
    identity, admin = store
    token = identity.issue_session(admin["id"])
    new_password = "another-long-password-2026"
    assert identity.bootstrap_admin("researcher", new_password, [NEW_CASE]) == admin
    assert identity.authenticate("researcher", ADMIN_PASSWORD) is None
    assert identity.authenticate("researcher", new_password) == admin
    assert identity.resolve_session(token) is None
    assert not identity.authorize_case(admin["id"], NEW_CASE, "view")


def test_member_has_no_implicit_cases_and_roles_are_distinct(store):
    identity, admin = store
    member = identity.create_user(admin["id"], "member-a", MEMBER_PASSWORD)
    assert member["global_role"] == "member"
    assert identity.authenticate("member-a", MEMBER_PASSWORD) == member
    assert not identity.authorize_case(member["id"], OLD_CASE, "view")
    identity.grant_case(admin["id"], OLD_CASE, member["id"], "viewer")
    assert identity.authorize_case(member["id"], OLD_CASE, "view")
    assert not identity.authorize_case(member["id"], OLD_CASE, "edit")
    assert not identity.authorize_case(member["id"], OLD_CASE, "review")
    identity.grant_case(admin["id"], OLD_CASE, member["id"], "editor")
    assert identity.authorize_case(member["id"], OLD_CASE, "edit")
    assert not identity.authorize_case(member["id"], OLD_CASE, "review")
    identity.grant_case(admin["id"], OLD_CASE, member["id"], "reviewer")
    assert identity.authorize_case(member["id"], OLD_CASE, "review")
    assert identity.authorize_case(member["id"], OLD_CASE, "share")
    assert not identity.authorize_case(member["id"], OLD_CASE, "edit")
    assert not identity.authorize_case(member["id"], OLD_CASE, "manage")
    assert identity.allowed_case_ids(member["id"], [NEW_CASE, OLD_CASE]) == [OLD_CASE]


def test_global_admin_does_not_bypass_case_grants_and_member_cannot_manage(store):
    identity, admin = store
    other_admin = identity.create_user(admin["id"], "admin-b", MEMBER_PASSWORD, "admin")
    assert identity.list_users(admin["id"])[0]["global_role"] == "admin"
    assert not identity.authorize_case(other_admin["id"], OLD_CASE, "view")
    with pytest.raises(PermissionError):
        identity.grant_case(other_admin["id"], OLD_CASE, other_admin["id"], "owner")
    member = identity.create_user(admin["id"], "member-a", MEMBER_PASSWORD)
    with pytest.raises(PermissionError):
        identity.create_user(member["id"], "another-user", MEMBER_PASSWORD)
    with pytest.raises(PermissionError):
        identity.grant_case(member["id"], OLD_CASE, other_admin["id"], "viewer")
    with pytest.raises(PermissionError):
        identity.list_users(member["id"])


def test_service_account_is_listed_but_cannot_be_disabled(store):
    identity, admin = store
    service = identity.mcp_principal()
    users = identity.list_users(admin["id"])
    assert service in users
    assert service["kind"] == "service"
    assert service["global_role"] == "service"
    with pytest.raises(access_identity.IdentityError):
        identity.disable_user(admin["id"], service["id"])
    assert identity.mcp_principal() == service


def test_new_case_owner_is_bound_explicitly_and_cannot_be_claimed_again(store):
    identity, admin = store
    owner = identity.create_user(admin["id"], "new-owner", MEMBER_PASSWORD)
    identity.bind_case_owner(NEW_CASE, owner["id"])
    identity.bind_case_owner(NEW_CASE, owner["id"])
    assert identity.authorize_case(owner["id"], NEW_CASE, "manage")
    assert not identity.authorize_case(admin["id"], NEW_CASE, "view")
    with pytest.raises(PermissionError):
        identity.bind_case_owner(NEW_CASE, admin["id"])
    with pytest.raises(PermissionError):
        identity.grant_case(admin["id"], NEW_CASE, admin["id"], "owner")


def test_disable_revokes_session_and_case_access(store):
    identity, admin = store
    member = identity.create_user(admin["id"], "member-a", MEMBER_PASSWORD)
    identity.grant_case(admin["id"], OLD_CASE, member["id"], "viewer")
    token = identity.issue_session(member["id"])
    identity.disable_user(admin["id"], member["id"])
    assert identity.resolve_session(token) is None
    assert identity.authenticate("member-a", MEMBER_PASSWORD) is None
    assert not identity.authorize_case(member["id"], OLD_CASE, "view")
    with pytest.raises(PermissionError):
        identity.issue_session(member["id"])
    with pytest.raises(access_identity.IdentityError):
        identity.disable_user(admin["id"], admin["id"])


def test_upload_ids_are_owned_and_completion_binds_resulting_case(store):
    identity, admin = store
    owner = identity.create_user(admin["id"], "member-a", MEMBER_PASSWORD)
    other = identity.create_user(admin["id"], "member-b", MEMBER_PASSWORD)
    identity.bind_upload_owner(UPLOAD, owner["id"])
    identity.bind_upload_owner(UPLOAD, owner["id"])
    assert identity.authorize_upload(owner["id"], UPLOAD)
    assert not identity.authorize_upload(other["id"], UPLOAD)
    with pytest.raises(PermissionError):
        identity.bind_upload_owner(UPLOAD, other["id"])
    with pytest.raises(PermissionError):
        identity.finish_upload(UPLOAD, NEW_CASE, other["id"])
    identity.finish_upload(UPLOAD, NEW_CASE, owner["id"])
    identity.finish_upload(UPLOAD, NEW_CASE, owner["id"])
    assert identity.upload_case_id(owner["id"], UPLOAD) == NEW_CASE
    assert identity.upload_case_id(other["id"], UPLOAD) is None
    assert identity.authorize_case(owner["id"], NEW_CASE, "manage")
    with pytest.raises(PermissionError):
        identity.finish_upload(UPLOAD, OLD_CASE, owner["id"])


@pytest.mark.parametrize("granted_role", ["viewer", "editor"])
def test_completed_upload_retry_accepts_other_grants_but_not_another_owner(store, granted_role):
    identity, admin = store
    owner = identity.create_user(admin["id"], "member-a", MEMBER_PASSWORD)
    viewer = identity.create_user(admin["id"], "member-b", MEMBER_PASSWORD)
    identity.bind_upload_owner(UPLOAD, owner["id"])
    identity.finish_upload(UPLOAD, NEW_CASE, owner["id"])
    identity.grant_case(owner["id"], NEW_CASE, viewer["id"], granted_role)
    identity.finish_upload(UPLOAD, NEW_CASE, owner["id"])
    assert identity.upload_case_id(owner["id"], UPLOAD) == NEW_CASE
    identity.grant_case(owner["id"], NEW_CASE, viewer["id"], "owner")
    with pytest.raises(PermissionError):
        identity.finish_upload(UPLOAD, NEW_CASE, owner["id"])


def test_owner_may_grant_mcp_explicit_case_scope(store):
    identity, admin = store
    service = identity.mcp_principal()
    assert not identity.authorize_case(service["id"], OLD_CASE, "view")
    identity.grant_case(admin["id"], OLD_CASE, service["id"], "viewer")
    assert identity.authorize_case(service["id"], OLD_CASE, "view")
    assert not identity.authorize_case(service["id"], OLD_CASE, "edit")
    identity.revoke_case_grant(admin["id"], OLD_CASE, service["id"])
    assert not identity.authorize_case(service["id"], OLD_CASE, "view")


def test_invalid_ids_and_unknown_user_fail_closed(store):
    identity, admin = store
    with pytest.raises(access_identity.IdentityError):
        identity.bind_case_owner("../private", admin["id"])
    with pytest.raises(access_identity.IdentityError):
        identity.bind_upload_owner("not-an-upload-id", admin["id"])
    with pytest.raises(access_identity.IdentityError):
        identity.grant_case(admin["id"], OLD_CASE, admin["id"], "doctor")
    with pytest.raises(access_identity.IdentityError):
        identity.authorize_case(admin["id"], OLD_CASE, "diagnose")
    assert identity.resolve_session("forged-token") is None
    assert identity.case_role("missing-user", OLD_CASE) is None


def test_duplicate_username_and_grant_revocation_are_visible(store):
    identity, admin = store
    member = identity.create_user(admin["id"], "member-a", MEMBER_PASSWORD)
    with pytest.raises(access_identity.IdentityError, match="already exists"):
        identity.create_user(admin["id"], "member-a", MEMBER_PASSWORD)
    identity.grant_case(admin["id"], OLD_CASE, member["id"], "viewer")
    identity.revoke_case_grant(admin["id"], OLD_CASE, member["id"])
    assert not identity.authorize_case(member["id"], OLD_CASE, "view")
    with pytest.raises(access_identity.IdentityError):
        identity.revoke_case_grant(admin["id"], OLD_CASE, admin["id"])


def test_only_owner_can_list_case_grants_without_credentials(store):
    identity, admin = store
    viewer = identity.create_user(admin["id"], "viewer-a", MEMBER_PASSWORD)
    identity.grant_case(admin["id"], OLD_CASE, viewer["id"], "viewer")
    identity.disable_user(admin["id"], viewer["id"])
    grants = identity.list_case_grants(admin["id"], OLD_CASE)
    assert grants == [
        {"id": admin["id"], "username": "researcher", "role": "owner", "active": True},
        {"id": viewer["id"], "username": "viewer-a", "role": "viewer", "active": False},
    ]
    assert all(set(grant) == {"id", "username", "role", "active"} for grant in grants)
    with pytest.raises(PermissionError):
        identity.list_case_grants(viewer["id"], OLD_CASE)
    other_admin = identity.create_user(admin["id"], "admin-b", MEMBER_PASSWORD, "admin")
    with pytest.raises(PermissionError):
        identity.list_case_grants(other_admin["id"], OLD_CASE)


def test_retire_case_clears_every_grant_and_upload_link_across_restart(store):
    identity, admin = store
    viewer = identity.create_user(admin["id"], "viewer-a", MEMBER_PASSWORD)
    identity.grant_case(admin["id"], OLD_CASE, viewer["id"], "viewer")
    identity.bind_upload_owner(UPLOAD, admin["id"])
    identity.finish_upload(UPLOAD, OLD_CASE, admin["id"])
    identity.retire_case(admin["id"], OLD_CASE)

    assert not identity.authorize_case(admin["id"], OLD_CASE, "manage")
    assert not identity.authorize_case(viewer["id"], OLD_CASE, "view")
    assert not identity.authorize_upload(admin["id"], UPLOAD)
    assert identity.upload_case_id(admin["id"], UPLOAD) is None
    restarted = access_identity.IdentityStore(identity.directory)
    restarted.bootstrap_admin("researcher", ADMIN_PASSWORD, [OLD_CASE])
    assert not restarted.authorize_case(admin["id"], OLD_CASE, "view")
    with pytest.raises(PermissionError, match="retired case id"):
        restarted.bind_case_owner(OLD_CASE, admin["id"])
    restarted.bind_case_owner(NEW_CASE, admin["id"])
    assert restarted.authorize_case(admin["id"], NEW_CASE, "manage")


def test_non_owner_cannot_retire_case_or_remove_grants(store):
    identity, admin = store
    viewer = identity.create_user(admin["id"], "viewer-a", MEMBER_PASSWORD)
    other_admin = identity.create_user(admin["id"], "admin-b", MEMBER_PASSWORD, "admin")
    identity.grant_case(admin["id"], OLD_CASE, viewer["id"], "viewer")
    for actor_id in (viewer["id"], other_admin["id"], "missing-user"):
        with pytest.raises(PermissionError):
            identity.retire_case(actor_id, OLD_CASE)
    assert identity.authorize_case(admin["id"], OLD_CASE, "manage")
    assert identity.authorize_case(viewer["id"], OLD_CASE, "view")


def test_bootstrap_and_case_claim_are_serialized_between_connections(tmp_path):
    identity = access_identity.IdentityStore(tmp_path / "concurrent-identity")
    with ThreadPoolExecutor(max_workers=2) as pool:
        admins = list(pool.map(
            lambda _: identity.bootstrap_admin("researcher", ADMIN_PASSWORD, [OLD_CASE]),
            range(2),
        ))
    assert admins[0] == admins[1]
    assert identity.authorize_case(admins[0]["id"], OLD_CASE, "manage")
    first = identity.create_user(admins[0]["id"], "first-owner", MEMBER_PASSWORD)
    second = identity.create_user(admins[0]["id"], "second-owner", MEMBER_PASSWORD)

    def claim(user_id):
        try:
            identity.bind_case_owner(NEW_CASE, user_id)
            return True
        except PermissionError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, (first["id"], second["id"])))
    assert sorted(outcomes) == [False, True]
    assert sum(identity.authorize_case(user["id"], NEW_CASE, "manage")
               for user in (first, second)) == 1


def test_expired_session_cannot_be_resolved(store):
    identity, admin = store
    token = identity.issue_session(admin["id"])
    with sqlite3.connect(identity.path) as connection:
        connection.execute("UPDATE sessions SET expires_at=0")
    assert identity.resolve_session(token) is None
