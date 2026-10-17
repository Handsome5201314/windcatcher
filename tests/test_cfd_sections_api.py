"""Section endpoints preserve mask boundaries and reject concurrent/stale input."""

import asyncio
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

import server
from pipeline import cases, cfd_postprocess_jobs, cfd_sections
from pipeline.errors import PipelineError
from tests.test_cfd_results_api import result_case


def test_unconfirmed_mask_blocks_sections(result_case):
    case_id, root, _ = result_case
    (root / "segment/segmentation.json").write_text('{"need_human_confirm":true}', encoding="utf-8")
    with pytest.raises(HTTPException) as error:
        asyncio.run(server.api_cfd_sections(case_id))
    assert error.value.status_code == 409


def test_active_solver_blocks_section_preview(result_case):
    case_id, _root, _ = result_case
    cases.update_stage(case_id, "cfd_run", "running")
    with pytest.raises(HTTPException) as error:
        asyncio.run(server.api_cfd_section_preview(case_id, server.CfdSectionPreviewBody(position_fraction=.5)))
    assert error.value.status_code == 409
    assert "正在处理" in error.value.detail["reason"]


@pytest.mark.parametrize("value", [-1, 0, 1, 2, float("nan"), float("inf")])
def test_nonfinite_and_out_of_range_section_position_rejected(value):
    with pytest.raises(ValidationError):
        server.CfdSectionPreviewBody(position_fraction=value)


def test_legacy_region_compute_endpoint_is_disabled(result_case):
    case_id, _root, _ = result_case
    body = server.CfdSectionBody(plane={"position_fraction": .5}, regions=[{"id": "left-upper"}],
                                confirmed=True, source_signature="synthetic-source", geometry_signature="synthetic-geometry")
    with pytest.raises(HTTPException) as error:
        asyncio.run(server.api_compute_cfd_sections(case_id, body))
    assert error.value.status_code == 410
    assert "六区" in error.value.detail["reason"]


def test_whole_section_analyze_endpoint_passes_position_and_source_signatures(result_case, monkeypatch):
    case_id, root, _ = result_case
    received = []

    def analyze(case_root, position_fraction, source_signature, geometry_signature):
        received.append((case_root, position_fraction, source_signature, geometry_signature))
        return {"status": "ready", "analysis_type": "whole_section"}

    monkeypatch.setattr(cfd_sections, "analyze_section", analyze)
    body = server.CfdSectionAnalysisBody(position_fraction=.62, source_signature="source", geometry_signature="geometry")
    result = asyncio.run(server.api_analyze_cfd_section(case_id, body))
    assert result["analysis_type"] == "whole_section"
    assert received == [(root, .62, "source", "geometry")]


def test_conflicting_mutation_rejected_before_section_reader(result_case, monkeypatch):
    case_id, _root, _ = result_case
    monkeypatch.setattr(server, "_record_activity", lambda _event: None)
    cfd_postprocess_jobs.reserve_mutation(case_id)
    try:
        response = TestClient(server.app).post(f"/api/cases/{case_id}/cfd-sections/preview", json={"position_fraction": .5})
    finally:
        cfd_postprocess_jobs.release_mutation(case_id)
    assert response.status_code == 409
    assert "正在处理" in response.json()["reason"]


def test_stale_analysis_error_remains_observable(result_case, monkeypatch):
    case_id, _root, _ = result_case

    def analyze(_root, _position, _source, _geometry):
        raise PipelineError("blocked", "截面预览已过期，请刷新")

    monkeypatch.setattr(cfd_sections, "analyze_section", analyze)
    body = server.CfdSectionAnalysisBody(position_fraction=.5, source_signature="old", geometry_signature="old")
    with pytest.raises(PipelineError, match="过期"):
        asyncio.run(server.api_analyze_cfd_section(case_id, body))
