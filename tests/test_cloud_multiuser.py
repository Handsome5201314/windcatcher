"""End-to-end authorization tests for the opt-in personal-account gateway."""
import hashlib
import json
import time

import pytest
from fastapi.testclient import TestClient

import cloud
import server
import pipeline.cases as cases
import pipeline.config as config
from pipeline import activity_monitor, ai_report, endplane_history, patient_shares, reference_cases


LEGACY = "case-20261102-000000-0001"
OTHER = "case-20261102-000001-0002"
ADMIN_PASSWORD = "test-admin-password-very-long"
MEMBER_PASSWORD = "test-member-password-very-long"


def _stub_case(root, case_id):
    folder = root / case_id
    folder.mkdir()
    (folder / "case.json").write_text(json.dumps({
        "case_id": case_id, "created_at": "2026-11-02T00:00:00+08:00",
        "source_type": "synthetic", "synthetic": True, "stages": {},
    }), encoding="utf-8")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "cases"
    root.mkdir()
    monkeypatch.setattr(ai_report, "CONFIG_PATH", tmp_path / "ai-report-config.json")
    monkeypatch.setattr(ai_report, "_USER_SESSION_KEYS", {})
    monkeypatch.setattr(ai_report, "_SESSION_API_KEY", "")
    monkeypatch.setattr(ai_report, "_SESSION_API_BASE_URL", "")
    monkeypatch.delenv(ai_report.API_KEY_ENV, raising=False)
    monkeypatch.delenv("WINDCATCHER_AI_API_URL", raising=False)
    monkeypatch.delenv("WINDCATCHER_AI_MODEL", raising=False)
    monkeypatch.setenv(ai_report.ALLOWED_HOSTS_ENV,
                       "personal.example,bootstrap.example,second.example")
    for module in (server, cases, config):
        monkeypatch.setattr(module, "CASES_DIR", root)
    monkeypatch.setattr(patient_shares, "SHARE_DIR", tmp_path / "shares")
    monkeypatch.setenv("WINDCATCHER_IDENTITY_DIR", str(tmp_path / "identity"))
    monkeypatch.setenv("WINDCATCHER_MULTIUSER", "1")
    monkeypatch.setenv("WINDCATCHER_DEPLOYMENT", "cloud")
    monkeypatch.setenv("WINDCATCHER_CLOUD_USERNAME", "admin-test")
    monkeypatch.setenv("WINDCATCHER_CLOUD_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("WINDCATCHER_CLOUD_SESSION_SECRET", "test-session-secret-long-enough-32-chars")
    monkeypatch.delenv("WINDCATCHER_MCP_TOKEN", raising=False)
    _stub_case(root, LEGACY)
    # No lifespan: do not start the workstation's CFD worker.
    app = cloud.create_app()
    admin = TestClient(app, base_url="https://windcatcher.test")
    member = TestClient(app, base_url="https://windcatcher.test")
    assert admin.post("/login", data={"username": "admin-test", "password": ADMIN_PASSWORD},
                      follow_redirects=False).status_code == 303
    identity = app.state.identity
    admin_id = identity.authenticate("admin-test", ADMIN_PASSWORD)["id"]
    member_id = identity.create_user(admin_id, "member-test", MEMBER_PASSWORD)["id"]
    assert member.post("/login", data={"username": "member-test", "password": MEMBER_PASSWORD},
                       follow_redirects=False).status_code == 303
    return app, admin, member, admin_id, member_id, root


def _ids(client):
    response = client.get("/api/cases")
    assert response.status_code == 200, response.text
    return [row["case_id"] for row in response.json()]


def test_orchestration_batch_requires_case_editor_and_hides_owner(workspace, monkeypatch):
    app, admin, member, admin_id, member_id, _root = workspace
    batch_id = "batch-" + "a" * 16
    stored = {}

    class Queue:
        def health(self):
            return {"worker_alive": True}

        def submit(self, key, items):
            stored["key"] = key
            stored["items"] = items
            return self.get(batch_id)

        def get(self, _batch_id):
            return {"batch_id": batch_id, "status": "queued",
                    "counts": {"queued": 1}, "poll_after_seconds": 60,
                    "items": [{"item_id": 1, "input": stored["items"][0],
                               "status": "queued", "result": None, "updated_at": "now"}]}

    monkeypatch.setattr(server.app.state, "orchestration_queue", Queue(), raising=False)
    body = {"request_key": "synthetic-gate", "case_ids": [LEGACY], "use_harness": False}
    assert member.post("/api/orchestration/batches", json=body).status_code == 404
    submitted = admin.post("/api/orchestration/batches", json=body)
    assert submitted.status_code == 202, submitted.text
    assert submitted.json()["items"][0]["input"] == {
        "case_id": LEGACY, "use_harness": False, "retry_failed": False}
    assert stored["key"] == f"{admin_id}:synthetic-gate"
    assert member.get(f"/api/orchestration/batches/{batch_id}").status_code == 404
    assert admin.get(f"/api/orchestration/batches/{batch_id}").status_code == 200
    app.state.identity.grant_case(admin_id, LEGACY, member_id, "editor")
    # A collaborator on the case still cannot read another actor's batch.
    assert member.get(f"/api/orchestration/batches/{batch_id}").status_code == 404


def _seed_public_outputs(case_id):
    token = "S" * 43
    share_id = "f" * 32
    reference_id = "e" * 32
    now = int(time.time())
    with patient_shares._db() as db:
        db.execute("INSERT INTO shares VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            share_id, case_id, hashlib.sha256(token.encode()).hexdigest(), b"salt", b"hash",
            now, now + 3600, None, "reviewer", "synthetic review", "signature",
            "digest", "{}", "{}", "{}",
        ))
    payload = b'{"synthetic":true}'
    digest = hashlib.sha256(payload).hexdigest()
    snapshot = reference_cases.snapshot_path(digest)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_bytes(payload)
    with reference_cases._db() as db:
        db.execute("INSERT INTO reference_cases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            reference_id, case_id, "合成参考", "合成测试用", "[]", "published",
            "anatomy", "reviewer", "synthetic review", 1, 1, digest, "{}",
            "signature", now, now,
        ))
    return token, reference_id


def test_legacy_migration_and_global_admin_cannot_cross_case_grants(workspace):
    app, admin, member, _, member_id, root = workspace
    assert _ids(admin) == [LEGACY]
    assert _ids(member) == []
    assert member.get(f"/api/cases/{LEGACY}").status_code in {403, 404}
    for suffix in ("/model", "/report", "/artifact?name=case.json", "/segment/endplanes"):
        assert member.get(f"/api/cases/{LEGACY}{suffix}").status_code in {403, 404}
    _stub_case(root, OTHER)
    app.state.identity.bind_case_owner(OTHER, member_id)
    assert OTHER in _ids(member)
    assert OTHER not in _ids(admin)
    assert admin.get(f"/api/cases/{OTHER}").status_code in {403, 404}


def test_accounts_and_case_grants_survive_gateway_restart(workspace):
    _, admin, _, _, member_id, _ = workspace
    assert admin.put(f"/api/cases/{LEGACY}/grants/{member_id}",
                     json={"role": "viewer"}).status_code == 200
    restarted_app = cloud.create_app()
    restarted_member = TestClient(restarted_app, base_url="https://windcatcher.test")
    login = restarted_member.post("/login", data={
        "username": "member-test", "password": MEMBER_PASSWORD,
    }, follow_redirects=False)
    assert login.status_code == 303 and cloud.COOKIE in login.headers["set-cookie"]
    assert _ids(restarted_member) == [LEGACY]
    assert restarted_member.get(f"/api/cases/{LEGACY}").status_code == 200
    assert restarted_app.state.identity.case_role(member_id, LEGACY) == "viewer"


def test_missing_identity_database_cannot_rebootstrap_existing_case_store(workspace):
    app, _, _, _, _, root = workspace
    database = app.state.identity.path
    assert database.is_file() and (root / LEGACY / "case.json").is_file()
    database.unlink()
    # The persisted enablement marker must prevent all old cases from being
    # silently re-owned by a newly bootstrapped account database.
    with pytest.raises(RuntimeError):
        cloud.create_app()


def test_enabled_case_store_cannot_fall_back_to_shared_login(workspace, monkeypatch):
    _, _, _, _, _, root = workspace
    assert (root / LEGACY / "case.json").is_file()
    monkeypatch.setenv("WINDCATCHER_MULTIUSER", "0")
    with pytest.raises(RuntimeError):
        cloud.create_app()


def test_owner_grants_view_without_review_or_management(workspace):
    _, admin, member, _, member_id, _ = workspace
    path = f"/api/cases/{LEGACY}"
    response = admin.put(f"{path}/grants/{member_id}", json={"role": "viewer"})
    assert response.status_code == 200, response.text
    assert LEGACY in _ids(member)
    assert member.get(path).status_code == 200
    assert member.get(f"{path}/grants").status_code in {403, 404}
    assert member.post(f"{path}/review-events", json={
        "operator": "forged-reviewer", "stage": "segment", "action": "confirm",
        "reason": "synthetic review test",
    }).status_code in {403, 404}
    assert admin.delete(f"{path}/grants/{member_id}").status_code == 200
    assert member.get(path).status_code in {403, 404}


def test_public_gateway_delete_override_revokes_only_owned_grant(workspace):
    app, admin, member, _, member_id, _ = workspace
    case_path = f"/api/cases/{LEGACY}"
    assert admin.put(f"{case_path}/grants/{member_id}",
                     json={"role": "viewer"}).status_code == 200
    denied = member.post(f"{case_path}/grants/{member_id}",
                         headers={"X-Windcatcher-Method": "DELETE"})
    assert denied.status_code in {403, 404}
    assert app.state.identity.case_role(member_id, LEGACY) == "viewer"
    revoked = admin.post(f"{case_path}/grants/{member_id}",
                         headers={"X-Windcatcher-Method": "DELETE"})
    assert revoked.status_code == 200, revoked.text
    assert app.state.identity.case_role(member_id, LEGACY) is None
    assert member.get(case_path).status_code in {403, 404}
    assert admin.post(f"{case_path}/review-events",
                      headers={"X-Windcatcher-Method": "DELETE"}).status_code == 400


def test_personal_preferences_bind_to_authenticated_user_and_reject_stale_writes(workspace):
    _, admin, member, admin_id, member_id, _ = workspace
    from pipeline.operator_preferences import DEFAULT_PREFERENCES

    initial = member.get("/api/operator/preferences")
    assert initial.status_code == 200, initial.text
    assert initial.json() == {"user_id": member_id, "schema_version": 1,
                              "version": 0, "preferences": DEFAULT_PREFERENCES}
    changed = {**DEFAULT_PREFERENCES, "favorite_steps": ["segment", "endplanes"]}
    saved = member.put("/api/operator/preferences", json={
        "expected_version": 0, "preferences": changed,
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 1
    assert admin.get("/api/operator/preferences").json()["user_id"] == admin_id
    assert admin.get("/api/operator/preferences").json()["version"] == 0
    assert member.put("/api/operator/preferences", json={
        "expected_version": 0, "preferences": DEFAULT_PREFERENCES,
    }).status_code == 409
    assert member.put("/api/operator/preferences", json={
        "expected_version": 1, "preferences": changed, "user_id": admin_id,
    }).status_code == 422
    assert member.put("/api/operator/preferences", json={
        "expected_version": 1, "preferences": {**changed, "case_id": LEGACY},
    }).status_code == 422
    assert member.get("/api/operator/preferences").json()["version"] == 1


def test_private_recipe_api_uses_session_owner_and_never_executes_tools(workspace):
    _, admin, member, admin_id, member_id, _ = workspace
    spec = {
        "suggested_steps": ["qc", "endplanes"],
        "allowed_tools": ["service_status"],
        "trigger_condition": "manual",
    }
    assert member.get("/api/operator/recipes").json() == {"recipes": []}
    created = member.post("/api/operator/recipes", json={"spec": spec})
    assert created.status_code == 201, created.text
    row = created.json()
    recipe_id = row["recipe_id"]
    assert row["owner_id"] == member_id and row["version"] == 1
    assert member.get(f"/api/operator/recipes/{recipe_id}").json() == row
    assert admin.get("/api/operator/recipes").json() == {"recipes": []}
    assert admin.get(f"/api/operator/recipes/{recipe_id}").status_code == 404
    assert admin.put(f"/api/operator/recipes/{recipe_id}", json={
        "expected_version": 1, "spec": spec,
    }).status_code == 404
    assert admin.post(f"/api/operator/recipes/{recipe_id}/revoke", json={
        "expected_version": 1,
    }).status_code == 404
    assert member.post("/api/operator/recipes", json={
        "spec": spec, "owner_id": admin_id,
    }).status_code == 422
    assert member.post("/api/operator/recipes", json={
        "spec": {**spec, "allowed_tools": ["submit_cfd_batch"]},
    }).status_code == 422
    changed = {**spec, "trigger_condition": "numeric_gate_failed"}
    updated = member.put(f"/api/operator/recipes/{recipe_id}", json={
        "expected_version": 1, "spec": changed,
    })
    assert updated.status_code == 200 and updated.json()["version"] == 2
    assert member.put(f"/api/operator/recipes/{recipe_id}", json={
        "expected_version": 1, "spec": spec,
    }).status_code == 409
    revoked = member.post(f"/api/operator/recipes/{recipe_id}/revoke", json={
        "expected_version": 2,
    })
    assert revoked.status_code == 200 and revoked.json()["revoked"] is True
    assert member.put(f"/api/operator/recipes/{recipe_id}", json={
        "expected_version": 3, "spec": spec,
    }).status_code == 409
    assert member.get("/api/operator/recipes").json()["recipes"] == [revoked.json()]


def test_recipe_candidates_require_three_currently_authorized_cases(workspace, monkeypatch, tmp_path):
    app, admin, member, admin_id, member_id, root = workspace
    assert member.get("/api/operator/preferences").json()["user_id"] == member_id
    monkeypatch.setenv(activity_monitor.DIRECTORY_ENV, str(tmp_path / "monitor"))
    second = "case-20261102-000002-0003"
    third = "case-20261102-000003-0004"
    for case_id in (second, third):
        _stub_case(root, case_id)
        app.state.identity.bind_case_owner(case_id, member_id)
    assert admin.put(f"/api/cases/{LEGACY}/grants/{member_id}",
                     json={"role": "viewer"}).status_code == 200
    events = [{"kind": "stage", "action": "action_finished", "case_id": case_id,
               "session_id": admin_id, "details": {"stage": stage, "status": "ok"}}
              for case_id in (LEGACY, second, third) for stage in ("qc", "segment")]
    ingested = member.post("/api/monitor/events", json={"events": events})
    assert ingested.status_code == 200, ingested.text
    session_ids = [row["session_id"] for row in activity_monitor.query_events(kind="stage")["events"]]
    assert session_ids == [member_id] * 6
    signals = activity_monitor.query_recipe_signals(member_id)
    assert len(signals) == 6
    result = member.get("/api/operator/recipe-candidates")
    assert result.status_code == 200, result.text
    assert result.json() == {"candidates": [{"suggested_steps": ["qc", "segment"],
                                              "case_count": 3}]}
    assert admin.get("/api/operator/recipe-candidates").json() == {"candidates": []}
    # Reading hints does not silently persist a recipe or grant any tool.
    assert member.get("/api/operator/recipes").json() == {"recipes": []}
    assert admin.delete(f"/api/cases/{LEGACY}/grants/{member_id}").status_code == 200
    assert member.get("/api/operator/recipe-candidates").json() == {"candidates": []}


def test_admin_disable_revokes_session(workspace):
    _, admin, member, _, member_id, _ = workspace
    response = admin.get("/api/admin/users")
    assert response.status_code == 200, response.text
    assert any(row["id"] == member_id for row in response.json())
    assert member.get("/api/cases").status_code == 200
    assert admin.post(f"/api/admin/users/{member_id}/disable").status_code == 200
    assert member.get("/api/cases").status_code == 401
    login = member.post("/login", data={"username": "member-test", "password": MEMBER_PASSWORD},
                        follow_redirects=False)
    assert "set-cookie" not in login.headers


def test_admin_creates_user_without_inheriting_existing_cases(workspace):
    _, admin, _, _, _, _ = workspace
    response = admin.post("/api/admin/users", json={
        "username": "second-member", "password": "test-second-password-long-enough",
        "global_role": "member",
    })
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["username"] == "second-member"
    assert "password" not in created and "password_digest" not in created
    second = TestClient(admin.app, base_url="https://windcatcher.test")
    assert second.post("/login", data={
        "username": "second-member", "password": "test-second-password-long-enough",
    }, follow_redirects=False).status_code == 303
    assert _ids(second) == []


def test_spoofed_browser_session_and_reviewer_are_ignored(workspace, monkeypatch):
    _, admin, member, _, member_id, _ = workspace
    path = f"/api/cases/{LEGACY}"
    forged = {"X-Windcatcher-Session": "admin-test"}
    assert member.get(path, headers=forged).status_code in {403, 404}
    assert admin.put(f"{path}/grants/{member_id}", json={"role": "reviewer"}).status_code == 200
    actors = []
    monkeypatch.setattr(patient_shares, "record_review",
                        lambda case_id, actor, stage, action, reason: actors.append(actor) or {"ok": True})
    response = member.post(f"{path}/review-events", headers=forged, json={
        "operator": "forged-reviewer", "stage": "segment", "action": "confirm",
        "reason": "synthetic review test",
    })
    assert response.status_code == 200, response.text
    assert len(actors) == 1 and "forged-reviewer" not in actors[0]
    assert member_id in actors[0] or "member-test" in actors[0]


def test_orchestration_advance_requires_editor_and_binds_real_actor(workspace, monkeypatch):
    app, admin, member, admin_id, member_id, _ = workspace
    path = f"/api/cases/{LEGACY}/orchestration/advance"
    actors = []

    def advance(case_id, *, retry=False):
        actors.append((case_id, retry, endplane_history.CURRENT_ACTOR_ID.get()))
        return {"case_id": case_id, "status": "prepared", "completed_steps": [],
                "next_action": "run_authoritative_cfd_preflight"}

    monkeypatch.setattr(server.orchestration, "advance_case", advance)
    assert member.post(path, json={}).status_code in {403, 404}
    assert admin.put(f"/api/cases/{LEGACY}/grants/{member_id}",
                     json={"role": "viewer"}).status_code == 200
    assert member.post(path, json={}).status_code in {403, 404}
    assert admin.put(f"/api/cases/{LEGACY}/grants/{member_id}",
                     json={"role": "editor"}).status_code == 200
    response = member.post(path, headers={"X-Windcatcher-Session": admin_id},
                           json={"retry": True})
    assert response.status_code == 200, response.text
    assert actors == [(LEGACY, True, member_id)]


def test_upload_id_cannot_be_taken_over(workspace):
    _, admin, member, _, _, _ = workspace
    response = member.post("/api/uploads", json={
        "files": [{"path": "001.dcm", "size": 3}], "synthetic": True, "anonymize": True,
    })
    assert response.status_code == 200, response.text
    upload_id = response.json()["upload_id"]
    for suffix, data in (("chunks?offset=0", b"abc"), ("complete", None), ("cancel", None)):
        path = f"/api/uploads/{upload_id}/{suffix}"
        assert admin.post(path, content=data).status_code in {403, 404}
    assert member.post(f"/api/uploads/{upload_id}/chunks?offset=0", content=b"abc").status_code == 200
    assert member.post(f"/api/uploads/{upload_id}/cancel").status_code == 200


def test_upload_owner_binding_failure_removes_staging(workspace, monkeypatch):
    app, _, member, _, _, root = workspace
    original_bind = app.state.identity.bind_upload_owner

    def fail_binding(_upload_id, _user_id):
        raise OSError("simulated identity write failure")

    monkeypatch.setattr(app.state.identity, "bind_upload_owner", fail_binding)
    failed = member.post("/api/uploads", json={
        "files": [{"path": "001.dcm", "size": 3}], "synthetic": True,
    })
    assert failed.status_code == 503, failed.text
    assert "simulated identity write failure" not in failed.text
    assert not list(root.glob(".windcatcher-chunks-*"))
    monkeypatch.setattr(app.state.identity, "bind_upload_owner", original_bind)
    for _ in range(4):
        assert member.post("/api/uploads", json={
            "files": [{"path": "001.dcm", "size": 3}], "synthetic": True,
        }).status_code == 200


def test_multiuser_complete_manifest_failure_is_idempotent(workspace, monkeypatch):
    from pipeline import uploads

    app, admin, member, _, member_id, root = workspace
    created = member.post("/api/uploads", json={
        "files": [{"path": "001.dcm", "size": 3}], "synthetic": True,
    })
    assert created.status_code == 200, created.text
    upload_id = created.json()["upload_id"]
    assert member.post(f"/api/uploads/{upload_id}/chunks?offset=0", content=b"abc").status_code == 200
    prepared = []
    monkeypatch.setattr(server, "_prepare_case", lambda case_id, source, anonymize:
                        prepared.append(case_id))
    original_save = uploads._save

    def fail_final_save(folder, manifest):
        if "result" in manifest:
            raise OSError("simulated final manifest failure")
        return original_save(folder, manifest)

    monkeypatch.setattr(uploads, "_save", fail_final_save)
    first = member.post(f"/api/uploads/{upload_id}/complete")
    assert first.status_code == 200, first.text
    case_id = first.json()["case_id"]
    assert prepared == [case_id]
    assert app.state.identity.upload_case_id(member_id, upload_id) == case_id
    assert case_id in _ids(member) and case_id not in _ids(admin)
    assert "result" not in uploads._load(root / (".windcatcher-chunks-" + upload_id))
    monkeypatch.setattr(uploads, "_save", original_save)
    second = member.post(f"/api/uploads/{upload_id}/complete")
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert prepared == [case_id]
    assert _ids(member) == [case_id]


def test_chunk_completion_owner_binding_failure_retains_owned_staging_for_retry(workspace, monkeypatch):
    app, _, member, _, member_id, root = workspace
    created = member.post("/api/uploads", json={
        "files": [{"path": "001.dcm", "size": 3}], "synthetic": True,
    })
    assert created.status_code == 200, created.text
    upload_id = created.json()["upload_id"]
    assert member.post(f"/api/uploads/{upload_id}/chunks?offset=0", content=b"abc").status_code == 200
    original_bind = app.state.identity.finish_upload
    original_rmtree = server.shutil.rmtree

    def fail_binding(_upload_id, _case_id, _user_id):
        raise OSError("simulated identity write failure")

    def fail_case_cleanup(path, *args, **kwargs):
        # Simulate rmtree(ignore_errors=True) leaving a partial case directory.
        candidate = cases.Path(path)
        if candidate.parent == root and candidate.name.startswith("case-"):
            return None
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(app.state.identity, "finish_upload", fail_binding)
    monkeypatch.setattr(server.shutil, "rmtree", fail_case_cleanup)
    failed = member.post(f"/api/uploads/{upload_id}/complete")
    assert failed.status_code == 500, failed.text
    assert "simulated identity write failure" not in failed.text
    assert app.state.identity.upload_case_id(member_id, upload_id) is None
    assert all(not (folder / "import" / "dicom").exists() for folder in root.glob("case-*"))
    staging = root / (".windcatcher-chunks-" + upload_id) / "files" / "001.dcm"
    assert staging.read_bytes() == b"abc"

    monkeypatch.setattr(app.state.identity, "finish_upload", original_bind)
    monkeypatch.setattr(server.shutil, "rmtree", original_rmtree)
    retried = member.post(f"/api/uploads/{upload_id}/complete")
    assert retried.status_code == 200, retried.text
    case_id = retried.json()["case_id"]
    assert app.state.identity.upload_case_id(member_id, upload_id) == case_id
    assert (root / case_id / "import" / "dicom" / "001.dcm").read_bytes() == b"abc"
    assert _ids(member) == [case_id]


def test_chunk_completion_move_failure_retries_same_owned_case(workspace, monkeypatch):
    app, _, member, _, member_id, root = workspace
    created = member.post("/api/uploads", json={
        "files": [{"path": "001.dcm", "size": 3}], "synthetic": True,
    })
    assert created.status_code == 200, created.text
    upload_id = created.json()["upload_id"]
    assert member.post(f"/api/uploads/{upload_id}/chunks?offset=0", content=b"abc").status_code == 200
    original_move = server.shutil.move
    attempts = 0

    def interrupted_move(source, target, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("simulated move failure")
        return original_move(source, target, *args, **kwargs)

    monkeypatch.setattr(server.shutil, "move", interrupted_move)
    first = member.post(f"/api/uploads/{upload_id}/complete")
    assert first.status_code == 500, first.text
    case_id = app.state.identity.upload_case_id(member_id, upload_id)
    assert case_id is not None
    assert (root / (".windcatcher-chunks-" + upload_id) / "files" / "001.dcm").read_bytes() == b"abc"
    assert server.recover_interrupted_cases() >= 1
    second = member.post(f"/api/uploads/{upload_id}/complete")
    assert second.status_code == 200, second.text
    assert second.json()["case_id"] == case_id
    assert (root / case_id / "import" / "dicom" / "001.dcm").read_bytes() == b"abc"


def test_direct_upload_binds_new_case_to_uploader(workspace, tmp_path):
    from tests.make_phantom import build_phantom

    _, admin, member, _, _, _ = workspace
    paths = sorted(build_phantom(tmp_path / "synthetic-upload").glob("*.dcm"))
    response = member.post("/api/cases/upload", data={
        "synthetic": "true", "anonymize": "true",
        "relative_paths": json.dumps([path.name for path in paths]),
    }, files=[("files", (path.name, path.read_bytes(), "application/dicom")) for path in paths])
    assert response.status_code == 200, response.text
    new_case = response.json()["case_id"]
    assert new_case in _ids(member)
    assert new_case not in _ids(admin)
    assert admin.get(f"/api/cases/{new_case}").status_code in {403, 404}


def test_direct_upload_owner_binding_failure_leaves_no_orphan_case(workspace, monkeypatch):
    app, _, member, _, _, root = workspace
    before = {path.name for path in root.iterdir()}

    def fail_binding(_case_id, _user_id):
        raise OSError("synthetic identity write failure")

    monkeypatch.setattr(app.state.identity, "bind_case_owner", fail_binding)
    response = member.post("/api/cases/upload", data={
        "synthetic": "true", "anonymize": "true",
        "relative_paths": json.dumps(["001.dcm"]),
    }, files=[("files", ("001.dcm", b"synthetic-not-a-real-dicom", "application/dicom"))])
    assert response.status_code >= 500, response.text
    assert "synthetic identity write failure" not in response.text
    assert {path.name for path in root.iterdir()} == before
    assert _ids(member) == []


def test_completed_chunk_upload_binds_case_to_upload_owner(workspace, tmp_path):
    from tests.make_phantom import build_phantom
    from pipeline import uploads

    _, admin, member, _, _, _ = workspace
    paths = sorted(build_phantom(tmp_path / "synthetic-chunks").glob("*.dcm"))
    manifest = [{"path": path.name, "size": path.stat().st_size} for path in paths]
    created = member.post("/api/uploads", json={
        "files": manifest, "synthetic": True, "anonymize": True,
    })
    assert created.status_code == 200, created.text
    upload_id = created.json()["upload_id"]
    stream = b"".join(path.read_bytes() for path in paths)
    for offset in range(0, len(stream), uploads.CHUNK_BYTES):
        chunk = stream[offset:offset + uploads.CHUNK_BYTES]
        sent = member.post(f"/api/uploads/{upload_id}/chunks?offset={offset}", content=chunk)
        assert sent.status_code == 200, sent.text
    complete = member.post(f"/api/uploads/{upload_id}/complete")
    assert complete.status_code == 200, complete.text
    case_id = complete.json()["case_id"]
    assert case_id in _ids(member)
    assert case_id not in _ids(admin)
    assert member.get(f"/api/cases/{case_id}").status_code == 200
    assert admin.get(f"/api/cases/{case_id}").status_code in {403, 404}


def test_denied_delete_has_no_share_or_reference_side_effect(workspace, monkeypatch):
    _, admin, member, _, _, root = workspace
    calls = []
    monkeypatch.setattr(patient_shares, "revoke_case", lambda case_id: calls.append(("share", case_id)))
    monkeypatch.setattr(reference_cases, "revoke_case", lambda case_id: calls.append(("reference", case_id)))
    path = f"/api/cases/{LEGACY}"
    denied = member.post(path, headers={"X-Windcatcher-Method": "DELETE"})
    assert denied.status_code in {403, 404}
    assert calls == []
    assert (root / LEGACY / "case.json").is_file()
    assert admin.get(path).status_code == 200


def test_running_case_delete_keeps_shares_files_and_grants(workspace, monkeypatch):
    app, admin, _, admin_id, member_id, root = workspace
    assert admin.put(f"/api/cases/{LEGACY}/grants/{member_id}",
                     json={"role": "viewer"}).status_code == 200
    case_file = root / LEGACY / "case.json"
    case = json.loads(case_file.read_text(encoding="utf-8"))
    case["job"] = {"status": "running", "stage": "segment"}
    case_file.write_text(json.dumps(case), encoding="utf-8")
    calls = []
    monkeypatch.setattr(patient_shares, "revoke_case", lambda case_id: calls.append(("share", case_id)))
    monkeypatch.setattr(reference_cases, "revoke_case", lambda case_id: calls.append(("reference", case_id)))
    monkeypatch.setattr(server.app.state, "batch_queue", None, raising=False)
    result = admin.delete(f"/api/cases/{LEGACY}")
    assert result.status_code == 409, result.text
    assert calls == []
    assert case_file.is_file()
    assert app.state.identity.case_role(admin_id, LEGACY) == "owner"
    assert app.state.identity.case_role(member_id, LEGACY) == "viewer"


@pytest.mark.parametrize("delete_method", ["native", "gateway_override"])
def test_successful_owner_delete_retires_all_grants_and_case_id(workspace, monkeypatch, delete_method):
    app, admin, member, admin_id, member_id, root = workspace
    assert admin.put(f"/api/cases/{LEGACY}/grants/{member_id}",
                     json={"role": "viewer"}).status_code == 200
    calls = []
    monkeypatch.setattr(patient_shares, "revoke_case", lambda case_id: calls.append(("share", case_id)))
    monkeypatch.setattr(reference_cases, "revoke_case", lambda case_id: calls.append(("reference", case_id)))
    monkeypatch.setattr(server.app.state, "batch_queue", None, raising=False)
    path = f"/api/cases/{LEGACY}"
    if delete_method == "native":
        result = admin.delete(path)
    else:
        result = admin.post(path, headers={"X-Windcatcher-Method": "DELETE"})
    assert result.status_code == 200, result.text
    assert calls == [("share", LEGACY), ("reference", LEGACY)]
    assert not (root / LEGACY).exists()
    assert app.state.identity.case_role(admin_id, LEGACY) is None
    assert app.state.identity.case_role(member_id, LEGACY) is None
    assert LEGACY not in _ids(admin) and LEGACY not in _ids(member)
    with pytest.raises(PermissionError, match="retired"):
        app.state.identity.bind_case_owner(LEGACY, admin_id)


def test_partial_rmtree_failure_stays_private_and_owner_can_retry(workspace, monkeypatch):
    app, admin, member, admin_id, member_id, root = workspace
    assert admin.put(f"/api/cases/{LEGACY}/grants/{member_id}", json={"role": "viewer"}).status_code == 200
    token, reference_id = _seed_public_outputs(LEGACY)
    assert member.get(f"/api/cases/{LEGACY}").status_code == 200
    assert member.get(f"/share/{token}").status_code == 200
    assert member.get(f"/api/references/{reference_id}/model").status_code == 200
    original_rmtree = cases.shutil.rmtree

    def fail_after_partial_delete(path, *args, **kwargs):
        if cases.Path(path) == root / LEGACY:
            (root / LEGACY / "case.json").unlink()
            raise OSError("simulated partial deletion")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(cases.shutil, "rmtree", fail_after_partial_delete)
    failed = admin.delete(f"/api/cases/{LEGACY}")
    assert failed.status_code == 409, failed.text
    marker = root / f".windcatcher-deleting-{LEGACY}.json"
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "case_id": LEGACY, "owner_id": admin_id,
    }
    assert app.state.identity.case_role(admin_id, LEGACY) == "owner"
    assert app.state.identity.case_role(member_id, LEGACY) is None
    assert member.get(f"/api/cases/{LEGACY}").status_code in {403, 404}
    assert admin.get(f"/api/cases/{LEGACY}").status_code == 404
    assert admin.put(f"/api/cases/{LEGACY}/grants/{member_id}", json={"role": "viewer"}).status_code == 404
    assert LEGACY not in _ids(admin) and LEGACY not in _ids(member)
    assert member.get(f"/share/{token}").status_code == 404
    assert member.get(f"/api/references/{reference_id}/model").status_code == 404
    assert member.get("/api/references").json()["items"] == []
    monkeypatch.setattr(cases.shutil, "rmtree", original_rmtree)
    retried = admin.delete(f"/api/cases/{LEGACY}")
    assert retried.status_code == 200, retried.text
    assert not marker.exists() and not (root / LEGACY).exists()
    assert app.state.identity.case_role(admin_id, LEGACY) is None


def test_acl_retirement_failure_is_private_and_retryable(workspace, monkeypatch):
    app, admin, member, admin_id, member_id, root = workspace
    assert admin.put(f"/api/cases/{LEGACY}/grants/{member_id}", json={"role": "viewer"}).status_code == 200
    token, reference_id = _seed_public_outputs(LEGACY)
    original_retire = app.state.identity.retire_case

    def fail_retire(_actor_id, _case_id):
        raise OSError("simulated identity failure")

    monkeypatch.setattr(app.state.identity, "retire_case", fail_retire)
    failed = admin.delete(f"/api/cases/{LEGACY}")
    assert failed.status_code == 503, failed.text
    assert "simulated identity failure" not in failed.text
    marker = root / f".windcatcher-deleting-{LEGACY}.json"
    assert marker.is_file() and not (root / LEGACY).exists()
    assert app.state.identity.case_role(admin_id, LEGACY) == "owner"
    assert app.state.identity.case_role(member_id, LEGACY) is None
    assert member.get(f"/api/cases/{LEGACY}").status_code in {403, 404}
    assert member.get(f"/share/{token}").status_code == 404
    assert member.get(f"/api/references/{reference_id}/model").status_code == 404
    monkeypatch.setattr(app.state.identity, "retire_case", original_retire)
    retried = admin.delete(f"/api/cases/{LEGACY}")
    assert retried.status_code == 200, retried.text
    assert not marker.exists()
    assert app.state.identity.case_role(admin_id, LEGACY) is None


def test_deletion_marker_blocks_public_views_before_revocation_completes(workspace, monkeypatch):
    app, admin, member, _, member_id, root = workspace
    assert admin.put(f"/api/cases/{LEGACY}/grants/{member_id}", json={"role": "viewer"}).status_code == 200
    token, reference_id = _seed_public_outputs(LEGACY)
    original_revoke = patient_shares.revoke_case

    def fail_revoke(_case_id):
        raise OSError("simulated share database failure")

    monkeypatch.setattr(patient_shares, "revoke_case", fail_revoke)
    failed = admin.delete(f"/api/cases/{LEGACY}")
    assert failed.status_code == 503, failed.text
    assert (root / LEGACY / "case.json").is_file()
    assert (root / f".windcatcher-deleting-{LEGACY}.json").is_file()
    assert app.state.identity.case_role(member_id, LEGACY) is None
    assert admin.get(f"/api/cases/{LEGACY}").status_code == 404
    assert member.get(f"/share/{token}").status_code == 404
    assert member.get(f"/api/references/{reference_id}/model").status_code == 404
    assert member.get("/api/references").json()["items"] == []
    monkeypatch.setattr(patient_shares, "revoke_case", original_revoke)
    assert admin.delete(f"/api/cases/{LEGACY}").status_code == 200


def test_only_original_owner_can_retry_pending_deletion(workspace, monkeypatch):
    app, admin, member, _, member_id, root = workspace
    assert admin.put(f"/api/cases/{LEGACY}/grants/{member_id}", json={"role": "owner"}).status_code == 200
    original_revoke = app.state.identity.revoke_case_collaborators

    def fail_revoke(_actor_id, _case_id):
        raise OSError("simulated grant write failure")

    monkeypatch.setattr(app.state.identity, "revoke_case_collaborators", fail_revoke)
    failed = admin.delete(f"/api/cases/{LEGACY}")
    assert failed.status_code == 503, failed.text
    assert (root / f".windcatcher-deleting-{LEGACY}.json").is_file()
    assert app.state.identity.case_role(member_id, LEGACY) == "owner"
    assert member.get(f"/api/cases/{LEGACY}").status_code == 404
    assert member.delete(f"/api/cases/{LEGACY}").status_code == 404
    monkeypatch.setattr(app.state.identity, "revoke_case_collaborators", original_revoke)
    assert admin.delete(f"/api/cases/{LEGACY}").status_code == 200


def test_reference_resource_id_and_candidates_respect_case_grant(workspace, monkeypatch):
    _, _, member, _, _, _ = workspace
    reference_id = "a" * 32
    revoked = []
    monkeypatch.setattr(reference_cases, "get_entry", lambda _ref: {"case_id": LEGACY})
    monkeypatch.setattr(reference_cases, "revoke", lambda *_args: revoked.append(True))
    candidates = member.get("/api/reference-admin/cases")
    assert candidates.status_code == 200, candidates.text
    assert candidates.json()["items"] == []
    assert member.post(f"/api/reference-admin/{reference_id}/revoke").status_code in {403, 404}
    assert revoked == []


def test_member_ai_provider_config_and_key_are_private(workspace, monkeypatch):
    _, admin, member, admin_id, member_id, _ = workspace
    ai_report.save_config("https://shared.example/v1", "legacy-model", "legacy-secret")
    monkeypatch.setenv(ai_report.API_KEY_ENV, "environment-secret")
    monkeypatch.setenv("WINDCATCHER_AI_API_URL", "https://environment.example/v1")
    member_config = member.get("/api/ai-report/config")
    assert member_config.status_code == 200
    assert member_config.json()["base_url"] == ""
    assert member_config.json()["api_key_configured"] is False
    assert member_config.json()["key_env"] == ""
    assert admin.get("/api/ai-report/config").json()["base_url"] == ""
    assert member.get("/api/capabilities").json()["ai_interpretation"]["base_url"] == ""

    url = "https://personal.example/v1"
    member_saved = member.put("/api/ai-report/config", json={
        "base_url": url, "model": "member-model", "api_key": "member-private-secret",
    })
    assert member_saved.status_code == 200, member_saved.text
    assert member_saved.json()["model"] == "member-model"
    assert "member-private-secret" not in member_saved.text
    assert admin.get("/api/ai-report/config").json()["base_url"] == ""
    admin_saved = admin.put("/api/ai-report/config", json={
        "base_url": url, "model": "admin-model", "api_key": "admin-private-secret",
    })
    assert admin_saved.status_code == 200, admin_saved.text
    assert member.get("/api/ai-report/config").json()["model"] == "member-model"
    assert admin.get("/api/ai-report/config").json()["model"] == "admin-model"
    assert member.get("/api/capabilities").json()["ai_interpretation"]["model"] == "member-model"
    assert ai_report.CONFIG_PATH.read_text(encoding="utf-8").find("legacy-secret") < 0
    for user_id in (admin_id, member_id):
        saved = (ai_report.CONFIG_PATH.parent / ".ai-report-users" / f"{user_id}.json").read_text(encoding="utf-8")
        assert "private-secret" not in saved

    seen = []

    def fake_open(request, timeout):
        seen.append(request.get_header("Authorization"))
        from io import BytesIO
        return BytesIO(b'{"data":[{"id":"test-model"}]}')

    monkeypatch.setattr(ai_report, "_open_provider_request", fake_open)
    for client in (member, admin):
        result = client.post("/api/ai-report/models", json={"base_url": url, "api_key": ""})
        assert result.status_code == 200, result.text
    assert seen == ["Bearer member-private-secret", "Bearer admin-private-secret"]

    restarted = cloud.create_app()
    after_restart = TestClient(restarted, base_url="https://windcatcher.test")
    assert after_restart.post("/login", data={"username": "member-test", "password": MEMBER_PASSWORD},
                              follow_redirects=False).status_code == 303
    assert after_restart.get("/api/ai-report/config").json()["model"] == "member-model"
    ai_report._USER_SESSION_KEYS.clear()  # A new process has no transient keys.
    after_restart_config = after_restart.get("/api/ai-report/config").json()
    assert after_restart_config["model"] == "member-model"
    assert after_restart_config["api_key_configured"] is False


def test_second_global_admin_uses_own_ai_key_even_with_case_grant(workspace):
    app, bootstrap, _, bootstrap_id, _, _ = workspace
    second_password = "test-second-admin-password-long-enough"
    response = bootstrap.post("/api/admin/users", json={
        "username": "second-admin", "password": second_password, "global_role": "admin",
    })
    assert response.status_code == 201, response.text
    second_id = response.json()["id"]
    assert bootstrap.put(f"/api/cases/{LEGACY}/grants/{second_id}",
                         json={"role": "editor"}).status_code == 200
    second = TestClient(app, base_url="https://windcatcher.test")
    assert second.post("/login", data={"username": "second-admin", "password": second_password},
                       follow_redirects=False).status_code == 303
    assert app.state.identity.authorize_case(second_id, LEGACY, "edit")
    assert bootstrap.put("/api/ai-report/config", json={
        "base_url": "https://bootstrap.example/v1", "model": "bootstrap-model",
        "api_key": "bootstrap-private-secret",
    }).status_code == 200
    own_config = second.get("/api/ai-report/config")
    assert own_config.status_code == 200
    assert own_config.json()["base_url"] == ""
    assert own_config.json()["api_key_configured"] is False
    assert second.post(f"/api/cases/{LEGACY}/ai-report").status_code == 409
    assert second.put("/api/ai-report/config", json={
        "base_url": "https://second.example/v1", "model": "second-model",
        "api_key": "second-private-secret",
    }).status_code == 200
    assert bootstrap.get("/api/ai-report/config").json()["model"] == "bootstrap-model"
    assert bootstrap.get("/api/ai-report/config").status_code == 200
    # The fixture has no confirmed segmentation, so the bootstrap admin is
    # allowed past ACL but safely blocked before any LLM/network request.
    assert bootstrap.post(f"/api/cases/{LEGACY}/ai-report").status_code == 409
    assert bootstrap_id != second_id


def test_public_monitor_is_admin_read_only_and_case_events_bind_actor(workspace, monkeypatch, tmp_path):
    public_root = tmp_path / "monitor-cases"
    public_root.mkdir()
    _stub_case(public_root, LEGACY)
    for module in (server, cases, config):
        monkeypatch.setattr(module, "CASES_DIR", public_root)
    monkeypatch.setenv("WINDCATCHER_DEPLOYMENT", "public")
    monkeypatch.setenv("WINDCATCHER_PUBLIC_URL", "https://windcatcher.test")
    monkeypatch.setenv("WINDCATCHER_PUBLIC_USERNAME", "public-admin")
    monkeypatch.setenv("WINDCATCHER_PUBLIC_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("WINDCATCHER_PUBLIC_SESSION_SECRET", "test-public-secret-long-enough-32-chars")
    monkeypatch.setenv("WINDCATCHER_IDENTITY_DIR", str(tmp_path / "monitor-identity"))
    app = cloud.create_app()
    identity = app.state.identity
    admin_id = identity.authenticate("public-admin", ADMIN_PASSWORD)["id"]
    member_id = identity.create_user(admin_id, "monitor-member", MEMBER_PASSWORD)["id"]
    member = TestClient(app, base_url="https://windcatcher.test")
    assert member.post("/login", data={"username": "monitor-member", "password": MEMBER_PASSWORD},
                       follow_redirects=False).status_code == 303
    assert member.get("/api/monitor/events").status_code in {403, 404}
    assert member.get("/api/monitor/status").status_code in {403, 404}
    writes = []
    monkeypatch.setattr(activity_monitor, "record_events",
                        lambda events: writes.append(events) or [1])
    event = {"kind": "ui", "action": "click", "case_id": LEGACY,
             "session_id": "forged-admin-session"}
    assert member.post("/api/monitor/events", json={"events": [event]}).status_code in {403, 404}
    assert writes == []
    identity.grant_case(admin_id, LEGACY, member_id, "viewer")
    accepted = member.post("/api/monitor/events", json={"events": [event]})
    assert accepted.status_code == 200, accepted.text
    assert len(writes) == 1 and writes[0][0]["session_id"] == member_id


def test_public_mcp_service_has_no_implicit_case_grants(workspace, monkeypatch, tmp_path):
    public_root = tmp_path / "mcp-cases"
    public_root.mkdir()
    _stub_case(public_root, LEGACY)
    for module in (server, cases, config):
        monkeypatch.setattr(module, "CASES_DIR", public_root)
    monkeypatch.setenv("WINDCATCHER_DEPLOYMENT", "public")
    monkeypatch.setenv("WINDCATCHER_PUBLIC_URL", "https://windcatcher.test")
    monkeypatch.setenv("WINDCATCHER_PUBLIC_USERNAME", "public-admin")
    monkeypatch.setenv("WINDCATCHER_PUBLIC_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("WINDCATCHER_PUBLIC_SESSION_SECRET", "test-public-secret-long-enough-32-chars")
    monkeypatch.setenv("WINDCATCHER_IDENTITY_DIR", str(tmp_path / "public-identity"))
    token = "z" * 43
    monkeypatch.setenv("WINDCATCHER_MCP_TOKEN", token)
    app = cloud.create_app()
    service = app.state.identity.mcp_principal()
    assert not app.state.identity.authorize_case(service["id"], LEGACY, "view")
    local = TestClient(app, base_url="https://127.0.0.1:8765", client=("127.0.0.1", 50000))
    headers = {"X-Windcatcher-MCP-Token": token}
    assert local.get(f"/api/automation/cases/{LEGACY}", headers=headers).status_code in {403, 404}
    assert local.post("/api/automation/batches", headers=headers, json={
        "request_key": "test-mcp-foreign-case", "items": [{"case_id": LEGACY}],
    }).status_code in {403, 404}
    cancelled = []

    class Queue:
        def health(self):
            return {"worker_alive": True}

        def get(self, _batch_id):
            return {"items": [{"input": {"case_id": LEGACY}}]}

        def cancel(self, _batch_id):
            cancelled.append(True)
            return {"status": "cancelled"}

    monkeypatch.setattr(server.app.state, "batch_queue", Queue(), raising=False)
    batch_id = "batch-" + "1" * 16
    assert local.get(f"/api/automation/batches/{batch_id}", headers=headers).status_code in {403, 404}
    assert local.post(f"/api/automation/batches/{batch_id}/cancel", headers=headers).status_code in {403, 404}
    assert cancelled == []
