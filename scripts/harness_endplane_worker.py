"""Tool-free Harness candidate generation for an allowlisted synthetic geometry.

The fixture is defined here, not supplied as arbitrary coordinates from a case.
This mode is only an integration proof; it cannot inspect CT or save a plan.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from copy import deepcopy
from pathlib import Path

if __package__:
    from . import harness_sdk_worker
    from .harness_readonly_sidecar import SummaryError, _unique_pairs
else:
    import harness_sdk_worker
    from harness_readonly_sidecar import SummaryError, _unique_pairs


TASK = "synthetic_endplane_candidate"
MAX_OUTPUT_BYTES = 4096
_FIXTURES = {
    "bent_tube_v1": {
        "schema_version": 1,
        "fixture": "bent_tube_v1",
        "scope": "main_airway",
        "mask_version": 0,
        "format": "oblique_local_inlet_v2",
        "centerline_lps_mm": [
            [0, 0, 0], [0, 8, 0], [2, 16, 1], [4, 24, 2], [8, 32, 3],
        ],
        "anterior_seed_lps_mm": [0, 0, 0],
    },
}


class CandidateError(ValueError):
    """Invalid synthetic fixture or model output."""


def validate_geometry(value: object) -> dict:
    if type(value) is not dict or set(value) != {"fixture"}:
        raise CandidateError("synthetic geometry fields invalid")
    fixture = value["fixture"]
    if type(fixture) is not str or fixture not in _FIXTURES:
        raise CandidateError("synthetic geometry fixture invalid")
    return deepcopy(_FIXTURES[fixture])


def _vector(value: object, *, normal: bool = False) -> list[int | float]:
    if type(value) is not list or len(value) != 3:
        raise CandidateError("candidate vector invalid")
    if any(type(item) not in (int, float) or not math.isfinite(item)
           or abs(item) > (1 if normal else 1000) for item in value):
        raise CandidateError("candidate vector invalid")
    if normal and not 0.95 <= math.sqrt(sum(item * item for item in value)) <= 1.05:
        raise CandidateError("candidate normal must be unit length")
    return value


def _distance(left: list, right: list) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right)))


def validate_model_output(text: str, geometry: dict) -> dict:
    if (type(text) is not str or not text
            or len(text.encode("utf-8")) > MAX_OUTPUT_BYTES):
        raise CandidateError("candidate response length invalid")
    try:
        plan = json.loads(text, object_pairs_hook=_unique_pairs)
    except (json.JSONDecodeError, SummaryError) as exc:
        raise CandidateError("candidate response JSON invalid") from exc
    expected = {"format", "scope", "mask_version", "anterior_plane",
                "posterior_plane", "anterior_seed_lps_mm"}
    if type(plan) is not dict or set(plan) != expected:
        raise CandidateError("candidate fields invalid")
    for key in ("format", "scope", "mask_version"):
        if type(plan[key]) is not type(geometry[key]) or plan[key] != geometry[key]:
            raise CandidateError("candidate fixture context mismatch")
    for side in ("anterior", "posterior"):
        plane = plan[f"{side}_plane"]
        if type(plane) is not dict or set(plane) != {"point_lps_mm", "normal_lps"}:
            raise CandidateError("candidate plane fields invalid")
        _vector(plane["point_lps_mm"])
        _vector(plane["normal_lps"], normal=True)
    seed = _vector(plan["anterior_seed_lps_mm"])
    if seed != geometry["anterior_seed_lps_mm"]:
        raise CandidateError("candidate seed changed")
    points = geometry["centerline_lps_mm"]
    for side, endpoint, neighbor, sign in (
        ("anterior", points[0], points[1], -1),
        ("posterior", points[-1], points[-2], -1),
    ):
        plane = plan[f"{side}_plane"]
        if _distance(plane["point_lps_mm"], endpoint) > 3:
            raise CandidateError("candidate plane outside synthetic endpoint")
        direction = [(a - b) * sign for a, b in zip(neighbor, endpoint)]
        length = math.sqrt(sum(item * item for item in direction))
        dot = sum(a * b for a, b in zip(plane["normal_lps"], direction)) / length
        if dot < 0.7:
            raise CandidateError("candidate normal orientation invalid")
    return plan


def _prompt(geometry: dict) -> str:
    return (
        "这是完全合成的弯管几何，仅用于测试端面候选接口，不是患者 CT 或诊断。"
        "根据中心线生成完整双端面候选。入口端面法向朝管外（反向于前两点切线），"
        "出口端面法向朝管外（同向于末两点切线）；法向必须是单位向量。"
        "端面中心距对应中心线端点不超过 3 mm。必须原样复制 scope、mask_version、"
        "format 和 anterior_seed_lps_mm。仅输出一个严格 JSON 对象，不加解释或 Markdown。"
        "对象字段必须且只能是 scope,mask_version,format,anterior_plane,posterior_plane,"
        "anterior_seed_lps_mm；每个 plane 必须且只能有 point_lps_mm 和 normal_lps，"
        "两者均为三元素数字数组。不得声明已审核、已保存或 CFD 合格。几何："
        + json.dumps(geometry, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    )


def run_model(geometry: dict) -> dict:
    dsh_bin = os.environ.get("WINDCATCHER_HARNESS_DSH_BIN", "")
    if not dsh_bin or not os.environ.get("DEEPSEEK_API_KEY", ""):
        raise harness_sdk_worker.ProfileError("Harness runtime configuration missing")
    from deepseek_harness import DeepSeekHarness

    model = os.environ.get("WINDCATCHER_HARNESS_MODEL", "")
    if model != harness_sdk_worker.RELAY_MODEL:
        raise harness_sdk_worker.ProfileError("Harness model invalid")
    harness_sdk_worker.configured_base_url()
    with tempfile.TemporaryDirectory(prefix="windcatcher-synthetic-harness-") as root:
        home = Path(root) / "home"
        workspace = Path(root) / "empty-workspace"
        workspace.mkdir()
        harness_sdk_worker.verify_runtime(Path(dsh_bin), harness_sdk_worker.PATCH, home)
        with DeepSeekHarness(
            provider=harness_sdk_worker.RELAY_PROVIDER, model=model, max_tokens=800,
            cwd=str(workspace), runtime_cwd=str(workspace),
            dsh_home=str(home), dsh_bin=str(Path(dsh_bin).resolve()),
            profile="sdk-minimal", patches=(str(harness_sdk_worker.PATCH),),
            initialize_timeout_seconds=15, request_timeout_seconds=15,
        ) as harness:
            result = harness.run(_prompt(geometry))
    if result.finish_reason != "completed":
        raise CandidateError("Harness synthetic candidate unfinished")
    candidate = validate_model_output(result.final_response, geometry)
    metering = harness_sdk_worker.extract_metering(result.events, model)
    return {"candidate": candidate,
            "metering": harness_sdk_worker.validate_metering(metering, model)}
