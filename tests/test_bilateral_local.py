"""Synthetic double-nostril / single-outlet local-geometry regression."""

from collections import Counter
import json

import numpy as np
import pytest

from pipeline import cases, endplanes, formal_ports, geom_qc, oblique, orchestration, surface
from pipeline.endplane_auto_candidate import (BILATERAL_GENERATOR_VERSION,
                                              suggest_outward_bilateral_inlet_shift)
from pipeline.endplane_targets import section_targets
from pipeline.bilateral_local import _port_boundary_loops, clip_mesh_bilateral_local
from pipeline.errors import PipelineError
from pipeline.volume import Volume, save_volume


@pytest.fixture
def bilateral_tube():
    mask = np.zeros((28, 38, 16), dtype=np.uint8)
    mask[4:11, 3:17, 5:12] = 1
    mask[17:24, 3:17, 5:12] = 1
    mask[4:24, 17:22, 5:12] = 1
    mask[9:19, 22:35, 5:12] = 1
    volume = Volume(
        array=np.zeros(mask.shape, dtype=np.float32),
        spacing=(1.0, 1.0, 1.0), origin=(0.0, 0.0, 0.0),
        axis_dirs=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    )
    left = {"point_lps_mm": [20.0, 9.0, 8.0], "normal_lps": [0.0, -1.0, 0.0]}
    right = {"point_lps_mm": [7.0, 9.0, 8.0], "normal_lps": [0.0, -1.0, 0.0]}
    outlet = {"point_lps_mm": [14.0, 29.0, 8.0], "normal_lps": [0.0, 1.0, 0.0]}
    seeds = ([20.0, 5.0, 8.0], [7.0, 5.0, 8.0], [14.0, 32.0, 8.0])
    return mask, volume, left, right, outlet, seeds


@pytest.fixture
def inferior_outlet_tube():
    """A joined nasal airway whose independent outlet turns toward the feet."""
    mask = np.zeros((28, 38, 24), dtype=np.uint8)
    mask[4:11, 3:17, 10:17] = 1
    mask[17:24, 3:17, 10:17] = 1
    mask[4:24, 17:22, 10:17] = 1
    mask[9:19, 22:31, 10:17] = 1
    mask[9:19, 26:33, 3:17] = 1
    volume = Volume(
        np.zeros(mask.shape, dtype=np.float32), (1., 1., 1.),
        (0., 0., 0.), tuple(map(tuple, np.eye(3))),
    )
    left = {"point_lps_mm": [20., 7., 13.], "normal_lps": [0., -1., 0.]}
    right = {"point_lps_mm": [7., 7., 13.], "normal_lps": [0., -1., 0.]}
    outlet = {"point_lps_mm": [14., 29., 8.], "normal_lps": [0., 0., -1.]}
    seeds = ([20., 5., 13.], [7., 5., 13.], [14., 29., 4.])
    return mask, volume, left, right, outlet, seeds


def _validate(args):
    mask, volume, left, right, outlet, seeds = args
    return oblique.validate_bilateral_local_planes(
        mask, volume, left, right, outlet, *seeds, main_airway=True,
    )


def test_inferior_bilateral_outlet_passes_voxel_and_actual_mesh_gates(inferior_outlet_tube):
    from skimage import measure

    mask, volume, left, right, outlet, seeds = inferior_outlet_tube
    checked = _validate(inferior_outlet_tube)
    assert checked["posterior_plane"] == outlet
    assert checked["retained_components"] == 1
    assert checked["posterior_deleted_voxels"] > 0
    retained = oblique.clip_mask_oblique_bilateral_local(
        mask, volume, left, right, outlet,
        left_inlet_seed_lps_mm=seeds[0], right_inlet_seed_lps_mm=seeds[1],
        posterior_seed_lps_mm=seeds[2],
    )
    assert not retained[14, 29, 4]
    assert retained[14, 29, 13]
    vertices, faces, _, _ = measure.marching_cubes(mask, level=0.5)
    _, _, left_cap, right_cap, outlet_cap, stats = clip_mesh_bilateral_local(
        vertices, faces, mask, volume,
        left_inlet_plane=left, right_inlet_plane=right, posterior_plane=outlet,
        left_inlet_seed_lps_mm=seeds[0], right_inlet_seed_lps_mm=seeds[1],
        posterior_seed_lps_mm=seeds[2],
    )
    assert stats["closed_caps"] is True
    assert all(len(cap) > 0 for cap in (left_cap, right_cap, outlet_cap))
    assert (stats["left_inlet_loops"], stats["right_inlet_loops"], stats["outlet_loops"]) == (1, 1, 1)


def test_inferior_bilateral_outlet_rejects_inward_normal_and_wrong_side_seed(inferior_outlet_tube):
    mask, volume, left, right, outlet, seeds = inferior_outlet_tube
    inward = {**outlet, "normal_lps": [0., 0., 1.]}
    with pytest.raises(PipelineError, match="法向"):
        oblique.validate_bilateral_local_planes(
            mask, volume, left, right, inward, *seeds, main_airway=True,
        )
    wrong_side_seeds = (*seeds[:2], [14., 29., 9.])
    with pytest.raises(PipelineError, match="定位点|锚点"):
        oblique.validate_bilateral_local_planes(
            mask, volume, left, right, outlet, *wrong_side_seeds, main_airway=True,
        )
    # The older two-end endpoint rule must not be widened by this local exception.
    with pytest.raises(PipelineError, match="法向"):
        oblique.validate_plane_endpoint(outlet, "posterior")


def test_outward_inlet_shift_uses_same_footward_outlet_gate(inferior_outlet_tube):
    mask, volume, left, right, outlet, seeds = inferior_outlet_tube
    result = suggest_outward_bilateral_inlet_shift(
        mask, volume, 1, "main_airway", side="left",
        left_inlet_plane=left, right_inlet_plane=right, posterior_plane=outlet,
        left_inlet_seed_lps_mm=seeds[0], right_inlet_seed_lps_mm=seeds[1],
        posterior_seed_lps_mm=seeds[2],
    )
    assert result["status"] == "proposed", result
    assert result["plan"]["posterior_plane"] == outlet
    assert result["mesh_preflight"]["closed_caps"] is True


@pytest.fixture
def auto_port_case(bilateral_tube, tmp_path, monkeypatch):
    mask, volume, left, right, outlet, seeds = bilateral_tube
    # Automatic proposals must preserve most of the external inlet context.
    left = {**left, "point_lps_mm": [20.0, 5.0, 8.0]}
    right = {**right, "point_lps_mm": [7.0, 5.0, 8.0]}
    outlet = {**outlet, "point_lps_mm": [14.0, 33.0, 8.0]}
    seeds = ([20.0, 4.0, 8.0], [7.0, 4.0, 8.0], [14.0, 34.0, 8.0])
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = cases.create_case("local_path", "<自动端面合成测试>", synthetic=False)
    root = cases.case_dir(case_id)
    save_volume(case_id, volume)
    np.save(root / "segment" / "mask.npy", mask)
    (root / "segment" / "segmentation.json").write_text(json.dumps({
        "params": {"ap_axis": 1, "hu_high": -300.0},
        "mask_version": 1, "need_human_confirm": False,
        "review_flags": {"external_air_suspected": False},
        "external_air_validation": {
            "mask_version": 1, "body_hu_threshold": -300.0,
            "outside_voxels": 0, "candidate_outside_voxels": 0,
        },
    }), encoding="utf-8")
    for stage in ("import", "anonymize", "qc", "segment"):
        cases.update_stage(case_id, stage, "ok")
    return case_id, root, left, right, outlet, seeds


def test_automated_three_port_plan_keeps_provenance_and_real_caps(auto_port_case):
    case_id, root, left, right, outlet, seeds = auto_port_case
    checks = []
    plan = endplanes.save_automated_bilateral_endplanes(
        case_id, "main_airway", left, right, outlet, *seeds, 1,
        generator_version=BILATERAL_GENERATOR_VERSION,
        before_persist=lambda: checks.append("authorized"),
    )
    assert checks == ["authorized"]
    assert plan["confirmation_source"] == "automated_technical_gate"
    assert plan["human_confirmed"] is False
    assert plan["mesh_preflight"]["closed_caps"] is True
    assert endplanes.load_plan(case_id, "main_airway") == plan
    assert not (root / "surface" / "airway.stl").exists()
    result = orchestration.advance_case(case_id)
    assert result["status"] == "prepared", result
    surface_result = json.loads((root / "surface" / "surface.json").read_text(encoding="utf-8"))
    assert surface_result["endplanes"]["source"] == "automated_bilateral_local_oblique_planes"
    qc = json.loads((root / "surface" / "geom_qc.json").read_text(encoding="utf-8"))
    assert qc["verdict"] == "pass"
    assert formal_ports.get_formal_port_state(case_id)["status"] == "ready"
    case_meta = json.loads((root / "cfd" / "case_meta.json").read_text(encoding="utf-8"))
    assert case_meta["boundary_layout"] == "bilateral_local_v3"


def test_automated_three_port_plan_never_overwrites_existing_plan(auto_port_case):
    case_id, root, left, right, outlet, seeds = auto_port_case
    saved = endplanes.save_bilateral_local_endplanes(
        case_id, "main_airway", left, right, outlet, *seeds, 1)
    with pytest.raises(PipelineError, match="不能覆盖"):
        endplanes.save_automated_bilateral_endplanes(
            case_id, "main_airway", left, right, outlet, *seeds, 1,
            generator_version=BILATERAL_GENERATOR_VERSION,
            before_persist=lambda: pytest.fail("已有端面不能触发保存回调"),
        )
    assert endplanes.load_plan(case_id, "main_airway") == saved


def test_automated_three_port_plan_rechecks_before_commit(auto_port_case):
    case_id, root, left, right, outlet, seeds = auto_port_case
    with pytest.raises(PipelineError, match="权限已变化"):
        endplanes.save_automated_bilateral_endplanes(
            case_id, "main_airway", left, right, outlet, *seeds, 1,
            generator_version=BILATERAL_GENERATOR_VERSION,
            before_persist=lambda: (_ for _ in ()).throw(
                PipelineError("blocked", "权限已变化", ["重新提交"])),
        )
    assert not (root / "segment" / "endplanes.json").exists()


def test_automated_plan_with_stale_generator_cannot_build_surface(auto_port_case):
    case_id, root, left, right, outlet, seeds = auto_port_case
    endplanes.save_automated_bilateral_endplanes(
        case_id, "main_airway", left, right, outlet, *seeds, 1,
        generator_version=BILATERAL_GENERATOR_VERSION,
        before_persist=lambda: None,
    )
    path = root / "segment" / "endplanes.json"
    plans = json.loads(path.read_text(encoding="utf-8"))
    plans["main_airway"]["generator_version"] = "old-generator"
    path.write_text(json.dumps(plans), encoding="utf-8")
    with pytest.raises(PipelineError, match="技术来源不完整"):
        surface.run_surface(case_id)
    assert not (root / "surface" / "airway.stl").exists()


def test_four_closed_mesh_loops_identify_right_nostril_without_auto_capping():
    """A second right-inlet ring is a named blocking defect, not a fourth cap."""
    def square(x, y, z, half):
        return np.asarray([
            [x - half, y, z - half], [x + half, y, z - half],
            [x + half, y, z + half], [x - half, y, z + half],
        ], dtype=float)

    planes = {
        "left": {"point_lps_mm": [10., 0., 0.], "normal_lps": [0., 1., 0.]},
        "right": {"point_lps_mm": [-10., 0., 0.], "normal_lps": [0., 1., 0.]},
        "outlet": {"point_lps_mm": [0., 10., 0.], "normal_lps": [0., 1., 0.]},
    }
    seeds = {"left": [10., -2., 0.], "right": [-10., -2., 0.],
             "outlet": [0., 12., 0.]}
    three = [square(10., 0., 0., 2.), square(-10., 0., 0., 2.),
             square(0., 10., 0., 2.)]
    grouped = _port_boundary_loops(three, 12, planes, seeds, 1.)
    assert {side: len(loops) for side, loops in grouped.items()} == {
        "left": 1, "right": 1, "outlet": 1,
    }

    with pytest.raises(PipelineError, match="右鼻孔切面形成 2 个闭合环") as exc:
        _port_boundary_loops(three + [square(-9.5, 0., 4., 0.25)],
                             16, planes, seeds, 1.)
    assert exc.value.status == "blocked"
    assert exc.value.detail["port_loop_counts"] == {
        "left": 1, "right": 2, "outlet": 1,
    }
    assert exc.value.detail["offending_ports"] == ["right"]


def test_bilateral_local_voxel_cut_has_two_distinct_inlets_and_one_outlet(bilateral_tube):
    mask, volume, left, right, outlet, seeds = bilateral_tube
    result = _validate(bilateral_tube)
    assert result["retained_components"] == 1
    assert result["left_deleted_voxels"] > 0
    assert result["right_deleted_voxels"] > 0
    assert result["posterior_deleted_voxels"] > 0
    retained = oblique.clip_mask_oblique_bilateral_local(
        mask, volume, left, right, outlet,
        left_inlet_seed_lps_mm=seeds[0],
        right_inlet_seed_lps_mm=seeds[1],
        posterior_seed_lps_mm=seeds[2],
    )
    assert not retained[20, 5, 8] and not retained[7, 5, 8]
    assert retained[20, 14, 8] and retained[7, 14, 8]
    assert retained[14, 25, 8] and not retained[14, 32, 8]


def test_outward_shift_proposal_keeps_angle_and_passes_real_three_cap_mesh(bilateral_tube):
    mask, volume, left, right, outlet, seeds = bilateral_tube
    right = {**right, "point_lps_mm": [7.0, 7.0, 8.0]}
    outlet = {**outlet, "point_lps_mm": [14.0, 32.0, 8.0]}
    seeds = (seeds[0], [7.0, 4.0, 8.0], [14.0, 34.0, 8.0])
    result = suggest_outward_bilateral_inlet_shift(
        mask, volume, 1, "main_airway", side="left",
        left_inlet_plane=left, right_inlet_plane=right, posterior_plane=outlet,
        left_inlet_seed_lps_mm=seeds[0], right_inlet_seed_lps_mm=seeds[1],
        posterior_seed_lps_mm=seeds[2],
    )
    assert result["status"] == "proposed", result
    assert 0 < result["shift_mm"]["left"] <= 2.0
    assert result["shift_mm"]["right"] == result["shift_mm"]["outlet"] == 0
    plan = result["plan"]
    assert plan["left_inlet_plane"]["normal_lps"] == left["normal_lps"]
    assert plan["right_inlet_plane"] == right
    assert plan["posterior_plane"] == outlet
    np.testing.assert_allclose(
        plan["left_inlet_plane"]["point_lps_mm"],
        np.asarray(left["point_lps_mm"]) + result["shift_mm"]["left"] * np.asarray(left["normal_lps"]),
    )
    assert result["mesh_preflight"] == {
        "closed_caps": True, "left_inlet_loops": 1,
        "right_inlet_loops": 1, "outlet_loops": 1,
    }
    old, _, _ = oblique._local_delete_component(mask, volume, left, seeds[0])
    new, _, _ = oblique._local_delete_component(
        mask, volume, plan["left_inlet_plane"], plan["left_inlet_seed_lps_mm"],
    )
    assert np.count_nonzero(new) < np.count_nonzero(old)
    assert not np.any(new & ~old)


def test_outward_shift_preserves_unselected_medial_branch(bilateral_tube):
    mask, volume, left, right, outlet, seeds = bilateral_tube
    mask = mask.copy()
    mask[12:15, 3:18, 5:12] = 1
    right = {**right, "point_lps_mm": [7.0, 7.0, 8.0]}
    outlet = {**outlet, "point_lps_mm": [14.0, 32.0, 8.0]}
    before = mask.copy()
    result = suggest_outward_bilateral_inlet_shift(
        mask, volume, 1, "main_airway", side="left",
        left_inlet_plane=left, right_inlet_plane=right, posterior_plane=outlet,
        left_inlet_seed_lps_mm=seeds[0], right_inlet_seed_lps_mm=[7.0, 4.0, 8.0],
        posterior_seed_lps_mm=[14.0, 34.0, 8.0],
    )
    assert result["status"] == "proposed"
    plan = result["plan"]
    retained = oblique.clip_mask_oblique_bilateral_local(
        mask, volume, plan["left_inlet_plane"], plan["right_inlet_plane"],
        plan["posterior_plane"],
        left_inlet_seed_lps_mm=plan["left_inlet_seed_lps_mm"],
        right_inlet_seed_lps_mm=plan["right_inlet_seed_lps_mm"],
        posterior_seed_lps_mm=plan["posterior_seed_lps_mm"],
    )
    assert retained[13, 5, 8]
    np.testing.assert_array_equal(mask, before)


def test_outward_shift_cannot_treat_third_outer_inlet_as_two_nostrils():
    mask = np.zeros((40, 38, 16), dtype=np.uint8)
    mask[4:9, 3:17, 5:12] = 1
    mask[17:22, 3:17, 5:12] = 1
    mask[27:32, 3:17, 5:12] = 1
    mask[4:32, 17:22, 5:12] = 1
    mask[12:25, 22:35, 5:12] = 1
    volume = Volume(np.zeros(mask.shape, dtype=np.float32), (1., 1., 1.),
                    (0., 0., 0.), tuple(map(tuple, np.eye(3))))
    left = {"point_lps_mm": [29., 9., 8.], "normal_lps": [0., -1., 0.]}
    right = {"point_lps_mm": [19., 7., 8.], "normal_lps": [0., -1., 0.]}
    outlet = {"point_lps_mm": [18., 32., 8.], "normal_lps": [0., 1., 0.]}
    result = suggest_outward_bilateral_inlet_shift(
        mask, volume, 1, "main_airway", side="left",
        left_inlet_plane=left, right_inlet_plane=right, posterior_plane=outlet,
        left_inlet_seed_lps_mm=[29., 5., 8.],
        right_inlet_seed_lps_mm=[19., 4., 8.], posterior_seed_lps_mm=[18., 34., 8.],
    )
    assert result["status"] == "unavailable"
    assert result["reason"] == "three_cap_preflight_unavailable"


def test_outward_shift_removes_shallow_fragment_from_picker_without_cutting_it(bilateral_tube):
    mask, volume, left, right, outlet, seeds = bilateral_tube
    mask = mask.copy()
    # The small sheet projects beside the left nostril at y=9, and joins it
    # only behind the plane. It is an independently numbered shallow target.
    mask[25:27, 8:11, 8] = 1
    mask[24:26, 9:11, 8] = 1
    before = section_targets(volume, mask, left, "anterior", 50, 100)
    assert len(before["candidates"]) == 3
    right = {**right, "point_lps_mm": [7., 7., 8.]}
    outlet = {**outlet, "point_lps_mm": [14., 32., 8.]}
    result = suggest_outward_bilateral_inlet_shift(
        mask, volume, 1, "main_airway", side="left",
        left_inlet_plane=left, right_inlet_plane=right, posterior_plane=outlet,
        left_inlet_seed_lps_mm=seeds[0], right_inlet_seed_lps_mm=[7., 4., 8.],
        posterior_seed_lps_mm=[14., 34., 8.],
    )
    assert result["status"] == "proposed", result
    assert result["shift_mm"]["left"] == 2.0
    plan = result["plan"]
    after = section_targets(volume, mask, plan["left_inlet_plane"], "anterior", 50, 100)
    assert len(after["candidates"]) == 2
    assert result["mesh_preflight"] == {
        "closed_caps": True, "left_inlet_loops": 1,
        "right_inlet_loops": 1, "outlet_loops": 1,
    }
    retained = oblique.clip_mask_oblique_bilateral_local(
        mask, volume, plan["left_inlet_plane"], plan["right_inlet_plane"],
        plan["posterior_plane"],
        left_inlet_seed_lps_mm=plan["left_inlet_seed_lps_mm"],
        right_inlet_seed_lps_mm=plan["right_inlet_seed_lps_mm"],
        posterior_seed_lps_mm=plan["posterior_seed_lps_mm"],
    )
    assert retained[25, 8, 8]  # shallow bump is preserved, not silently merged into the cut


def test_bilateral_local_mesh_has_three_disjoint_water_tight_caps(bilateral_tube):
    from skimage import measure

    mask, volume, left, right, outlet, seeds = bilateral_tube
    vertices, faces, _, _ = measure.marching_cubes(mask, level=0.5)
    points, triangles, left_cap, right_cap, out_cap, stats = clip_mesh_bilateral_local(
        vertices, faces, mask, volume,
        left_inlet_plane=left, right_inlet_plane=right, posterior_plane=outlet,
        left_inlet_seed_lps_mm=seeds[0], right_inlet_seed_lps_mm=seeds[1],
        posterior_seed_lps_mm=seeds[2],
    )
    assert stats["format"] == "oblique_mesh_clip_bilateral_local_v3"
    assert stats["closed_caps"] is True
    assert all(len(cap) > 0 for cap in (left_cap, right_cap, out_cap))
    assert not set(left_cap) & set(right_cap)
    assert not set(left_cap) & set(out_cap)
    assert not set(right_cap) & set(out_cap)
    assert points[triangles[left_cap]][:, :, 0].mean() > points[triangles[right_cap]][:, :, 0].mean()
    counts = Counter(
        tuple(sorted((int(a), int(b))))
        for face in triangles
        for a, b in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0]))
    )
    assert counts and set(counts.values()) == {2}


def test_bilateral_local_preserves_seed_distant_subvoxel_plane_graze(bilateral_tube):
    """A tiny wall bump grazed by the plane is not a second inlet."""
    from skimage import measure

    mask, volume, left, right, outlet, seeds = bilateral_tube
    mask = mask.copy()
    # Connected behind the cut, this bump has a separate tiny positive-side
    # wall sheet, but its voxel label is the same as the selected nostril.
    mask[24:26, 10, 8] = 1
    mask[25, 9:11, 8] = 1
    left = {**left, "point_lps_mm": [20.0, 8.9, 8.0]}
    vertices, faces, _, _ = measure.marching_cubes(mask, level=0.5)

    points, triangles, left_cap, right_cap, out_cap, stats = clip_mesh_bilateral_local(
        vertices, faces, mask, volume,
        left_inlet_plane=left, right_inlet_plane=right, posterior_plane=outlet,
        left_inlet_seed_lps_mm=seeds[0], right_inlet_seed_lps_mm=seeds[1],
        posterior_seed_lps_mm=seeds[2],
    )

    assert points[:, 0].max() >= 25.5  # the grazed bump remains in the model
    assert stats["closed_caps"] is True
    assert all(len(cap) > 0 for cap in (left_cap, right_cap, out_cap))
    edge_counts = Counter(
        tuple(sorted((int(a), int(b))))
        for face in triangles
        for a, b in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0]))
    )
    assert set(edge_counts.values()) == {2}


def test_bilateral_local_still_blocks_second_substantial_sheet(bilateral_tube):
    """A long internal wall opening cannot be dismissed as a plane graze."""
    from skimage import measure

    mask, volume, left, right, outlet, seeds = bilateral_tube
    mask = mask.copy()
    mask[19:22, 4:14, 7:10] = 0
    vertices, faces, _, _ = measure.marching_cubes(mask, level=0.5)
    with pytest.raises(PipelineError) as exc:
        clip_mesh_bilateral_local(
            vertices, faces, mask, volume,
            left_inlet_plane=left, right_inlet_plane=right, posterior_plane=outlet,
            left_inlet_seed_lps_mm=[20.0, 5.0, 6.0],
            right_inlet_seed_lps_mm=seeds[1], posterior_seed_lps_mm=seeds[2],
        )
    assert exc.value.status == "blocked"
    assert exc.value.detail["code"] == "multiple_substantial_mesh_sheets"
    assert exc.value.detail["selected_mesh_components"] == 2
    assert len(exc.value.detail["matched_mesh_sheets"]) == 2


@pytest.mark.parametrize("right_plane_y", [9.0, 10.0])
def test_two_branches_on_one_side_cannot_impersonate_bilateral_inlets(right_plane_y):
    """A real third anterior branch outside the chosen pair must block CFD."""
    mask = np.zeros((40, 38, 16), dtype=np.uint8)
    mask[4:9, 3:17, 5:12] = 1  # unselected true contralateral branch
    mask[17:22, 3:17, 5:12] = 1
    mask[27:32, 3:17, 5:12] = 1
    mask[4:32, 17:22, 5:12] = 1
    mask[12:25, 22:35, 5:12] = 1
    volume = Volume(np.zeros(mask.shape, dtype=np.float32), (1., 1., 1.),
                    (0., 0., 0.), tuple(map(tuple, np.eye(3))))
    left = {"point_lps_mm": [29., 9., 8.], "normal_lps": [0., -1., 0.]}
    right = {"point_lps_mm": [19., right_plane_y, 8.], "normal_lps": [0., -1., 0.]}
    outlet = {"point_lps_mm": [18., 29., 8.], "normal_lps": [0., 1., 0.]}
    seeds = ([29., 5., 8.], [19., 5., 8.], [18., 32., 8.])
    with pytest.raises(PipelineError, match="未覆盖外侧") as exc:
        oblique.validate_bilateral_local_planes(
            mask, volume, left, right, outlet, *seeds, main_airway=True,
        )
    assert exc.value.status == "blocked"
    assert exc.value.detail["unselected_outer_branches"] >= 1
    from skimage import measure
    vertices, faces, _, _ = measure.marching_cubes(mask, level=0.5)
    with pytest.raises(PipelineError, match="未覆盖外侧"):
        clip_mesh_bilateral_local(
            vertices, faces, mask, volume,
            left_inlet_plane=left, right_inlet_plane=right, posterior_plane=outlet,
            left_inlet_seed_lps_mm=seeds[0], right_inlet_seed_lps_mm=seeds[1],
            posterior_seed_lps_mm=seeds[2],
        )


@pytest.mark.parametrize("tiny_side", ["left", "right"])
def test_unmatched_inlet_reports_side_and_selected_voxels(bilateral_tube, tiny_side):
    """A tiny connected spur cannot impersonate the substantial inlet."""
    mask, volume, left, right, outlet, seeds = bilateral_tube
    mask = mask.copy()
    volume = Volume(
        volume.array, (0.5, 0.5, 0.5), volume.origin, volume.axis_dirs,
    )
    left = {**left, "point_lps_mm": [x * 0.5 for x in left["point_lps_mm"]]}
    right = {**right, "point_lps_mm": [x * 0.5 for x in right["point_lps_mm"]]}
    outlet = {**outlet, "point_lps_mm": [x * 0.5 for x in outlet["point_lps_mm"]]}
    seeds = [[x * 0.5 for x in seed] for seed in seeds]
    if tiny_side == "left":
        mask[26, 8:10, 8] = 1
        mask[23:27, 9, 8] = 1
        seeds[0] = [13., 4., 4.]
    else:
        mask[1, 8:10, 8] = 1
        mask[1:5, 9, 8] = 1
        seeds[1] = [0.5, 4., 4.]
    with pytest.raises(PipelineError, match="未覆盖当前切面的显著前端支段") as exc:
        oblique.validate_bilateral_local_planes(
            mask, volume, left, right, outlet, *seeds, main_airway=True,
        )
    assert exc.value.status == "blocked"
    assert exc.value.detail["unmatched_port"] == tiny_side
    assert exc.value.detail["selected_port_voxels"] == 1
    assert exc.value.detail["unassigned_branches"] >= 1
    assert ("左鼻孔" if tiny_side == "left" else "右鼻孔") in exc.value.reason
    assert "重新选定位点" in exc.value.remediation[0]


@pytest.mark.parametrize("left_tilt", [0.0, 0.2])
def test_bilateral_local_preserves_unselected_branch_crossed_by_inlet_planes(
    bilateral_tube, left_tilt,
):
    """A plane may cross a third branch without authorizing its removal."""
    from skimage import measure

    mask, volume, left, right, outlet, seeds = bilateral_tube
    mask = mask.copy()
    # This extra branch joins the same posterior airway but is not selected.
    mask[12:15, 3:18, 5:12] = 1
    normal = np.asarray([left_tilt, -1.0, 0.0])
    left = {**left, "normal_lps": (normal / np.linalg.norm(normal)).tolist()}
    retained = oblique.clip_mask_oblique_bilateral_local(
        mask, volume, left, right, outlet,
        left_inlet_seed_lps_mm=seeds[0],
        right_inlet_seed_lps_mm=seeds[1],
        posterior_seed_lps_mm=seeds[2],
    )
    assert np.array_equal(retained[12:15, 3:18, 5:12], mask[12:15, 3:18, 5:12])
    assert not retained[20, 5, 8] and not retained[7, 5, 8]

    vertices, faces, _, _ = measure.marching_cubes(mask, level=0.5)
    points, triangles, left_cap, right_cap, out_cap, stats = clip_mesh_bilateral_local(
        vertices, faces, mask, volume,
        left_inlet_plane=left, right_inlet_plane=right, posterior_plane=outlet,
        left_inlet_seed_lps_mm=seeds[0], right_inlet_seed_lps_mm=seeds[1],
        posterior_seed_lps_mm=seeds[2],
    )
    # Both infinite inlet half-spaces would delete this distal surface.
    untouched = vertices[
        (vertices[:, 0] >= 11.5) & (vertices[:, 0] <= 14.5) &
        (vertices[:, 1] < 7.0)
    ]
    actual_vertices = {tuple(point) for point in np.round(points, 6)}
    assert len(untouched) > 0
    assert all(tuple(point) in actual_vertices for point in np.round(untouched, 6))
    assert stats["closed_caps"] is True
    assert all(len(cap) > 0 for cap in (left_cap, right_cap, out_cap))
    counts = Counter(
        tuple(sorted((int(a), int(b))))
        for face in triangles
        for a, b in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0]))
    )
    assert set(counts.values()) == {2}


def test_bilateral_local_rejects_swapped_sides_and_overlapping_branches(bilateral_tube):
    mask, volume, left, right, outlet, seeds = bilateral_tube
    with pytest.raises(PipelineError, match="左右鼻孔"):
        oblique.validate_bilateral_local_planes(
            mask, volume, left, right, outlet, seeds[1], seeds[0], seeds[2],
            main_airway=True,
        )
    with pytest.raises(PipelineError, match="重叠|锚点|管段"):
        oblique.validate_bilateral_local_planes(
            mask, volume, left, left, outlet, seeds[0], seeds[0], seeds[2],
            main_airway=True,
        )


def test_bilateral_local_rejects_non_main_scope(bilateral_tube):
    mask, volume, left, right, outlet, seeds = bilateral_tube
    with pytest.raises(PipelineError, match="主鼻腔"):
        oblique.validate_bilateral_local_planes(
            mask, volume, left, right, outlet, *seeds, main_airway=False,
        )


def test_bilateral_local_save_surface_and_qc_keep_three_independent_caps(
    bilateral_tube, tmp_path, monkeypatch,
):
    mask, volume, left, right, outlet, seeds = bilateral_tube
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = cases.create_case("local_path", "<双侧三端面合成测试>", synthetic=False)
    root = cases.case_dir(case_id)
    assert formal_ports.get_formal_port_state(case_id)["status"] == "not_generated"
    save_volume(case_id, volume)
    np.save(root / "segment" / "mask.npy", mask)
    (root / "segment" / "segmentation.json").write_text(json.dumps({
        "params": {"ap_axis": 1, "hu_high": -300.0},
        "mask_version": 1, "need_human_confirm": False,
        "review_flags": {"external_air_suspected": False},
        "external_air_validation": {
            "mask_version": 1, "body_hu_threshold": -300.0,
            "outside_voxels": 0, "candidate_outside_voxels": 0,
        },
    }), encoding="utf-8")
    proposal = endplanes.preview_bilateral_local_endplanes(
        case_id, "main_airway", left, right, outlet, *seeds, 1,
    )
    assert proposal["can_save"] is False
    assert proposal["retained_components"] == 1
    plan = endplanes.save_bilateral_local_endplanes(
        case_id, "main_airway", left, right, outlet, *seeds, 1,
    )
    assert plan["format"] == endplanes.BILATERAL_LOCAL_FORMAT
    assert plan["mesh_preflight"]["closed_caps"] is True
    assert formal_ports.get_formal_port_state(case_id)["status"] == "not_generated"
    result = surface.run_surface(case_id)
    assert result["endplanes"]["source"] == "user_confirmed_bilateral_local_oblique_planes"
    assert result["oblique_clip"]["left_inlet_cap_faces"] > 0
    assert result["oblique_clip"]["right_inlet_cap_faces"] > 0
    assert not (root / "surface" / "inlet_triangles.npy").exists()
    for name in ("inlet_left_triangles.npy", "inlet_right_triangles.npy", "outlet_triangles.npy"):
        assert len(np.load(root / "surface" / name)) > 0
    assert formal_ports.get_formal_port_state(case_id)["status"] == "not_generated"
    qc = geom_qc.run_geom_qc(case_id)
    assert qc["verdict"] == "pass", qc["reasons"]
    assert qc["openings_by_end"] == {"anterior": 2, "posterior": 1, "middle": 0}
    assert {item["side"] for item in qc["openings"]} == {"left", "right", "outlet"}
    assert qc["bilateral_local_caps"]["components_by_side"] == {
        "left": 1, "right": 1, "outlet": 1,
    }
    manifest = formal_ports.get_formal_port_state(case_id)
    assert manifest["status"] == "ready"
    assert manifest["context_source"] == "cleaned_confirmed_mask"
    assert {port["side"] for port in manifest["ports"]} == {"left", "right", "posterior"}
    assert len({port["port_id"] for port in manifest["ports"]}) == 3
    for port in manifest["ports"]:
        filename = {"left": "inlet_left_triangles.npy", "right": "inlet_right_triangles.npy",
                    "posterior": "outlet_triangles.npy"}[port["side"]]
        assert port["cap_face_ids"] == np.load(root / "surface" / filename).tolist()
        assert port["provenance"]["mesh_hash"] == manifest["mesh_hash"]
        assert len(port["boundary_loop_lps_mm"]) >= 3
    assert formal_ports.get_formal_port_state(case_id) == manifest
    seg_path = root / "segment" / "segmentation.json"
    seg_data = json.loads(seg_path.read_text(encoding="utf-8"))
    seg_path.write_text(json.dumps({**seg_data, "mask_version": 2}), encoding="utf-8")
    with pytest.raises(PipelineError, match="掩膜版本已过期"):
        formal_ports.get_formal_port_state(case_id)
    seg_path.write_text(json.dumps(seg_data), encoding="utf-8")
    stl_path = root / "surface" / "airway.stl"
    original_stl = stl_path.read_bytes()
    changed_stl = bytearray(original_stl)
    changed_stl[100] ^= 1
    stl_path.write_bytes(changed_stl)
    with pytest.raises(PipelineError, match="STL 与 CFD 网格坐标不一致"):
        formal_ports.get_formal_port_state(case_id)
    stl_path.write_bytes(original_stl)
    with pytest.raises(PipelineError, match="几何质检早于当前正式网格"):
        formal_ports.get_formal_port_state(case_id)
    (root / "surface" / "inlet_right_triangles.npy").unlink()
    with pytest.raises(PipelineError, match="封盖文件缺失"):
        geom_qc.check_mesh(str(root / "surface"))
    with pytest.raises(PipelineError, match="封盖面索引缺失"):
        formal_ports.get_formal_port_state(case_id)
    plans_path = root / "segment" / "endplanes.json"
    plans = json.loads(plans_path.read_text(encoding="utf-8"))
    plans["main_airway"]["format"] = endplanes.LOCAL_INLET_FORMAT
    plans_path.write_text(json.dumps(plans), encoding="utf-8")
    with pytest.raises(PipelineError, match="旧版或单支段端面"):
        formal_ports.get_formal_port_state(case_id)
