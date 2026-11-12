"""Synthetic-only candidate generation in the tool-free Harness container."""

import json
import sys
from types import SimpleNamespace

import pytest

from pipeline.endplane_candidates import validate_candidate_plan
from scripts import harness_endplane_worker as worker


def candidate():
    return {
        "scope": "main_airway", "mask_version": 0,
        "format": "oblique_local_inlet_v2",
        "anterior_plane": {"point_lps_mm": [0, 0, 0],
                           "normal_lps": [0, -1, 0]},
        "posterior_plane": {"point_lps_mm": [8, 32, 3],
                            "normal_lps": [4 / 9, 8 / 9, 1 / 9]},
        "anterior_seed_lps_mm": [0, 0, 0],
    }


def events():
    return [
        {"type": "turn/start", "data": {"turn": 1}},
        {"type": "step/start", "data": {"turn": 1, "step": 1}},
        {"type": "assistant/message", "data": {"turn": 1, "step": 1,
            "usage": {"inputTokens": 10, "outputTokens": 12, "totalTokens": 22}}},
        {"type": "step/end", "data": {"turn": 1, "step": 1}},
        {"type": "turn/end", "data": {"turn": 1}},
    ]


def test_fixture_selector_is_the_only_input_and_returns_a_copy():
    first = worker.validate_geometry({"fixture": "bent_tube_v1"})
    first["centerline_lps_mm"][0][0] = 99
    assert worker.validate_geometry({"fixture": "bent_tube_v1"})["centerline_lps_mm"][0][0] == 0
    for bad in ({}, {"fixture": "unknown"},
                {"fixture": "bent_tube_v1", "patient_name": "private"},
                {"fixture": "bent_tube_v1", "case_path": "C:/private"}):
        with pytest.raises(worker.CandidateError):
            worker.validate_geometry(bad)


def test_valid_model_candidate_matches_host_plan_validator():
    geometry = worker.validate_geometry({"fixture": "bent_tube_v1"})
    plan = worker.validate_model_output(json.dumps(candidate()), geometry)
    assert validate_candidate_plan("main_airway", 0, plan) == plan
    prompt = worker._prompt(geometry)
    assert "centerline_lps_mm" in prompt
    assert "case_id" not in prompt and "patient_name" not in prompt


@pytest.mark.parametrize("change", [
    {"posterior_plane": None},
    {"scope": "full_candidate"},
    {"mask_version": True},
    {"anterior_seed_lps_mm": [1, 0, 0]},
    {"patient_name": "private"},
    {"anterior_plane": {"point_lps_mm": [0, 0, 0], "normal_lps": [0, 1, 0]}},
    {"posterior_plane": {"point_lps_mm": [50, 32, 3], "normal_lps": [4 / 9, 8 / 9, 1 / 9]}},
    {"posterior_plane": {"point_lps_mm": [8, 32, 3], "normal_lps": [0, 0, 0]}},
])
def test_invalid_or_unbounded_model_candidate_is_rejected(change):
    geometry = worker.validate_geometry({"fixture": "bent_tube_v1"})
    with pytest.raises(worker.CandidateError):
        worker.validate_model_output(json.dumps(candidate() | change), geometry)


@pytest.mark.parametrize("response", [
    "```json\n{}\n```",
    '{"scope":"main_airway","scope":"main_airway"}',
    "{}" * 3000,
])
def test_unstructured_model_response_is_rejected(response):
    with pytest.raises(worker.CandidateError):
        worker.validate_model_output(response, worker.validate_geometry({"fixture": "bent_tube_v1"}))


def test_real_sdk_call_path_uses_minimal_profile_and_reports_usage_without_api_request(
    tmp_path, monkeypatch,
):
    dsh_bin = tmp_path / "dsh"
    dsh_bin.write_text("synthetic", encoding="utf-8")
    monkeypatch.setenv("WINDCATCHER_HARNESS_DSH_BIN", str(dsh_bin))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("WINDCATCHER_HARNESS_MODEL", "gpt-6.1-sol")
    monkeypatch.setenv("WINDCATCHER_HARNESS_BASE_URL",
                       "https://tongyimohe.aitoken.credit/v1")
    monkeypatch.setattr(worker.harness_sdk_worker, "verify_runtime", lambda *_args: None)
    seen = {}

    class FakeHarness:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def run(self, prompt):
            assert "centerline_lps_mm" in prompt
            return SimpleNamespace(finish_reason="completed",
                                   final_response=json.dumps(candidate()), events=events())

    monkeypatch.setitem(sys.modules, "deepseek_harness",
                        SimpleNamespace(DeepSeekHarness=FakeHarness))
    result = worker.run_model(worker.validate_geometry({"fixture": "bent_tube_v1"}))
    assert result["candidate"] == candidate()
    assert result["metering"]["status"] == "reported"
    assert result["metering"]["total_tokens"] == 22
    assert seen["profile"] == "sdk-minimal"
    assert seen["patches"] == (str(worker.harness_sdk_worker.PATCH),)
    assert seen["provider"] == "windcatcher-relay"
    assert seen["model"] == "gpt-6.1-sol"
    assert "base_url" not in seen
