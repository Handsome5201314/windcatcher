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


def test_sections_compute_keeps_segmentation_and_stage_untouched(result_case, monkeypatch):
    case_id, root, _ = result_case
    mask_before = (root / "segment/segmentation.json").read_bytes()
    case_before = json.dumps(cases.get_case(case_id), sort_keys=True)
    received = []

    def compute(case_root, body):
        received.append((case_root, body))
        return {"status": "ready", "analysis_signature": "synthetic-analysis"}

    monkeypatch.setattr(cfd_sections, "compute_sections", compute)
    body = server.CfdSectionBody(plane={"position_fraction": .5}, regions=[{"id": "left-upper"}],
                                confirmed=True, source_signature="synthetic-source", geometry_signature="synthetic-geometry")
    result = asyncio.run(server.api_compute_cfd_sections(case_id, body))
    assert result["status"] == "ready"
    assert received == [(root, body.model_dump())]
    assert (root / "segment/segmentation.json").read_bytes() == mask_before
    assert json.dumps(cases.get_case(case_id), sort_keys=True) == case_before


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


def test_stale_preview_error_remains_observable(result_case, monkeypatch):
    case_id, _root, _ = result_case

    def compute(_root, _body):
        raise PipelineError("blocked", "截面预览已过期，请刷新")

    monkeypatch.setattr(cfd_sections, "compute_sections", compute)
    body = server.CfdSectionBody(plane={}, regions=[{}], confirmed=True, source_signature="old", geometry_signature="old")
    with pytest.raises(PipelineError, match="过期"):
        asyncio.run(server.api_compute_cfd_sections(case_id, body))
