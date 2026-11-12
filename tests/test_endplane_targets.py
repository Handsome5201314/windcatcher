"""CT picker invariants: shared plane, explicit regions, no global clipping."""
import asyncio
import json

import numpy as np
import pytest
from scipy import ndimage

from pipeline import oblique, endplanes, access_policy
from pipeline.endplane_targets import section_targets
from pipeline.endplane_section import section_coordinates
from pipeline.volume import Volume
from pipeline.errors import PipelineError


@pytest.fixture
def geometry():
    mask = np.zeros((34, 38, 18), dtype=np.uint8)
    mask[4:11, 3:17, 5:12] = 1
    mask[17:24, 3:17, 5:12] = 1
    mask[4:24, 17:22, 5:12] = 1
    mask[9:19, 22:35, 5:12] = 1
    v = Volume(np.zeros(mask.shape, dtype=np.float32), (1., 1., 1.), (0., 0., 0.), tuple(map(tuple, np.eye(3))))
    inlet = {"point_lps_mm": [14., 9., 8.], "normal_lps": [0., -1., 0.]}
    outlet = {"point_lps_mm": [14., 29., 8.], "normal_lps": [0., 1., 0.]}
    return mask, v, inlet, outlet


def test_common_plane_resolves_two_distinct_inlets_and_exact_local_cuts(geometry):
    mask, v, plane, outlet = geometry
    before = mask.copy()
    result = section_targets(v, mask, plane, "anterior", 50, 100)
    assert len(result["candidates"]) == len(result["automatic_ids"]) == 2
    chosen = {c["id"]: c for c in result["candidates"]}
    seeds = [chosen[i]["seed_lps_mm"] for i in result["automatic_ids"]]
    assert seeds[0][0] > seeds[1][0]
    post = section_targets(v, mask, outlet, "posterior", 50, 100)
    outlet_seed = post["candidates"][0]["seed_lps_mm"]
    check = oblique.validate_bilateral_local_planes(mask, v, plane, plane, outlet, *seeds, outlet_seed, main_airway=True)
    assert check["retained_components"] == 1
    np.testing.assert_array_equal(mask, before)
    restored = section_targets(v, mask, plane, "anterior", 50, 100, seeds=seeds)
    assert restored["selected_ids"] == result["automatic_ids"]


def test_third_outer_branch_is_not_misreported_as_bilateral(geometry):
    mask, v, plane, outlet = geometry
    mask[27:32, 3:19, 5:12] = 1
    mask[20:32, 17:22, 5:12] = 1
    result = section_targets(v, mask, plane, "anterior", 60, 120)
    assert len(result["candidates"]) == 3
    assert result["automatic_ids"] == []
    selected = sorted(result["candidates"], key=lambda c: c["seed_lps_mm"][0])[:2]
    seeds = [c["seed_lps_mm"] for c in reversed(selected)]
    post = section_targets(v, mask, outlet, "posterior", 60, 120)
    with pytest.raises(PipelineError, match="未覆盖外侧") as exc:
        oblique.clip_mask_oblique_bilateral_local(mask, v, plane, plane, outlet,
            left_inlet_seed_lps_mm=seeds[0], right_inlet_seed_lps_mm=seeds[1],
            posterior_seed_lps_mm=post["candidates"][0]["seed_lps_mm"])
    assert exc.value.detail["unselected_outer_branches"] == 1


def test_tiny_spur_is_visible_but_not_a_selectable_nostril(geometry):
    mask, volume, plane, _ = geometry
    mask = mask.copy()
    volume = Volume(volume.array, (0.5, 0.5, 0.5), volume.origin, volume.axis_dirs)
    plane = {**plane, "point_lps_mm": [x * 0.5 for x in plane["point_lps_mm"]]}
    # A four-voxel deletion-side spur joins the right inlet behind the plane, but its
    # one-voxel crossing cannot stand in for the substantial right inlet.
    mask[1, 5:11, 8] = 1
    mask[1:5, 10, 8] = 1
    before = mask.copy()
    selected, _, _ = oblique._local_delete_component(mask, volume, plane, [0.5, 4., 4.])
    assert np.count_nonzero(selected) == 4
    result = section_targets(volume, mask, plane, "anterior", 25, 100, seeds=[[0.5, 4., 4.]])
    tiny = next(c for c in result["candidates"] if c["center_lps_mm"][0] < 1.5)
    assert tiny["runs"]
    assert tiny["valid"] is False
    assert "不能作为完整鼻孔入口" in tiny["reason"]
    assert tiny["id"] not in result["automatic_ids"]
    assert tiny["id"] not in result["selected_ids"]
    assert result["uncovered_count"] > 0
    np.testing.assert_array_equal(mask, before)


def test_merged_nostrils_block_automatic_selection(geometry):
    mask, v, plane, _ = geometry
    plane["point_lps_mm"][1] = 19
    result = section_targets(v, mask, plane, "anterior", 60, 120)
    assert not result["automatic_ids"]
    assert any(not c["valid"] for c in result["candidates"])


def test_plane_outside_mask_returns_no_targets(geometry):
    mask, v, plane, _ = geometry
    plane["point_lps_mm"][1] = -10
    result = section_targets(v, mask, plane, "anterior", 60, 120)
    assert result["candidates"] == result["automatic_ids"] == []


def test_overlay_runs_keep_all_visible_regions_and_valid_pixel_bounds(geometry):
    mask, v, plane, _ = geometry
    result = section_targets(v, mask, plane, "anterior", 50, 100)
    pixels = set()
    for region in result["candidates"]:
        assert region["runs"]
        for row, start, end in region["runs"]:
            assert 0 <= row < 100 and 0 <= start < end <= 100
            for col in range(start, end):
                assert (row, col) not in pixels
                pixels.add((row, col))


@pytest.mark.parametrize("span_mm,size", [(50, 100), (30, 384)])
def test_straight_inlet_overlay_covers_complete_source_cross_sections(geometry, span_mm, size):
    mask, volume, plane, _ = geometry
    result = section_targets(volume, mask, plane, "anterior", span_mm, size)
    section = ndimage.map_coordinates(
        mask, section_coordinates(volume, plane, span_mm, size),
        order=0, mode="constant", cval=0, prefilter=False,
    ).astype(bool)
    overlay = np.zeros((size, size), dtype=bool)
    for region in result["candidates"]:
        for row, start, end in region["runs"]:
            overlay[row, start:end] = True

    # Two straight, independent inlet branches cross this plane. Their
    # complete visible cross sections must remain selectable, including
    # pixels nearest the occupied label crop's half-voxel boundary.
    assert len(result["candidates"]) == 2
    np.testing.assert_array_equal(overlay, section)
    assert result["uncovered_count"] == 0
    assert result["uncovered_runs"] == []


def test_invalid_candidate_is_reported_as_uncovered_not_selectable(geometry):
    mask, volume, plane, _ = geometry
    plane["point_lps_mm"][1] = 19
    result = section_targets(volume, mask, plane, "anterior", 60, 120)
    uncovered = np.zeros((120, 120), dtype=bool)
    valid = np.zeros_like(uncovered)
    for row, start, end in result["uncovered_runs"]:
        uncovered[row, start:end] = True
    for candidate in result["candidates"]:
        if candidate["valid"]:
            for row, start, end in candidate["runs"]:
                valid[row, start:end] = True
    section = ndimage.map_coordinates(
        mask, section_coordinates(volume, plane, 60, 120),
        order=0, mode="constant", cval=0, prefilter=False,
    ).astype(bool)
    assert result["uncovered_count"] == np.count_nonzero(uncovered) > 0
    assert not np.any(valid & uncovered)
    np.testing.assert_array_equal(valid | uncovered, section)


def test_new_route_requires_case_view_permission():
    path = "/api/cases/case-20261104-010203-abcd/segment/endplanes/section-targets"
    policy = access_policy.classify(path, "POST")
    assert policy.capability == "view"
    assert policy.case_id == "case-20261104-010203-abcd"
    assert access_policy.classify(path, "GET").kind == "deny"
    suggestion = "/api/cases/case-20261104-010203-abcd/segment/endplanes/suggest-single-port"
    assert access_policy.classify(suggestion, "POST").capability == "view"
    assert access_policy.classify(suggestion, "GET").kind == "deny"


def test_shift_api_is_read_only_and_returns_reviewable_plan(monkeypatch):
    import server

    monkeypatch.setattr(server, "_require_case", lambda _: None)
    seen = []
    monkeypatch.setattr(endplanes, "suggest_bilateral_local_inlet_shift",
                        lambda *args: seen.append(args) or {"status": "unavailable", "reason": "no_safe_port"})
    body = server.EndplaneShiftBody(
        scope="main_airway", mask_version=3, format=endplanes.BILATERAL_LOCAL_FORMAT,
        side="left", left_inlet_plane={"point_lps_mm": [20, 9, 8], "normal_lps": [0, -1, 0]},
        right_inlet_plane={"point_lps_mm": [7, 9, 8], "normal_lps": [0, -1, 0]},
        posterior_plane={"point_lps_mm": [14, 29, 8], "normal_lps": [0, 1, 0]},
        left_inlet_seed_lps_mm=[20, 5, 8], right_inlet_seed_lps_mm=[7, 5, 8],
        posterior_seed_lps_mm=[14, 32, 8],
    )
    response = asyncio.run(server.api_suggest_single_port("case-test", body))
    assert response.headers["cache-control"] == "no-store"
    assert json.loads(response.body) == {"status": "unavailable", "reason": "no_safe_port"}
    assert seen[0][-2:] == (3, "left")


def test_target_inspection_rejects_version_change_during_processing(monkeypatch, geometry):
    mask, v, plane, _ = geometry
    monkeypatch.setattr(endplanes, "_oblique_context", lambda *_: ("main_airway", None, v, mask, None, None, None))
    monkeypatch.setattr(endplanes, "_segmentation", lambda _: {"mask_version": 4})
    with pytest.raises(PipelineError, match="版本发生变化"):
        endplanes.inspect_endplane_targets("case-test", "main_airway", plane, 3, 50, "anterior")


def test_api_returns_uncached_regions_without_saving(monkeypatch, geometry):
    import server
    mask, v, plane, _ = geometry
    monkeypatch.setattr(server, "_require_case", lambda _: None)
    monkeypatch.setattr(endplanes, "_oblique_context", lambda *_: ("main_airway", None, v, mask, None, None, None))
    monkeypatch.setattr(endplanes, "_segmentation", lambda _: {"mask_version": 3})
    body = server.EndplaneTargetsBody(scope="main_airway", mask_version=3, plane=plane, role="anterior", span_mm=50)
    response = asyncio.run(server.api_endplane_targets("case-test", body))
    assert response.headers["cache-control"] == "no-store"
    result = json.loads(response.body)
    assert result["mask_version"] == 3 and len(result["automatic_ids"]) == 2
