"""Synthetic-only checks for the strict advisory status projection."""

import json

import pytest

from pipeline import harness_projection
from pipeline.config import STAGES
from pipeline.errors import PipelineError


CASE_ID = "synthetic-harness-projection"


def synthetic_case(**statuses):
    stages = {name: {"status": statuses.get(name, "ok"),
                     "message": "PRIVATE_STAGE_TEXT", "data": {"path": "PRIVATE_CT_PATH"}}
              for name in STAGES}
    return {"case_id": CASE_ID, "patient_name": "PRIVATE_PATIENT_NAME",
            "source_path": "PRIVATE_CT_PATH", "stages": stages}


@pytest.fixture
def source(monkeypatch):
    current = {"case": synthetic_case()}
    monkeypatch.setattr(harness_projection.cases, "get_case", lambda _id: current["case"])
    monkeypatch.setattr(harness_projection.automation, "preflight", lambda _item: {"input": "PRIVATE_CT_PATH"})
    monkeypatch.setattr(harness_projection.openfoam_runner, "check_result_gate",
                        lambda _id: {"numeric_ok": True, "eligible": True,
                                     "reasons": ["PRIVATE_GATE_TEXT"]})
    return current


def test_projection_drops_sensitive_case_and_gate_fields(source):
    result = harness_projection.project_case_state(CASE_ID)
    assert set(result) == {"schema_version", "case_id", "stages", "baseline_ready",
                           "numeric_ok", "eligible", "state_sha256"}
    assert result["baseline_ready"] is True
    assert result["numeric_ok"] is True and result["eligible"] is True
    assert result["stages"] == {name: "ok" for name in STAGES}
    encoded = json.dumps(result)
    assert "PRIVATE_" not in encoded
    assert len(result["state_sha256"]) == 64
    assert "case_version" not in result


def test_unfinished_case_does_not_run_preflight_or_claim_numeric_result(source, monkeypatch):
    source["case"] = synthetic_case(cfd_prep="pending", cfd_run="pending")
    monkeypatch.setattr(harness_projection.automation, "preflight",
                        lambda _item: pytest.fail("preflight must not run"))
    monkeypatch.setattr(harness_projection.openfoam_runner, "check_result_gate",
                        lambda _id: pytest.fail("gate must not run"))
    result = harness_projection.project_case_state(CASE_ID)
    assert result["baseline_ready"] is False
    assert result["numeric_ok"] is None and result["eligible"] is None


def test_projection_digest_changes_when_status_changes(source):
    first = harness_projection.project_case_state(CASE_ID)
    source["case"] = synthetic_case(report="pending")
    second = harness_projection.project_case_state(CASE_ID)
    assert first["state_sha256"] != second["state_sha256"]
    assert second == harness_projection.project_case_state(CASE_ID)


@pytest.mark.parametrize("change", ["missing", "invalid", "mismatch"])
def test_incomplete_or_invalid_status_is_explicit(source, change):
    if change == "missing":
        source["case"]["stages"].pop("segment")
    elif change == "invalid":
        source["case"]["stages"]["segment"]["status"] = "skipped"
    else:
        source["case"]["case_id"] = "synthetic-other"
    with pytest.raises(harness_projection.ProjectionError):
        harness_projection.project_case_state(CASE_ID)


@pytest.mark.parametrize("failure", [PermissionError("PRIVATE_CT_PATH"),
                                      PipelineError("failed", "PRIVATE_PATIENT_NAME")])
def test_unreadable_case_is_explicit_without_echoing_source(source, monkeypatch, failure):
    def unreadable(_id):
        raise failure

    monkeypatch.setattr(harness_projection.cases, "get_case", unreadable)
    with pytest.raises(harness_projection.ProjectionError, match="病例状态不可读取") as caught:
        harness_projection.project_case_state(CASE_ID)
    assert "PRIVATE_" not in str(caught.value)


def test_preflight_blocked_is_not_ready_but_failure_is_not_hidden(source, monkeypatch):
    def blocked(_item):
        raise PipelineError("blocked", "waiting")

    monkeypatch.setattr(harness_projection.automation, "preflight", blocked)
    monkeypatch.setattr(harness_projection.openfoam_runner, "check_result_gate",
                        lambda _id: {"numeric_ok": False, "eligible": False})
    assert harness_projection.project_case_state(CASE_ID)["baseline_ready"] is False

    def failed(_item):
        raise PipelineError("failed", "PRIVATE_CT_PATH")

    monkeypatch.setattr(harness_projection.automation, "preflight", failed)
    with pytest.raises(harness_projection.ProjectionError, match="基准算例准备状态不可读取"):
        harness_projection.project_case_state(CASE_ID)


def test_gate_rejects_missing_or_contradictory_booleans(source, monkeypatch):
    monkeypatch.setattr(harness_projection.openfoam_runner, "check_result_gate",
                        lambda _id: {"numeric_ok": None, "eligible": False})
    with pytest.raises(harness_projection.ProjectionError, match="数值质量状态无效"):
        harness_projection.project_case_state(CASE_ID)
    monkeypatch.setattr(harness_projection.openfoam_runner, "check_result_gate",
                        lambda _id: {"numeric_ok": False, "eligible": True})
    with pytest.raises(harness_projection.ProjectionError, match="数值质量状态相互矛盾"):
        harness_projection.project_case_state(CASE_ID)
