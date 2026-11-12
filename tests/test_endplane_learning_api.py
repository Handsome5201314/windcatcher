"""The endplane learning API stores proposals and feedback without saving plans."""

import asyncio

import pytest

import server
from pipeline import endplane_learning


CASE_ID = "case-20261104-123456-abcd"


def plan(*, mask_version=3, anterior=10, posterior=30):
    return {
        "scope": "main_airway",
        "mask_version": mask_version,
        "format": "oblique_planes_v1",
        "anterior_plane": {"point_lps_mm": [anterior, 0, 0], "normal_lps": [0, -1, 0]},
        "posterior_plane": {"point_lps_mm": [posterior, 0, 0], "normal_lps": [0, 1, 0]},
    }


@pytest.fixture(autouse=True)
def isolated_learning(tmp_path, monkeypatch):
    monkeypatch.setenv(endplane_learning.DIRECTORY_ENV, str(tmp_path / "learning"))
    endplane_learning._DEFAULT_STORE = None
    monkeypatch.setattr(server, "_require_case", lambda case_id: {"case_id": case_id})
    monkeypatch.setattr(server.endplanes, "get_endplanes", lambda case_id, scope: {
        "case_id": case_id, "scope": scope, "mask_version": 3,
        "can_save": True,
        "axis": 1, "anterior_sign": -1, "count": 64, "ap_spacing_mm": 0.6,
    })
    yield
    endplane_learning._DEFAULT_STORE = None


def test_proposal_endpoint_records_candidate_but_does_not_save_case_plan():
    body = server.EndplaneLearningProposalBody(
        scope="main_airway", mask_version=3, plan=plan(anterior=9, posterior=29),
        source="model_candidate", source_version="v1",
    )
    result = asyncio.run(server.api_propose_endplane_candidate(CASE_ID, body))
    assert result["status"] == "available"
    assert result["proposal"]["status"] == "proposed"
    assert result["proposal"]["case_id"] == CASE_ID
    assert result["proposal"]["plan"] == plan(anterior=9, posterior=29)
    assert result["proposal_id"] == result["proposal"]["proposal_id"]
    assert result["human_review_required"] is True
    assert result["committed"] is False
    assert server.endplanes.load_plan(CASE_ID, "main_airway") is None


def test_proposal_endpoint_refuses_stale_mask_version():
    body = server.EndplaneLearningProposalBody(
        scope="main_airway", mask_version=2, plan=plan(mask_version=2),
        source="model_candidate", source_version="v1",
    )
    with pytest.raises(server.HTTPException) as exc:
        asyncio.run(server.api_propose_endplane_candidate(CASE_ID, body))
    assert exc.value.status_code == 409


def test_feedback_endpoint_requires_proposal_belongs_to_case_and_keeps_plan_unchanged():
    proposal = endplane_learning.create_proposal(
        CASE_ID, "main_airway", 3, plan(), "reference_knn", source_version="v1",
    )
    body = server.EndplaneLearningFeedbackBody(
        proposal_id=proposal["proposal_id"], status="adjusted",
        feedback={"adjusted_plan": plan(anterior=11), "reason_code": "operator_adjusted"},
    )
    result = asyncio.run(server.api_record_endplane_feedback(CASE_ID, body))
    assert result["status"] == "adjusted"
    assert result["proposal_id"] == proposal["proposal_id"]
    assert server.endplanes.load_plan(CASE_ID, "main_airway") is None

    wrong_case = "case-20261104-123457-abcd"
    with pytest.raises(server.HTTPException) as exc:
        asyncio.run(server.api_record_endplane_feedback(wrong_case, body))
    assert exc.value.status_code == 404


def test_no_registered_candidate_is_explicitly_unavailable():
    body = server.EndplaneLearningProposalBody(
        scope="main_airway", mask_version=3, source="model_candidate", source_version="v1",
    )
    result = asyncio.run(server.api_propose_endplane_candidate(CASE_ID, body))
    assert result["status"] == "unavailable"
    assert result["reason"] == "no_registered_candidate_provider"
    assert endplane_learning.list_proposals() == []


def test_unconfirmed_current_mask_cannot_create_candidate_or_review_label(monkeypatch):
    proposal = endplane_learning.create_proposal(
        CASE_ID, "main_airway", 3, plan(), "model_candidate", source_version="v1",
    )
    monkeypatch.setattr(server.endplanes, "get_endplanes", lambda *args: {
        "mask_version": 3, "can_save": False,
    })
    operations = [
        lambda: server.api_propose_endplane_candidate(CASE_ID, server.EndplaneLearningProposalBody(
            scope="main_airway", mask_version=3, plan=plan(anterior=12),
            source="model_candidate", source_version="v1")),
        lambda: server.api_record_endplane_feedback(CASE_ID, server.EndplaneLearningFeedbackBody(
            proposal_id=proposal["proposal_id"], status="accepted")),
    ]
    for operation in operations:
        with pytest.raises(server.HTTPException) as exc:
            asyncio.run(operation())
        assert exc.value.status_code == 409
        assert exc.value.detail["status"] == "unconfirmed_mask"
    assert len(endplane_learning.list_proposals()) == 1
    assert endplane_learning.get_proposal(proposal["proposal_id"])["status"] == "proposed"


@pytest.mark.parametrize("bad_plan", [
    {**plan(), "mask_version": 2},
    {**plan(), "scope": "full_candidate"},
    {**plan(), "patient_name": "private"},
    {**plan(), "anterior_plane": {"point_lps_mm": [0, 0, 0], "normal_lps": [0, 0, 0]}},
    {**plan(), "format": "oblique_local_inlet_v2"},
])
def test_model_candidates_fail_closed_on_invalid_current_geometry_contract(bad_plan):
    body = server.EndplaneLearningProposalBody(
        scope="main_airway", mask_version=3, plan=bad_plan,
        source="model_candidate", source_version="v1",
    )
    with pytest.raises(server.HTTPException) as exc:
        asyncio.run(server.api_propose_endplane_candidate(CASE_ID, body))
    assert exc.value.status_code == 422
    assert endplane_learning.list_proposals() == []


def test_feedback_is_version_bound_and_cannot_be_replayed(monkeypatch):
    proposal = endplane_learning.create_proposal(
        CASE_ID, "main_airway", 3, plan(), "model_candidate", source_version="v1",
    )
    body = server.EndplaneLearningFeedbackBody(proposal_id=proposal["proposal_id"], status="accepted")
    asyncio.run(server.api_record_endplane_feedback(CASE_ID, body))
    with pytest.raises(server.HTTPException) as exc:
        asyncio.run(server.api_record_endplane_feedback(CASE_ID, body))
    assert exc.value.status_code == 409
    other = endplane_learning.create_proposal(
        CASE_ID, "main_airway", 3, plan(anterior=12), "model_candidate", source_version="v1",
    )
    monkeypatch.setattr(server.endplanes, "get_endplanes", lambda *args: {"mask_version": 4})
    with pytest.raises(server.HTTPException) as exc:
        asyncio.run(server.api_record_endplane_feedback(CASE_ID,
            server.EndplaneLearningFeedbackBody(proposal_id=other["proposal_id"], status="accepted")))
    assert exc.value.status_code == 409
    assert endplane_learning.get_proposal(other["proposal_id"])["status"] == "proposed"


def test_request_models_forbid_unknown_fields_and_coerced_versions():
    from pydantic import ValidationError
    for extra in ({"reference_cohort": []}, {"patient_name": "private"}, {"mask_version": "3"}):
        with pytest.raises(ValidationError):
            server.EndplaneLearningProposalBody(**{
                "scope": "main_airway", "mask_version": 3, "plan": plan(),
                "source": "model_candidate", "source_version": "v1", **extra,
            })


def test_case_reviewer_can_review_service_proposal_without_impersonating_submitter():
    proposal = endplane_learning.create_proposal(
        CASE_ID, "main_airway", 3, plan(), "model_candidate", source_version="v1",
        actor_id="service-mcp",
    )
    token = server.endplane_history.CURRENT_ACTOR_ID.set("human-reviewer")
    try:
        listing = asyncio.run(server.api_get_endplane_learning(CASE_ID))
        assert [item["proposal_id"] for item in listing["proposals"]] == [proposal["proposal_id"]]
        body = server.EndplaneLearningFeedbackBody(proposal_id=proposal["proposal_id"], status="accepted")
        result = asyncio.run(server.api_record_endplane_feedback(CASE_ID, body))
        assert result["status"] == "accepted"
        listing = asyncio.run(server.api_get_endplane_learning(CASE_ID))
        assert listing["proposals"][0]["feedback"][0]["actor_id"] == "human-reviewer"
        assert server.endplanes.load_plan(CASE_ID, "main_airway") is None
    finally:
        server.endplane_history.CURRENT_ACTOR_ID.reset(token)
