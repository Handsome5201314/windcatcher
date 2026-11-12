"""Synthetic contract tests for the pinned, tool-free Harness relay adapter."""

import json
import sys
from types import SimpleNamespace

import pytest

from scripts.check_harness_profile import (
    DISABLED_ROWS, EXPECTED_ROWS, ProfileError, check_composed_config,
)
from scripts.harness_sdk_worker import (
    AdviceError, _prompt, extract_metering, validate_metering, validate_model_output,
)
from scripts import harness_sdk_worker


def summary():
    return {
        "schema_version": 1, "case_id": "synthetic-harness-001", "state_sha256": "a" * 64,
        "stages": {name: "ok" for name in ("import", "anonymize", "qc", "segment",
                                                   "surface", "cfd_prep", "cfd_run", "report")},
        "baseline_ready": True, "numeric_ok": True, "eligible": True,
    }


def composed(*, enable=None, extra=None, mode="read-only"):
    rows = []
    for row_id, name in EXPECTED_ROWS.items():
        part = f"- id: {row_id}\n  name: '{name}'\n"
        if row_id in DISABLED_ROWS and row_id != enable:
            part += "  disabled: true\n"
        if row_id == "sandbox-policy":
            part += f"  config:\n    mode: {mode}\n"
        if row_id == "agent-loop":
            part += "  config:\n    agents: []\n"
        if row_id == "llm-pi-ai":
            part += (
                "  config:\n    providers:\n      windcatcher-relay:\n"
                "        apiKeyEnv: DEEPSEEK_API_KEY\n"
                "        api: openai-completions\n"
                "        baseURL: https://tongyimohe.aitoken.credit/v1\n"
                "        defaultContextWindow: 32768\n"
                "        defaultMaxTokens: 800\n"
                "        compat:\n"
                "          maxTokensField: max_completion_tokens\n"
                "        models:\n"
                "          - id: gpt-6.1-sol\n"
                "            name: gpt-6.1-sol\n"
            )
        rows.append(part)
    if extra:
        rows.append(extra)
    return "".join(rows)


def model_json(**overrides):
    value = {
        "next_step": {"code": "review_next_stage", "stage": "qc"},
        "failure_recap": [],
        "skill_draft": {"trigger_condition": "manual", "suggested_steps": ["qc"],
                        "allowed_tools": []},
        "human_review_required": True,
        "automated_action": False,
    }
    value.update(overrides)
    return json.dumps(value)


def test_composed_profile_is_tool_free_and_fails_on_drift():
    check_composed_config(composed())
    for changed in (
        composed(enable="persistent-pwsh"),
        composed(enable="tool-fs"),
        composed(enable="mcp-resources"),
        composed(enable="llm-deepseek"),
        composed(extra="- id: injected-tool\n  name: '@deepseek-ai/dsh-tool-fs'\n"),
        composed(mode="danger-full-access"),
        composed().replace("api: openai-completions", "api: anthropic-messages"),
        composed().replace("baseURL: https://tongyimohe.aitoken.credit/v1",
                           "baseURL: https://other.example/v1"),
        composed().replace("defaultContextWindow: 32768",
                           "defaultContextWindow: 8192"),
    ):
        with pytest.raises(ProfileError):
            check_composed_config(changed)


def test_worker_prompt_excludes_case_identifier_and_digest():
    value = summary()
    prompt = _prompt(value)
    assert value["case_id"] not in prompt
    assert value["state_sha256"] not in prompt
    assert "patient_name" not in prompt
    assert "automated_action:false" in prompt


def test_worker_accepts_codes_only_and_marks_real_source():
    value = summary()
    result = validate_model_output(model_json(), value)
    assert result["source"] == "deepseek-harness"
    assert result["case_id"] == value["case_id"]
    assert result["state_sha256"] == value["state_sha256"]
    assert result["automated_action"] is False


@pytest.mark.parametrize("reply", [
    model_json(automated_action=True),
    model_json(next_step={"code": "diagnose_patient", "stage": "qc"}),
    model_json(skill_draft={"trigger_condition": "manual", "suggested_steps": ["qc"],
                                 "allowed_tools": ["submit_cfd_batch"]}),
    model_json(patient_name="PRIVATE_PATIENT_NAME"),
    '{"next_step":{},"next_step":{}}',
    "<html>not JSON</html>",
])
def test_worker_rejects_unsafe_or_unstructured_model_reply(reply):
    with pytest.raises(AdviceError):
        validate_model_output(reply, summary())


def usage(*, input_tokens=12, output_tokens=8, total_tokens=20, **extra):
    return {"inputTokens": input_tokens, "outputTokens": output_tokens,
            "totalTokens": total_tokens, **extra}


def one_turn(message_data):
    return [
        {"type": "agent/inbox/spliced", "data": {}},  # SDK admission precedes the Turn.
        {"type": "turn/start", "data": {"turn": 1}},
        {"type": "step/start", "data": {"turn": 1, "step": 1}},
        {"type": "assistant/message", "data": {"turn": 1, "step": 1, **message_data}},
        {"type": "step/end", "data": {"turn": 1, "step": 1}},
        {"type": "turn/end", "data": {"turn": 1}},
    ]


def retried_turn(first_usage, second_usage):
    return [
        {"type": "turn/start", "data": {"turn": 1}},
        {"type": "step/start", "data": {"turn": 1, "step": 1}},
        {"type": "assistant/attempt", "data": {"turn": 1, "step": 1,
            "stream": ([] if first_usage is None else [
                {"type": "chunk", "chunk": {"type": "usage", "usage": first_usage}},
            ])}},
        {"type": "llm/retry", "data": {"turn": 1, "step": 1}},
        {"type": "llm/retry-started", "data": {"turn": 1, "step": 1}},
        {"type": "assistant/message", "data": {"turn": 1, "step": 1,
            "usage": second_usage}},
        {"type": "step/end", "data": {"turn": 1, "step": 1}},
        {"type": "turn/end", "data": {"turn": 1}},
    ]


def test_sdk_event_usage_keeps_missing_cache_and_cost_unknown():
    events = one_turn({"usage": usage()})
    metering = extract_metering(events, "gpt-6.1-sol")
    assert validate_metering(metering, "gpt-6.1-sol") == metering
    assert metering["status"] == "reported"
    assert metering["attempt_count"] == metering["reported_attempt_count"] == 1
    assert metering["input_tokens"] == 12
    assert metering["output_tokens"] == 8
    assert metering["total_tokens"] == 20
    assert metering["cache_read_tokens"] is None
    assert metering["cache_write_tokens"] is None
    assert metering["cost_usd"] is None


def test_sdk_event_usage_counts_each_retry_and_committed_message_once():
    first = usage(input_tokens=10, output_tokens=2, total_tokens=13, cacheReadTokens=1,
                  cacheWriteTokens=0)
    second = usage(input_tokens=12, output_tokens=8, total_tokens=22, cacheReadTokens=2,
                   cacheWriteTokens=0)
    events = retried_turn(first, second)
    events[2]["data"]["stream"].insert(0, {"type": "chunk", "chunk": {
        "type": "usage", "usage": usage(input_tokens=10, output_tokens=1, total_tokens=11),
    }})
    events[5]["data"]["stream"] = [{"type": "chunk", "chunk": {
        "type": "usage", "usage": second,
    }}]
    metering = extract_metering(events, "gpt-6.1-sol")
    assert metering["attempt_count"] == metering["reported_attempt_count"] == 2
    assert metering["status"] == "reported"
    assert metering["input_tokens"] == 22
    assert metering["output_tokens"] == 10
    assert metering["total_tokens"] == 35
    assert metering["cache_read_tokens"] == 3
    assert metering["cache_write_tokens"] == 0


def test_committed_message_stream_usage_is_used_when_data_usage_absent():
    events = one_turn({"stream": [{"type": "chunk", "chunk": {
        "type": "usage", "usage": usage(),
    }}]})
    metering = extract_metering(events, "gpt-6.1-sol")
    assert metering["status"] == "reported"
    assert metering["total_tokens"] == 20


def test_retry_without_settled_attempt_never_reports_complete_totals():
    events = retried_turn(None, usage())
    events.pop(2)  # Retry event remains, but the failed attempt was not settled.
    metering = extract_metering(events, "gpt-6.1-sol")
    assert metering["status"] != "reported"
    assert metering["total_tokens"] is None


@pytest.mark.parametrize("events,expected_status,attempts,reported", [
    ([], "unavailable", 0, 0),
    (one_turn({}), "unavailable", 1, 0),
    (retried_turn(None, usage()), "partial", 2, 1),
    (one_turn({"usage": {"inputTokens": 12, "outputTokens": 8}}), "partial", 1, 1),
    (one_turn({"usage": usage()})[:-1], "unavailable", 0, 0),
])
def test_missing_usage_is_unknown_not_zero(events, expected_status, attempts, reported):
    metering = extract_metering(events, "gpt-6.1-sol")
    assert validate_metering(metering, "gpt-6.1-sol") == metering
    assert metering["status"] == expected_status
    assert metering["attempt_count"] == attempts
    assert metering["reported_attempt_count"] == reported
    assert all(metering[name] is None for name in (
        "input_tokens", "output_tokens", "total_tokens",
        "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
        "cost_usd",
    ))


@pytest.mark.parametrize("bad", [
    usage(input_tokens=-1),
    usage(output_tokens=True),
    usage(total_tokens=19, cacheReadTokens=1),
    usage(total_tokens=23, cacheReadTokens=1, cacheWriteTokens=1),
    usage(reasoningTokens=9),
])
def test_invalid_sdk_usage_fails_closed(bad):
    with pytest.raises(AdviceError):
        extract_metering(one_turn({"usage": bad}),
                         "gpt-6.1-sol")


def test_sdk_model_call_returns_advice_and_event_metering_without_real_request(tmp_path, monkeypatch):
    dsh_bin = tmp_path / "dsh"
    dsh_bin.write_text("synthetic", encoding="utf-8")
    monkeypatch.setenv("WINDCATCHER_HARNESS_DSH_BIN", str(dsh_bin))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("WINDCATCHER_HARNESS_MODEL", "gpt-6.1-sol")
    monkeypatch.setenv("WINDCATCHER_HARNESS_BASE_URL",
                       "https://tongyimohe.aitoken.credit/v1")
    monkeypatch.setattr(harness_sdk_worker, "verify_runtime", lambda *_args: None)
    seen = {}

    class FakeHarness:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def run(self, _prompt):
            return SimpleNamespace(finish_reason="completed", final_response=model_json(),
                                   events=one_turn({"usage": usage()}))

    monkeypatch.setitem(sys.modules, "deepseek_harness",
                        SimpleNamespace(DeepSeekHarness=FakeHarness))
    result = harness_sdk_worker.run_model(summary())
    assert result["advice"]["source"] == "deepseek-harness"
    assert result["metering"]["status"] == "reported"
    assert result["metering"]["total_tokens"] == 20
    assert result["metering"]["cost_usd"] is None
    assert seen["provider"] == "windcatcher-relay"
    assert seen["model"] == "gpt-6.1-sol"
    assert "base_url" not in seen
