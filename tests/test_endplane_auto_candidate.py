"""Local, read-only endplane suggestion regression tests using synthetic masks."""

from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from pipeline import cases, endplane_auto_candidate as auto, endplane_candidates, oblique
from pipeline.errors import PipelineError
from pipeline.volume import Volume, save_volume


def _volume(shape):
    return Volume(
        array=np.zeros(shape, dtype=np.float32),
        spacing=(1.0, 1.0, 1.0), origin=(0.0, 0.0, 0.0),
        axis_dirs=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    )


def _two_nostrils():
    shape = (28, 38, 16)
    mask = np.zeros(shape, dtype=np.uint8)
    mask[4:11, 3:17, 5:12] = 1
    mask[17:24, 3:17, 5:12] = 1
    mask[4:24, 17:22, 5:12] = 1
    mask[9:19, 22:35, 5:12] = 1
    return mask, _volume(shape)


def _single_nostril():
    shape = (18, 32, 18)
    mask = np.zeros(shape, dtype=np.uint8)
    mask[5:13, 4:28, 5:13] = 1
    return mask, _volume(shape)


def _split_posterior():
    mask, volume = _two_nostrils()
    mask[:, 22:35, :] = 0
    mask[9:13, 22:35, 5:12] = 1
    mask[15:19, 22:35, 5:12] = 1
    return mask, volume


def test_single_branch_candidate_has_a_reviewable_local_plan():
    mask, volume = _single_nostril()
    candidates = auto._candidate_plans(mask, volume, 1, -1, "main_airway", 3)
    assert [candidate["label"] for candidate in candidates] == ["single"]
    plan = candidates[0]["plan"]
    assert endplane_candidates.validate_candidate_plan("main_airway", 3, plan) == plan
    assert plan["anterior_plane"]["normal_lps"][1] < -0.8
    assert plan["posterior_plane"]["normal_lps"][1] > 0.8


def test_two_nostrils_are_separate_and_neither_candidate_cuts_the_other():
    mask, volume = _two_nostrils()
    candidates = auto._candidate_plans(mask, volume, 1, -1, "main_airway", 3)
    assert [candidate["label"] for candidate in candidates] == ["left", "right"]
    for candidate in candidates:
        plan = candidate["plan"]
        assert endplane_candidates.validate_candidate_plan("main_airway", 3, plan) == plan
        checked = oblique.validate_local_inlet_pair(
            mask, volume, plan["anterior_plane"], plan["posterior_plane"],
            plan["anterior_seed_lps_mm"], main_airway=True,
        )
        assert checked["anterior_opening_components"] == 1
        assert checked["unselected_region_changed_voxels"] == 0
        assert checked["anterior_unselected_positive_voxels"] > 0
        clipped = oblique.clip_mask_oblique_local(
            mask, volume, plan["anterior_plane"], plan["posterior_plane"],
            anterior_seed_lps_mm=plan["anterior_seed_lps_mm"],
        )
        other_slice = 7 if candidate["label"] == "left" else 20
        assert clipped[other_slice, 5, 8] == 1


def test_bilateral_candidate_requires_exactly_three_real_caps():
    mask, volume = _two_nostrils()
    candidates = auto._bilateral_candidate_plans(mask, volume, 1, -1, "main_airway", 3)
    assert [item["label"] for item in candidates] == ["bilateral"]
    plan = candidates[0]["plan"]
    assert plan["format"] == "oblique_bilateral_local_v3"
    assert endplane_candidates.validate_candidate_plan("main_airway", 3, plan) == plan
    checked = oblique.validate_bilateral_local_planes(
        mask, volume, plan["left_inlet_plane"], plan["right_inlet_plane"],
        plan["posterior_plane"], plan["left_inlet_seed_lps_mm"],
        plan["right_inlet_seed_lps_mm"], plan["posterior_seed_lps_mm"],
        main_airway=True,
    )
    assert checked["retained_components"] == 1
    retention = candidates[0]["retention"]
    assert retention["retained_voxels"] == checked["retained_voxels"]
    assert retention["left_deleted_voxels"] == checked["left_deleted_voxels"]
    assert retention["right_deleted_voxels"] == checked["right_deleted_voxels"]
    assert retention["posterior_deleted_voxels"] == checked["posterior_deleted_voxels"]
    assert retention["removed_fraction"] <= auto._MAX_REMOVED_FRACTION
    assert all(retention[key] <= auto._MAX_END_DEPTH_MM for key in (
        "left_depth_mm", "right_depth_mm", "posterior_depth_mm"
    ))


def test_end_depths_use_millimeters_and_never_exceed_length_limit():
    isotropic = auto._end_depths(100, 1.0, minimum_slices=3)
    finer = auto._end_depths(200, 0.5, minimum_slices=3)
    assert [depth * 1.0 for depth in isotropic] == [
        depth * 0.5 for depth in finer if depth * 0.5 >= 3.0
    ]
    assert isotropic == sorted(isotropic)
    assert max(isotropic) <= auto._MAX_END_DEPTH_MM
    short = auto._end_depths(24, 1.0, minimum_slices=3)
    assert max(short) <= 24 * auto._MAX_END_DEPTH_FRACTION


def test_deep_front_branch_is_not_misreported_as_shallow_two_nostrils():
    mask, volume = _two_nostrils()
    # A substantial third connected branch occupies the first 11 mm. It joins
    # the left branch only farther inside, where an automatic cut is too deep.
    mask[4:11, 3:15, 0:3] = 1
    mask[4:11, 14:17, 2:6] = 1
    diagnostics = {}
    assert auto._candidate_plans(mask, volume, 1, -1, "main_airway", 3,
                                 diagnostics=diagnostics) == []
    assert diagnostics == {"reason": "excessive_cut_required"}


def test_volume_guard_blocks_auto_candidate_instead_of_choosing_deeper_cut(monkeypatch):
    mask, volume = _two_nostrils()
    monkeypatch.setattr(auto, "_MAX_REMOVED_FRACTION", 0.05)
    diagnostics = {}
    assert auto._candidate_plans(mask, volume, 1, -1, "main_airway", 3,
                                 diagnostics=diagnostics) == []
    assert diagnostics == {"reason": "excessive_cut_required"}
    monkeypatch.setattr(auto, "_MAX_REMOVED_FRACTION", 0.10)
    diagnostics = {}
    assert auto._bilateral_candidate_plans(mask, volume, 1, -1, "main_airway", 3,
                                           diagnostics=diagnostics) == []
    assert diagnostics == {"reason": "excessive_cut_required"}


def test_each_end_has_its_own_deletion_guard(monkeypatch):
    mask, volume = _single_nostril()
    monkeypatch.setattr(auto, "_MAX_END_REMOVED_FRACTION", 0.10)
    diagnostics = {}
    assert auto._candidate_plans(mask, volume, 1, -1, "main_airway", 3,
                                 diagnostics=diagnostics) == []
    assert diagnostics == {"reason": "excessive_cut_required"}


def test_bilateral_candidate_blocks_single_branch_and_failed_mesh(monkeypatch):
    single, volume = _single_nostril()
    assert auto._bilateral_candidate_plans(single, volume, 1, -1, "main_airway", 3) == []
    mask, volume = _two_nostrils()

    def reject_mesh(*_args, **_kwargs):
        raise PipelineError("blocked", "synthetic exact mesh failure")

    monkeypatch.setattr(auto.local_preview, "build_bilateral_local_mesh", reject_mesh)
    assert auto._bilateral_candidate_plans(mask, volume, 1, -1, "main_airway", 3) == []


def test_merged_anterior_branches_do_not_make_a_false_local_candidate():
    mask, volume = _two_nostrils()
    mask[10:17, 5:7, 7:10] = 1
    assert auto._candidate_plans(mask, volume, 1, -1, "main_airway", 3) == []


def test_split_posterior_is_not_guessed_into_a_single_outlet():
    mask, volume = _split_posterior()
    diagnostics = {}
    assert auto._candidate_plans(mask, volume, 1, -1, "main_airway", 3,
                                 diagnostics=diagnostics) == []
    assert diagnostics == {"reason": "posterior_single_section_missing"}


def test_one_failed_branch_does_not_hide_the_other(monkeypatch):
    mask, volume = _two_nostrils()
    original = auto.local_preview.build_local_inlet_mesh

    def reject_left(source, geometry, axis, scope, checked):
        if checked["anterior_seed_lps_mm"][0] > 14:
            raise PipelineError("blocked", "synthetic left branch preflight failure")
        return original(source, geometry, axis, scope, checked)

    monkeypatch.setattr(auto.local_preview, "build_local_inlet_mesh", reject_left)
    candidates = auto._candidate_plans(mask, volume, 1, -1, "main_airway", 3)
    assert [candidate["label"] for candidate in candidates] == ["right"]


def test_all_mesh_failures_return_no_candidate(monkeypatch):
    mask, volume = _two_nostrils()

    def reject_all(*_args, **_kwargs):
        raise PipelineError("blocked", "synthetic mesh preflight failure")

    monkeypatch.setattr(auto.local_preview, "build_local_inlet_mesh", reject_all)
    assert auto._candidate_plans(mask, volume, 1, -1, "main_airway", 3) == []


@pytest.fixture
def confirmed_case(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = cases.create_case("synthetic", "<synthetic>", synthetic=True)
    root = cases.case_dir(case_id)
    mask, volume = _two_nostrils()
    save_volume(case_id, volume)
    np.save(root / "segment" / "mask.npy", mask)
    record = {
        "params": {"ap_axis": 1, "hu_high": -300.0},
        "mask_version": 3, "need_human_confirm": False,
        "candidate_cleaning": {"status": "ok"},
        "review_flags": {"external_air_suspected": False},
        "human_review": {"status": "已确认"},
        "external_air_validation": {
            "mask_version": 3, "body_hu_threshold": -300.0,
            "outside_voxels": 0, "candidate_outside_voxels": 0,
        },
    }
    (root / "segment" / "segmentation.json").write_text(
        json.dumps(record), encoding="utf-8",
    )
    (root / "segment" / "endplanes.json").write_bytes(b'{"existing":true}')
    return case_id, root, record


def _snapshot(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def test_case_generator_uses_only_current_confirmed_mask_and_is_read_only(confirmed_case):
    case_id, root, _record = confirmed_case
    before = _snapshot(root)
    result = auto.generate_auto_candidates(case_id, "main_airway", 3)
    assert result["status"] == "available"
    assert result["generator_version"] == auto.GENERATOR_VERSION
    assert [candidate["label"] for candidate in result["candidates"]] == ["left", "right"]
    assert _snapshot(root) == before


def test_bilateral_case_generator_is_review_only_and_read_only(confirmed_case):
    case_id, root, _record = confirmed_case
    before = _snapshot(root)
    result = auto.generate_bilateral_candidate(case_id, "main_airway", 3)
    assert result["status"] == "available"
    assert result["generator_version"] == auto.BILATERAL_GENERATOR_VERSION
    assert [item["label"] for item in result["candidates"]] == ["bilateral"]
    assert result["candidates"][0]["plan"]["format"] == "oblique_bilateral_local_v3"
    assert _snapshot(root) == before
    unavailable = auto.generate_bilateral_candidate(case_id, "main_airway", 2)
    assert unavailable["status"] == "unavailable"
    assert unavailable["generator_version"] == auto.BILATERAL_GENERATOR_VERSION
    assert _snapshot(root) == before


def test_stale_or_unconfirmed_mask_has_no_candidate(confirmed_case):
    case_id, root, record = confirmed_case
    before = _snapshot(root)
    stale = auto.generate_auto_candidates(case_id, "main_airway", 2)
    assert stale["status"] == "unavailable"
    assert stale["candidates"] == []
    assert _snapshot(root) == before

    record["need_human_confirm"] = True
    (root / "segment" / "segmentation.json").write_text(json.dumps(record), encoding="utf-8")
    before = _snapshot(root)
    unconfirmed = auto.generate_auto_candidates(case_id, "main_airway", 3)
    assert unconfirmed["status"] == "unavailable"
    assert unconfirmed["candidates"] == []
    assert _snapshot(root) == before


def test_full_candidate_scope_never_generates_local_inlet(confirmed_case):
    case_id, root, _record = confirmed_case
    before = _snapshot(root)
    result = auto.generate_auto_candidates(case_id, "full_candidate", 3)
    assert result["status"] == "unavailable"
    assert result["reason"] == "unsupported_scope"
    assert _snapshot(root) == before


def test_split_posterior_reports_specific_reason_without_writing(confirmed_case):
    case_id, root, _record = confirmed_case
    mask, _volume = _split_posterior()
    np.save(root / "segment" / "mask.npy", mask)
    before = _snapshot(root)
    result = auto.generate_auto_candidates(case_id, "main_airway", 3)
    assert result["status"] == "unavailable"
    assert result["reason"] == "posterior_single_section_missing"
    assert result["candidates"] == []
    assert _snapshot(root) == before


def test_confirmed_synthetic_case_through_auto_proposal_api(confirmed_case, monkeypatch):
    import server
    from pipeline import endplane_learning

    case_id, root, _record = confirmed_case
    monkeypatch.setattr(server, "CASES_DIR", cases.CASES_DIR)
    monkeypatch.setattr(endplane_learning, "_DEFAULT_STORE", None)
    monkeypatch.setattr(endplane_learning, "_DEFAULT_STORE_PATH", None)
    before = _snapshot(root)
    result = asyncio.run(server.api_auto_propose_endplane_candidate(
        case_id, server.EndplaneAutoProposalBody(scope="main_airway", mask_version=3),
    ))
    assert result["status"] == "available"
    assert result["human_review_required"] is True and result["committed"] is False
    assert {item["source"] for item in result["proposals"]} == {
        "local_mask_geometry_left", "local_mask_geometry_right",
    }
    assert all(item["status"] == "proposed" for item in result["proposals"])
    assert _snapshot(root) == before


def test_confirmed_synthetic_case_through_bilateral_proposal_api(confirmed_case, monkeypatch):
    import server
    from pipeline import endplane_learning

    case_id, root, _record = confirmed_case
    monkeypatch.setattr(server, "CASES_DIR", cases.CASES_DIR)
    monkeypatch.setattr(endplane_learning, "_DEFAULT_STORE", None)
    monkeypatch.setattr(endplane_learning, "_DEFAULT_STORE_PATH", None)
    before = _snapshot(root)
    result = asyncio.run(server.api_auto_propose_bilateral_endplanes(
        case_id, server.EndplaneAutoProposalBody(scope="main_airway", mask_version=3),
    ))
    assert result["status"] == "available"
    assert result["human_review_required"] is True and result["committed"] is False
    assert result["proposal"]["source"] == "bilateral_mask_geometry"
    assert result["proposal"]["plan"]["format"] == "oblique_bilateral_local_v3"
    assert _snapshot(root) == before
