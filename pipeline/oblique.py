"""以患者 LPS 毫米坐标定义两张鼻孔/鼻咽裁切面。

法向均指向删除侧；体素裁切仅供方案验证和体积对照。真正的平面
交线及封盖必须由三角网格裁切产生，不能把体素阶梯当作旋转端面。
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage

from .config import MIN_OPENING_AREA_MM2
from .errors import PipelineError
from .volume import ANTERIOR_LPS


def _plane(value: dict, name: str) -> dict:
    if not isinstance(value, dict):
        raise PipelineError("failed", f"{name}缺少三维坐标", ["在 3D 视窗重新选择裁切面"])
    try:
        point = np.asarray(value["point_lps_mm"], dtype=float)
        normal = np.asarray(value["normal_lps"], dtype=float)
    except (KeyError, TypeError, ValueError) as exc:
        raise PipelineError("failed", f"{name}的坐标或法向无效", ["在 3D 视窗重新选择裁切面"]) from exc
    if point.shape != (3,) or normal.shape != (3,) or not np.all(np.isfinite(point)) or not np.all(np.isfinite(normal)):
        raise PipelineError("failed", f"{name}的坐标或法向无效", ["在 3D 视窗重新选择裁切面"])
    length = float(np.linalg.norm(normal))
    if length < 1e-8:
        raise PipelineError("failed", f"{name}法向长度为零", ["旋转裁切面后重试"])
    normal /= length
    return {"point_lps_mm": point.tolist(), "normal_lps": normal.tolist()}


def validate_plane_endpoint(plane: dict, side: str) -> dict:
    """Validate one saved plane's outward direction before its peer exists."""
    if side not in {"anterior", "posterior"}:
        raise PipelineError("failed", "端面方向无效", ["分别选择鼻孔端或鼻咽端"])
    name = "鼻孔端" if side == "anterior" else "鼻咽端"
    normalized = _plane(plane, name)
    alignment = float(np.dot(normalized["normal_lps"], ANTERIOR_LPS))
    if (side == "anterior" and alignment <= 0.1) or (side == "posterior" and alignment >= -0.1):
        raise PipelineError(
            "blocked", f"{name}法向没有朝向解剖外侧",
            ["在 3D 视窗选中当前端面并翻转或旋转方向"],
        )
    return normalized


def _bilateral_local_outlet_plane(plane: dict, seed_lps_mm) -> dict:
    """Allow a footward outlet only for the seeded three-port local cut."""
    normalized = _plane(plane, "鼻咽端")
    normal = np.asarray(normalized["normal_lps"], dtype=float)
    posterior_alignment = -float(np.dot(normal, ANTERIOR_LPS))
    inferior_alignment = -float(normal[2])  # LPS +Z is superior.
    if not (posterior_alignment > 0.1 or
            (inferior_alignment > 0.5 and inferior_alignment > abs(posterior_alignment))):
        raise PipelineError(
            "blocked", "鼻咽端法向没有朝向后方或足侧出口",
            ["在三维模型选中鼻咽出口，确认箭头指向待删除的外侧支段"],
        )
    try:
        seed = np.asarray(seed_lps_mm, dtype=float)
    except (TypeError, ValueError) as exc:
        raise PipelineError(
            "blocked", "鼻咽出口定位点无效", ["在切面外侧的出口管段重新定位"],
        ) from exc
    if seed.shape != (3,) or not np.all(np.isfinite(seed)):
        raise PipelineError(
            "blocked", "鼻咽出口定位点无效", ["在切面外侧的出口管段重新定位"],
        )
    if float(np.dot(seed - np.asarray(normalized["point_lps_mm"]), normal)) <= 0.1:
        raise PipelineError(
            "blocked", "鼻咽出口定位点必须位于切面的删除侧",
            ["在箭头所指的外侧出口管段重新定位"],
        )
    return normalized


def _distance_coefficients(volume, plane: dict) -> tuple[float, np.ndarray]:
    normal = np.asarray(plane["normal_lps"], dtype=float)
    point = np.asarray(plane["point_lps_mm"], dtype=float)
    axes_mm = np.asarray(volume.axis_dirs, dtype=float) * np.asarray(volume.spacing, dtype=float)[:, None]
    return float(np.dot(np.asarray(volume.origin, dtype=float) - point, normal)), axes_mm @ normal


def _distances(offset: float, coefficients: np.ndarray, start: int, stop: int, shape) -> np.ndarray:
    return (
        offset
        + np.arange(start, stop, dtype=float)[:, None, None] * coefficients[0]
        + np.arange(shape[1], dtype=float)[None, :, None] * coefficients[1]
        + np.arange(shape[2], dtype=float)[None, None, :] * coefficients[2]
    )


def clip_mask_oblique(mask: np.ndarray, volume, anterior_plane: dict, posterior_plane: dict) -> np.ndarray:
    """按体素中心裁切副本；用于面积/体积估算，不作为精确端面网格。"""
    if np.shape(mask) != tuple(volume.shape):
        raise PipelineError("failed", "气道掩膜与 CT 尺寸不一致", ["重新导入匹配的掩膜"])
    out = np.asarray(mask, dtype=bool).copy()
    planes = (_plane(anterior_plane, "鼻孔端"), _plane(posterior_plane, "鼻咽端"))
    for plane in planes:
        offset, coefficients = _distance_coefficients(volume, plane)
        for start in range(0, out.shape[0], 12):
            stop = min(start + 12, out.shape[0])
            chunk = out[start:stop]
            if not chunk.any():
                continue
            chunk &= _distances(offset, coefficients, start, stop, out.shape) <= 1e-8
    return out


def _label_positive_components(positive: np.ndarray) -> tuple[np.ndarray, tuple[slice, slice, slice], int]:
    """Label only the occupied crop, not an entire CT-sized integer volume."""
    occupied = np.where(positive)
    if not len(occupied[0]):
        return np.zeros((0, 0, 0), dtype=np.int32), (slice(0, 0),) * 3, 0
    bounds = tuple(slice(int(axis.min()), int(axis.max()) + 1) for axis in occupied)
    labels, count = ndimage.label(
        positive[bounds], structure=ndimage.generate_binary_structure(3, 1)
    )
    return labels, bounds, int(count)


def _local_delete_component(
    mask: np.ndarray, volume, anterior_plane: dict, anterior_seed_lps_mm,
    *, role: str = "anterior",
) -> tuple[np.ndarray, np.ndarray, dict]:
    """Find the *voxel-connected* anterior branch selected by the user.

    A seed can lie on the displayed isosurface, half a voxel outside the mask.
    It must still be close to one unambiguous positive-side mask component.
    """
    source = np.asarray(mask, dtype=bool)
    if source.shape != tuple(volume.shape) or not source.any():
        raise PipelineError("blocked", "气道掩膜为空或与 CT 尺寸不一致", ["重新复核当前建模范围的掩膜"])
    try:
        seed = np.asarray(anterior_seed_lps_mm, dtype=float)
    except (TypeError, ValueError) as exc:
        raise PipelineError("blocked", "鼻孔端局部锚点无效", ["在待删除的鼻孔外侧支段重新选点"]) from exc
    if seed.shape != (3,) or not np.all(np.isfinite(seed)):
        raise PipelineError("blocked", "鼻孔端局部锚点无效", ["在待删除的鼻孔外侧支段重新选点"])

    anterior = _plane(anterior_plane, "鼻孔端")
    offset, coefficients = _distance_coefficients(volume, anterior)
    positive = np.zeros_like(source)
    for start in range(0, source.shape[0], 12):
        stop = min(start + 12, source.shape[0])
        positive[start:stop] = source[start:stop] & (
            _distances(offset, coefficients, start, stop, source.shape) > 1e-8
        )
    labels, crop, component_count = _label_positive_components(positive)
    if component_count == 0:
        raise PipelineError("blocked", "鼻孔端切面外侧没有可删除的气道", ["将切面移入目标鼻孔支段"])

    axes_mm = np.asarray(volume.axis_dirs, dtype=float) * np.asarray(volume.spacing, dtype=float)[:, None]
    try:
        seed_ijk = (seed - np.asarray(volume.origin, dtype=float)) @ np.linalg.inv(axes_mm)
    except np.linalg.LinAlgError as exc:
        raise PipelineError("failed", "CT 体素坐标系不可逆", ["核查 DICOM 几何方向"]) from exc
    center = np.rint(seed_ijk).astype(int)
    radius = 2
    bounds = [range(max(0, int(c) - radius), min(int(n), int(c) + radius + 1)) for c, n in zip(center, source.shape)]
    candidates: list[tuple[float, int]] = []
    for i in bounds[0]:
        for j in bounds[1]:
            for k in bounds[2]:
                if not all(start <= index < stop for index, start, stop in zip(
                    (i, j, k), (axis.start for axis in crop), (axis.stop for axis in crop)
                )):
                    continue
                label = int(labels[i - crop[0].start, j - crop[1].start, k - crop[2].start])
                if label:
                    point = np.asarray(volume.origin, dtype=float) + np.asarray([i, j, k]) @ axes_mm
                    candidates.append((float(np.linalg.norm(point - seed)), label))
    if not candidates:
        raise PipelineError("blocked", "局部锚点没有落在待切的鼻孔支段附近", ["在切面外侧目标鼻孔气道上重新选点"])
    nearest_by_label: dict[int, float] = {}
    for distance, label in candidates:
        nearest_by_label[label] = min(nearest_by_label.get(label, float("inf")), distance)
    ranked = sorted((distance, label) for label, distance in nearest_by_label.items())
    limit = 2.5 * float(max(volume.spacing))
    if ranked[0][0] > limit:
        raise PipelineError("blocked", "局部锚点距离鼻孔气道过远", ["在切面外侧目标鼻孔气道上重新选点"])
    if len(ranked) > 1 and ranked[1][0] - ranked[0][0] < 0.5 * float(min(volume.spacing)):
        raise PipelineError("blocked", "局部锚点位于两个相邻气道之间，目标不明确", ["在目标鼻孔支段更靠内部的位置重新选点"])
    selected_label = ranked[0][1]
    selected = np.zeros_like(source)
    selected[crop] = labels == selected_label
    # A plane placed behind the point where both nostrils merge has only one
    # cut ring, yet its positive side contains *both* anterior branches.
    # Count the terminal branches in a short anterior-most slab as a second
    # guard, independent of the number of plane intersections.
    if role not in {"anterior", "posterior"}:
        raise ValueError("unsupported local cut role")
    terminal_components = 0
    if role == "anterior":
        selected_coords = np.column_stack(np.where(selected))
        anterior_direction = np.asarray(ANTERIOR_LPS, dtype=float)
        anterior_position = selected_coords @ (axes_mm @ anterior_direction)
        terminal_coords = selected_coords[
            anterior_position >= anterior_position.max() - 2.5 * float(max(volume.spacing))
        ]
        terminal_start = terminal_coords.min(axis=0)
        terminal_shape = terminal_coords.max(axis=0) - terminal_start + 1
        terminal_mask = np.zeros(tuple(terminal_shape), dtype=bool)
        terminal_mask[tuple((terminal_coords - terminal_start).T)] = True
        _terminal_labels, terminal_components = ndimage.label(
            terminal_mask, structure=ndimage.generate_binary_structure(3, 3)
        )
        if terminal_components != 1:
            raise PipelineError(
                "blocked", "所选删除侧跨支段，包含多个鼻孔前端",
                ["把局部切面前移到单一鼻孔支段；若仍相连需显式局部轮廓"],
                {"anterior_terminal_components": int(terminal_components)},
            )
    retained_side = source & ~positive
    boundary = selected & ndimage.binary_dilation(
        retained_side, structure=ndimage.generate_binary_structure(3, 1)
    )
    opening_labels, opening_components = ndimage.label(
        boundary, structure=ndimage.generate_binary_structure(3, 3)
    )
    del opening_labels
    if opening_components != 1:
        label = "鼻孔" if role == "anterior" else "鼻咽"
        raise PipelineError(
            "blocked", f"所选{label}支段形成多个端面或没有形成端面",
            ["把切面移到单一、连续的目标管段；跨支段时需改用明确的局部轮廓"],
            {"anterior_opening_components": int(opening_components)},
        )
    return selected, positive, {
        "anterior_seed_lps_mm": seed.tolist(),
        "anterior_deleted_voxels": int(np.count_nonzero(selected)),
        "anterior_candidate_components": int(component_count),
        "anterior_opening_components": int(opening_components),
        "anterior_terminal_components": int(terminal_components),
        "anterior_unselected_positive_voxels": int(np.count_nonzero(positive & ~selected)),
    }


def clip_mask_oblique_local(
    mask: np.ndarray, volume, anterior_plane: dict, posterior_plane: dict, *, anterior_seed_lps_mm,
) -> np.ndarray:
    """Coarse volume/QC mask matching the selected local inlet branch."""
    selected, _positive, _stats = _local_delete_component(
        mask, volume, anterior_plane, anterior_seed_lps_mm
    )
    out = np.asarray(mask, dtype=bool).copy()
    out[selected] = False
    offset, coefficients = _distance_coefficients(volume, _plane(posterior_plane, "鼻咽端"))
    for start in range(0, out.shape[0], 12):
        stop = min(start + 12, out.shape[0])
        if out[start:stop].any():
            out[start:stop] &= _distances(offset, coefficients, start, stop, out.shape) <= 1e-8
    return out


def _area_estimate(mask: np.ndarray, volume, plane: dict, other: dict | None) -> float:
    """平面附近体素的线性加权截面积；只用于保存前技术初筛。"""
    offset, coefficients = _distance_coefficients(volume, plane)
    other_geometry = _distance_coefficients(volume, other) if other is not None else None
    width = float(np.abs(coefficients).sum())
    if width < 1e-8:
        raise PipelineError("failed", "CT 体素几何无效", ["核查 DICOM 几何方向与间距"])
    voxel_volume = float(np.prod(volume.spacing))
    weighted_voxels = 0.0
    for start in range(0, mask.shape[0], 12):
        stop = min(start + 12, mask.shape[0])
        chunk = mask[start:stop]
        if not chunk.any():
            continue
        distance = _distances(offset, coefficients, start, stop, mask.shape)
        near = chunk & (np.abs(distance) < width)
        if not near.any():
            continue
        if other_geometry is not None:
            other_offset, other_coefficients = other_geometry
            other_distance = _distances(other_offset, other_coefficients, start, stop, mask.shape)
            near &= other_distance <= 1e-8
        weighted_voxels += float(np.sum(np.maximum(0.0, 1.0 - np.abs(distance[near]) / width)))
    return weighted_voxels * voxel_volume / width


def _retained_component_touches_plane(mask: np.ndarray, volume, plane: dict) -> bool:
    offset, coefficients = _distance_coefficients(volume, plane)
    width = float(np.abs(coefficients).sum())
    for start in range(0, mask.shape[0], 12):
        stop = min(start + 12, mask.shape[0])
        chunk = mask[start:stop]
        if chunk.any():
            distance = _distances(offset, coefficients, start, stop, mask.shape)
            if np.any(chunk & (distance >= -width) & (distance <= 1e-8)):
                return True
    return False


def _planes_intersect_mask(mask: np.ndarray, volume, anterior: dict, posterior: dict) -> bool:
    """Detect two planes crossing or getting too close inside the airway mask.

    A voxel whose centre is close to both planes is a conservative indicator of
    an internal plane intersection. The tolerance is tied to the voxel diagonal
    so this check remains meaningful across CT resolutions.
    """
    source = np.asarray(mask, dtype=bool)
    if not source.any():
        return False
    anterior_offset, anterior_coefficients = _distance_coefficients(volume, anterior)
    posterior_offset, posterior_coefficients = _distance_coefficients(volume, posterior)
    tolerance = 0.75 * float(np.linalg.norm(np.asarray(volume.spacing, dtype=float)))
    for start in range(0, source.shape[0], 12):
        stop = min(start + 12, source.shape[0])
        chunk = source[start:stop]
        if not chunk.any():
            continue
        anterior_distance = _distances(
            anterior_offset, anterior_coefficients, start, stop, source.shape,
        )
        posterior_distance = _distances(
            posterior_offset, posterior_coefficients, start, stop, source.shape,
        )
        if np.any(
            chunk
            & (np.abs(anterior_distance) <= tolerance)
            & (np.abs(posterior_distance) <= tolerance)
        ):
            return True
    return False


def validate_plane_pair(
    mask: np.ndarray, volume, anterior_plane: dict, posterior_plane: dict, *, main_airway: bool,
) -> dict:
    """验证用户双平面的方向、位置和可用截面，返回规范化 LPS 参数。"""
    anterior = _plane(anterior_plane, "鼻孔端")
    posterior = _plane(posterior_plane, "鼻咽端")
    forward = np.asarray(ANTERIOR_LPS, dtype=float)
    anterior_alignment = float(np.dot(anterior["normal_lps"], forward))
    posterior_alignment = float(np.dot(posterior["normal_lps"], forward))
    if anterior_alignment <= 0.1 or posterior_alignment >= -0.1:
        raise PipelineError(
            "blocked", "两张切面法向必须分别朝鼻孔外侧和鼻咽外侧",
            ["在 3D 视窗翻转或旋转切面；端面法向应指向准备删除的外侧，气流箭头仅表示进气和向肺侧流向"],
        )
    anterior_ap = float(np.dot(anterior["point_lps_mm"], forward))
    posterior_ap = float(np.dot(posterior["point_lps_mm"], forward))
    if anterior_ap <= posterior_ap:
        raise PipelineError("blocked", "鼻孔切面必须位于鼻咽切面前方", ["移动两张 3D 切面后重试"])
    source = np.asarray(mask, dtype=bool)
    if source.shape != tuple(volume.shape) or not source.any():
        raise PipelineError("blocked", "所选范围的气道掩膜为空或尺寸不符", ["重新复核掩膜"])
    if main_airway:
        from .surface import select_largest_component

        working, _ = select_largest_component(source)
    else:
        working = source
    if _planes_intersect_mask(working, volume, anterior, posterior):
        raise PipelineError(
            "blocked", "两张切面在气道内部相交或间距过小",
            ["把鼻孔端和鼻咽端分别移到连续气道段后重试"],
        )
    anterior_area = _area_estimate(working, volume, anterior, posterior)
    posterior_area = _area_estimate(working, volume, posterior, anterior)
    if anterior_area < MIN_OPENING_AREA_MM2 or posterior_area < MIN_OPENING_AREA_MM2:
        raise PipelineError(
            "blocked", "3D 切面未形成足够大的鼻孔端或鼻咽端截面",
            ["把两张切面分别移动到连续、清晰的气道段；保存后仍需用精确网格质检"],
            {
                "anterior_area_estimate_mm2": round(anterior_area, 3),
                "posterior_area_estimate_mm2": round(posterior_area, 3),
                "technical_minimum_mm2": MIN_OPENING_AREA_MM2,
            },
        )
    retained = clip_mask_oblique(working, volume, anterior, posterior)
    retained_voxels = int(np.count_nonzero(retained))
    if retained_voxels == 0:
        raise PipelineError("blocked", "两张 3D 切面之间没有保留气道", ["调整切面位置与方向"])
    occupied = np.where(retained)
    bbox = tuple(slice(int(coords.min()), int(coords.max()) + 1) for coords in occupied)
    cropped = retained[bbox]
    labels, retained_components = ndimage.label(
        cropped, structure=ndimage.generate_binary_structure(3, 1)
    )
    if main_airway:
        sizes = np.bincount(labels.ravel())
        sizes[0] = 0
        largest = np.zeros_like(retained)
        largest[bbox] = labels == int(np.argmax(sizes))
        if not (_retained_component_touches_plane(largest, volume, anterior)
                and _retained_component_touches_plane(largest, volume, posterior)):
            raise PipelineError(
                "blocked", "主气道保留的最大连通区域未同时到达两张切面",
                ["核对鼻孔与鼻咽之间的气道连通性，必要时先修正掩膜"],
            )
    return {
        "anterior_plane": anterior,
        "posterior_plane": posterior,
        "source_voxels": int(np.count_nonzero(source)),
        "retained_voxels": retained_voxels,
        "retained_components": int(retained_components),
        "retained_volume_ml": round(retained_voxels * float(np.prod(volume.spacing)) / 1000.0, 3),
        "anterior_area_estimate_mm2": round(anterior_area, 3),
        "posterior_area_estimate_mm2": round(posterior_area, 3),
    }


def validate_local_inlet_pair(
    mask: np.ndarray, volume, anterior_plane: dict, posterior_plane: dict,
    anterior_seed_lps_mm, *, main_airway: bool,
) -> dict:
    """Preflight a selected anterior branch without clipping its neighbors.

    The voxel opening count is a conservative preflight; the generated mesh
    must independently prove a single inlet loop and watertight caps.
    """
    anterior = validate_plane_endpoint(anterior_plane, "anterior")
    posterior = validate_plane_endpoint(posterior_plane, "posterior")
    forward = np.asarray(ANTERIOR_LPS, dtype=float)
    if float(np.dot(anterior["point_lps_mm"], forward)) <= float(np.dot(posterior["point_lps_mm"], forward)):
        raise PipelineError("blocked", "鼻孔切面必须位于鼻咽切面前方", ["移动两张 3D 切面后重试"])
    source = np.asarray(mask, dtype=bool)
    if source.shape != tuple(volume.shape) or not source.any():
        raise PipelineError("blocked", "所选范围的气道掩膜为空或尺寸不符", ["重新复核掩膜"])
    if main_airway:
        from .surface import select_largest_component

        working, _ = select_largest_component(source)
    else:
        working = source
    if _planes_intersect_mask(working, volume, anterior, posterior):
        raise PipelineError("blocked", "两张切面在气道内部相交或间距过小", ["调整两端切面后重试"])
    selected, positive, local_stats = _local_delete_component(
        working, volume, anterior, anterior_seed_lps_mm
    )
    anterior_only = working.copy()
    anterior_only[selected] = False
    retained = clip_mask_oblique_local(
        working, volume, anterior, posterior, anterior_seed_lps_mm=local_stats["anterior_seed_lps_mm"]
    )
    # Compare the final local result with the source on all other anterior
    # components, excluding voxels the independent posterior cut must remove.
    posterior_offset, posterior_coefficients = _distance_coefficients(volume, posterior)
    unselected_changed = 0
    for start in range(0, working.shape[0], 12):
        stop = min(start + 12, working.shape[0])
        posterior_kept = _distances(
            posterior_offset, posterior_coefficients, start, stop, working.shape
        ) <= 1e-8
        protected = (positive[start:stop] & ~selected[start:stop]) & posterior_kept
        unselected_changed += int(np.count_nonzero(protected & ~retained[start:stop]))
    if unselected_changed:
        raise PipelineError("failed", "未选鼻孔在局部裁切中发生变化", ["停止使用当前端面方案"])
    retained_voxels = int(np.count_nonzero(retained))
    if not retained_voxels:
        raise PipelineError("blocked", "两张切面之间没有保留气道", ["调整切面位置与方向"])
    labels, retained_components = ndimage.label(
        retained, structure=ndimage.generate_binary_structure(3, 1)
    )
    if main_airway and retained_components != 1:
        raise PipelineError(
            "blocked", "局部鼻孔裁切后主气道不再连通",
            ["核对鼻孔与鼻咽之间的连通性并修正掩膜"],
            {"retained_components": int(retained_components)},
        )
    del labels
    reaches_inlet = bool(np.any(
        retained & ndimage.binary_dilation(selected, structure=ndimage.generate_binary_structure(3, 1))
    ))
    if not reaches_inlet or not _retained_component_touches_plane(retained, volume, posterior):
        raise PipelineError(
            "blocked", "局部入口与鼻咽出口不在有效保留气道上",
            ["检查目标鼻孔、鼻咽切面及掩膜连通性"],
        )
    # Include one voxel of the retained side so the estimate spans the plane.
    inlet_neighborhood = working & ndimage.binary_dilation(
        selected, structure=ndimage.generate_binary_structure(3, 1)
    )
    inlet_neighborhood |= selected
    anterior_area = _area_estimate(inlet_neighborhood, volume, anterior, posterior)
    posterior_area = _area_estimate(anterior_only, volume, posterior, anterior)
    if anterior_area < MIN_OPENING_AREA_MM2 or posterior_area < MIN_OPENING_AREA_MM2:
        raise PipelineError(
            "blocked", "局部鼻孔端或鼻咽端截面积过小",
            ["把两张切面移动到连续、清晰的气道段"],
            {
                "anterior_area_estimate_mm2": round(anterior_area, 3),
                "posterior_area_estimate_mm2": round(posterior_area, 3),
                "technical_minimum_mm2": MIN_OPENING_AREA_MM2,
            },
        )
    return {
        "anterior_plane": anterior,
        "posterior_plane": posterior,
        **local_stats,
        "source_voxels": int(np.count_nonzero(source)),
        "retained_voxels": retained_voxels,
        "retained_components": int(retained_components),
        "retained_volume_ml": round(retained_voxels * float(np.prod(volume.spacing)) / 1000.0, 3),
        "anterior_unselected_deleted_voxels": unselected_changed,
        "unselected_region_changed_voxels": unselected_changed,
        "anterior_area_estimate_mm2": round(anterior_area, 3),
        "posterior_area_estimate_mm2": round(posterior_area, 3),
    }


def _assert_outer_inlet_branches_covered(
    source: np.ndarray, volume, planes: dict, deleted: dict[str, np.ndarray],
    positives: dict[str, np.ndarray],
) -> None:
    """Reject an unselected outer anterior branch posing as the opposite nostril.

    A third, medial branch may be intentionally preserved by a local cut.
    This technical check does not establish anatomic left/right identity.
    """
    axes_mm = np.asarray(volume.axis_dirs, dtype=float) * np.asarray(volume.spacing, dtype=float)[:, None]
    voxel_volume = float(np.prod(volume.spacing))
    origin_x = float(volume.origin[0])
    for plane_side in ("left", "right"):
        positive = positives[plane_side]
        labels, crop, count = _label_positive_components(positive)
        if not count:
            continue
        retained_side = source & ~positive
        boundary = positive & ndimage.binary_dilation(
            retained_side, structure=ndimage.generate_binary_structure(3, 1)
        )
        cropped_boundary = boundary[crop]
        crossing = np.bincount(labels[cropped_boundary], minlength=count + 1)
        _offset, coefficients = _distance_coefficients(volume, planes[plane_side])
        voxel_section_area = voxel_volume / float(np.abs(coefficients).sum())
        significant = {
            label for label in range(1, count + 1)
            if crossing[label] * voxel_section_area >= MIN_OPENING_AREA_MM2
        }
        if not significant:
            continue
        owner_labels = {}
        for target in ("left", "right"):
            overlap = np.bincount(labels[deleted[target][crop]], minlength=count + 1)
            owner_labels[target] = significant & {
                label for label in range(1, count + 1) if overlap[label] > 0
            }
        if (len(owner_labels["left"]) > 1 or len(owner_labels["right"]) > 1
                or owner_labels["left"] & owner_labels["right"]):
            raise PipelineError(
                "blocked", "左右鼻孔在当前切面无法分别对应独立支段",
                ["把切面移到左右鼻孔各自独立的前端管段"],
                {"plane_side": plane_side, "crossing_branches": len(significant)},
            )
        unassigned = significant - owner_labels["left"] - owner_labels["right"]
        if not unassigned:
            continue
        if len(owner_labels["left"]) != 1 or len(owner_labels["right"]) != 1:
            unmatched = [side for side in ("left", "right") if len(owner_labels[side]) != 1]
            if len(unmatched) == 1 and not owner_labels[unmatched[0]]:
                side = unmatched[0]
                label = "左鼻孔" if side == "left" else "右鼻孔"
                selected_voxels = int(np.count_nonzero(deleted[side]))
                raise PipelineError(
                    "blocked",
                    f"{label}定位点选中 {selected_voxels} 个体素，未覆盖当前切面的显著前端支段",
                    [f"检查切面显示，在{label}完整主气道上重新选定位点后重试实际裁切预检"],
                    {"plane_side": plane_side, "unmatched_port": side,
                     "selected_port_voxels": selected_voxels,
                     "unassigned_branches": len(unassigned)},
                )
            raise PipelineError(
                "blocked", "额外前端支段无法与左右鼻孔可靠区分",
                ["分别调整左右鼻孔切面到可辨认的独立入口"],
                {"plane_side": plane_side, "unassigned_branches": len(unassigned)},
            )

        def section_x(label: int) -> float:
            indices = np.column_stack(np.where(cropped_boundary & (labels == label)))
            center = indices.mean(axis=0) + np.asarray([part.start for part in crop])
            return origin_x + float(center @ axes_mm[:, 0])

        selected_x = [section_x(next(iter(owner_labels[side]))) for side in ("left", "right")]
        low, high = min(selected_x), max(selected_x)
        tolerance = float(max(volume.spacing))
        outer = [label for label in unassigned
                 if section_x(label) < low - tolerance or section_x(label) > high + tolerance]
        if outer:
            raise PipelineError(
                "blocked", "左右鼻孔方案未覆盖外侧前端支段",
                ["检查是否把同一鼻孔的两块误选为左右鼻孔；先修正分割或调整切面"],
                {"plane_side": plane_side, "unselected_outer_branches": len(outer),
                 "unassigned_branches": len(unassigned), "technical_minimum_mm2": MIN_OPENING_AREA_MM2},
            )


def _bilateral_local_selection(
    mask: np.ndarray, volume, left_inlet_plane: dict, right_inlet_plane: dict,
    posterior_plane: dict, left_inlet_seed_lps_mm, right_inlet_seed_lps_mm,
    posterior_seed_lps_mm,
) -> tuple[dict, dict[str, np.ndarray], np.ndarray]:
    """Select three independent deletion-side voxel branches in patient LPS."""
    source = np.asarray(mask, dtype=bool)
    if source.shape != tuple(volume.shape) or not source.any():
        raise PipelineError("blocked", "双侧气道掩膜为空或与 CT 尺寸不一致", ["重新复核主气道掩膜"])
    planes = {
        "left": validate_plane_endpoint(left_inlet_plane, "anterior"),
        "right": validate_plane_endpoint(right_inlet_plane, "anterior"),
        "outlet": _bilateral_local_outlet_plane(posterior_plane, posterior_seed_lps_mm),
    }
    seeds = {
        "left": left_inlet_seed_lps_mm,
        "right": right_inlet_seed_lps_mm,
        "outlet": posterior_seed_lps_mm,
    }
    try:
        left_seed = np.asarray(left_inlet_seed_lps_mm, dtype=float)
        right_seed = np.asarray(right_inlet_seed_lps_mm, dtype=float)
    except (TypeError, ValueError) as exc:
        raise PipelineError("failed", "双侧鼻孔定位点无效", ["重新选择左右鼻孔定位点"]) from exc
    if (left_seed.shape != (3,) or right_seed.shape != (3,)
            or not np.all(np.isfinite(left_seed)) or not np.all(np.isfinite(right_seed))
            or left_seed[0] <= right_seed[0]):
        raise PipelineError(
            "blocked", "左右鼻孔定位点与患者 LPS 左右方向不一致",
            ["在左鼻孔选择 LPS X 较大的锚点，在右鼻孔选择 X 较小的锚点"],
        )
    anterior_axis = np.asarray(ANTERIOR_LPS, dtype=float)
    outlet_ap = float(np.dot(planes["outlet"]["point_lps_mm"], anterior_axis))
    if any(float(np.dot(planes[side]["point_lps_mm"], anterior_axis)) <= outlet_ap
           for side in ("left", "right")):
        raise PipelineError("blocked", "鼻孔切面必须位于鼻咽切面前方", ["调整三张局部切面后重试"])
    selected: dict[str, np.ndarray] = {}
    positives: dict[str, np.ndarray] = {}
    stats: dict[str, dict] = {}
    for side in ("left", "right", "outlet"):
        selected[side], positives[side], stats[side] = _local_delete_component(
            source, volume, planes[side], seeds[side],
            role="posterior" if side == "outlet" else "anterior",
        )
    if any(np.any(selected[first] & selected[second])
           for first, second in (("left", "right"), ("left", "outlet"), ("right", "outlet"))):
        raise PipelineError(
            "blocked", "三个局部切面选中的删除支段发生重叠",
            ["将左右鼻孔切面移到分叉前端，鼻咽切面移到独立出口管段"],
        )
    _assert_outer_inlet_branches_covered(source, volume, planes, selected, positives)
    retained = source.copy()
    for branch in selected.values():
        retained[branch] = False
    if not retained.any():
        raise PipelineError("blocked", "三处局部裁切后没有保留气道", ["调整切面和定位点"])
    return {"planes": planes, "seeds": seeds, "stats": stats}, selected, retained


def clip_mask_oblique_bilateral_local(
    mask: np.ndarray, volume, left_inlet_plane: dict, right_inlet_plane: dict,
    posterior_plane: dict, *, left_inlet_seed_lps_mm, right_inlet_seed_lps_mm,
    posterior_seed_lps_mm,
) -> np.ndarray:
    """Coarse effective mask for a double-inlet, single-outlet local cut."""
    _selection, _deleted, retained = _bilateral_local_selection(
        mask, volume, left_inlet_plane, right_inlet_plane, posterior_plane,
        left_inlet_seed_lps_mm, right_inlet_seed_lps_mm, posterior_seed_lps_mm,
    )
    return retained


def validate_bilateral_local_planes(
    mask: np.ndarray, volume, left_inlet_plane: dict, right_inlet_plane: dict,
    posterior_plane: dict, left_inlet_seed_lps_mm, right_inlet_seed_lps_mm,
    posterior_seed_lps_mm, *, main_airway: bool,
) -> dict:
    """Conservative voxel preflight; exact mesh must still prove three caps."""
    if not main_airway:
        raise PipelineError("blocked", "双侧局部端面仅支持主鼻腔气流模型", ["切换到主气道范围"])
    from .surface import select_largest_component

    source = np.asarray(mask, dtype=bool)
    if source.shape != tuple(volume.shape) or not source.any():
        raise PipelineError("blocked", "双侧气道掩膜为空或尺寸不符", ["重新复核掩膜"])
    working, _ = select_largest_component(source)
    selected_info, deleted, retained = _bilateral_local_selection(
        working, volume, left_inlet_plane, right_inlet_plane, posterior_plane,
        left_inlet_seed_lps_mm, right_inlet_seed_lps_mm, posterior_seed_lps_mm,
    )
    _labels, components = ndimage.label(
        retained, structure=ndimage.generate_binary_structure(3, 1)
    )
    if components != 1:
        raise PipelineError(
            "blocked", "双侧局部裁切后主气道不连通",
            ["核查左右鼻孔与鼻咽之间的掩膜连通性"],
            {"retained_components": int(components)},
        )
    areas: dict[str, float] = {}
    for side in ("left", "right", "outlet"):
        if not np.any(retained & ndimage.binary_dilation(
            deleted[side], structure=ndimage.generate_binary_structure(3, 1)
        )):
            raise PipelineError(
                "blocked", f"{side}端面没有接通保留气道",
                ["把切面移到连续且独立的管段，并核对掩膜"],
            )
        neighborhood = working & ndimage.binary_dilation(
            deleted[side], structure=ndimage.generate_binary_structure(3, 1)
        )
        neighborhood |= deleted[side]
        areas[side] = _area_estimate(neighborhood, volume, selected_info["planes"][side], None)
        if areas[side] < MIN_OPENING_AREA_MM2:
            raise PipelineError(
                "blocked", f"{side}局部端面面积过小",
                ["沿目标气道移动切面后重新预检"],
                {"area_estimate_mm2": round(areas[side], 3),
                 "technical_minimum_mm2": MIN_OPENING_AREA_MM2},
            )
    return {
        "left_inlet_plane": selected_info["planes"]["left"],
        "right_inlet_plane": selected_info["planes"]["right"],
        "posterior_plane": selected_info["planes"]["outlet"],
        "left_inlet_seed_lps_mm": selected_info["stats"]["left"]["anterior_seed_lps_mm"],
        "right_inlet_seed_lps_mm": selected_info["stats"]["right"]["anterior_seed_lps_mm"],
        "posterior_seed_lps_mm": selected_info["stats"]["outlet"]["anterior_seed_lps_mm"],
        "left_inlet_area_estimate_mm2": round(areas["left"], 3),
        "right_inlet_area_estimate_mm2": round(areas["right"], 3),
        "posterior_area_estimate_mm2": round(areas["outlet"], 3),
        "left_deleted_voxels": int(np.count_nonzero(deleted["left"])),
        "right_deleted_voxels": int(np.count_nonzero(deleted["right"])),
        "posterior_deleted_voxels": int(np.count_nonzero(deleted["outlet"])),
        "source_voxels": int(np.count_nonzero(source)),
        "retained_voxels": int(np.count_nonzero(retained)),
        "retained_components": int(components),
        "retained_volume_ml": round(float(np.count_nonzero(retained)) * float(np.prod(volume.spacing)) / 1000.0, 3),
    }
