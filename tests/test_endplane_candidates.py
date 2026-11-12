"""Strict current-case candidate contract; no provider is registered in phase 1."""

from copy import deepcopy

import pytest

from pipeline.endplane_candidates import (
    CandidateInputError,
    propose_candidate,
    validate_candidate_plan,
)


def _plan(fmt="oblique_planes_v1", *, scope="main_airway", mask_version=3):
    result = {
        "scope": scope,
        "mask_version": mask_version,
        "format": fmt,
        "anterior_plane": {"point_lps_mm": [1, 2, 3], "normal_lps": [0, -1, 0]},
        "posterior_plane": {"point_lps_mm": [1, 30, 3], "normal_lps": [0, 1, 0]},
    }
    if fmt == "oblique_local_inlet_v2":
        result["anterior_seed_lps_mm"] = [1, 4, 3]
    return result


def test_default_provider_is_unavailable_and_returns_no_generated_plan():
    assert propose_candidate("main_airway", 3) == {
        "status": "unavailable",
        "reason": "no_registered_candidate_provider",
        "scope": "main_airway",
        "mask_version": 3,
        "proposal_id": None,
    }


def test_default_provider_accepts_neither_cohort_authorization_nor_provider_injection():
    with pytest.raises(TypeError):
        propose_candidate("main_airway", 3, reference_cohort={"authorized": True, "items": []})
    with pytest.raises(TypeError):
        propose_candidate("main_airway", 3, adapter=object())


@pytest.mark.parametrize("scope,version", [
    ("unknown", 3), (None, 3), ([], 3), ("main_airway", True),
    ("main_airway", -1), ("main_airway", 1.0), ("main_airway", 2**31),
])
def test_request_context_requires_known_scope_and_integer_mask_version(scope, version):
    with pytest.raises(CandidateInputError):
        propose_candidate(scope, version)
    with pytest.raises(CandidateInputError):
        validate_candidate_plan(scope, version, _plan())


@pytest.mark.parametrize("fmt", ["oblique_planes_v1", "oblique_local_inlet_v2"])
def test_explicit_current_case_plan_roundtrips_as_independent_snapshot(fmt):
    plan = _plan(fmt)
    before = deepcopy(plan)

    canonical = validate_candidate_plan("main_airway", 3, plan)
    assert canonical == before
    canonical["anterior_plane"]["point_lps_mm"][0] = 999
    assert plan == before


@pytest.mark.parametrize("field", ["scope", "mask_version"])
def test_context_fields_are_required_and_cannot_be_auto_filled(field):
    plan = _plan()
    del plan[field]
    with pytest.raises(CandidateInputError, match=field):
        validate_candidate_plan("main_airway", 3, plan)


@pytest.mark.parametrize("changes", [
    {"scope": "full_candidate"}, {"mask_version": 4},
    {"mask_version": True}, {"format": "ct_indices_v1"}, {"format": "oblique_v999"},
])
def test_plan_must_match_current_context_and_supported_format(changes):
    with pytest.raises(CandidateInputError):
        validate_candidate_plan("main_airway", 3, _plan() | changes)


@pytest.mark.parametrize("field", ["anterior_plane", "posterior_plane", "format"])
def test_complete_oblique_pair_and_format_are_required(field):
    plan = _plan()
    del plan[field]
    with pytest.raises(CandidateInputError):
        validate_candidate_plan("main_airway", 3, plan)


@pytest.mark.parametrize("normal", [
    [0, 0, 0], [float("nan"), 1, 0], [float("inf"), 0, 1],
    [True, 0, 1], [1, 0], "0,1,0",
])
@pytest.mark.parametrize("side", ["anterior_plane", "posterior_plane"])
def test_each_normal_must_be_three_finite_numbers_and_nonzero(side, normal):
    plan = _plan()
    plan[side]["normal_lps"] = normal
    with pytest.raises(CandidateInputError):
        validate_candidate_plan("main_airway", 3, plan)


@pytest.mark.parametrize("unknown", ["patient_name", "source_path", "features", "authorized"])
def test_unknown_top_level_fields_are_rejected_instead_of_projected_away(unknown):
    with pytest.raises(CandidateInputError):
        validate_candidate_plan("main_airway", 3, _plan() | {unknown: "untrusted"})


def test_unknown_nested_fields_are_rejected():
    plan = _plan()
    plan["anterior_plane"]["patient_name"] = "untrusted"
    with pytest.raises(CandidateInputError):
        validate_candidate_plan("main_airway", 3, plan)


@pytest.mark.parametrize("field,value", [("anterior_index", 20), ("posterior_index", 8)])
def test_oblique_contract_cannot_mix_indices(field, value):
    with pytest.raises(CandidateInputError):
        validate_candidate_plan("main_airway", 3, _plan() | {field: value})


def test_local_inlet_requires_seed_and_main_airway_scope():
    plan = _plan("oblique_local_inlet_v2")
    del plan["anterior_seed_lps_mm"]
    with pytest.raises(CandidateInputError):
        validate_candidate_plan("main_airway", 3, plan)
    with pytest.raises(CandidateInputError):
        validate_candidate_plan("full_candidate", 3,
                                _plan("oblique_local_inlet_v2", scope="full_candidate"))


def test_plain_oblique_contract_rejects_local_seed():
    with pytest.raises(CandidateInputError):
        validate_candidate_plan("main_airway", 3, _plan() | {"anterior_seed_lps_mm": [1, 4, 3]})


@pytest.mark.parametrize("value", [[float("nan"), 2, 3], [float("inf"), 2, 3],
                                   [10**400, 2, 3], [1, 2], [True, 2, 3]])
@pytest.mark.parametrize("field", ["anterior_seed_lps_mm", "point_lps_mm"])
def test_seed_and_points_reject_invalid_coordinates(field, value):
    plan = _plan("oblique_local_inlet_v2")
    if field == "point_lps_mm":
        plan["posterior_plane"][field] = value
    else:
        plan[field] = value
    with pytest.raises(CandidateInputError):
        validate_candidate_plan("main_airway", 3, plan)


def test_validation_and_unavailable_request_do_not_write_case_or_ledger(tmp_path, monkeypatch):
    case = tmp_path / "case" / "segment" / "endplanes.json"
    case.parent.mkdir(parents=True)
    case.write_bytes(b'{"sentinel":true}')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WINDCATCHER_ENDPLANE_LEARNING_DIR", str(tmp_path / "learning"))
    before = {str(path.relative_to(tmp_path)): path.read_bytes()
              for path in tmp_path.rglob("*") if path.is_file()}

    validate_candidate_plan("main_airway", 3, _plan())
    propose_candidate("main_airway", 3)

    after = {str(path.relative_to(tmp_path)): path.read_bytes()
             for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before
