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


def _area_estimate(mask: np.ndarray, volume, plane: dict, other: dict) -> float:
    """平面附近体素的线性加权截面积；只用于保存前技术初筛。"""
    offset, coefficients = _distance_coefficients(volume, plane)
    other_offset, other_coefficients = _distance_coefficients(volume, other)
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
            ["在 3D 视窗翻转或旋转切面；箭头应指向准备删除的一侧"],
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
