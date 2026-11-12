"""可选 AI 报告解读层的安全边界测试。

测试只使用合成报告，并把模型调用替换为本地函数；绝不访问真实患者数据或外部 API。
"""

from __future__ import annotations

import json
import ssl

import pytest

from pipeline import ai_report, cases


@pytest.fixture
def isolated_ai(tmp_path, monkeypatch):
    monkeypatch.setattr("pipeline.config.CASES_DIR", tmp_path / "cases")
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path / "cases")
    monkeypatch.setattr(ai_report, "CONFIG_PATH", tmp_path / "ai-config.json")
    monkeypatch.delenv(ai_report.API_KEY_ENV, raising=False)
    monkeypatch.delenv("WINDCATCHER_AI_API_URL", raising=False)
    monkeypatch.delenv("WINDCATCHER_AI_MODEL", raising=False)
    monkeypatch.delenv(ai_report.ALLOWED_HOSTS_ENV, raising=False)
    monkeypatch.setattr(ai_report, "_SESSION_API_KEY", "")
    monkeypatch.setattr(ai_report, "_SESSION_API_BASE_URL", "")
    monkeypatch.setattr(ai_report, "_USER_SESSION_KEYS", {})
    case_id = cases.create_case("synthetic", "<合成测试>", synthetic=True)
    return case_id, cases.case_dir(case_id)


def _report(*, eligible: bool = True) -> dict:
    return {
        "generated_at": "2026-10-06T12:00:00+08:00",
        "case_id": "patient-A",
        "source_path": r"E:\patient-A\source",
        "patient_name": "真实姓名",
        "gate": {"eligible": eligible, "status": "passed" if eligible else "blocked", "reasons": []},
        "baseline_flow": {
            "status": "observed",
            "data_status": "real",
            "validation_status": "passed" if eligible else "pending",
            "flow_lpm": 15.0,
            "pressure_drop": {"value": 3.2, "unit": "Pa"},
            "velocity_distribution": {
                "velocity": {"min": 0.1, "mean": 1.2, "p95": 3.4, "max": 4.5, "sample_count": 12, "unit": "m/s"},
                "pressure": {"min": 0.0, "mean": 1.1, "p95": 2.8, "max": 3.2, "sample_count": 12, "unit": "Pa"},
            },
            "left_right_flow": {"left_lpm": 8.0, "right_lpm": 7.0, "left_fraction": 0.53, "right_fraction": 0.47},
            "narrow_regions": {"status": "not_available", "reason": "未生成沿程曲线"},
        },
        "obstruction_comparison": {"status": "not_available", "items": []},
        "design_reference": {"status": "pending", "uncertainty": []},
    }


def _model_response(*, recommendation_allowed: bool = False) -> dict:
    return {
        "choices": [{"message": {"content": json.dumps({
            "summary": "仅根据已提供的事实统计进行解读。",
            "observations": ["入口流量记录为 15 L/min。"],
            "limitations": ["狭窄区域沿程曲线尚未生成。"],
            "design_reference": {
                "status": "pending",
                "text": "需要工程复核后再讨论设计。",
                "recommendation_allowed": recommendation_allowed,
            },
            "source_fields": ["baseline_flow.flow_lpm"],
        }, ensure_ascii=False)}}],
    }


def test_safe_payload_has_only_whitelisted_observations():
    payload = ai_report.build_safe_payload(_report())

    encoded = json.dumps(payload, ensure_ascii=False)
    assert "patient-A" not in encoded
    assert "source_path" not in encoded
    assert "真实姓名" not in encoded
    assert "DICOM" not in encoded
    assert "STL" not in encoded
    assert "case_id" not in payload
    assert "source_path" not in payload
    assert payload["baseline_flow"]["flow_lpm"] == 15.0


def test_invalid_api_url_is_rejected(isolated_ai):
    with pytest.raises(ai_report.AIReportError) as exc_info:
        ai_report.save_config("file:///tmp/key", "test-model")
    assert exc_info.value.status_code == 400

    with pytest.raises(ai_report.AIReportError) as exc_info:
        ai_report.save_config("https://api.example.test/v1?api_key=secret", "test-model")
    assert exc_info.value.status_code == 400


def test_generate_requires_environment_key(isolated_ai, monkeypatch):
    case_id, _root = isolated_ai
    ai_report.save_config("https://example.invalid/v1", "test-model")

    with pytest.raises(ai_report.AIReportError) as exc_info:
        ai_report.generate(case_id, _report())
    assert exc_info.value.status_code == 409
    assert "密钥" in exc_info.value.reason


@pytest.mark.parametrize("content", ["not-json", json.dumps({"summary": "不完整"})])
def test_invalid_model_response_is_not_saved(isolated_ai, monkeypatch, content):
    case_id, root = isolated_ai
    ai_report.save_config("https://example.invalid/v1", "test-model")
    monkeypatch.setenv(ai_report.API_KEY_ENV, "test-key")
    monkeypatch.setattr(ai_report, "_call_model", lambda _config, _messages: {"choices": [{"message": {"content": content}}]})

    with pytest.raises(ai_report.AIReportError) as exc_info:
        ai_report.generate(case_id, _report())
    assert exc_info.value.status_code == 502
    assert not (root / "report" / "ai-interpretation.json").exists()


def test_pending_gate_rejects_deterministic_design_recommendation(isolated_ai, monkeypatch):
    case_id, root = isolated_ai
    ai_report.save_config("https://example.invalid/v1", "test-model")
    monkeypatch.setenv(ai_report.API_KEY_ENV, "test-key")
    monkeypatch.setattr(ai_report, "_call_model", lambda _config, _messages: _model_response(recommendation_allowed=True))

    with pytest.raises(ai_report.AIReportError, match="结果闸门未通过"):
        ai_report.generate(case_id, _report(eligible=False))
    assert not (root / "report" / "ai-interpretation.json").exists()


def test_valid_response_is_saved_as_local_interpretation(isolated_ai, monkeypatch):
    case_id, root = isolated_ai
    ai_report.save_config("https://example.invalid/v1", "test-model")
    monkeypatch.setenv(ai_report.API_KEY_ENV, "test-key")
    monkeypatch.setattr(ai_report, "_call_model", lambda _config, _messages: _model_response())

    result = ai_report.generate(case_id, _report())
    output = root / "report" / "ai-interpretation.json"
    assert output.exists()
    assert result["case_id"] == case_id
    assert result["interpretation"]["design_reference"]["recommendation_allowed"] is False
    assert json.loads(output.read_text(encoding="utf-8"))["model"] == "test-model"


class _ModelsResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.payload


def test_list_models_uses_transient_key_and_returns_sorted_ids(isolated_ai, monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["authorization"] = request.get_header("Authorization")
        seen["method"] = request.method
        seen["timeout"] = timeout
        return _ModelsResponse(json.dumps({"data": [
            {"id": "zeta", "owned_by": "provider"},
            {"id": "alpha"},
            {"id": "zeta"},
            {"object": "model"},
        ]}).encode("utf-8"))

    monkeypatch.setattr(ai_report.urllib.request, "urlopen", fake_urlopen)
    result = ai_report.list_models("https://example.test/v1", "temporary-secret")

    assert seen == {
        "url": "https://example.test/v1/models",
        "authorization": "Bearer temporary-secret",
        "method": "GET",
        "timeout": 30,
    }
    assert result["models"] == [{"id": "alpha"}, {"id": "zeta", "owned_by": "provider"}]
    assert not (isolated_ai[1] / ".ai-report-config.json").exists()
    assert ai_report._active_api_key("https://example.test/v1") == ""


def test_list_models_rejects_missing_key_and_malformed_response(isolated_ai, monkeypatch):
    with pytest.raises(ai_report.AIReportError) as exc_info:
        ai_report.list_models("https://example.test/v1", "")
    assert exc_info.value.status_code == 400

    monkeypatch.setattr(ai_report.urllib.request, "urlopen", lambda *_args, **_kwargs: _ModelsResponse(b'{"data": {}}'))
    with pytest.raises(ai_report.AIReportError, match="data 数组"):
        ai_report.list_models("https://example.test/v1", "temporary-secret")


def test_save_config_accepts_key_without_persisting_it(isolated_ai):
    ai_report.save_config("https://example.test/v1", "test-model", "temporary-secret")
    saved = json.loads(ai_report.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved == {"base_url": "https://example.test/v1", "model": "test-model"}
    assert ai_report._active_api_key("https://example.test/v1") == "temporary-secret"


def test_endpoint_urls_normalize_to_one_base(isolated_ai):
    assert ai_report._valid_url("https://example.test/v1/models") == "https://example.test/v1"
    assert ai_report._valid_url("https://example.test/v1/chat/completions") == "https://example.test/v1"
    assert ai_report._models_endpoint("https://example.test/v1/chat/completions") == "https://example.test/v1/models"


def test_model_lookup_can_fallback_to_same_url_key_without_overwriting_it(isolated_ai, monkeypatch):
    ai_report.save_config("https://old.example/v1", "old-model", "old-secret")
    seen = {}

    def fake_urlopen(request, timeout):
        seen["authorization"] = request.get_header("Authorization")
        return _ModelsResponse(b'{"data":[{"id":"new-model"}]}')

    monkeypatch.setattr(ai_report.urllib.request, "urlopen", fake_urlopen)
    result = ai_report.list_models("https://old.example/v1/models", "")
    assert result["base_url"] == "https://old.example/v1"
    assert seen["authorization"] == "Bearer old-secret"
    assert ai_report._active_api_key("https://old.example/v1") == "old-secret"


def test_session_key_does_not_cross_urls_and_overrides_environment(isolated_ai, monkeypatch):
    monkeypatch.setenv(ai_report.API_KEY_ENV, "environment-secret")
    ai_report.save_config("https://saved.example/v1", "saved-model", "saved-secret")
    assert ai_report._active_api_key("https://saved.example/v1") == "saved-secret"
    assert ai_report._active_api_key("https://other.example/v1") == "environment-secret"


@pytest.mark.parametrize("url", [
    "http://allowed.example/v1", "https://allowed.example:8443/v1",
    "https://127.0.0.1/v1", "https://10.0.0.1/v1",
    "https://[::1]/v1", "https://localhost/v1",
    "https://allowed.example.evil/v1", "https://other.example/v1",
])
def test_personal_provider_rejects_unsafe_or_unlisted_targets(isolated_ai, monkeypatch, url):
    monkeypatch.setenv(ai_report.ALLOWED_HOSTS_ENV, "allowed.example")
    token = ai_report.CURRENT_USER_ID.set("a" * 32)
    try:
        with pytest.raises(ai_report.AIReportError):
            ai_report.save_config(url, "test-model", "test-only-secret")
        assert not (ai_report.CONFIG_PATH.parent / ".ai-report-users").exists()
    finally:
        ai_report.CURRENT_USER_ID.reset(token)


def test_personal_provider_fails_closed_without_admin_allowlist(isolated_ai, monkeypatch):
    monkeypatch.setenv(ai_report.API_KEY_ENV, "environment-secret")
    token = ai_report.CURRENT_USER_ID.set("a" * 32)
    try:
        assert ai_report.get_config()["api_key_configured"] is False
        with pytest.raises(ai_report.AIReportError, match="允许域名"):
            ai_report.save_config("https://allowed.example/v1", "test-model", "test-only-secret")
        with pytest.raises(ai_report.AIReportError, match="允许域名"):
            ai_report.list_models("https://allowed.example/v1", "test-only-secret")
    finally:
        ai_report.CURRENT_USER_ID.reset(token)


def test_personal_provider_blocks_private_dns_before_network(isolated_ai, monkeypatch):
    monkeypatch.setenv(ai_report.ALLOWED_HOSTS_ENV, "allowed.example")
    monkeypatch.setattr(ai_report.socket, "getaddrinfo",
                        lambda *_args, **_kwargs: [(2, 1, 6, "", ("10.8.0.2", 443))])
    monkeypatch.setattr(ai_report.urllib.request, "build_opener",
                        lambda *_args: pytest.fail("network opener must not be built"))
    token = ai_report.CURRENT_USER_ID.set("a" * 32)
    try:
        with pytest.raises(ai_report.AIReportError, match="非公网地址"):
            ai_report.list_models("https://allowed.example/v1", "test-only-secret")
    finally:
        ai_report.CURRENT_USER_ID.reset(token)


def test_personal_provider_rejects_redirect_to_other_host(isolated_ai, monkeypatch):
    monkeypatch.setenv(ai_report.ALLOWED_HOSTS_ENV, "allowed.example")
    monkeypatch.setattr(ai_report.socket, "getaddrinfo",
                        lambda *_args, **_kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))])

    class RedirectingOpener:
        def __init__(self, handlers):
            assert any(isinstance(item, ai_report._RejectRedirect) for item in handlers)
            self.handler = next(item for item in handlers if isinstance(item, ai_report._RejectRedirect))

        def open(self, request, timeout):
            return self.handler.redirect_request(request, None, 302, "Found", {}, "https://private.example/v1/models")

    monkeypatch.setattr(ai_report.urllib.request, "build_opener",
                        lambda *handlers: RedirectingOpener(handlers))
    token = ai_report.CURRENT_USER_ID.set("a" * 32)
    try:
        with pytest.raises(ai_report.AIReportError, match="重定向"):
            ai_report.list_models("https://allowed.example/v1", "test-only-secret")
    finally:
        ai_report.CURRENT_USER_ID.reset(token)


def test_personal_provider_pins_verified_ip_and_ignores_proxy(isolated_ai, monkeypatch):
    monkeypatch.setenv(ai_report.ALLOWED_HOSTS_ENV, "allowed.example")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8080")
    lookups = []
    connected = []
    wrapped = []

    def changing_dns(host, port, **kwargs):
        lookups.append((host, port))
        if len(lookups) > 1:
            return [(ai_report.socket.AF_INET, ai_report.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        return [(ai_report.socket.AF_INET, ai_report.socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    class FakeSocket:
        def settimeout(self, timeout):
            assert timeout == 30

        def connect(self, address):
            connected.append(address)

        def close(self):
            pass

    class FakeContext:
        def wrap_socket(self, sock, server_hostname):
            wrapped.append(server_hostname)
            return sock

    def fake_opener(*handlers):
        assert any(isinstance(h, ai_report._RejectRedirect) for h in handlers)
        assert any(isinstance(h, ai_report.urllib.request.ProxyHandler) and h.proxies == {} for h in handlers)
        pinned = next(h for h in handlers if isinstance(h, ai_report._PinnedHTTPSHandler))

        class Opener:
            def open(self, request, timeout):
                def do_open(connection_factory, req, **_kwargs):
                    conn = connection_factory(req.host, context=FakeContext(), timeout=timeout)
                    conn.connect()
                    return _ModelsResponse(b'{"data":[]}')

                pinned.do_open = do_open
                return pinned.https_open(request)

        return Opener()

    monkeypatch.setattr(ai_report.socket, "getaddrinfo", changing_dns)
    monkeypatch.setattr(ai_report.socket, "socket", lambda *_args: FakeSocket())
    monkeypatch.setattr(ai_report.urllib.request, "build_opener", fake_opener)
    token = ai_report.CURRENT_USER_ID.set("a" * 32)
    try:
        assert ai_report.list_models("https://allowed.example/v1", "test-only-secret")["count"] == 0
    finally:
        ai_report.CURRENT_USER_ID.reset(token)
    assert lookups == [("allowed.example", 443)]
    assert connected == [("8.8.8.8", 443)]
    assert wrapped == ["allowed.example"]


def test_pinned_https_connection_preserves_certificate_verification():
    conn = ai_report._PinnedHTTPSConnection(
        "allowed.example", ((ai_report.socket.AF_INET, ai_report.socket.SOCK_STREAM, 6, ("8.8.8.8", 443)),),
    )
    assert conn._context.check_hostname is True
    assert conn._context.verify_mode == ssl.CERT_REQUIRED


def test_revoked_allowlist_blocks_existing_personal_config(isolated_ai, monkeypatch):
    monkeypatch.setenv(ai_report.ALLOWED_HOSTS_ENV, "allowed.example")
    token = ai_report.CURRENT_USER_ID.set("a" * 32)
    try:
        ai_report.save_config("https://allowed.example/v1", "test-model", "test-only-secret")
        assert ai_report.get_config()["configured"] is True
        monkeypatch.setenv(ai_report.ALLOWED_HOSTS_ENV, "other.example")
        config = ai_report.get_config()
        assert config["configured"] is False
        assert "未获管理员允许" in config["reason"]
        with pytest.raises(ai_report.AIReportError, match="未获管理员允许"):
            ai_report.list_models("https://allowed.example/v1", "")
    finally:
        ai_report.CURRENT_USER_ID.reset(token)


def test_generate_uses_each_accounts_key_without_global_fallback(isolated_ai, monkeypatch):
    case_id, folder = isolated_ai
    monkeypatch.setenv(ai_report.ALLOWED_HOSTS_ENV, "allowed.example")
    monkeypatch.setenv(ai_report.API_KEY_ENV, "global-secret")
    seen = []

    def fake_open(request, timeout):
        from io import BytesIO
        seen.append(request.get_header("Authorization"))
        return BytesIO(json.dumps(_model_response()).encode("utf-8"))

    monkeypatch.setattr(ai_report, "_open_provider_request", fake_open)
    first = ai_report.CURRENT_USER_ID.set("a" * 32)
    try:
        ai_report.save_config("https://allowed.example/v1", "first-model", "first-secret")
        ai_report.generate(case_id, _report())
    finally:
        ai_report.CURRENT_USER_ID.reset(first)
    second = ai_report.CURRENT_USER_ID.set("b" * 32)
    try:
        with pytest.raises(ai_report.AIReportError, match="URL"):
            ai_report.generate(case_id, _report())
        ai_report.save_config("https://allowed.example/v1", "second-model")
        with pytest.raises(ai_report.AIReportError, match="密钥"):
            ai_report.generate(case_id, _report())
        ai_report.save_config("https://allowed.example/v1", "second-model", "second-secret")
        ai_report.generate(case_id, _report())
    finally:
        ai_report.CURRENT_USER_ID.reset(second)
    assert seen == ["Bearer first-secret", "Bearer second-secret"]
    saved_report = (folder / "report" / "ai-interpretation.json").read_text(encoding="utf-8")
    assert "first-secret" not in saved_report and "second-secret" not in saved_report
