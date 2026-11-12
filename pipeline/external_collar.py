"""Conservative, read-only candidates for finite air outside two nostrils.

This module does not edit a confirmed airway mask, save a case, construct a
port/cap, or assert that a candidate is suitable for CFD. Coordinates are
voxel indices in the same array geometry as all three input masks. Axis
directions are unit LPS vectors, one row per voxel axis. The caller must
explicitly choose a physical search extent; there is no clinical default.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from scipy import ndimage

from .errors import PipelineError


FORMAT = "external_nostril_collar_candidate_v1"
_SIX_CONNECTED = ndimage.generate_binary_structure(3, 1)


def _block(reason: str, detail: dict | None = None) -> None:
    raise PipelineError(
        "blocked", reason,
        ["核对左右鼻孔种子和 CT 扫描范围；本候选不能用于自动裁切或 CFD"],
        detail,
    )


def _binary_volume(value: object, label: str) -> np.ndarray:
    array = np.asarray(value)
    if array.ndim != 3 or not all(size > 0 for size in array.shape):
        _block(f"{label}必须是非空三维数组")
    if array.dtype.kind not in "bu" or np.any(array > 1):
        _block(f"{label}必须是二值数组")
    return np.asarray(array, dtype=bool)


def _spacing(value: object) -> tuple[float, float, float]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != 3:
        _block("体素间距必须是三个毫米数值")
    try:
        spacing = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        _block("体素间距包含非数值")
    if any(not math.isfinite(item) or item <= 0 for item in spacing):
        _block("体素间距必须为有限正数")
    return spacing


def _axis_dirs(value: object) -> np.ndarray:
    try:
        directions = np.asarray(value, dtype=float)
    except (TypeError, ValueError):
        _block("CT 轴方向必须是三个 LPS 单位方向向量")
    if directions.shape != (3, 3) or not np.all(np.isfinite(directions)):
        _block("CT 轴方向必须是三个有限的 LPS 单位方向向量")
    gram = directions @ directions.T
    if not np.allclose(gram, np.eye(3), atol=1e-3, rtol=0):
        _block("CT 轴方向不正交或未归一，无法判定左右鼻孔")
    return directions


def _seed(value: object, label: str, mask: np.ndarray) -> tuple[int, int, int]:
    if (isinstance(value, (str, bytes)) or not isinstance(value, Sequence)
            or len(value) != 3 or any(type(item) not in (int, np.int32, np.int64) for item in value)):
        _block(f"{label}必须是三个体素整数索引")
    point = tuple(int(item) for item in value)
    if any(item < 0 or item >= mask.shape[axis] for axis, item in enumerate(point)):
        _block(f"{label}超出 CT 体数据范围")
    if not mask[point]:
        _block(f"{label}必须落在已确认的主气道掩膜内")
    return point


def _candidate_for_side(
    side: str,
    confirmed: np.ndarray,
    raw_air: np.ndarray,
    envelope: np.ndarray,
    spacing: tuple[float, float, float],
    seed: tuple[int, int, int],
    other_seed: tuple[int, int, int],
    max_extent_mm: float,
) -> dict:
    # Include one extra voxel around the physical ball, so the component and
    # its interface can be checked without treating the ROI edge as anatomy.
    pad = [int(math.ceil(max_extent_mm / spacing[axis])) + 1 for axis in range(3)]
    starts = [max(0, seed[axis] - pad[axis]) for axis in range(3)]
    stops = [min(confirmed.shape[axis], seed[axis] + pad[axis] + 1) for axis in range(3)]
    roi = tuple(slice(starts[axis], stops[axis]) for axis in range(3))
    grid = np.ogrid[tuple(slice(starts[axis], stops[axis]) for axis in range(3))]
    distance_sq = sum(
        ((grid[axis] - seed[axis]) * spacing[axis]) ** 2 for axis in range(3)
    )
    local = (raw_air[roi] & ~envelope[roi] & ~confirmed[roi]
             & (distance_sq <= max_extent_mm * max_extent_mm + 1e-8))
    if not local.any():
        _block(f"{side}鼻孔附近没有可验证的外侧空气", {"side": side})

    labels, component_count = ndimage.label(local, structure=_SIX_CONNECTED)
    if component_count != 1:
        _block(
            f"{side}鼻孔有限范围内的外侧空气不连通，不能唯一提取",
            {"side": side, "components": int(component_count)},
        )
    contact = local & ndimage.binary_dilation(confirmed[roi], structure=_SIX_CONNECTED)
    if not contact.any():
        _block(f"{side}鼻孔外侧空气没有与已确认气道接触", {"side": side})
    _, contact_components = ndimage.label(contact, structure=_SIX_CONNECTED)
    if contact_components != 1:
        _block(
            f"{side}鼻孔与外侧空气有多个分离接触区，不能确定唯一开口",
            {"side": side, "contact_components": int(contact_components)},
        )

    local_indices = np.argwhere(local)
    global_indices = local_indices + np.asarray(starts, dtype=int)
    if any(np.any((global_indices[:, axis] == 0)
                  | (global_indices[:, axis] == confirmed.shape[axis] - 1))
           for axis in range(3)):
        _block(f"{side}鼻孔外侧候选触及 CT 边界", {"side": side})
    # A finite exterior collar must reach its chosen physical limit along a
    # connected path from the confirmed-airway interface. The shell voxel
    # itself cannot merely be the one contacting the airway.
    local_distances = np.sqrt(distance_sq[local])
    shell = local & (distance_sq >= (max_extent_mm - min(spacing) / 2) ** 2)
    contact_distances = np.sqrt(distance_sq[contact])
    outward_shell = shell & ~contact
    if (not outward_shell.any() or
            np.max(np.sqrt(distance_sq[outward_shell])) <= np.max(contact_distances) + min(spacing) / 2):
        _block(f"{side}候选未沿连通路径延伸到指定外侧范围", {"side": side})

    contact_indices = np.argwhere(contact) + np.asarray(starts, dtype=int)
    own = np.sum(((contact_indices - seed) * spacing) ** 2, axis=1)
    other = np.sum(((contact_indices - other_seed) * spacing) ** 2, axis=1)
    if np.any(other <= own):
        _block(f"{side}鼻孔接触区无法唯一归属当前种子", {"side": side})

    return {
        "bounds_ijk": [[starts[axis], stops[axis]] for axis in range(3)],
        "mask": local.copy(),
        "contact_mask": contact.copy(),
        "stats": {
            "voxels": int(local.sum()),
            "contact_voxels": int(contact.sum()),
            "volume_mm3": float(local.sum() * math.prod(spacing)),
            "max_distance_mm": float(local_distances.max()),
            "components": int(component_count),
            "contact_components": int(contact_components),
            "touches_ct_boundary": False,
        },
        "global_indices": global_indices,
    }


def build_external_collar_candidates(
    confirmed_mask: np.ndarray,
    raw_air_candidate: np.ndarray,
    body_envelope: np.ndarray,
    spacing_mm: Sequence[float],
    axis_dirs: Sequence[Sequence[float]],
    left_seed_ijk: Sequence[int],
    right_seed_ijk: Sequence[int],
    max_extent_mm: float,
) -> dict:
    """Return isolated, finite candidate masks and audit data for both sides.

    The two local masks are cropped to bounds_ijk (exclusive upper bound).
    They are never automatically added to the confirmed mask. Six-neighbour
    contact is required; a single global exterior component may still connect
    the nostrils farther away, outside these deliberately finite candidates.
    """
    confirmed = _binary_volume(confirmed_mask, "已确认气道掩膜")
    raw_air = _binary_volume(raw_air_candidate, "原始空气候选")
    envelope = _binary_volume(body_envelope, "体内包络")
    if confirmed.shape != raw_air.shape or confirmed.shape != envelope.shape:
        _block("已确认掩膜、原始空气和体内包络尺寸不一致")
    spacing = _spacing(spacing_mm)
    directions = _axis_dirs(axis_dirs)
    if isinstance(max_extent_mm, bool):
        _block("外侧空气范围必须是显式毫米正数")
    try:
        extent = float(max_extent_mm)
    except (TypeError, ValueError):
        _block("外侧空气范围必须是显式毫米正数")
    if not math.isfinite(extent) or extent <= 0:
        _block("外侧空气范围必须是显式毫米正数")
    if extent <= max(spacing):
        _block("外侧空气范围小于或等于最大体素间距，不能形成可验证的有限空气区域")
    left_seed = _seed(left_seed_ijk, "左鼻孔种子", confirmed)
    right_seed = _seed(right_seed_ijk, "右鼻孔种子", confirmed)
    if left_seed == right_seed:
        _block("左右鼻孔种子不能重合")
    left_minus_right_lps_x = float(np.dot(
        (np.asarray(left_seed, dtype=float) - np.asarray(right_seed, dtype=float))
        * np.asarray(spacing, dtype=float), directions[:, 0],
    ))
    if left_minus_right_lps_x <= 1e-6:
        _block(
            "左鼻孔种子必须位于右鼻孔种子的 LPS 左侧",
            {"left_minus_right_lps_x_mm": round(left_minus_right_lps_x, 6)},
        )
    if np.any(confirmed & ~envelope):
        _block("已确认气道掩膜位于体内包络外，不能提取外侧候选")

    left = _candidate_for_side(
        "左", confirmed, raw_air, envelope, spacing, left_seed, right_seed, extent,
    )
    right = _candidate_for_side(
        "右", confirmed, raw_air, envelope, spacing, right_seed, left_seed, extent,
    )
    left_points = {tuple(int(value) for value in point) for point in left.pop("global_indices")}
    right_points = {tuple(int(value) for value in point) for point in right.pop("global_indices")}
    if left_points & right_points:
        _block("左右鼻孔外侧候选重叠，不能分别归属")
    for point in left_points:
        for axis in range(3):
            neighbor = list(point)
            neighbor[axis] += 1
            if tuple(neighbor) in right_points:
                _block("左右鼻孔外侧候选在网格上相连，不能分别归属")
            neighbor[axis] -= 2
            if tuple(neighbor) in right_points:
                _block("左右鼻孔外侧候选在网格上相连，不能分别归属")

    return {
        "format": FORMAT,
        "status": "candidate_only",
        "eligible_for_cfd": False,
        "spacing_mm": list(spacing),
        "axis_dirs_lps": directions.tolist(),
        "max_extent_mm": extent,
        "left": left,
        "right": right,
        "stats": {
            "confirmed_mask_voxels": int(confirmed.sum()),
            "raw_air_candidate_voxels": int(raw_air.sum()),
            "total_candidate_voxels": left["stats"]["voxels"] + right["stats"]["voxels"],
        },
    }


__all__ = ["FORMAT", "build_external_collar_candidates"]
