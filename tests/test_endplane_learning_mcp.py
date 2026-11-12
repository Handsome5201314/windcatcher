"""MCP contract tests for the anonymous endplane learning sidecar."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

pytest.importorskip("mcp")
import windcatcher_mcp as mcp_adapter


CASE_ID = "case-20261104-123456-abcd"


def _plan(anterior: int = 10, posterior: int = 30) -> dict:
    return {
        "scope": "main_airway",
        "mask_version": 3,
        "format": "oblique_planes_v1",
        "anterior_plane": {"point_lps_mm": [anterior, 0, 0], "normal_lps": [0, -1, 0]},
        "posterior_plane": {"point_lps_mm": [posterior, 0, 0], "normal_lps": [0, 1, 0]},
    }


def _proposal() -> mcp_adapter.EndplaneLearningProposalItem:
    return mcp_adapter.EndplaneLearningProposalItem(
        scope="main_airway",
        mask_version=3,
        plan=_plan(),
        source="model_candidate",
        source_version="v1",
    )


def test_learning_tools_are_registered_and_discoverable():
    names = set(mcp_adapter.mcp._tool_manager._tools)
    assert {"inspect_endplane_learning", "propose_endplanes", "auto_propose_endplanes"} <= names
    assert "record_endplane_feedback" not in names
    assert not hasattr(mcp_adapter, "record_endplane_feedback")


@pytest.mark.parametrize("field", ["patient_name", "file_path", "ct_series", "raw_text"])
def test_learning_models_reject_sensitive_fields(field):
    payload = _proposal().model_dump()
    payload["plan"][field] = "forbidden"
    with pytest.raises(ValidationError):
        mcp_adapter.EndplaneLearningProposalItem.model_validate(payload)


@pytest.mark.parametrize("version", [True, "3", 3.0])
def test_mask_version_rejects_non_integer_values(version):
    payload = _proposal().model_dump()
    payload["mask_version"] = version
    with pytest.raises(ValidationError):
        mcp_adapter.EndplaneLearningProposalItem.model_validate(payload)


@pytest.mark.parametrize("field,value", [
    ("current_summary", {}), ("reference_cohort", []), ("status", "accepted"),
])
def test_learning_model_rejects_legacy_and_human_feedback_fields(field, value):
    payload = _proposal().model_dump()
    payload[field] = value
    with pytest.raises(ValidationError):
        mcp_adapter.EndplaneLearningProposalItem.model_validate(payload)


def test_omitted_plan_preserves_unavailable_request_contract():
    request = mcp_adapter.EndplaneLearningProposalItem(
        scope="main_airway", mask_version=3, source="model_candidate", source_version="v1",
    )
    assert request.plan is None


def test_inspect_and_propose_use_learning_routes_only(monkeypatch):
    calls: list[tuple[str, str, dict | None, dict | None]] = []

    async def fake_request(method, path, *, body=None, params=None):
        calls.append((method, path, body, params))
        return {"ok": True}

    monkeypatch.setattr(mcp_adapter, "_request", fake_request)
    request = _proposal()
    asyncio.run(mcp_adapter.inspect_endplane_learning(case_id=CASE_ID, scope="main_airway"))
    asyncio.run(mcp_adapter.propose_endplanes(request=request, case_id=CASE_ID))
    asyncio.run(mcp_adapter.auto_propose_endplanes(case_id=CASE_ID, scope="main_airway", mask_version=3))

    assert calls[0] == (
        "GET", f"/api/cases/{CASE_ID}/segment/endplanes/learning", None, {"scope": "main_airway"}
    )
    assert calls[1] == (
        "POST", f"/api/cases/{CASE_ID}/segment/endplanes/learning/propose",
        request.model_dump(), None,
    )
    assert calls[2] == (
        "POST", f"/api/cases/{CASE_ID}/segment/endplanes/learning/auto-propose",
        {"scope": "main_airway", "mask_version": 3}, None,
    )
    serialized = repr(calls)
    assert "endplanes.json" not in serialized
    assert "/learning/feedback" not in serialized
    assert all(path != f"/api/cases/{CASE_ID}/segment/endplanes" for _, path, _, _ in calls)
