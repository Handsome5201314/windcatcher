"""Three-opening drafts remain candidates until an explicit human save."""

import copy

import pytest

from pipeline import endplane_candidates, endplane_learning


def _plan():
    plane = lambda y: {"point_lps_mm": [0.0, y, 0.0], "normal_lps": [0.0, -1.0, 0.0]}
    return {
        "format": "oblique_bilateral_local_v3", "scope": "main_airway", "mask_version": 3,
        "left_inlet_plane": plane(1.0), "right_inlet_plane": plane(2.0),
        "posterior_plane": plane(30.0),
        "left_inlet_seed_lps_mm": [-5.0, 0.0, 0.0],
        "right_inlet_seed_lps_mm": [5.0, 0.0, 0.0],
        "posterior_seed_lps_mm": [0.0, 31.0, 0.0],
    }


def test_three_opening_candidate_can_be_recorded_without_becoming_a_saved_plan():
    plan = _plan()
    result = endplane_candidates.validate_candidate_plan("main_airway", 3, plan)
    assert result == plan
    assert endplane_learning.plan_hash(result) == endplane_learning.plan_hash(copy.deepcopy(plan))
    plan["left_inlet_seed_lps_mm"][0] = -999.0
    assert result["left_inlet_seed_lps_mm"][0] == -5.0


@pytest.mark.parametrize("change", [
    {"posterior_seed_lps_mm": None},
    {"anterior_plane": {"point_lps_mm": [0, 0, 0], "normal_lps": [0, 1, 0]}},
    {"scope": "full_candidate"},
    {"patient_name": "not allowed"},
])
def test_three_opening_candidate_rejects_incomplete_mixed_or_private_fields(change):
    plan = {**_plan(), **change}
    with pytest.raises(endplane_candidates.CandidateInputError):
        endplane_candidates.validate_candidate_plan(plan["scope"], 3, plan)


def test_three_opening_saved_mesh_evidence_is_structured_and_format_bound():
    plan = {**_plan(), "mesh_preflight": {
        "format": "oblique_mesh_clip_bilateral_local_v3", "closed_caps": True,
        "left_inlet_loops": 1, "right_inlet_loops": 1, "outlet_loops": 1,
        "left_inlet_area_mm2": 18.2, "right_inlet_area_mm2": 20.1,
    }}
    assert endplane_learning.validate_plan(plan)["mesh_preflight"]["left_inlet_loops"] == 1
    plan["mesh_preflight"]["format"] = "oblique_mesh_clip_local_v2"
    with pytest.raises(endplane_learning.LearningError, match="mismatch"):
        endplane_learning.validate_plan(plan)
