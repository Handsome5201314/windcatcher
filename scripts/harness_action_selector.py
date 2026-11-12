"""Tool-free Harness action selection over an anonymous, server-scoped allowlist.

This is a decision boundary, not a tool executor. The server must derive
allowed_actions from its own case permissions, versions, and pipeline gates,
then independently recheck them before any mutation. No case ID, path, image,
mesh, coordinate, or free-text clinical content is accepted here.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

if __package__:
    from . import harness_sdk_worker
    from .harness_readonly_sidecar import STAGES, STAGE_STATUSES, SummaryError, _unique_pairs
else:
    import harness_sdk_worker
    from harness_readonly_sidecar import STAGES, STAGE_STATUSES, SummaryError, _unique_pairs


SCHEMA_VERSION = 1
TASK = "select_next_action"
MAX_INPUT_BYTES = 4096
MAX_OUTPUT_BYTES = 256
ACTIONS = frozenset({
    "segment_candidate", "propose_endplanes", "prepare_surface_cfd",
    "submit_ready_cfd", "inspect_numeric_gate", "generate_report",
    "wait_for_review",
})


class ActionError(ValueError):
    """Invalid or untrusted action selection, without echoing input content."""


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def validate_request(value: object) -> dict:
    """Accept only a bounded stage projection and explicit action allowlist."""
    if type(value) is not dict or set(value) != {
        "schema_version", "stages", "baseline_ready", "numeric_ok",
        "eligible", "allowed_actions",
    }:
        raise ActionError("Harness 动作输入字段无效")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise ActionError("Harness 动作输入版本无效")
    stages = value["stages"]
    if (type(stages) is not dict or set(stages) != set(STAGES)
            or any(type(item) is not str or item not in STAGE_STATUSES
                   for item in stages.values())):
        raise ActionError("Harness 阶段状态无效")
    for name in ("baseline_ready", "numeric_ok", "eligible"):
        if value[name] is not None and type(value[name]) is not bool:
            raise ActionError("Harness 质量状态无效")
    if value["eligible"] is True and (
        value["numeric_ok"] is not True or value["baseline_ready"] is not True
    ):
        raise ActionError("Harness 质量状态矛盾")
    allowed = value["allowed_actions"]
    if (type(allowed) is not list or not 1 <= len(allowed) <= len(ACTIONS)
            or any(type(item) is not str or item not in ACTIONS for item in allowed)
            or len(allowed) != len(set(allowed))):
        raise ActionError("Harness 允许动作无效")
    if ("submit_ready_cfd" in allowed and (
        value["baseline_ready"] is not True or stages["cfd_prep"] not in {"ok", "warn"}
    )):
        raise ActionError("Harness 求解动作缺少就绪闸门")
    if ("propose_endplanes" in allowed or "prepare_surface_cfd" in allowed):
        if stages["segment"] != "ok":
            raise ActionError("Harness 几何动作缺少已确认掩膜")
    if "inspect_numeric_gate" in allowed and stages["cfd_run"] not in {"ok", "warn"}:
        raise ActionError("Harness 数值检查动作缺少求解结果")
    if "generate_report" in allowed and (
        stages["cfd_run"] not in {"ok", "warn"}
        or value["numeric_ok"] is not True or value["eligible"] is not True
    ):
        raise ActionError("Harness 报告动作缺少已合格的 CFD 结果")
    try:
        encoded = _canonical(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ActionError("Harness 动作输入无法编码") from exc
    if len(encoded) > MAX_INPUT_BYTES:
        raise ActionError("Harness 动作输入过长")
    # Freeze a private copy. A concurrent caller must not change the allowlist
    # between the prompt and the response membership check.
    return json.loads(encoded)


def parse_request(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_INPUT_BYTES:
        raise ActionError("Harness 动作输入长度无效")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, SummaryError) as exc:
        raise ActionError("Harness 动作输入不是严格 JSON") from exc
    return validate_request(value)


def validate_model_output(raw: str, request: dict) -> dict:
    request = validate_request(request)
    if type(raw) is not str or not raw or len(raw.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ActionError("Harness 动作输出长度无效")
    try:
        result = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (json.JSONDecodeError, SummaryError) as exc:
        raise ActionError("Harness 动作输出不是严格 JSON") from exc
    if type(result) is not dict or set(result) != {"action"}:
        raise ActionError("Harness 动作输出字段无效")
    action = result["action"]
    if type(action) is not str or action not in request["allowed_actions"]:
        raise ActionError("Harness 选择了未授权动作")
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "deepseek-harness",
        "status": "selected_not_executed",
        "input_sha256": hashlib.sha256(_canonical(request)).hexdigest(),
        "action": action,
    }


def _prompt(request: dict) -> str:
    request = validate_request(request)
    return (
        "这是去标识的科研工作流阶段状态。只能从 allowed_actions 精确选择一个动作。"
        "动作只供工作台服务端重新校验，模型不能直接执行、越过人工复核、诊断或发布结果。"
        "仅返回严格 JSON 对象 {\"action\":\"枚举值\"}，不要解释或 Markdown。状态："
        + _canonical(request).decode("ascii")
    )


def run_model(request: dict) -> dict:
    request = validate_request(request)
    dsh_bin = os.environ.get("WINDCATCHER_HARNESS_DSH_BIN", "")
    if not dsh_bin or not os.environ.get("DEEPSEEK_API_KEY", ""):
        raise harness_sdk_worker.ProfileError("Harness runtime configuration missing")
    model = os.environ.get("WINDCATCHER_HARNESS_MODEL", "")
    if model != harness_sdk_worker.RELAY_MODEL:
        raise harness_sdk_worker.ProfileError("Harness model invalid")
    harness_sdk_worker.configured_base_url()
    from deepseek_harness import DeepSeekHarness

    with tempfile.TemporaryDirectory(prefix="windcatcher-action-harness-") as root:
        home = Path(root) / "home"
        workspace = Path(root) / "empty-workspace"
        workspace.mkdir()
        harness_sdk_worker.verify_runtime(
            Path(dsh_bin), harness_sdk_worker.PATCH, home,
        )
        try:
            with DeepSeekHarness(
                provider=harness_sdk_worker.RELAY_PROVIDER,
                model=model,
                max_tokens=256,
                cwd=str(workspace), runtime_cwd=str(workspace),
                dsh_home=str(home), dsh_bin=str(Path(dsh_bin).resolve()),
                profile="sdk-minimal", patches=(str(harness_sdk_worker.PATCH),),
                initialize_timeout_seconds=15, request_timeout_seconds=15,
            ) as harness:
                result = harness.run(_prompt(request))
        except Exception as exc:
            # SDK diagnostics may contain credentials or host paths. Do not
            # surface them across the worker boundary.
            raise ActionError(f"Harness 动作选择失败（{type(exc).__name__}）") from None
    if result.finish_reason != "completed":
        raise ActionError("Harness 动作选择未完成")
    selected = validate_model_output(result.final_response, request)
    metering = harness_sdk_worker.extract_metering(result.events, model)
    return {"selection": selected,
            "metering": harness_sdk_worker.validate_metering(metering, model)}


__all__ = [
    "ACTIONS", "ActionError", "TASK", "parse_request", "run_model",
    "validate_model_output", "validate_request",
]
