"""One-shot DeepSeek Harness advisory worker; never opens a Windcatcher case.

Only an anonymous, fixed-code state summary arrives on stdin. This process has
no Windcatcher MCP server and the pinned sdk-minimal profile advertises no tools.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

if __package__:
    from .check_harness_profile import ProfileError, verify_runtime
    from .harness_readonly_sidecar import (
        MAX_INPUT_BYTES, OUTPUT_STEPS, SCHEMA_VERSION, STAGES, SummaryError,
        _unique_pairs, _validate,
    )
else:
    from check_harness_profile import ProfileError, verify_runtime
    from harness_readonly_sidecar import (
    MAX_INPUT_BYTES, OUTPUT_STEPS, SCHEMA_VERSION, STAGES, SummaryError,
    _unique_pairs, _validate,
    )


NEXT_CODES = frozenset({
    "review_failed_stage", "review_numeric_gate", "review_warning_stage",
    "review_preparation", "review_eligibility", "review_next_stage", "review_report",
})
RECAP_CODES = frozenset({
    "stage_failure", "stage_warning", "baseline_blocked",
    "numeric_gate_failed", "eligibility_not_met",
})
TRIGGERS = frozenset({"manual", "numeric_gate_failed"})
MAX_OUTPUT_BYTES = 4096
PATCH = Path(__file__).resolve().parents[1] / "deploy" / "harness-readonly.patch.yml"
RELAY_BASE_URL = "https://tongyimohe.aitoken.credit/v1"
RELAY_MODEL = "gpt-6.1-sol"
RELAY_PROVIDER = "windcatcher-relay"
METERING_FIELDS = ("input_tokens", "output_tokens", "total_tokens",
                   "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
_USAGE_KEYS = dict(zip(METERING_FIELDS, ("inputTokens", "outputTokens", "totalTokens",
                                         "cacheReadTokens", "cacheWriteTokens", "reasoningTokens")))


class AdviceError(ValueError):
    """A model response failed the code-only contract."""


def configured_base_url() -> str:
    value = os.environ.get("WINDCATCHER_HARNESS_BASE_URL")
    if value != RELAY_BASE_URL:
        raise ProfileError("Harness base URL invalid")
    return value


def _usage_from_stream(stream: object) -> dict | None:
    if not isinstance(stream, list):
        return None
    reported = None
    for row in stream:
        if not isinstance(row, dict) or row.get("type") != "chunk":
            continue
        chunk = row.get("chunk")
        if isinstance(chunk, dict) and chunk.get("type") == "usage":
            reported = chunk.get("usage")  # Last cumulative value for this attempt.
    return reported


def _usage_from_event(event: dict) -> dict | None:
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    if event["type"] == "assistant/message" and data.get("usage") is not None:
        # The stream repeats cumulative usage. Prefer the committed message's
        # sample and count this attempt once.
        return data["usage"]
    return _usage_from_stream(data.get("stream"))


def _validated_usage(raw: object) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise AdviceError("Harness 用量格式无效")
    values = {}
    for local, source in _USAGE_KEYS.items():
        value = raw.get(source)
        if value is not None and (type(value) is not int or not 0 <= value <= 1_000_000_000):
            raise AdviceError("Harness 用量数值无效")
        values[local] = value
    known_sum = sum(values[name] or 0 for name in (
        "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))
    if values["total_tokens"] is not None and values["total_tokens"] < known_sum:
        raise AdviceError("Harness 用量总数矛盾")
    if (values["total_tokens"] is not None and values["cache_read_tokens"] is not None
            and values["cache_write_tokens"] is not None
            and values["total_tokens"] != known_sum):
        raise AdviceError("Harness 缓存用量总数矛盾")
    if (values["reasoning_tokens"] is not None and values["output_tokens"] is not None
            and values["reasoning_tokens"] > values["output_tokens"]):
        raise AdviceError("Harness 推理用量总数矛盾")
    return values


def extract_metering(events: list, model: str) -> dict:
    """Extract bounded provider-reported counts, never estimate missing usage."""
    if not isinstance(events, list) or len(events) > 10_000:
        raise AdviceError("Harness 事件数量无效")
    starts = [index for index, event in enumerate(events)
              if isinstance(event, dict) and event.get("type") == "turn/start"]
    ends = [index for index, event in enumerate(events)
            if isinstance(event, dict) and event.get("type") == "turn/end"]
    bounded = len(starts) == len(ends) == 1 and starts[0] < ends[0]
    lifecycle_types = {"turn/start", "turn/end", "step/start", "step/end",
                       "assistant/message", "assistant/attempt",
                       "llm/retry", "llm/retry-started"}
    outside = (events[:starts[0]] + events[ends[0] + 1:]) if bounded else []
    lifecycle_ok = bounded and not any(
        not isinstance(event, dict) or event.get("type") in lifecycle_types
        for event in outside
    )
    turn_events = events[starts[0]:ends[0] + 1] if bounded else events
    observations = []
    state = "idle"
    turn = None
    step = None
    saw_end = False
    for event in turn_events:
        if not isinstance(event, dict):
            lifecycle_ok = False
            break
        kind = event.get("type")
        data = event.get("data")
        if not isinstance(data, dict):
            lifecycle_ok = False
            break
        if kind == "turn/start":
            if (turn is not None or state != "idle" or type(data.get("turn")) is not int
                    or data["turn"] < 0):
                lifecycle_ok = False
                break
            turn = data["turn"]
            continue
        if turn is None or saw_end:
            lifecycle_ok = False
            break
        if kind == "turn/end":
            if data.get("turn") != turn or state != "idle":
                lifecycle_ok = False
                break
            saw_end = True
            continue
        if kind == "step/start":
            if (data.get("turn") != turn or state != "idle"
                    or type(data.get("step")) is not int or data["step"] < 0):
                lifecycle_ok = False
                break
            step = data["step"]
            state = "open"
        elif kind == "llm/retry-started":
            if data.get("turn") != turn or data.get("step") != step or state != "retry":
                lifecycle_ok = False
                break
            state = "open"
        elif kind in {"assistant/message", "assistant/attempt"}:
            if data.get("turn") != turn or data.get("step") != step or state != "open":
                lifecycle_ok = False
                break
            if len(observations) >= 64:
                raise AdviceError("Harness 模型尝试过多")
            observations.append(_validated_usage(_usage_from_event(event)))
            state = "message" if kind == "assistant/message" else "attempt"
        elif kind == "llm/retry":
            if data.get("turn") != turn or data.get("step") != step or state not in {"open", "attempt"}:
                lifecycle_ok = False
                break
            if state == "open":
                # A retry without a settled attempt has unknown billed usage.
                observations.append(None)
            state = "retry"
        elif kind == "step/end":
            if data.get("turn") != turn or data.get("step") != step or state not in {
                "message", "attempt", "retry",
            }:
                lifecycle_ok = False
                break
            state = "idle"
            step = None
    lifecycle_ok = lifecycle_ok and saw_end and state == "idle"
    reported = [item for item in observations if item is not None]
    complete = lifecycle_ok and bool(observations) and len(reported) == len(observations) and all(
        all(item[name] is not None for name in ("input_tokens", "output_tokens", "total_tokens"))
        for item in reported
    )
    status = "reported" if complete else "partial" if reported else "unavailable"
    counts = {}
    for name in METERING_FIELDS:
        counts[name] = (sum(item[name] for item in reported)
                        if complete and all(item[name] is not None for item in reported) else None)
    return {
        "schema_version": 1, "source": "deepseek-harness-sdk-events", "model": model,
        "status": status, "attempt_count": len(observations),
        "reported_attempt_count": len(reported), "cost_usd": None, **counts,
    }


def validate_metering(value: object, model: str) -> dict:
    """Check container metering without trusting its claimed provenance or cost."""
    expected_keys = {"schema_version", "source", "model", "status",
                     "attempt_count", "reported_attempt_count", "cost_usd",
                     *METERING_FIELDS}
    if (not isinstance(value, dict) or set(value) != expected_keys
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["source"] != "deepseek-harness-sdk-events"
            or value["model"] != model or value["cost_usd"] is not None):
        raise AdviceError("Harness 用量来源无效")
    attempts, reported = value["attempt_count"], value["reported_attempt_count"]
    if (type(attempts) is not int or type(reported) is not int
            or not 0 <= reported <= attempts <= 64):
        raise AdviceError("Harness 用量次数无效")
    status = value["status"]
    if status not in {"reported", "partial", "unavailable"}:
        raise AdviceError("Harness 用量状态无效")
    for name in METERING_FIELDS:
        item = value[name]
        if item is not None and (type(item) is not int or not 0 <= item <= 64_000_000_000):
            raise AdviceError("Harness 用量数值无效")
    if status == "reported":
        if (not attempts or reported != attempts or any(
                value[name] is None for name in ("input_tokens", "output_tokens", "total_tokens"))):
            raise AdviceError("Harness 用量状态矛盾")
        known_sum = sum(value[name] or 0 for name in (
            "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))
        if value["total_tokens"] < known_sum:
            raise AdviceError("Harness 用量总数矛盾")
        if (value["cache_read_tokens"] is not None and value["cache_write_tokens"] is not None
                and value["total_tokens"] != known_sum):
            raise AdviceError("Harness 缓存用量总数矛盾")
        if (value["reasoning_tokens"] is not None
                and value["reasoning_tokens"] > value["output_tokens"]):
            raise AdviceError("Harness 推理用量总数矛盾")
    elif (reported > 0) != (status == "partial") or any(value[name] is not None for name in METERING_FIELDS):
        raise AdviceError("Harness 用量状态矛盾")
    return value


def _canonical_sha256(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def validate_model_output(text: str, summary: dict) -> dict:
    if not isinstance(text, str) or not text or len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise AdviceError("模型建议长度无效")
    try:
        result = json.loads(text, object_pairs_hook=_unique_pairs)
    except (json.JSONDecodeError, SummaryError) as exc:
        raise AdviceError("模型建议不是严格 JSON") from exc
    if not isinstance(result, dict) or set(result) != {
        "next_step", "failure_recap", "skill_draft", "human_review_required", "automated_action",
    }:
        raise AdviceError("模型建议字段无效")
    next_step = result["next_step"]
    if (not isinstance(next_step, dict) or set(next_step) != {"code", "stage"}
            or type(next_step["code"]) is not str or next_step["code"] not in NEXT_CODES
            or type(next_step["stage"]) is not str or next_step["stage"] not in STAGES):
        raise AdviceError("下一步建议代码无效")
    recap = result["failure_recap"]
    if (not isinstance(recap, list) or len(recap) > len(RECAP_CODES)
            or any(type(code) is not str or code not in RECAP_CODES for code in recap)
            or len(set(recap)) != len(recap)):
        raise AdviceError("失败复盘代码无效")
    draft = result["skill_draft"]
    if not isinstance(draft, dict) or set(draft) != {
        "trigger_condition", "suggested_steps", "allowed_tools",
    }:
        raise AdviceError("技能草案字段无效")
    steps, tools = draft["suggested_steps"], draft["allowed_tools"]
    if (type(draft["trigger_condition"]) is not str or draft["trigger_condition"] not in TRIGGERS
            or not isinstance(steps, list) or not 1 <= len(steps) <= 3
            or any(type(step) is not str or step not in OUTPUT_STEPS for step in steps)
            or not isinstance(tools, list) or tools not in ([], ["inspect_cfd_case"])):
        raise AdviceError("技能草案代码无效")
    if result["human_review_required"] is not True or result["automated_action"] is not False:
        raise AdviceError("模型建议超出人工复核边界")
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "deepseek-harness",
        "status": "advisory_only",
        "case_id": summary["case_id"],
        "state_sha256": summary["state_sha256"],
        "input_sha256": _canonical_sha256(summary),
        **result,
    }


def _prompt(summary: dict) -> str:
    # The model receives no case identifier, digest, path, raw CT, or free text.
    model_input = {key: summary[key] for key in (
        "schema_version", "stages", "baseline_ready", "numeric_ok", "eligible",
    )}
    return (
        "仅分析以下科研工作流状态，不判断疾病、诊疗、CFD 数值合格或分享资格。"
        "只输出单个 JSON 对象，不加 Markdown、解释或自由文本。"
        "字段必须且只能是 next_step:{code,stage}, failure_recap:[],"
        "skill_draft:{trigger_condition,suggested_steps,allowed_tools},"
        "human_review_required:true,automated_action:false。"
        "next_step.code 只能取 " + ",".join(sorted(NEXT_CODES)) + "；stage 只能取 "
        + ",".join(STAGES) + "；failure_recap 元素只能取 "
        + ",".join(sorted(RECAP_CODES)) + "；trigger_condition 只能取 "
        + ",".join(sorted(TRIGGERS)) + "；suggested_steps 元素只能取 "
        + ",".join(sorted(OUTPUT_STEPS)) + "，最多三个；allowed_tools 只能是空数组或"
        "[\"inspect_cfd_case\"]，该字段只是草案，不可调用工具。"
        "遇到未知状态时建议人工检查。状态："
        + json.dumps(model_input, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    )


def run_model(summary: dict) -> dict:
    dsh_bin = os.environ.get("WINDCATCHER_HARNESS_DSH_BIN", "")
    if not dsh_bin or not os.environ.get("DEEPSEEK_API_KEY", ""):
        raise ProfileError("Harness 运行配置缺失")
    from deepseek_harness import DeepSeekHarness  # Optional, installed only in worker venv.

    model = os.environ.get("WINDCATCHER_HARNESS_MODEL", "")
    if model != RELAY_MODEL:
        raise ProfileError("Harness model invalid")
    configured_base_url()
    with tempfile.TemporaryDirectory(prefix="windcatcher-harness-") as root:
        home = Path(root) / "home"
        workspace = Path(root) / "empty-workspace"
        workspace.mkdir()
        verify_runtime(Path(dsh_bin), PATCH, home)
        with DeepSeekHarness(
            provider=RELAY_PROVIDER,
            model=model,
            max_tokens=600,
            cwd=str(workspace),
            runtime_cwd=str(workspace),
            dsh_home=str(home),
            dsh_bin=str(Path(dsh_bin).resolve()),
            profile="sdk-minimal",
            patches=(str(PATCH),),
            initialize_timeout_seconds=15,
            request_timeout_seconds=15,
        ) as harness:
            result = harness.run(_prompt(summary))
    if result.finish_reason != "completed":
        raise AdviceError("Harness 未完成建议生成")
    advice = validate_model_output(result.final_response, summary)
    metering = extract_metering(result.events, model)
    return {"advice": advice, "metering": validate_metering(metering, model)}


def main() -> int:
    try:
        summary = _validate(sys.stdin.buffer.read(MAX_INPUT_BYTES + 1))
        advice = run_model(summary)
    except Exception as exc:
        # Do not print the SDK exception: its diagnostics may contain paths or credentials.
        print(f"Harness 建议不可用（{type(exc).__name__}）", file=sys.stderr)
        return 2
    print(json.dumps(advice, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
