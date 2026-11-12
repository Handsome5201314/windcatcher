"""Cloud uploads must stay below the gateway cap without splitting a CT case."""
import pytest
from fastapi.testclient import TestClient

import cloud
import server
import pipeline.cases as cases
import pipeline.config as config
from tests.make_phantom import build_phantom
from pipeline import uploads


@pytest.fixture
def client(tmp_path, monkeypatch):
    root = tmp_path / "cases"
    for module in (server, cases, config):
        monkeypatch.setattr(module, "CASES_DIR", root)
    monkeypatch.setenv("WINDCATCHER_CLOUD_USERNAME", "test")
    monkeypatch.setenv("WINDCATCHER_CLOUD_PASSWORD", "test-password-long-enough")
    monkeypatch.setenv("WINDCATCHER_CLOUD_SESSION_SECRET", "test-session-secret-long-enough-32-chars")
    client = TestClient(cloud.create_app(), base_url="https://cloud.test")
    client.post("/login", data={"username": "test", "password": "test-password-long-enough"})
    return client, root


def start(client, files):
    response = client.post("/api/uploads", json={"files": files, "synthetic": True, "anonymize": True})
    assert response.status_code == 200, response.text
    return response.json()


def send(client, upload_id, offset, body):
    return client.post(f"/api/uploads/{upload_id}/chunks?offset={offset}", content=body, headers={"Content-Type": "application/octet-stream"})


def test_chunks_cross_file_boundaries_and_only_complete_creates_case(client, tmp_path):
    c, root = client
    paths = sorted(build_phantom(tmp_path / "synthetic").glob("*.dcm"))
    files = [{"path": "Series/" + p.name, "size": p.stat().st_size} for p in paths]
    created = start(c, files)
    assert 0 < created["chunk_size"] <= 4 * 1024 * 1024
    upload_id = created["upload_id"]
    stream = b"".join(p.read_bytes() for p in paths)
    chunk_size = 160001  # Deliberately cuts through DICOM files.
    for offset in range(0, len(stream), chunk_size):
        part = stream[offset:offset + chunk_size]
        response = send(c, upload_id, offset, part)
        assert response.status_code == 200, response.text
        assert response.json()["received_bytes"] == offset + len(part)
        assert c.get("/api/cases").json() == []
        # A lost acknowledgement can safely retry the same chunk.
        assert send(c, upload_id, offset, part).json() == response.json()
    result = c.post(f"/api/uploads/{upload_id}/complete")
    assert result.status_code == 200, result.text
    case_id = result.json()["case_id"]
    case = c.get("/api/cases/" + case_id).json()
    assert case["stages"]["import"]["status"] == "ok"
    assert case["stages"]["qc"]["status"] in {"ok", "warn"}
    assert case["synthetic"] is True
    # A completion retry must not register a second case.
    assert c.post(f"/api/uploads/{upload_id}/complete").json() == result.json()
    assert len(c.get("/api/cases").json()) == 1


def test_incomplete_and_wrong_offset_do_not_register_a_case(client):
    c, _ = client
    upload_id = start(c, [{"path": "001.dcm", "size": 6}])["upload_id"]
    assert send(c, upload_id, 1, b"abc").status_code == 409
    assert send(c, upload_id, 0, b"abc").status_code == 200
    assert send(c, upload_id, 0, b"xyz").status_code == 409
    assert c.post(f"/api/uploads/{upload_id}/complete").status_code == 409
    assert c.get("/api/cases").json() == []
    assert c.post(f"/api/uploads/{upload_id}/cancel").status_code == 200
    assert send(c, upload_id, 3, b"def").status_code == 404


@pytest.mark.parametrize("files", [
    [{"path": "../outside.dcm", "size": 1}],
    [{"path": "same.dcm", "size": 1}, {"path": "SAME.dcm", "size": 1}],
    [{"path": "001.dcm", "size": 0}],
    [{"path": "001.dcm", "size": 128 * 1024 * 1024 + 1}],
])
def test_manifest_validation(client, files):
    c, root = client
    response = c.post("/api/uploads", json={"files": files})
    assert response.status_code in {400, 413, 422}
    assert c.get("/api/cases").json() == []
    assert not list(root.glob(".windcatcher-chunks-*"))


def test_chunk_size_and_total_size_are_enforced(client):
    c, _ = client
    upload_id = start(c, [{"path": "001.dcm", "size": 8}])["upload_id"]
    assert send(c, upload_id, 0, b"x" * (4 * 1024 * 1024 + 1)).status_code == 413
    assert send(c, upload_id, 0, b"x" * 9).status_code == 413


def test_chunk_routes_require_login_and_same_origin(client):
    c, _ = client
    upload_id = start(c, [{"path": "001.dcm", "size": 1}])["upload_id"]
    assert c.post(f"/api/uploads/{upload_id}/chunks?offset=0", content=b"x", headers={"Origin": "https://other.test"}).status_code == 403
    c.post("/logout")
    assert c.post("/api/uploads", json={"files": []}).status_code == 401
    assert send(c, upload_id, 0, b"x").status_code == 401


def test_total_limit_checked_before_allocating_staging(client):
    c, root = client
    response = c.post("/api/uploads", json={"files": [
        {"path": f"{i:03d}.dcm", "size": 128 * 1024 * 1024} for i in range(17)
    ]})
    assert response.status_code == 413
    assert not list(root.glob(".windcatcher-chunks-*"))


def test_expired_session_is_removed(client, monkeypatch):
    c, root = client
    upload_id = start(c, [{"path": "001.dcm", "size": 6}])["upload_id"]
    now = uploads.time.time()
    monkeypatch.setattr(uploads.time, "time", lambda: now + uploads.SESSION_TTL + 1)
    assert send(c, upload_id, 0, b"abc").status_code == 410
    assert not list(root.glob(".windcatcher-chunks-*"))


def test_orphan_staging_does_not_block_new_upload_or_remove_live_session(client):
    c, root = client
    active_id = start(c, [{"path": "active.dcm", "size": 3}])["upload_id"]
    assert send(c, active_id, 0, b"a").status_code == 200
    orphan = root / (".windcatcher-chunks-" + "a" * 32)
    (orphan / "files").mkdir(parents=True)
    (orphan / "files" / "incomplete.dcm").write_bytes(b"partial")

    created = start(c, [{"path": "new.dcm", "size": 1}])
    assert created["upload_id"] != active_id
    assert not orphan.exists()
    active = root / (".windcatcher-chunks-" + active_id)
    assert (active / "manifest.json").is_file()
    assert send(c, active_id, 1, b"bc").status_code == 200
    assert (active / "files" / "active.dcm").read_bytes() == b"abc"


def test_retry_repairs_bytes_written_before_a_manifest_failure(client, monkeypatch):
    c, root = client
    upload_id = start(c, [{"path": "001.dcm", "size": 3}, {"path": "002.dcm", "size": 3}])["upload_id"]
    save = uploads._save
    def fail_save(*_args):
        raise OSError("test disk interruption")
    monkeypatch.setattr(uploads, "_save", fail_save)
    assert send(c, upload_id, 0, b"abcd").status_code == 507
    monkeypatch.setattr(uploads, "_save", save)
    assert send(c, upload_id, 0, b"abcd").status_code == 200
    assert send(c, upload_id, 4, b"ef").status_code == 200
    staging = root / (".windcatcher-chunks-" + upload_id) / "files"
    assert (staging / "001.dcm").read_bytes() == b"abc"
    assert (staging / "002.dcm").read_bytes() == b"def"


def test_complete_final_manifest_failure_keeps_one_case_and_runs_import(client, monkeypatch):
    c, root = client
    upload_id = start(c, [{"path": "001.dcm", "size": 3}])["upload_id"]
    assert send(c, upload_id, 0, b"abc").status_code == 200
    prepared = []
    monkeypatch.setattr(server, "_prepare_case", lambda case_id, source, anonymize:
                        prepared.append((case_id, source, anonymize)))
    original_save = uploads._save

    def fail_final_save(folder, manifest):
        if "result" in manifest:
            raise OSError("simulated final manifest failure")
        return original_save(folder, manifest)

    monkeypatch.setattr(uploads, "_save", fail_final_save)
    first = c.post(f"/api/uploads/{upload_id}/complete")
    assert first.status_code == 200, first.text
    case_id = first.json()["case_id"]
    assert [item[0] for item in prepared] == [case_id]
    assert (root / case_id / "import" / "dicom" / "001.dcm").read_bytes() == b"abc"
    assert "result" not in uploads._load(root / (".windcatcher-chunks-" + upload_id))
    monkeypatch.setattr(uploads, "_save", original_save)
    retried = c.post(f"/api/uploads/{upload_id}/complete")
    assert retried.status_code == 200, retried.text
    assert retried.json() == first.json()
    assert [item[0] for item in prepared] == [case_id]
    assert [row["case_id"] for row in c.get("/api/cases").json()] == [case_id]


def test_completed_upload_retry_restarts_only_interrupted_import(client, monkeypatch):
    c, root = client
    upload_id = start(c, [{"path": "001.dcm", "size": 3}])["upload_id"]
    assert send(c, upload_id, 0, b"abc").status_code == 200
    prepared = []
    monkeypatch.setattr(server, "_prepare_case", lambda case_id, source, anonymize:
                        prepared.append((case_id, anonymize)))
    first = c.post(f"/api/uploads/{upload_id}/complete")
    assert first.status_code == 200, first.text
    case_id = first.json()["case_id"]
    assert prepared == [(case_id, True)]
    assert c.post(f"/api/uploads/{upload_id}/complete").status_code == 200
    assert prepared == [(case_id, True)]
    assert server.recover_interrupted_cases() == 1
    retried = c.post(f"/api/uploads/{upload_id}/complete")
    assert retried.status_code == 200, retried.text
    assert retried.json() == first.json()
    assert prepared == [(case_id, True), (case_id, True)]
    assert [row["case_id"] for row in c.get("/api/cases").json()] == [case_id]


def test_restart_cleanup_keeps_resumable_chunks(client):
    c, root = client
    upload_id = start(c, [{"path": "001.dcm", "size": 3}])["upload_id"]
    multipart = root / ".windcatcher-upload-stale"
    multipart.mkdir()
    server._cleanup_upload_staging()
    assert not multipart.exists()
    assert (root / (".windcatcher-chunks-" + upload_id) / "manifest.json").is_file()
    assert send(c, upload_id, 0, b"abc").status_code == 200
