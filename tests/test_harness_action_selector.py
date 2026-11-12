"""Synthetic tests for tool-free, server-scoped Harness action selection."""

import json
import sys
from types import SimpleNamespace

import pytest

from scripts import harness_action_selector as selector


def request():
    return {
        "schema_version": 1,
        "stages": {name: "pending" for name in (
            "import", "anonymize", "qc", "segment", "surface",
            "cfd_prep", "cfd_run", "report",
        )},
        "baseline_ready": False,
        "numeric_ok": None,
        "eligible": None,
        "allowed_actions": ["segment_candidate", "wait_for_review"],
    }


def events():
    return [
        {"type": "turn/start", "data": {"turn": 1}},
        {"type": "step/start", "data": {"turn": 1, "step": 1}},
        {"type": "assistant/message", "data": {"turn": 1, "step": 1,
            "usage": {"inputTokens": 10, "outputTokens": 4, "totalTokens": 14}}},
        {"type": "step/end", "data": {"turn": 1, "step": 1}},
        {"type": "turn/end", "data": {"turn": 1}},
    ]


def test_request_accepts_only_fixed_anonymous_status_and_allowlist():
    value = request()
    assert selector.parse_request(json.dumps(value).encode()) == value
    prompt = selector._prompt(value)
    assert "segment_candidate" in prompt
    for marker in ("case_id", "patient_name", "CT", "point_lps_mm", "C:/private"):
        assert marker not in prompt


@pytest.mark.parametrize("change", [
    {"case_id": "case-20261102-000000-abcd"},
    {"patient_name": "private"},
    {"stages": {"segment": "ok"}},
    {"allowed_actions": ["submit_ready_cfd", "submit_ready_cfd"]},
    {"allowed_actions": ["confirm_mask"]},
    {"allowed_actions": []},
    {"allowed_actions": ["submit_ready_cfd"]},
    {"allowed_actions": ["propose_endplanes"]},
    {"allowed_actions": ["inspect_numeric_gate"]},
    {"eligible": True},
    {"baseline_ready": 1},
])
def test_rejects_invalid_or_sensitive_request(change):
    with pytest.raises(selector.ActionError):
        selector.validate_request(request() | change)


@pytest.mark.parametrize("raw", [
    b'{"schema_version":1,"schema_version":1}',
    b"<html>not JSON</html>",
    b"x" * (selector.MAX_INPUT_BYTES + 1),
])
def test_request_parser_rejects_malformed_payload(raw):
    with pytest.raises(selector.ActionError):
        selector.parse_request(raw)


def test_model_output_binds_to_input_but_does_not_execute():
    value = request()
    result = selector.validate_model_output(
        '{"action":"segment_candidate"}', value,
    )
    assert result["status"] == "selected_not_executed"
    assert result["action"] == "segment_candidate"
    assert len(result["input_sha256"]) == 64
    assert "case_id" not in result


def test_request_is_copied_before_model_call():
    value = request()
    frozen = selector.validate_request(value)
    value["allowed_actions"].append("submit_ready_cfd")
    assert frozen["allowed_actions"] == ["segment_candidate", "wait_for_review"]


def test_submit_requires_both_service_preflight_and_prepared_stage():
    value = request()
    value["baseline_ready"] = True
    value["allowed_actions"] = ["submit_ready_cfd"]
    with pytest.raises(selector.ActionError):
        selector.validate_request(value)
    value["stages"]["cfd_prep"] = "ok"
    assert selector.validate_request(value)["allowed_actions"] == ["submit_ready_cfd"]


@pytest.mark.parametrize("raw", [
    '{"action":"confirm_mask"}',
    '{"action":"submit_ready_cfd"}',
    '{"action":"segment_candidate","approved":true}',
    '{"action":"segment_candidate","action":"wait_for_review"}',
    "NOT JSON",
    '{}',
    'x' * (selector.MAX_OUTPUT_BYTES + 1),
])
def test_model_output_fails_closed_on_unauthorized_or_unstructured_action(raw):
    with pytest.raises(selector.ActionError):
        selector.validate_model_output(raw, request())


def test_sdk_path_uses_pinned_tool_free_profile_and_reports_usage(tmp_path, monkeypatch):
    dsh_bin = tmp_path / "dsh"
    dsh_bin.write_text("synthetic", encoding="utf-8")
    monkeypatch.setenv("WINDCATCHER_HARNESS_DSH_BIN", str(dsh_bin))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("WINDCATCHER_HARNESS_MODEL", "gpt-6.1-sol")
    monkeypatch.setenv("WINDCATCHER_HARNESS_BASE_URL",
                       "https://tongyimohe.aitoken.credit/v1")
    monkeypatch.setattr(selector.harness_sdk_worker, "verify_runtime", lambda *_: None)
    seen = {}

    class FakeHarness:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def run(self, prompt):
            assert "allowed_actions" in prompt
            return SimpleNamespace(finish_reason="completed",
                final_response='{"action":"segment_candidate"}', events=events())

    monkeypatch.setitem(sys.modules, "deepseek_harness",
                        SimpleNamespace(DeepSeekHarness=FakeHarness))
    result = selector.run_model(request())
    assert result["selection"]["action"] == "segment_candidate"
    assert result["metering"]["status"] == "reported"
    assert result["metering"]["total_tokens"] == 14
    assert seen["profile"] == "sdk-minimal"
    assert seen["patches"] == (str(selector.harness_sdk_worker.PATCH),)
    assert seen["provider"] == "windcatcher-relay"
    assert seen["model"] == "gpt-6.1-sol"


@pytest.mark.parametrize("finish,answer", [
    ("timeout", '{"action":"segment_candidate"}'),
    ("completed", '{"action":"confirm_mask"}'),
])
def test_sdk_failure_never_returns_action(tmp_path, monkeypatch, finish, answer):
    dsh_bin = tmp_path / "dsh"
    dsh_bin.write_text("synthetic", encoding="utf-8")
    monkeypatch.setenv("WINDCATCHER_HARNESS_DSH_BIN", str(dsh_bin))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("WINDCATCHER_HARNESS_MODEL", "gpt-6.1-sol")
    monkeypatch.setenv("WINDCATCHER_HARNESS_BASE_URL",
                       "https://tongyimohe.aitoken.credit/v1")
    monkeypatch.setattr(selector.harness_sdk_worker, "verify_runtime", lambda *_: None)

    class FakeHarness:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def run(self, _prompt):
            return SimpleNamespace(finish_reason=finish, final_response=answer,
                                   events=events())

    monkeypatch.setitem(sys.modules, "deepseek_harness",
                        SimpleNamespace(DeepSeekHarness=FakeHarness))
    with pytest.raises(selector.ActionError):
        selector.run_model(request())


def test_sdk_timeout_is_sanitized_and_fails_closed(tmp_path, monkeypatch):
    dsh_bin = tmp_path / "dsh"
    dsh_bin.write_text("synthetic", encoding="utf-8")
    monkeypatch.setenv("WINDCATCHER_HARNESS_DSH_BIN", str(dsh_bin))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("WINDCATCHER_HARNESS_MODEL", "gpt-6.1-sol")
    monkeypatch.setenv("WINDCATCHER_HARNESS_BASE_URL",
                       "https://tongyimohe.aitoken.credit/v1")
    monkeypatch.setattr(selector.harness_sdk_worker, "verify_runtime", lambda *_: None)

    class FakeHarness:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def run(self, _prompt):
            raise TimeoutError("secret-key-and-path")

    monkeypatch.setitem(sys.modules, "deepseek_harness",
                        SimpleNamespace(DeepSeekHarness=FakeHarness))
    with pytest.raises(selector.ActionError) as error:
        selector.run_model(request())
    assert "TimeoutError" in str(error.value)
    assert "secret-key-and-path" not in str(error.value)
