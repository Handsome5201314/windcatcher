"""The public cloud must never expose local paths or research data anonymously."""
import json
import time

import pytest
from fastapi.testclient import TestClient

import cloud


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("WINDCATCHER_DEPLOYMENT", "cloud")
    monkeypatch.setenv("WINDCATCHER_MULTIUSER", "0")
    monkeypatch.setenv("WINDCATCHER_IDENTITY_DIR", str(tmp_path / "identity"))
    monkeypatch.setenv("WINDCATCHER_CLOUD_USERNAME", "researcher")
    monkeypatch.setenv("WINDCATCHER_CLOUD_PASSWORD", "test-only-password-long-enough")
    monkeypatch.setenv("WINDCATCHER_CLOUD_SESSION_SECRET", "test-only-session-secret-32-characters-minimum")
    import server
    import pipeline.cases as cases
    import pipeline.config as config
    from pipeline import patient_shares
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path / "cases")
    monkeypatch.setattr(server, "CASES_DIR", tmp_path / "cases")
    monkeypatch.setattr(config, "CASES_DIR", tmp_path / "cases")
    monkeypatch.setattr(patient_shares, "SHARE_DIR", tmp_path / "shares")
    # No lifespan: gateway tests must not recover jobs in the local workstation.
    return TestClient(cloud.create_app(), base_url="https://windcatcher.test")


def login(client):
    return client.post("/login", data={"username": "researcher", "password": "test-only-password-long-enough"}, follow_redirects=False)


def test_missing_secrets_block_startup(monkeypatch, tmp_path):
    from pipeline import config
    monkeypatch.setenv("WINDCATCHER_DEPLOYMENT", "cloud")
    monkeypatch.setenv("WINDCATCHER_MULTIUSER", "0")
    monkeypatch.setenv("WINDCATCHER_IDENTITY_DIR", str(tmp_path / "identity"))
    monkeypatch.setattr(config, "CASES_DIR", tmp_path / "cases")
    monkeypatch.delenv("WINDCATCHER_CLOUD_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="WINDCATCHER_CLOUD 访问配置缺失"):
        cloud.create_app()


def test_oblique_ct_section_requires_login(client):
    response = client.post("/api/cases/case-20261010-221018-4e0a/segment/endplanes/section", json={
        "scope": "main_airway", "mask_version": 3,
        "plane": {"point_lps_mm": [0, 0, 0], "normal_lps": [0, -1, 0]}, "span_mm": 120,
    })
    assert response.status_code == 401
    assert "image/png" not in response.headers.get("content-type", "")


def test_public_landing_has_no_workbench_or_cases(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "进入研究工作台" in response.text
    assert "病例、模型、计算任务和报告需要账号密码" in response.text
    assert "魔搭研究空间" in response.text
    assert "正在验证" in response.text
    assert 'id="newcase-form"' not in response.text
    assert response.headers["referrer-policy"] == "same-origin"
    assert 'target="_top"' not in response.text
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/assets/logo-badge.svg").status_code == 200


def test_public_deployment_uses_only_public_credentials(client, monkeypatch):
    monkeypatch.setenv("WINDCATCHER_DEPLOYMENT", "public")
    monkeypatch.setenv("WINDCATCHER_PUBLIC_URL", "https://windcatcher.test")
    monkeypatch.setenv("WINDCATCHER_PUBLIC_USERNAME", "public-team")
    monkeypatch.setenv("WINDCATCHER_PUBLIC_PASSWORD", "public-test-password-very-long")
    monkeypatch.setenv("WINDCATCHER_PUBLIC_SESSION_SECRET", "independent-public-test-secret-32-characters")
    public = TestClient(cloud.create_app(), base_url="https://windcatcher.test")
    assert "本机公网研究空间" in public.get("/").text
    assert "魔搭服务器" not in public.get("/").text
    assert public.get("/api/cases").status_code == 401
    wrong = public.post("/login", data={"username": "researcher", "password": "test-only-password-long-enough"}, follow_redirects=False)
    assert wrong.status_code == 303
    assert "set-cookie" not in wrong.headers
    assert public.get("/api/cases").status_code == 401
    good = public.post("/login", data={"username": "public-team", "password": "public-test-password-very-long"}, follow_redirects=False)
    assert good.status_code == 303
    assert "__Host-windcatcher=" in good.headers["set-cookie"]
    assert 'window.WindcatcherDeployment = "public"' in public.get("/").text
    assert public.get("/api/cases").status_code == 200


def test_public_deployment_requires_own_secret_and_origin(client, monkeypatch):
    monkeypatch.setenv("WINDCATCHER_DEPLOYMENT", "public")
    monkeypatch.setenv("WINDCATCHER_PUBLIC_USERNAME", "public-team")
    monkeypatch.setenv("WINDCATCHER_PUBLIC_PASSWORD", "public-test-password-very-long")
    monkeypatch.delenv("WINDCATCHER_PUBLIC_SESSION_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="WINDCATCHER_PUBLIC 访问配置缺失"):
        cloud.create_app()
    monkeypatch.setenv("WINDCATCHER_PUBLIC_SESSION_SECRET", "independent-public-test-secret-32-characters")
    monkeypatch.delenv("WINDCATCHER_PUBLIC_URL", raising=False)
    with pytest.raises(RuntimeError, match="WINDCATCHER_PUBLIC_URL"):
        cloud.create_app()


@pytest.mark.parametrize("path", ["/api/cases", "/api/capabilities", "/api/ai-report/config", "/static/app.js", "/openapi.json", "/reference-result"])
def test_anonymous_cannot_read_research_endpoints(client, path):
    assert client.get(path).status_code == 401


def test_login_cookie_unlocks_workbench_and_logout_locks_it(client):
    response = login(client)
    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    for flag in ["__Host-windcatcher=", "HttpOnly", "Secure", "SameSite=lax", "Path=/"]:
        assert flag in cookie
    page = client.get("/")
    assert 'window.WindcatcherCloudAccess = true' in page.text
    assert 'window.WindcatcherDeployment = "cloud"' in page.text
    assert 'id="newcase-form"' in page.text
    assert 'data-route="share"' in page.text
    assert 'id="page-share"' in page.text
    assert 'href="/share-admin"' not in page.text
    assert 'src="/reference-result"' not in page.text
    assert client.get("/api/cases").json() == []
    legacy = client.get("/share-admin", follow_redirects=False)
    assert legacy.status_code == 303
    assert legacy.headers["location"] == "/#share"
    assert client.post("/logout", follow_redirects=False).status_code == 303
    assert client.get("/api/cases").status_code == 401


@pytest.mark.parametrize("method,path", [("GET", "/api/pick-directory"), ("POST", "/api/cases"), ("POST", "/api/cases/"), ("POST", "/api/environment/install"), ("POST", "/api/fetch"), ("GET", "/api/monitor/status"), ("GET", "/api/automation/cases"), ("POST", "/api/cases/example/open-slicer")])
def test_cloud_blocks_local_operations_even_after_login(client, method, path):
    login(client)
    assert client.request(method, path).status_code == 403


def test_public_mcp_requires_local_host_and_dedicated_token(client, monkeypatch):
    import server

    class ReadyQueue:
        def health(self):
            return {"worker_alive": True}

    monkeypatch.setenv("WINDCATCHER_DEPLOYMENT", "public")
    monkeypatch.setenv("WINDCATCHER_PUBLIC_URL", "https://windcatcher.test")
    monkeypatch.setenv("WINDCATCHER_PUBLIC_USERNAME", "public-team")
    monkeypatch.setenv("WINDCATCHER_PUBLIC_PASSWORD", "public-test-password-very-long")
    monkeypatch.setenv("WINDCATCHER_PUBLIC_SESSION_SECRET", "independent-public-test-secret-32-characters")
    token = "a" * 43
    monkeypatch.setenv("WINDCATCHER_MCP_TOKEN", token)
    monkeypatch.setattr(server.app.state, "batch_queue", ReadyQueue(), raising=False)
    local = TestClient(cloud.create_app(), base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    path = "/api/automation/status"
    header = {"X-Windcatcher-MCP-Token": token}
    assert local.get(path).status_code == 401
    assert local.get(path, headers={"X-Windcatcher-MCP-Token": "wrong"}).status_code == 401
    assert local.get(path, headers=header).json()["worker_alive"] is True
    assert local.get(path, headers={**header, "X-Forwarded-For": "203.0.113.5"}).status_code == 401
    assert local.get(path, headers={**header, "Host": "windcatcher.test"}).status_code == 401
    assert local.get("/api/cases", headers=header).status_code == 401


def test_cloud_deployment_never_accepts_local_mcp_token(client, monkeypatch):
    monkeypatch.setenv("WINDCATCHER_MCP_TOKEN", "a" * 43)
    local = TestClient(cloud.create_app(), base_url="http://127.0.0.1:8765")
    response = local.get("/api/automation/status", headers={"X-Windcatcher-MCP-Token": "a" * 43})
    assert response.status_code == 401


@pytest.mark.parametrize("path", ["/login", "/logout", "/api/cases/upload"])
def test_cross_site_mutations_blocked(client, path):
    login(client)
    assert client.post(path, headers={"Origin": "https://untrusted.test"}).status_code == 403


def test_null_form_origin_is_rejected(client):
    assert client.post("/login", headers={"Origin": "null"}).status_code == 403


def test_public_origin_works_behind_proxy_without_accepting_other_origins(client, monkeypatch):
    monkeypatch.setenv("WINDCATCHER_CLOUD_PUBLIC_ORIGIN", "https://published.example")
    proxied = TestClient(cloud.create_app(), base_url="https://windcatcher.test")
    response = proxied.post("/login", headers={"Origin": "https://published.example"}, data={
        "username": "researcher", "password": "test-only-password-long-enough",
    }, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert proxied.post("/logout", headers={"Origin": "https://untrusted.test"}).status_code == 403
    assert proxied.post("/logout", headers={"Origin": "https://published.example"}, follow_redirects=False).status_code == 303


def test_invalid_public_origin_blocks_startup(monkeypatch, client):
    monkeypatch.setenv("WINDCATCHER_CLOUD_PUBLIC_ORIGIN", "https://user:password@invalid.test/path")
    with pytest.raises(RuntimeError, match="可信的 HTTPS"):
        cloud.create_app()


def test_expired_and_tampered_cookies_rejected(client):
    access = client.app.state.access
    for token in [access.session(expires=int(time.time()) - 1), access.session() + "x", "9999999999.nonce.invalid"]:
        client.cookies.set(cloud.COOKIE, token)
        assert client.get("/api/cases").status_code == 401
        client.cookies.clear()


def test_login_is_rate_limited_without_revealing_credentials(client):
    for _ in range(5):
        response = client.post("/login", data={"username": "researcher", "password": "wrong"}, follow_redirects=False)
        assert response.status_code == 303
        assert "set-cookie" not in response.headers
    error_page = client.get("/login?error=1")
    assert 'role="alert"' in error_page.text
    assert "账号或密码错误" in error_page.text
    limited = login(client)
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "900"
    assert "登录尝试过多" in limited.text


def test_malformed_login_rejected(client):
    assert client.post("/login", content="a=1&b=2&c=3&d=4&e=5").status_code == 400
    assert client.post("/login", content="x" * 4097).status_code == 413


@pytest.mark.parametrize("path,method", [("/api/cases/upload", "DELETE"), ("/api/cases/case-20261027-120000-abcd", "PUT"), ("/logout", "DELETE")])
def test_method_adapter_rejects_unlisted_operations(client, path, method):
    login(client)
    assert client.post(path, headers={"X-Windcatcher-Method": method}).status_code == 400


def test_cloud_case_delete_requires_session_and_same_origin(client):
    path = "/api/cases/case-20261027-120000-abcd"
    assert client.post(path, headers={"X-Windcatcher-Method": "DELETE"}).status_code == 401
    login(client)
    assert client.post(path, headers={"X-Windcatcher-Method": "DELETE", "Origin": "https://untrusted.test"}).status_code == 403


def test_shared_delete_checks_running_case_before_revoking_public_views(client, monkeypatch):
    from pipeline import cases, patient_shares, reference_cases

    case_id = "case-20261027-120000-abcd"
    root = cases.CASES_DIR / case_id
    root.mkdir(parents=True)
    (root / "case.json").write_text(json.dumps({
        "case_id": case_id, "stages": {"import": {"status": "running"}},
        "job": {"status": "running", "stage": "import"},
    }), encoding="utf-8")
    calls = []
    monkeypatch.setattr(patient_shares, "revoke_case", lambda _case_id: calls.append("share"))
    monkeypatch.setattr(reference_cases, "revoke_case", lambda _case_id: calls.append("reference"))
    assert login(client).status_code == 303
    result = client.post(f"/api/cases/{case_id}", headers={"X-Windcatcher-Method": "DELETE"})
    assert result.status_code == 409, result.text
    assert calls == []
    assert (root / "case.json").is_file()
    assert not (cases.CASES_DIR / f".windcatcher-deleting-{case_id}.json").exists()


def test_shared_partial_delete_can_retry_without_reexposing_case(client, monkeypatch):
    from pipeline import cases

    case_id = "case-20261027-120000-abcd"
    root = cases.CASES_DIR / case_id
    root.mkdir(parents=True)
    (root / "case.json").write_text(json.dumps({
        "case_id": case_id, "stages": {"import": {"status": "ok"}},
        "job": {"status": "completed"},
    }), encoding="utf-8")
    original_rmtree = cases.shutil.rmtree

    def fail_partial(path, *args, **kwargs):
        if cases.Path(path) == root:
            (root / "case.json").unlink()
            raise OSError("simulated partial deletion")
        return original_rmtree(path, *args, **kwargs)

    assert login(client).status_code == 303
    monkeypatch.setattr(cases.shutil, "rmtree", fail_partial)
    first = client.post(f"/api/cases/{case_id}", headers={"X-Windcatcher-Method": "DELETE"})
    assert first.status_code == 409, first.text
    marker = cases.CASES_DIR / f".windcatcher-deleting-{case_id}.json"
    assert marker.is_file()
    assert client.get(f"/api/cases/{case_id}").status_code == 404
    assert client.get(f"/api/cases/{case_id}/model").status_code == 404
    assert client.get("/api/cases").json() == []
    monkeypatch.setattr(cases.shutil, "rmtree", original_rmtree)
    second = client.post(f"/api/cases/{case_id}", headers={"X-Windcatcher-Method": "DELETE"})
    assert second.status_code == 200, second.text
    assert not marker.exists() and not root.exists()


def test_authenticated_cloud_does_not_expose_operator_history(client):
    login(client)
    assert client.get('/api/monitor/status').status_code == 403
    assert client.get('/api/monitor/events').status_code == 403
    assert client.post('/api/monitor/events', json={'events': []}).status_code == 403
    page = client.get('/').text
    assert 'data-route="activity"' in page
    assert 'data-route="activity" type="button" title="操作记录" aria-label="操作记录" hidden' in page


def test_login_allows_real_synthetic_upload_and_qc(client, tmp_path):
    from tests.make_phantom import build_phantom
    login(client)
    dicoms = sorted(build_phantom(tmp_path / "synthetic-input").glob("*.dcm"))
    response = client.post("/api/cases/upload", data={
        "synthetic": "true", "anonymize": "true",
        "relative_paths": json.dumps([p.name for p in dicoms]),
    }, files=[("files", (p.name, p.read_bytes(), "application/dicom")) for p in dicoms])
    assert response.status_code == 200, response.text
    case = client.get("/api/cases/" + response.json()["case_id"]).json()
    assert case["synthetic"] is True
    assert case["source_type"] == "browser_upload"
    assert case["stages"]["import"]["status"] == "ok"
    assert case["stages"]["qc"]["status"] in {"ok", "warn"}
    assert client.get("/reference-result").status_code == 404
    response = client.post("/api/cases/" + case["case_id"], headers={"X-Windcatcher-Method": "DELETE"})
    assert response.status_code == 200, response.text
    assert client.get("/api/cases").json() == []
