"""Synthetic-case checks for patient publication and the public boundary."""
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

import cloud
import server
from pipeline import automation, cases, cfd_results, openfoam_runner, patient_shares
from pipeline import config


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("WINDCATCHER_DEPLOYMENT", "public")
    monkeypatch.setenv("WINDCATCHER_PUBLIC_USERNAME", "researcher")
    monkeypatch.setenv("WINDCATCHER_PUBLIC_PASSWORD", "test-only-password-long-enough")
    monkeypatch.setenv("WINDCATCHER_PUBLIC_SESSION_SECRET", "test-only-session-secret-32-characters-minimum")
    monkeypatch.setenv("WINDCATCHER_PUBLIC_URL", "https://windcatcher.test")
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path / "cases")
    monkeypatch.setattr(server, "CASES_DIR", tmp_path / "cases")
    monkeypatch.setattr(config, "CASES_DIR", tmp_path / "cases")
    monkeypatch.setattr(patient_shares, "SHARE_DIR", tmp_path / "shares")
    case_id = cases.create_case("synthetic", "", synthetic=True)
    root = cases.case_dir(case_id)
    model = root / "cfd" / "results" / "generations" / "v1" / "payload.json"
    model.parent.mkdir(parents=True)
    payload = {
        "units": {"coordinates": "m", "velocity": "m/s", "pressure": "Pa"},
        "bounds_m": [0, 1, 0, 1, 0, 1],
        "wall": {"positions": [0, 0, 0, 1, 0, 0, 0, 1, 0], "indices": [0, 1, 2],
                 "private_note": "patient name"},
        "patches": {}, "streamlines": [], "slices": [],
        "ranges": {"velocity": [0, 1], "pressure": [0, 1]},
        "source": {"case_id": case_id, "private_path": "CT/secret.dcm"},
        "metadata": {"private_note": "patient name"},
    }
    model.write_text(json.dumps(payload), encoding="utf-8")
    (root / "cfd" / "case_meta.json").write_text(json.dumps({"solver": "simpleFoam", "total_flow_lpm": 15}), encoding="utf-8")
    (root / "segment" / "mask.npy").write_bytes(b"synthetic-mask-v1")
    version = {"signature": "a" * 64}
    monkeypatch.setattr(cfd_results, "get_results", lambda _case: {
        "status": "ready", "source_signature": version["signature"],
        "payload": "cfd/results/generations/v1/payload.json"})
    monkeypatch.setattr(automation, "require_confirmed_mask", lambda _case: None)
    client = TestClient(cloud.create_app(), base_url="https://windcatcher.test")
    return client, case_id, version, model


def login(client):
    assert client.post("/login", data={
        "username": "researcher", "password": "test-only-password-long-enough",
    }, follow_redirects=False).status_code == 303


def approve(monkeypatch):
    monkeypatch.setattr(openfoam_runner, "check_result_gate", lambda _case: {
        "eligible": True, "numeric_ok": True,
        "checks": [{"name": "工程复核", "status": "ok"}],
    })


def publish(client, case_id):
    response = client.post(f"/api/cases/{case_id}/shares", json={
        "reviewer": "测试审核员", "review_note": "已人工复核真实流场与质量闸门，批准患者查看", "days": 7,
    })
    assert response.status_code == 200, response.text
    return response.json()


def test_gate_blocks_publication_without_actual_solver_evidence(setup):
    client, case_id, _, _ = setup
    login(client)
    response = client.post(f"/api/cases/{case_id}/shares", json={
        "reviewer": "测试审核员", "review_note": "人工审核说明已填写，但求解闸门尚未通过"})
    assert response.status_code == 409
    assert client.get(f"/api/cases/{case_id}/shares").json() == []


def test_real_case_test_share_does_not_mark_failed_gate_as_approved(setup):
    client, case_id, _, _ = setup
    case = cases.get_case(case_id)
    case["synthetic"] = False
    case["source_type"] = "browser_upload"
    (cases.case_dir(case_id) / "case.json").write_text(json.dumps(case), encoding="utf-8")
    payload = {"operator": "测试操作员", "reason": "仅核对患者链接与访问码是否能够打开", "recipient_confirmed": True}
    path = f"/api/cases/{case_id}/shares/test"
    assert client.post(path, json=payload).status_code == 401
    login(client)
    assert client.post(path, json=payload, headers={"Origin": "https://other.test"}).status_code == 403
    assert client.post(path, json={**payload, "recipient_confirmed": False}).status_code == 400
    assert client.post(path, json=payload).status_code == 409
    (cases.case_dir(case_id) / "cfd" / "run_status.json").write_text(
        json.dumps({"status": "ran", "run_id": "test-run-001"}), encoding="utf-8")
    formal = client.post(f"/api/cases/{case_id}/shares", json={
        "reviewer": "测试操作员", "review_note": "正式结果检查尚未通过不能发布"})
    assert formal.status_code == 409
    shared_response = client.post(path, json=payload)
    assert shared_response.status_code == 200, shared_response.text
    shared = shared_response.json()
    assert shared["mode"] == "test"
    assert 86390 <= shared["expires_at"] - shared["created_at"] <= 86400
    records = client.get(f"/api/cases/{case_id}/shares").json()
    assert len(records) == 1 and records[0]["mode"] == "test"
    gate = json.loads(records[0]["gate_summary"])
    assert gate["eligible"] is False and gate["numeric_ok"] is False
    page = client.get(shared["url"])
    assert page.status_code == 200
    assert "未验证测试可视化" in page.text
    assert "经过团队复核" not in page.text
    assert client.get(shared["url"] + "/model").status_code == 401
    assert client.post(shared["url"] + "/unlock", json={"access_code": shared["access_code"]}).status_code == 200
    model = client.get(shared["url"] + "/model")
    assert model.status_code == 200
    assert set(model.json()) == set(patient_shares.PUBLIC_FIELDS)
    assert "patient name" not in model.text
    metadata = client.get(shared["url"] + "/metadata").json()
    assert metadata["kind"] == "flow_test" and metadata["flow_verified"] is False
    assert "未复核" in metadata["warning"]
    assert client.post(f"/api/cases/{case_id}/shares/{shared['id']}/revoke").status_code == 200
    assert client.get(shared["url"]).status_code == 404


def test_test_share_route_is_not_available_on_modelscope(setup, monkeypatch):
    _, case_id, _, _ = setup
    monkeypatch.setenv("WINDCATCHER_DEPLOYMENT", "cloud")
    monkeypatch.setenv("WINDCATCHER_CLOUD_USERNAME", "researcher")
    monkeypatch.setenv("WINDCATCHER_CLOUD_PASSWORD", "test-only-password-long-enough")
    monkeypatch.setenv("WINDCATCHER_CLOUD_SESSION_SECRET", "test-only-session-secret-32-characters-minimum")
    client = TestClient(cloud.create_app(), base_url="https://windcatcher.test")
    login(client)
    response = client.post(f"/api/cases/{case_id}/shares/test", json={
        "operator": "测试操作员", "reason": "只检查页面和访问码是否能够打开", "recipient_confirmed": True,
    })
    assert response.status_code == 404
    assert patient_shares.list_shares(case_id) == []


def test_share_requires_separate_code_and_never_exposes_research_data(setup, monkeypatch):
    client, case_id, _, _ = setup
    approve(monkeypatch)
    login(client)
    assert client.post(f"/api/cases/{case_id}/review-events", json={
        "operator": "测试操作员", "stage": "segment", "action": "correction", "reason": "人工清理体外空气后重算",
    }).status_code == 200
    review = client.get(f"/api/cases/{case_id}/review-events").json()[0]
    recorded = json.loads(review["input_hashes"])
    saved = cases.case_dir(case_id) / recorded["review_snapshot/mask.npy"]["path"]
    assert saved.read_bytes() == b"synthetic-mask-v1"
    shared = publish(client, case_id)
    assert shared["url"].startswith("/share/")
    assert "access_code" in shared
    assert shared["access_code"] not in json.dumps(client.get(f"/api/cases/{case_id}/shares").json())
    client.cookies.clear()
    approved_page = client.get(shared["url"])
    assert approved_page.status_code == 200
    assert "经过团队复核" in approved_page.text
    assert "未验证测试可视化" not in approved_page.text
    assert "<!--SHARE_" not in approved_page.text
    assert 'id="tour-toggle"' in approved_page.text
    assert 'id="view-reset"' in approved_page.text
    assert 'id="tour-progress"' in approved_page.text
    for asset in ("/static/patient-view.css", "/static/patient-view.js",
                  "/static/cfd-viewer.js", "/static/vendor/three.min.js",
                  "/static/vendor/OrbitControls.js"):
        assert client.get(asset).status_code == 200
    assert client.get("/share-admin").status_code == 401
    assert client.get(shared["url"]).headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in client.get(shared["url"]).headers["content-security-policy"]
    assert client.get(shared["url"] + "/model").status_code == 401
    assert client.get(f"/api/cases/{case_id}").status_code == 401
    assert client.get(f"/api/cases/{case_id}/artifact?rel=case.json").status_code == 401
    assert client.post(shared["url"] + "/unlock", json={"access_code": "wrong"}).status_code == 403
    unlocked = client.post(shared["url"] + "/unlock", json={"access_code": shared["access_code"]})
    assert unlocked.status_code == 200
    model = client.get(shared["url"] + "/model")
    assert model.status_code == 200
    assert set(model.json()) == set(patient_shares.PUBLIC_FIELDS)
    assert case_id not in model.text and "secret.dcm" not in model.text and "patient name" not in model.text
    assert client.get(f"/api/cases/{case_id}").status_code == 401
    with sqlite3.connect(patient_shares.SHARE_DIR / "shares.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM access_events").fetchone()[0] >= 3


def test_share_is_version_pinned_and_other_share_cookie_is_rejected(setup, monkeypatch):
    client, case_id, version, model = setup
    approve(monkeypatch)
    login(client)
    first = publish(client, case_id)
    version["signature"] = "b" * 64
    raw = json.loads(model.read_text(encoding="utf-8"))
    raw["ranges"]["velocity"] = [2, 3]
    model.write_text(json.dumps(raw), encoding="utf-8")
    second = publish(client, case_id)
    client.cookies.clear()
    client.post(first["url"] + "/unlock", json={"access_code": first["access_code"]})
    assert client.get(first["url"] + "/model").json()["ranges"]["velocity"] == [0, 1]
    assert client.get(second["url"] + "/model").status_code == 401
    client.cookies.set(cloud.PATIENT_COOKIE, client.app.state.access.patient_session(first["id"], first["expires_at"]),
                       domain="windcatcher.test", path="/")
    assert client.get(second["url"] + "/model").status_code == 401
    client.cookies.delete(cloud.PATIENT_COOKIE, domain="windcatcher.test", path="/")
    assert client.post(second["url"] + "/unlock", json={"access_code": second["access_code"]}).status_code == 200
    assert client.get(second["url"] + "/model").json()["ranges"]["velocity"] == [2, 3]
    assert client.get(first["url"] + "/model").json()["ranges"]["velocity"] == [0, 1]


def test_expiry_revocation_and_cross_origin_unlock(setup, monkeypatch):
    client, case_id, _, _ = setup
    approve(monkeypatch)
    login(client)
    shared = publish(client, case_id)
    client.cookies.clear()
    assert client.post(shared["url"] + "/unlock", json={"access_code": shared["access_code"]},
                       headers={"Origin": "https://other.test"}).status_code == 403
    assert client.post(shared["url"] + "/unlock", json={"access_code": shared["access_code"]}).status_code == 200
    login(client)
    assert client.post(f"/api/cases/{case_id}/shares/{shared['id']}/revoke").status_code == 200
    assert client.get(shared["url"] + "/model").status_code == 404
    another = publish(client, case_id)
    with sqlite3.connect(patient_shares.SHARE_DIR / "shares.sqlite3") as db:
        db.execute("UPDATE shares SET expires_at=? WHERE id=?", (int(time.time()) - 1, another["id"]))
    assert client.get(another["url"]).status_code == 404
    assert client.get(another["url"] + "/model").status_code == 404


def test_wrong_codes_rate_limit_and_invalid_payload_block(setup, monkeypatch):
    client, case_id, _, model = setup
    approve(monkeypatch)
    login(client)
    shared = publish(client, case_id)
    client.cookies.clear()
    for _ in range(5):
        assert client.post(shared["url"] + "/unlock", json={"access_code": "wrong"}).status_code == 403
    assert client.post(shared["url"] + "/unlock", json={"access_code": shared["access_code"]}).status_code == 429
    login(client)
    raw = json.loads(model.read_text(encoding="utf-8"))
    raw["wall"]["positions"] = [0, 1]
    model.write_text(json.dumps(raw), encoding="utf-8")
    response = client.post(f"/api/cases/{case_id}/shares", json={
        "reviewer": "测试审核员", "review_note": "已人工复核真实流场与质量闸门，申请发布"})
    assert response.status_code == 409
    assert len(client.get(f"/api/cases/{case_id}/shares").json()) == 1


def test_upstream_change_during_publication_is_rejected(setup, monkeypatch):
    client, case_id, _, _ = setup
    approve(monkeypatch)
    login(client)
    calls = iter(("a" * 64, "b" * 64))
    monkeypatch.setattr(cfd_results, "get_results", lambda _case: {
        "status": "ready", "source_signature": next(calls),
        "payload": "cfd/results/generations/v1/payload.json"})
    response = client.post(f"/api/cases/{case_id}/shares", json={
        "reviewer": "测试审核员", "review_note": "已人工复核当前算例及全部质量闸门"})
    assert response.status_code == 409
    assert client.get(f"/api/cases/{case_id}/shares").json() == []


def test_case_deletion_revokes_all_shares_and_removes_snapshots(setup, monkeypatch):
    client, case_id, _, _ = setup
    approve(monkeypatch)
    login(client)
    shared = publish(client, case_id)
    snapshot = patient_shares.SHARE_DIR / "snapshots" / (shared["id"] + ".json")
    assert snapshot.is_file()
    deleted = client.post(f"/api/cases/{case_id}", headers={"X-Windcatcher-Method": "DELETE"})
    assert deleted.status_code == 200, deleted.text
    assert not snapshot.exists()
    assert client.get(shared["url"]).status_code == 404


def kinematic_model(model, case_id, density, unit="m2/s2"):
    raw = json.loads(model.read_text(encoding="utf-8"))
    raw["units"]["pressure"] = unit
    raw["ranges"]["pressure"] = [-1.58, 23.14]
    raw["wall"]["pressure"] = [-1.58, 0, 23.14]
    raw["patches"]["inlet"] = {
        "positions": raw["wall"]["positions"], "pressure": [1, 2, 3]}
    raw["slices"] = [{"axis": "x", "positions": raw["wall"]["positions"],
                      "pressure": [-1, 0, 1], "speeds": [1, 2, 3]}]
    raw["streamlines"] = [{"positions": [0, 0, 0, 1, 0, 0], "speeds": [1, 2],
                           "pressure": [3, 4], "integration_time_s": [0, 1]}]
    model.write_text(json.dumps(raw), encoding="utf-8")
    meta = {"solver": "simpleFoam", "total_flow_lpm": 15,
            "rho_kg_m3": density, "pressure_field_unit": "m2/s2"}
    (cases.case_dir(case_id) / "cfd" / "case_meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return raw


@pytest.mark.parametrize("unit", ["m2/s2", "m²/s²"])
def test_patient_pressure_uses_pinned_density_and_preserves_snapshot(setup, monkeypatch, unit):
    client, case_id, _, model = setup
    raw = kinematic_model(model, case_id, 1.2, unit)
    approve(monkeypatch)
    login(client)
    shared = publish(client, case_id)
    snapshot = patient_shares.SHARE_DIR / "snapshots" / (shared["id"] + ".json")
    original = snapshot.read_bytes()
    # Later live-case edits must never change this share's conversion factor.
    kinematic_model(model, case_id, 2.0)
    assert client.post(shared["url"] + "/unlock", json={"access_code": shared["access_code"]}).status_code == 200
    response = client.get(shared["url"] + "/model")
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["units"]["pressure"] == "Pa"
    assert view["ranges"]["pressure"] == pytest.approx([-1.896, 27.768])
    assert view["wall"]["pressure"] == pytest.approx([-1.896, 0, 27.768])
    assert view["patches"]["inlet"]["pressure"] == pytest.approx([1.2, 2.4, 3.6])
    assert view["slices"][0]["pressure"] == pytest.approx([-1.2, 0, 1.2])
    assert view["streamlines"][0]["pressure"] == pytest.approx([3.6, 4.8])
    assert view["streamlines"][0]["speeds"] == raw["streamlines"][0]["speeds"]
    assert view["wall"]["positions"] == raw["wall"]["positions"]
    assert set(view) == set(patient_shares.PUBLIC_FIELDS)
    assert "rho_kg_m3" not in response.text and case_id not in response.text
    assert snapshot.read_bytes() == original
    assert client.get(shared["url"] + "/model").json() == view


@pytest.mark.parametrize("density", [None, 0, -1, True, "not-a-number", float("nan"), float("inf")])
def test_patient_pressure_without_valid_density_keeps_honest_units(setup, monkeypatch, density):
    client, case_id, _, model = setup
    raw = kinematic_model(model, case_id, density)
    approve(monkeypatch)
    login(client)
    shared = publish(client, case_id)
    client.post(shared["url"] + "/unlock", json={"access_code": shared["access_code"]})
    view = client.get(shared["url"] + "/model").json()
    assert view["units"]["pressure"] == "m2/s2"
    assert view["ranges"]["pressure"] == raw["ranges"]["pressure"]
    assert view["wall"]["pressure"] == raw["wall"]["pressure"]


def test_patient_pressure_conversion_rejects_nonfinite_output(setup, monkeypatch):
    client, case_id, _, model = setup
    kinematic_model(model, case_id, 1e308)
    approve(monkeypatch)
    login(client)
    shared = publish(client, case_id)
    client.post(shared["url"] + "/unlock", json={"access_code": shared["access_code"]})
    response = client.get(shared["url"] + "/model")
    assert response.status_code == 503
    assert "换算" in response.json()["detail"]["reason"]


def test_patient_pressure_already_in_pa_is_not_converted_again(setup, monkeypatch):
    client, case_id, _, model = setup
    raw = kinematic_model(model, case_id, 1.2, "Pa")
    approve(monkeypatch)
    login(client)
    shared = publish(client, case_id)
    client.post(shared["url"] + "/unlock", json={"access_code": shared["access_code"]})
    response = client.get(shared["url"] + "/model")
    assert response.status_code == 200
    view = response.json()
    assert view["units"]["pressure"] == "Pa"
    assert view["ranges"]["pressure"] == raw["ranges"]["pressure"]
    assert view["wall"]["pressure"] == raw["wall"]["pressure"]
