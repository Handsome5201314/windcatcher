from __future__ import annotations

from collections import Counter

import numpy as np
from skimage import measure

from pipeline.surface import clip_mesh_by_oblique_planes, mask_to_stl, prepare_volume, select_largest_component


def _boundary_edge_count(faces: np.ndarray) -> int:
    edges: Counter[tuple[int, int]] = Counter()
    for face in faces:
        for first, second in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0])):
            edges[tuple(sorted((int(first), int(second))))] += 1
    return sum(count == 1 for count in edges.values())


def test_prepare_volume_keeps_main_airway_open_when_loose_region_extends_bbox():
    mask = np.zeros((12, 30, 20), dtype=np.uint8)
    # 主气道沿前后轴（axis 1）延伸；孤立区域把候选整体范围拉到更前端。
    mask[4:8, 8:21, 8:12] = 1
    mask[6, 2:4, 2:4] = 1

    prepared, offsets, stats = prepare_volume(mask, ap_axis=1)

    assert offsets[1] == 7
    assert stats["dropped_components"] == 1
    assert stats["voxels_dropped"] == 4
    assert stats["padding_mode"] == "legacy_ap_extension"
    assert prepared[:, 0, :].sum() > 0
    assert prepared[:, -1, :].sum() > 0

    _, faces, _, _ = measure.marching_cubes(prepared, level=0.5)
    assert _boundary_edge_count(faces) > 0


def test_prepare_volume_closed_padding_keeps_extreme_protrusion_sealed():
    mask = np.zeros((16, 20, 14), dtype=np.uint8)
    mask[4:14, 5:15, 4:9] = 1
    # Two connected voxels at the A-P minimum become an unwanted six-edge
    # opening when the old axial extension is applied before an oblique cut.
    mask[5, 2:5, 5:7] = 1

    prepared, _offsets, stats = prepare_volume(mask, ap_axis=1, extend_ap_edges=False)

    assert not np.any(prepared[:, 0, :])
    assert not np.any(prepared[:, -1, :])
    _, faces, _, _ = measure.marching_cubes(prepared, level=0.5)
    assert _boundary_edge_count(faces) == 0
    assert stats["padding_mode"] == "closed_zero_padding"


def test_select_largest_component_matches_surface_voxel_source():
    mask = np.zeros((5, 6, 7), dtype=np.uint8)
    mask[1:4, 1:5, 2:5] = 1
    mask[0, 0, 0] = 1

    selected, stats = select_largest_component(mask)

    assert selected.sum() == 36
    assert stats == {
        "source_voxels": 37,
        "voxels_in": 36,
        "voxels_dropped": 1,
        "components": 2,
        "dropped_components": 1,
    }


def test_prepare_volume_full_candidate_preserves_disconnected_components():
    mask = np.zeros((12, 30, 20), dtype=np.uint8)
    mask[4:8, 8:21, 8:12] = 1
    # A separate threshold candidate inside the same reviewed A-P extent.
    mask[5:7, 12:14, 2:4] = 1

    prepared, _offsets, stats = prepare_volume(
        mask, ap_axis=1, keep_largest_component=False
    )

    assert stats["components"] == 2
    assert stats["voxels_in"] == int(mask.sum())
    assert stats["voxels_dropped"] == 0
    assert stats["dropped_components"] == 0
    assert prepared[:, 0, :].sum() > 0
    assert prepared[:, -1, :].sum() > 0


def test_mask_to_stl_full_candidate_does_not_delete_surface_fragments():
    mask = np.zeros((12, 30, 20), dtype=np.uint8)
    mask[4:8, 8:21, 8:12] = 1
    mask[5:7, 12:14, 2:4] = 1

    result = mask_to_stl(
        mask,
        spacing=(1.0, 1.0, 1.0),
        origin=(0.0, 0.0, 0.0),
        axis_dirs=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        ap_axis=1,
        params={"model_scope": "full_candidate"},
    )

    assert result["params"]["model_scope"] == "full_candidate"
    assert result["params"]["keep_largest_component"] is False
    assert result["params"]["extend_ap_edges"] is True
    assert result["manifold_prep"]["components"] == 2
    assert result["manifold_prep"]["dropped_components"] == 0
    assert result["loose_cleanup"]["removed_components"] == 0


def test_oblique_mesh_clip_is_watertight_and_records_caps():
    volume = np.zeros((10, 40, 10), dtype=np.uint8)
    volume[1:-1, 2:35, 1:-1] = 1
    vertices, faces, _, _ = measure.marching_cubes(volume, level=0.5)
    anterior = {"point_lps_mm": [0.0, 2.0, 0.0], "normal_lps": [0.0, -1.0, 0.0]}
    posterior = {"point_lps_mm": [0.0, 34.0, 0.0], "normal_lps": [0.0, 1.0, 0.0]}

    clipped_vertices, clipped_faces, inlet, outlet, stats = clip_mesh_by_oblique_planes(
        vertices, faces, anterior, posterior
    )

    assert stats["format"] == "oblique_mesh_clip_v1"
    assert len(inlet) > 0 and len(outlet) > 0
    assert int(inlet.max()) < len(clipped_faces)
    assert int(outlet.max()) < len(clipped_faces)
    assert stats["inlet_area_mm2"] > 1.0
    assert stats["outlet_area_mm2"] > 1.0
    assert stats["closed_caps"] is True
    edges: Counter[tuple[int, int]] = Counter()
    for face in clipped_faces:
        for first, second in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0])):
            edges[tuple(sorted((int(first), int(second))))] += 1
    assert _boundary_edge_count(clipped_faces) == 0
    assert all(count == 2 for count in edges.values())
    assert np.all(np.isfinite(clipped_vertices))
