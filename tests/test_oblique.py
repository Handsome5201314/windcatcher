"""任意旋转双平面的合成管道回归验证。"""

from __future__ import annotations

import json

import numpy as np
import pytest

from pipeline import cases, endplanes, oblique, surface
from pipeline.errors import PipelineError
from pipeline.volume import Volume, save_volume


@pytest.fixture
def tube():
    shape = (24, 40, 24)
    mask = np.zeros(shape, dtype=np.uint8)
    mask[7:17, 5:35, 7:17] = 1
    volume = Volume(
        array=np.zeros(shape, dtype=np.float32),
        spacing=(0.8, 0.8, 0.8),
        origin=(0.0, 0.0, 0.0),
        axis_dirs=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    )
    anterior = {"point_lps_mm": [9.6, 8.0, 9.6], "normal_lps": [0.20, -0.98, 0.08]}
    posterior = {"point_lps_mm": [9.6, 24.0, 9.6], "normal_lps": [-0.13, 0.99, 0.04]}
    return mask, volume, anterior, posterior


def test_oblique_pair_retains_tube_and_reports_preflight(tube):
    mask, volume, anterior, posterior = tube

    checked = oblique.validate_plane_pair(mask, volume, anterior, posterior, main_airway=True)
    retained = oblique.clip_mask_oblique(mask, volume, checked["anterior_plane"], checked["posterior_plane"])

    assert checked["retained_voxels"] == int(retained.sum())
    assert checked["retained_components"] == 1
    assert checked["anterior_area_estimate_mm2"] > 1.0
    assert checked["posterior_area_estimate_mm2"] > 1.0
    assert np.isclose(np.linalg.norm(checked["anterior_plane"]["normal_lps"]), 1.0)
    assert retained.sum() < mask.sum()
    assert checked["retained_volume_ml"] == round(float(retained.sum()) * 0.8**3 / 1000.0, 3)


def test_oblique_pair_rejects_reversed_cut_normals(tube):
    mask, volume, anterior, posterior = tube
    anterior["normal_lps"] = [0.0, 1.0, 0.0]

    with pytest.raises(PipelineError, match="法向"):
        oblique.validate_plane_pair(mask, volume, anterior, posterior, main_airway=True)


@pytest.mark.parametrize("main_airway", [True, False])
def test_oblique_pair_rejects_planes_crossing_inside_airway(tube, main_airway):
    mask, volume, _, _ = tube
    # Both normals point to their respective anatomical outside and the AP
    # ordering is valid, but the two tilted planes meet inside the tube.
    anterior = {"point_lps_mm": [9.6, 12.0, 9.6], "normal_lps": [0.4, -0.8, 0.4]}
    posterior = {"point_lps_mm": [9.6, 15.2, 9.6], "normal_lps": [-0.4, 0.8, 0.4]}

    with pytest.raises(PipelineError, match="相交|过近"):
        oblique.validate_plane_pair(mask, volume, anterior, posterior, main_airway=main_airway)


def test_oblique_pair_rejects_parallel_planes_with_unresolved_gap(tube):
    mask, volume, _, _ = tube
    anterior = {"point_lps_mm": [9.6, 12.0, 9.6], "normal_lps": [0.0, -1.0, 0.0]}
    posterior = {"point_lps_mm": [9.6, 12.8, 9.6], "normal_lps": [0.0, 1.0, 0.0]}

    with pytest.raises(PipelineError, match="间距过小"):
        oblique.validate_plane_pair(mask, volume, anterior, posterior, main_airway=True)


def test_oblique_surface_only_opens_at_user_planes_with_extreme_protrusion(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = cases.create_case("local_path", "<合成 CT 测试>", synthetic=False)
    root = cases.case_dir(case_id)
    mask = np.zeros((16, 20, 14), dtype=np.uint8)
    mask[4:14, 5:15, 4:9] = 1
    mask[5, 2:5, 5:7] = 1
    save_volume(case_id, Volume(
        array=np.zeros(mask.shape, dtype=np.float32),
        spacing=(1.0, 1.0, 1.0), origin=(0.0, 0.0, 0.0),
        axis_dirs=((0.0, 0.0, 1.0), (0.0, 1.0, 0.0), (1.0, 0.0, 0.0)),
        meta={"shape": list(mask.shape)},
    ))
    np.save(root / "segment" / "mask.npy", mask)
    record = {
        "params": {"ap_axis": 1, "hu_high": -300.0},
        "mask_version": 1, "need_human_confirm": False,
        "review_flags": {"external_air_suspected": False},
        "external_air_validation": {
            "mask_version": 1, "body_hu_threshold": -300.0,
            "outside_voxels": 0, "candidate_outside_voxels": 0,
        },
    }
    (root / "segment" / "segmentation.json").write_text(json.dumps(record), encoding="utf-8")
    anterior = {"point_lps_mm": [6.0, 5.0, 9.5], "normal_lps": [0.0, -1.0, 1.0]}
    posterior = {"point_lps_mm": [6.0, 12.0, 8.0], "normal_lps": [0.0, 1.0, 0.0]}
    endplanes.save_oblique_endplanes(case_id, "main_airway", anterior, posterior, 1)

    result = surface.run_surface(case_id)

    faces = np.load(root / "surface" / "faces.npy")
    edges = np.sort(np.concatenate((faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]])), axis=1)
    _, counts = np.unique(edges, axis=0, return_counts=True)
    assert np.count_nonzero(counts == 1) == 0
    assert np.all(counts == 2)
    assert result["params"]["extend_ap_edges"] is False
    assert result["manifold_prep"]["padding_mode"] == "closed_zero_padding"
    assert result["endplanes"]["source"] == "user_confirmed_oblique_planes"
    assert result["oblique_clip"]["inlet_cap_faces"] > 0
    assert result["oblique_clip"]["outlet_cap_faces"] > 0


@pytest.fixture
def two_nostrils_with_shared_posterior_airway():
    """Two anterior branches meet behind a steeply tilted inlet plane."""
    shape = (28, 38, 16)
    mask = np.zeros(shape, dtype=np.uint8)
    mask[4:11, 3:17, 5:12] = 1
    mask[17:24, 3:17, 5:12] = 1
    mask[4:24, 17:22, 5:12] = 1
    mask[9:19, 22:35, 5:12] = 1
    volume = Volume(
        array=np.zeros(shape, dtype=np.float32),
        spacing=(1.0, 1.0, 1.0), origin=(0.0, 0.0, 0.0),
        axis_dirs=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    )
    anterior = {"point_lps_mm": [7.0, 9.0, 8.0], "normal_lps": [0.45, -0.893, 0.0]}
    posterior = {"point_lps_mm": [14.0, 29.0, 8.0], "normal_lps": [0.0, 1.0, 0.0]}
    seed = [7.0, 5.0, 8.0]
    return mask, volume, anterior, posterior, seed


def test_local_mesh_fragment_vote_only_blocks_selected_branch():
    def selected(votes):
        return surface._is_selected_local_mesh_group(
            votes, 8, group_index=2, group_size=133,
            sampled_fragments=32, voxel_component_count=49,
        )

    # A detached small fragment can have several nearby voxel labels.  It
    # must remain untouched if none is the nostril selected by the seed.
    assert selected({41: 2, 44: 6, 45: 3, 47: 3}) is False
    assert selected({}) is False
    assert selected({8: 29, 9: 3}) is True
    with pytest.raises(PipelineError, match="跨越多个气道区域"):
        selected({8: 8, 9: 7})
    with pytest.raises(PipelineError, match="跨越多个气道区域"):
        selected({8: 1, 9: 20})


def test_local_inlet_mask_removes_only_selected_nostril(two_nostrils_with_shared_posterior_airway):
    mask, volume, anterior, posterior, seed = two_nostrils_with_shared_posterior_airway
    globally_clipped = oblique.clip_mask_oblique(mask, volume, anterior, posterior)
    assert globally_clipped[20, 5, 8] == 0  # Reproduces the present bug.
    checked = oblique.validate_local_inlet_pair(
        mask, volume, anterior, posterior, seed, main_airway=True
    )
    assert checked["anterior_opening_components"] == 1
    assert checked["anterior_deleted_voxels"] > 0
    assert checked["anterior_unselected_positive_voxels"] > 0
    assert checked["unselected_region_changed_voxels"] == 0
    assert checked["anterior_seed_lps_mm"] == seed

    locally_clipped = oblique.clip_mask_oblique_local(
        mask, volume, anterior, posterior, anterior_seed_lps_mm=seed
    )
    assert locally_clipped[7, 5, 8] == 0
    assert locally_clipped[20, 5, 8] == 1
    assert locally_clipped[14, 30, 8] == 0
    assert locally_clipped[14, 25, 8] == 1


def test_local_inlet_mesh_keeps_other_nostril_and_has_one_watertight_cap(
    two_nostrils_with_shared_posterior_airway,
):
    from skimage import measure

    mask, volume, anterior, posterior, seed = two_nostrils_with_shared_posterior_airway
    vertices, faces, _, _ = measure.marching_cubes(mask, level=0.5)
    cut_vertices, cut_faces, inlet, outlet, stats = surface.clip_mesh_by_oblique_planes_local(
        vertices, faces, anterior, posterior, mask, volume, anterior_seed_lps_mm=seed
    )
    edges = np.sort(
        np.concatenate((cut_faces[:, [0, 1]], cut_faces[:, [1, 2]], cut_faces[:, [2, 0]])), axis=1
    )
    _, counts = np.unique(edges, axis=0, return_counts=True)
    assert np.all(counts == 2)
    assert np.any((cut_vertices[:, 0] > 17) & (cut_vertices[:, 1] < 6))
    assert not np.any((cut_vertices[:, 0] < 11) & (cut_vertices[:, 1] < 6))
    assert len(inlet) > 0 and len(outlet) > 0
    assert stats["inlet_loops"] == 1
    assert stats["inlet_area_mm2"] > 1
    assert int(inlet.max()) < len(cut_faces)
    assert int(outlet.max()) < len(cut_faces)
    inlet_points = cut_vertices[cut_faces[inlet]]
    inlet_normals = np.cross(
        inlet_points[:, 1] - inlet_points[:, 0], inlet_points[:, 2] - inlet_points[:, 0]
    )
    assert np.all(inlet_normals @ np.asarray(anterior["normal_lps"]) > 0)


def test_local_inlet_rejects_connected_delete_side_with_two_cap_loops(
    two_nostrils_with_shared_posterior_airway,
):
    from skimage import measure

    mask, volume, anterior, posterior, seed = two_nostrils_with_shared_posterior_airway
    mask[10:17, 5:7, 7:10] = 1  # Joins both nostrils before either plane crossing.
    vertices, faces, _, _ = measure.marching_cubes(mask, level=0.5)
    with pytest.raises(PipelineError, match="多|连通|入口"):
        surface.clip_mesh_by_oblique_planes_local(
            vertices, faces, anterior, posterior, mask, volume, anterior_seed_lps_mm=seed
        )


def test_local_inlet_uses_patient_lps_with_rotated_anisotropic_voxels(
    two_nostrils_with_shared_posterior_airway,
):
    from skimage import measure

    mask, _unit_volume, anterior, posterior, seed = two_nostrils_with_shared_posterior_airway
    volume = Volume(
        array=np.zeros(mask.shape, dtype=np.float32),
        spacing=(0.8, 1.2, 0.6), origin=(10.0, -20.0, 30.0),
        axis_dirs=((0.0, 0.0, 1.0), (0.0, 1.0, 0.0), (1.0, 0.0, 0.0)),
    )
    axes = np.asarray(volume.axis_dirs) * np.asarray(volume.spacing)[:, None]
    origin = np.asarray(volume.origin)

    def in_lps(plane):
        return {
            "point_lps_mm": (origin + np.asarray(plane["point_lps_mm"]) @ axes).tolist(),
            "normal_lps": np.linalg.solve(axes, np.asarray(plane["normal_lps"])).tolist(),
        }

    anterior_lps, posterior_lps = in_lps(anterior), in_lps(posterior)
    seed_lps = (origin + np.asarray(seed) @ axes).tolist()
    clipped = oblique.clip_mask_oblique_local(
        mask, volume, anterior_lps, posterior_lps, anterior_seed_lps_mm=seed_lps
    )
    assert clipped[7, 5, 8] == 0 and clipped[20, 5, 8] == 1
    vertices_ijk, faces, _, _ = measure.marching_cubes(mask, level=0.5)
    vertices_lps = surface.voxel_to_mm(
        vertices_ijk, volume.spacing, volume.origin, volume.axis_dirs
    )
    output_lps, output_faces, inlet, _outlet, _stats = surface.clip_mesh_by_oblique_planes_local(
        vertices_lps, faces, anterior_lps, posterior_lps, mask, volume,
        anterior_seed_lps_mm=seed_lps,
    )
    output_ijk = (output_lps - origin) @ np.linalg.inv(axes)
    assert np.any((output_ijk[:, 0] > 17) & (output_ijk[:, 1] < 6))
    assert not np.any((output_ijk[:, 0] < 11) & (output_ijk[:, 1] < 6))
    assert len(inlet) > 0 and int(inlet.max()) < len(output_faces)


def test_local_inlet_rejects_ambiguous_or_remote_seed(two_nostrils_with_shared_posterior_airway):
    mask, volume, anterior, posterior, _seed = two_nostrils_with_shared_posterior_airway
    with pytest.raises(PipelineError, match="锚点"):
        oblique.clip_mask_oblique_local(
            mask, volume, anterior, posterior, anterior_seed_lps_mm=[14.0, 5.0, 8.0]
        )


def test_local_inlet_rejects_plane_behind_merged_nostrils(
    two_nostrils_with_shared_posterior_airway,
):
    mask, volume, _anterior, posterior, seed = two_nostrils_with_shared_posterior_airway
    anterior = {"point_lps_mm": [14.0, 25.0, 8.0], "normal_lps": [0.0, -1.0, 0.0]}
    with pytest.raises(PipelineError, match="跨支段|多个鼻孔|多入口"):
        oblique.clip_mask_oblique_local(
            mask, volume, anterior, posterior, anterior_seed_lps_mm=seed
        )


def test_run_surface_local_inlet_keeps_unselected_anterior_airway(
    tmp_path, monkeypatch, two_nostrils_with_shared_posterior_airway,
):
    mask, volume, anterior, posterior, seed = two_nostrils_with_shared_posterior_airway
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = cases.create_case("local_path", "<双鼻孔合成测试>", synthetic=False)
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
    plan = {
        "format": "oblique_local_inlet_v2", "model_scope": "main_airway",
        "anterior_plane": anterior, "posterior_plane": posterior,
        "anterior_seed_lps_mm": seed,
    }
    monkeypatch.setattr(endplanes, "current_plan", lambda *_args: plan)

    result = surface.run_surface(case_id)

    effective = np.load(root / "surface" / "effective_mask.npy")
    vertices = np.load(root / "surface" / "vertices.npy")
    faces = np.load(root / "surface" / "faces.npy")
    assert effective[7, 5, 8] == 0
    assert effective[20, 5, 8] == 1
    assert np.any((vertices[:, 0] > 17) & (vertices[:, 1] < 6))
    assert result["endplanes"]["source"] == "user_confirmed_local_oblique_planes"
    assert result["oblique_clip"]["inlet_loops"] == 1
    assert result["oblique_clip"]["closed_caps"] is True
    edges = np.sort(np.concatenate((faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]])), axis=1)
    _, counts = np.unique(edges, axis=0, return_counts=True)
    assert np.all(counts == 2)
