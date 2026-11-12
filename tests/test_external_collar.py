"""Synthetic-only safety tests for an isolated exterior-air candidate."""

import numpy as np
import pytest

from pipeline.errors import PipelineError
from pipeline.external_collar import build_external_collar_candidates


# Array axis 2 points toward LPS right; lower axis-2 indices are anatomical left.
AXIS_DIRS_LPS = ((0, 0, 1), (0, 1, 0), (-1, 0, 0))


def _fixture():
    shape = (25, 25, 35)
    envelope = np.zeros(shape, dtype=bool)
    envelope[2:23, 7:23, 2:33] = True
    confirmed = np.zeros(shape, dtype=bool)
    confirmed[8:11, 7:11, 7:10] = True
    confirmed[8:11, 7:11, 25:28] = True
    raw = confirmed.copy()
    raw[8:11, 2:7, 7:10] = True
    raw[8:11, 2:7, 25:28] = True
    return confirmed, raw, envelope


def _build(confirmed, raw, envelope, *, right_seed=(9, 9, 26), extent=6.0,
           axis_dirs=AXIS_DIRS_LPS):
    return build_external_collar_candidates(
        confirmed, raw, envelope, (1.0, 1.0, 1.0),
        axis_dirs, (9, 9, 8), right_seed, extent,
    )


def test_finite_bilateral_candidates_are_separate_and_do_not_mutate_inputs():
    confirmed, raw, envelope = _fixture()
    snapshots = [values.copy() for values in (confirmed, raw, envelope)]

    result = _build(confirmed, raw, envelope)

    assert result["status"] == "candidate_only"
    assert result["eligible_for_cfd"] is False
    assert result["max_extent_mm"] == 6.0
    assert result["left"]["stats"]["voxels"] > 0
    assert result["right"]["stats"]["voxels"] > 0
    assert result["left"]["stats"]["contact_components"] == 1
    assert result["right"]["stats"]["contact_components"] == 1
    assert result["left"]["stats"]["max_distance_mm"] <= 6.0
    assert result["stats"]["total_candidate_voxels"] == (
        result["left"]["stats"]["voxels"] + result["right"]["stats"]["voxels"]
    )
    for actual, before in zip((confirmed, raw, envelope), snapshots):
        np.testing.assert_array_equal(actual, before)


def test_candidate_touching_ct_boundary_is_blocked():
    confirmed, raw, envelope = _fixture()
    raw[8:11, 0:7, 7:10] = True
    with pytest.raises(PipelineError, match="触及 CT 边界"):
        _build(confirmed, raw, envelope, extent=9.0)


def test_disconnected_exterior_air_is_blocked_instead_of_silently_dropped():
    confirmed, raw, envelope = _fixture()
    raw[9, 5, 12] = True
    with pytest.raises(PipelineError, match="不连通"):
        _build(confirmed, raw, envelope)


def test_exterior_air_without_confirmed_airway_contact_is_blocked():
    confirmed, raw, envelope = _fixture()
    raw[:, 6, :] = False
    with pytest.raises(PipelineError, match="没有与已确认气道接触"):
        _build(confirmed, raw, envelope)


def test_two_separate_contact_patches_are_not_accepted_as_one_opening():
    confirmed, raw, envelope = _fixture()
    confirmed[9, 7:10, 11] = True
    raw[9, 7:10, 11] = True
    raw[9, 5:7, 11] = True
    raw[9, 5, 9:12] = True
    with pytest.raises(PipelineError, match="多个分离接触区"):
        _build(confirmed, raw, envelope)


def test_bilateral_exterior_air_merged_inside_local_extents_is_blocked():
    confirmed, raw, envelope = _fixture()
    confirmed[:, :, 25:28] = False
    raw[:, :, 25:28] = False
    confirmed[8:11, 7:11, 15:18] = True
    raw[8:11, 7:11, 15:18] = True
    raw[8:11, 2:7, 15:18] = True
    raw[9, 5, 9:16] = True
    with pytest.raises(PipelineError, match="左右鼻孔外侧候选.*(?:重叠|相连)"):
        _build(confirmed, raw, envelope, right_seed=(9, 9, 16))


def test_physical_extent_uses_anisotropic_mm_spacing():
    confirmed, raw, envelope = _fixture()
    result = build_external_collar_candidates(
        confirmed, raw, envelope, (1.0, 2.0, 1.0),
        AXIS_DIRS_LPS, (9, 9, 8), (9, 9, 26), 8.1,
    )
    assert result["left"]["stats"]["max_distance_mm"] <= 8.1
    assert result["left"]["stats"]["max_distance_mm"] >= 8.0
    starts = [item[0] for item in result["left"]["bounds_ijk"]]
    assert result["left"]["mask"][9 - starts[0], 5 - starts[1], 8 - starts[2]]
    assert not result["left"]["mask"][9 - starts[0], 3 - starts[1], 8 - starts[2]]


def test_swapped_left_right_seeds_are_blocked_by_lps_x():
    confirmed, raw, envelope = _fixture()
    with pytest.raises(PipelineError, match="LPS 左侧"):
        build_external_collar_candidates(
            confirmed, raw, envelope, (1, 1, 1), AXIS_DIRS_LPS,
            (9, 9, 26), (9, 9, 8), 6,
        )


def test_invalid_lps_axis_directions_are_blocked():
    confirmed, raw, envelope = _fixture()
    with pytest.raises(PipelineError, match="不正交或未归一"):
        _build(confirmed, raw, envelope, axis_dirs=((1, 0, 0), (1, 0, 0), (0, 0, 1)))


def test_single_external_contact_voxel_is_not_an_outward_path():
    confirmed, _, envelope = _fixture()
    raw = confirmed.copy()
    raw[9, 6, 8] = True
    raw[9, 6, 26] = True
    with pytest.raises(PipelineError, match="未沿连通路径延伸"):
        build_external_collar_candidates(
            confirmed, raw, envelope, (1, 1, 1), AXIS_DIRS_LPS,
            (9, 7, 8), (9, 7, 26), 1.5,
        )


def test_invalid_seed_or_confirmed_air_outside_envelope_is_blocked():
    confirmed, raw, envelope = _fixture()
    with pytest.raises(PipelineError, match="种子必须落在已确认"):
        build_external_collar_candidates(
            confirmed, raw, envelope, (1, 1, 1), AXIS_DIRS_LPS,
            (0, 0, 0), (9, 9, 26), 6,
        )
    confirmed[9, 6, 8] = True
    with pytest.raises(PipelineError, match="位于体内包络外"):
        _build(confirmed, raw, envelope)


@pytest.mark.parametrize("extent", [0, -1, float("nan"), None, True])
def test_missing_or_invalid_physical_extent_is_blocked(extent):
    confirmed, raw, envelope = _fixture()
    with pytest.raises(PipelineError, match="显式毫米正数"):
        _build(confirmed, raw, envelope, extent=extent)


def test_extent_at_or_below_voxel_spacing_is_blocked():
    confirmed, raw, envelope = _fixture()
    with pytest.raises(PipelineError, match="最大体素间距"):
        _build(confirmed, raw, envelope, extent=1.0)
