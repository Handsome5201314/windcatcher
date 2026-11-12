from __future__ import annotations

import numpy as np
import pytest

from pipeline.endplane_section import render_section
from pipeline.preview import _to_grayscale, WINDOW_LEVEL, WINDOW_WIDTH
from pipeline.volume import Volume
from pipeline.errors import PipelineError


@pytest.mark.parametrize("rotate", [False, True])
def test_reslice_uses_lps_origin_axis_directions_and_anisotropic_spacing(rotate):
    grid = np.indices((11, 13, 15))
    hu = (-900 + grid[0] * 30 + grid[1] * 5 + grid[2]).astype(np.float32)
    directions = np.eye(3)[[2, 1, 0]]
    if rotate:
        theta = np.pi / 5
        rotation = np.array([[np.cos(theta), -np.sin(theta), 0], [np.sin(theta), np.cos(theta), 0], [0, 0, 1]])
        directions = directions @ rotation.T
    volume = Volume(hu, (2., 3., 4.), (10., 20., 30.), tuple(map(tuple, directions)))
    center_index = np.array([5., 6., 7.])
    point = np.array(volume.origin) + center_index @ (directions * np.array(volume.spacing)[:, None])
    image = np.asarray(render_section(volume, np.zeros_like(hu, dtype=np.uint8), {
        "point_lps_mm": point.tolist(), "normal_lps": (-directions[1]).tolist(),
    }, span_mm=44., size=11))
    gray = lambda value: int(_to_grayscale(np.array([[value]], dtype=np.float32), WINDOW_LEVEL, WINDOW_WIDTH)[0, 0])
    assert image[5, 5].tolist() == [gray(hu[5, 6, 7])] * 3
    assert image[4, 5].tolist() == [gray(hu[7, 6, 7])] * 3  # up = patient superior, 4 mm = 2 voxels
    assert image[5, 6].tolist() == [gray(hu[5, 6, 8])] * 3  # right = 4 mm = 1 voxel


def test_reslice_draws_mask_boundary_and_pads_outside_ct_without_mutating_inputs():
    hu = np.full((11, 11, 11), -500., dtype=np.float32)
    mask = np.zeros_like(hu, dtype=np.uint8)
    mask[4:7, 4:7, 4:7] = 1
    volume = Volume(hu, (1., 1., 1.), (0., 0., 0.), tuple(map(tuple, np.eye(3))))
    before = mask.copy()
    image = np.asarray(render_section(volume, mask, {"point_lps_mm": [5, 5, 5], "normal_lps": [0, 0, 1]}, 11., 11))
    assert np.any(np.all(image == [104, 200, 186], axis=-1))
    assert not np.all(image[5, 5] == [104, 200, 186])  # interior remains CT, not a filled fake opening
    np.testing.assert_array_equal(mask, before)
    assert np.all(hu == -500.)
    outside = np.asarray(render_section(volume, mask, {"point_lps_mm": [50, 50, 50], "normal_lps": [0, 0, 1]}, 11., 11))
    assert np.all(outside == outside[0, 0])


def test_tilted_plane_interpolates_ct_instead_of_showing_a_nearby_orthogonal_slice():
    grid = np.indices((11, 11, 11))
    hu = (-900 + grid[0] * 30 + grid[1] * 10 + grid[2]).astype(np.float32)
    volume = Volume(hu, (1., 1., 1.), (0., 0., 0.), tuple(map(tuple, np.eye(3))))
    image = np.asarray(render_section(volume, np.zeros_like(hu, dtype=np.uint8), {
        "point_lps_mm": [5, 5, 5], "normal_lps": [0, -1, 1],
    }, 11., 11))
    expected_hu = hu[5, 5, 5] + 11 / np.sqrt(2)
    expected = int(_to_grayscale(np.array([[expected_hu]]), WINDOW_LEVEL, WINDOW_WIDTH)[0, 0])
    assert image[4, 5].tolist() == [expected] * 3


@pytest.mark.parametrize("span", [float("nan"), float("inf"), 0., 401.])
def test_section_rejects_unbounded_display_range(span):
    volume = Volume(np.zeros((2, 2, 2)), (1., 1., 1.), (0., 0., 0.), tuple(map(tuple, np.eye(3))))
    with pytest.raises(PipelineError, match="显示范围"):
        render_section(volume, np.zeros((2, 2, 2)), {"point_lps_mm": [0, 0, 0], "normal_lps": [0, 0, 1]}, span)
