from __future__ import annotations

from collections import Counter

import numpy as np
import pytest
from skimage import measure

from pipeline.errors import PipelineError
from pipeline.surface import (
    MAX_LOCAL_INLET_FACES, clip_mesh_by_oblique_planes, ensure_local_mesh_face_capacity,
    local_geometry_guard, mask_to_stl, prepare_volume, select_largest_component,
)


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


def test_local_geometry_capacity_and_nonblocking_guard_release_after_failure():
    ensure_local_mesh_face_capacity(MAX_LOCAL_INLET_FACES)
    with pytest.raises(PipelineError, match="网格过大"):
        ensure_local_mesh_face_capacity(MAX_LOCAL_INLET_FACES + 1)
    with pytest.raises(RuntimeError):
        with local_geometry_guard():
            with pytest.raises(PipelineError, match="正在运行"):
                with local_geometry_guard():
                    pass
            raise RuntimeError("synthetic failure")
    with local_geometry_guard():
        pass

def test_opposite_coincident_face_pairs_are_removed_without_moving_geometry():
    from pipeline.surface import remove_opposite_coincident_faces
    vertices = np.asarray([[0.,0.,0.],[1.,0.,0.],[0.,1.,0.],[0.,0.,1.],[0.2,0.2,0.2]])
    shell = np.asarray([[0,2,1],[0,1,3],[1,2,3],[2,0,3]], dtype=np.int32)
    # A zero-thickness interior sheet shares a wall edge, creating four owners.
    damaged = np.vstack([shell, [[0,1,4],[4,1,0]]])
    original_vertices = vertices.copy()
    cleaned, stats = remove_opposite_coincident_faces(damaged)
    assert stats["removed_faces"] == 2
    assert np.array_equal(cleaned, shell)
    assert np.array_equal(vertices, original_vertices)
    signed_volume = lambda faces: np.einsum("ij,ij->i", vertices[faces[:,0]], np.cross(vertices[faces[:,1]],vertices[faces[:,2]])).sum()/6
    assert signed_volume(cleaned) == pytest.approx(signed_volume(damaged))
    edges = Counter(tuple(sorted((int(a),int(b)))) for tri in cleaned for a,b in zip(tri,np.roll(tri,-1)))
    assert set(edges.values()) == {2}

def test_coincident_cleanup_does_not_remove_same_orientation_or_distinct_thin_surfaces():
    from pipeline.surface import remove_opposite_coincident_faces
    faces = np.asarray([[0,1,2],[1,2,0],[3,5,4]],dtype=np.int32)
    cleaned, stats = remove_opposite_coincident_faces(faces)
    assert np.array_equal(cleaned, faces)
    assert stats["removed_faces"] == 0
