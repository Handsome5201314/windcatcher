"""人工复核掩膜的导入、确认与下游失效处理。"""

from __future__ import annotations

import json
import math
import os
import secrets
import shutil
from datetime import datetime
from pathlib import Path
from collections.abc import Mapping, Sequence

import numpy as np
from scipy import ndimage

from .cases import case_dir, get_case, invalidate_downstream, update_stage
from .errors import PipelineError
from .volume import anterior_sign, load_volume

CORRECTED_MASK_REL = "review/airway_corrected_mask.nrrd"
EDIT_VIEW_AXES = {"axial": 0, "coronal": 1, "sagittal": 2}
EDIT_ACTIONS = {"paint", "erase", "fill_slice"}
MAX_EDIT_POINTS = 256
MAX_EDIT_RADIUS = 64

# 自动去外部空气只使用 CT 灰度建立一个保守的体内包络。这个工具仍然
# 要求人工复核，阈值和闭运算半径都会写入 segmentation.json，便于追溯。
# 包络阈值必须与气道候选的 HU 上限一致，避免 -500..-300 HU 的皮肤
# 部分容积薄层同时被当作“空气候选”和“身体内部”。调用方可传入自定义上限。
EXTERNAL_AIR_BODY_HU = -300.0
EXTERNAL_AIR_CLOSURE_MM = 3.0
EXTERNAL_AIR_MIN_RETAINED_VOXELS = 5_000

# ``mask.npy`` 是主鼻腔范围，``candidate_mask.npy`` 是完整候选范围。
# 浏览器复核同时作用于两者，避免用户在 CT 里看到的是一个范围、
# STL 阶段却悄悄使用另一个范围。以下两个文件只作为可回滚的初始副本。
MAIN_MASK_BACKUP_NAME = "mask_candidate.npy"
FULL_MASK_BACKUP_NAME = "candidate_mask_original.npy"
PRE_CLEAN_MAIN_MASK_NAME = "mask_before_external_air.npy"
PRE_CLEAN_FULL_MASK_NAME = "candidate_before_external_air.npy"


def external_air_cleanup_pending(segmentation: dict) -> bool:
    cleanup = segmentation.get("candidate_cleaning")
    return isinstance(cleanup, dict) and cleanup.get("status") == "pending_manual"


def has_current_external_air_validation(segmentation: dict) -> bool:
    """检查当前掩膜版本是否通过与空气阈值一致的体外空气复核。"""
    if external_air_cleanup_pending(segmentation):
        return False
    validation = segmentation.get("external_air_validation")
    version = segmentation.get("mask_version")
    if not isinstance(validation, dict) or type(version) is not int:
        return False
    if type(validation.get("mask_version")) is not int or validation["mask_version"] != version:
        return False
    if any(
        type(validation.get(name)) is not int or validation[name] != 0
        for name in ("outside_voxels", "candidate_outside_voxels")
    ):
        return False
    params = segmentation.get("params")
    if not isinstance(params, dict):
        return False
    body_threshold = validation.get("body_hu_threshold")
    air_threshold = params.get("hu_high", EXTERNAL_AIR_BODY_HU)
    if type(body_threshold) not in (int, float) or type(air_threshold) not in (int, float):
        return False
    return math.isfinite(body_threshold) and math.isfinite(air_threshold) and body_threshold >= air_threshold


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _direction_for_sitk(axis_dirs) -> tuple[float, ...]:
    """把数组轴方向 (z,y,x) 转为 SimpleITK 的 x/y/z 方向矩阵。"""
    x_axis, y_axis, z_axis = axis_dirs[2], axis_dirs[1], axis_dirs[0]
    return tuple(float(axis[row]) for row in range(3) for axis in (x_axis, y_axis, z_axis))


def _read_volume_geometry(case_id: str) -> dict:
    path = case_dir(case_id) / "volume" / "meta.json"
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError("blocked", "还没有可用于校验掩膜的 CT 几何信息", ["先完成体数据导入与输入质控"]) from exc
    required = ("shape", "spacing_mm", "origin_mm", "axis_dirs")
    if any(key not in meta for key in required):
        raise PipelineError("failed", "CT 几何信息不完整，无法安全导入掩膜", ["重新导入该病例后再试"])
    return meta


def _load_segmentation(case_id: str) -> tuple[Path, dict]:
    seg_path = case_dir(case_id) / "segment" / "segmentation.json"
    try:
        segmentation = json.loads(seg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError("blocked", "还没有可编辑的气道掩膜记录", ["先完成气道分割，再打开手动编辑工具"]) from exc
    if not isinstance(segmentation, dict):
        raise PipelineError("failed", "气道掩膜记录格式无效", ["重新执行气道分割后再试"])
    return seg_path, segmentation


def _load_mask_array(path: Path, expected_shape: tuple[int, int, int], label: str) -> np.ndarray:
    try:
        values = np.asarray(np.load(path))
    except (OSError, ValueError) as exc:
        raise PipelineError("failed", f"无法读取{label}", ["重新执行气道分割后再试"]) from exc
    if values.ndim != 3 or tuple(values.shape) != expected_shape:
        raise PipelineError(
            "failed",
            f"{label}尺寸 {list(values.shape)} 与 CT 尺寸 {list(expected_shape)} 不一致",
            ["重新执行气道分割，确保掩膜没有被重采样"],
        )
    if not np.isfinite(values).all() or np.any(values < 0):
        raise PipelineError("failed", f"{label}包含无效标签值", ["只保留背景 0 和气道标签（大于 0）"])
    return (values > 0).astype(np.uint8)


def _atomic_save_npy(path: Path, values: np.ndarray) -> None:
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with temp.open("wb") as handle:
            np.save(handle, values)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _ensure_edit_backups(
    root: Path,
    mask: np.ndarray,
    candidate: np.ndarray,
) -> None:
    """在第一次浏览器编辑前保存两个范围的副本，后续编辑不覆盖它们。"""
    main_backup = root / "segment" / MAIN_MASK_BACKUP_NAME
    full_backup = root / "segment" / FULL_MASK_BACKUP_NAME
    if not main_backup.exists():
        _atomic_save_npy(main_backup, np.asarray(mask, dtype=np.uint8))
    if not full_backup.exists():
        _atomic_save_npy(full_backup, np.asarray(candidate, dtype=np.uint8))


def _current_masks(
    root: Path,
    expected_shape: tuple[int, int, int],
) -> tuple[np.ndarray, np.ndarray]:
    """读取当前主气道与完整候选；旧病例没有候选时回退到主气道。"""
    mask_path = root / "segment" / "mask.npy"
    if not mask_path.exists():
        raise PipelineError("blocked", "还没有可编辑的气道掩膜", ["先完成气道分割，再打开手动编辑工具"])
    mask = _load_mask_array(mask_path, expected_shape, "当前气道掩膜")
    candidate_path = root / "segment" / "candidate_mask.npy"
    candidate = (
        _load_mask_array(candidate_path, expected_shape, "当前完整候选掩膜")
        if candidate_path.exists()
        else mask.copy()
    )
    return mask, candidate


def _view_shape(shape: tuple[int, int, int], view: str) -> tuple[int, int, int]:
    try:
        axis = EDIT_VIEW_AXES[view]
    except KeyError as exc:
        raise PipelineError("failed", f"未知视图：{view}", ["视图只能是 axial、coronal 或 sagittal"]) from exc
    remaining = [i for i in range(3) if i != axis]
    return int(shape[remaining[0]]), int(shape[remaining[1]]), axis


def _validate_edit_input(
    shape: tuple[int, int, int],
    view: str,
    index: int,
    action: str,
    points: Sequence[Mapping[str, object]],
    radius: int,
) -> tuple[int, int, int]:
    if isinstance(index, bool) or not isinstance(index, (int, np.integer)):
        raise PipelineError("failed", "切片层号必须是整数", ["请使用当前视图范围内的层号"])
    height, width, axis = _view_shape(shape, view)
    if int(index) < 0 or int(index) >= shape[axis]:
        raise PipelineError("failed", f"切片层号超出范围：{index}", [f"当前视图共有 {shape[axis]} 层"])
    if action not in EDIT_ACTIONS:
        raise PipelineError("failed", f"不支持的编辑动作：{action}", ["动作只能是 paint、erase 或 fill_slice"])
    if not isinstance(points, Sequence) or isinstance(points, (str, bytes)) or not points:
        raise PipelineError("failed", "至少需要一个编辑点", ["在 CT 切片上点击或圈选气道区域"])
    if len(points) > MAX_EDIT_POINTS:
        raise PipelineError("failed", f"编辑点数量不能超过 {MAX_EDIT_POINTS}", ["减少笔划点后再提交"])
    if isinstance(radius, bool) or not isinstance(radius, (int, np.integer)):
        raise PipelineError("failed", "笔刷半径必须是整数", [f"半径范围为 0 到 {MAX_EDIT_RADIUS} 像素"])
    if int(radius) < 0 or int(radius) > MAX_EDIT_RADIUS:
        raise PipelineError("failed", f"笔刷半径超出范围：{radius}", [f"半径范围为 0 到 {MAX_EDIT_RADIUS} 像素"])
    if action == "fill_slice" and len(points) < 3:
        raise PipelineError("failed", "填充切片至少需要 3 个多边形点", ["按顺序圈出需要保留的气道区域"])
    for point in points:
        if not isinstance(point, Mapping):
            raise PipelineError("failed", "编辑点格式无效", ["每个编辑点都应包含整数 x、y"])
        x, y = point.get("x"), point.get("y")
        if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, (int, np.integer)) or not isinstance(y, (int, np.integer)):
            raise PipelineError("failed", "编辑点坐标必须是整数", ["请使用原始 CT 切片像素坐标"])
        if int(x) < 0 or int(x) >= width or int(y) < 0 or int(y) >= height:
            raise PipelineError("failed", f"编辑点 ({x}, {y}) 超出当前切片范围", [f"当前切片尺寸为 {width} × {height} 像素"])
    return height, width, axis


def _slice_view(mask: np.ndarray, view: str, index: int) -> np.ndarray:
    axis = EDIT_VIEW_AXES[view]
    if axis == 0:
        return mask[index]
    if axis == 1:
        return mask[:, index, :]
    return mask[:, :, index]


def _paint_disks(target: np.ndarray, points: Sequence[Mapping[str, object]], radius: int, value: bool) -> None:
    for point in points:
        x, y = int(point["x"]), int(point["y"])
        y0, y1 = max(0, y - radius), min(target.shape[0] - 1, y + radius)
        x0, x1 = max(0, x - radius), min(target.shape[1] - 1, x + radius)
        yy, xx = np.ogrid[y0 : y1 + 1, x0 : x1 + 1]
        disk = (xx - x) ** 2 + (yy - y) ** 2 <= radius * radius
        target[y0 : y1 + 1, x0 : x1 + 1][disk] = 1 if value else 0


def _fill_polygon(target: np.ndarray, points: Sequence[Mapping[str, object]], value: bool) -> None:
    vertices = [(float(point["x"]), float(point["y"])) for point in points]
    min_y = max(0, int(min(y for _, y in vertices)))
    max_y = min(target.shape[0] - 1, int(max(y for _, y in vertices)))
    for y in range(min_y, max_y + 1):
        scan_y = y + 0.5
        intersections: list[float] = []
        for first, second in zip(vertices, vertices[1:] + vertices[:1]):
            x1, y1 = first
            x2, y2 = second
            if (y1 <= scan_y < y2) or (y2 <= scan_y < y1):
                intersections.append(x1 + (scan_y - y1) * (x2 - x1) / (y2 - y1))
        intersections.sort()
        for left, right in zip(intersections[0::2], intersections[1::2]):
            x0 = max(0, int(np.ceil(min(left, right))))
            x1 = min(target.shape[1] - 1, int(np.floor(max(left, right))))
            if x0 <= x1:
                target[y, x0 : x1 + 1] = 1 if value else 0


def _review_flags(mask: np.ndarray, ap_axis: int) -> dict:
    boundary_by_axis = [
        bool(mask[0].any() or mask[-1].any()),
        bool(mask[:, 0, :].any() or mask[:, -1, :].any()),
        bool(mask[:, :, 0].any() or mask[:, :, -1].any()),
    ]
    return {
        "mask_boundary_by_axis": boundary_by_axis,
        "external_air_suspected": any(
            touched for axis, touched in enumerate(boundary_by_axis) if axis != ap_axis
        ),
    }


def _disk_structure(radius: int) -> np.ndarray:
    """返回二维圆盘结构元素，供逐轴向切片建立体内包络。"""
    yy, xx = np.ogrid[-radius : radius + 1, -radius : radius + 1]
    return (xx * xx + yy * yy) <= radius * radius


def _body_envelope(
    hu: np.ndarray,
    spacing: tuple[float, float, float],
    body_hu: float = EXTERNAL_AIR_BODY_HU,
    closure_mm: float = EXTERNAL_AIR_CLOSURE_MM,
) -> tuple[np.ndarray, int]:
    """从 CT 建立保守的体内包络。

    轴 0 是应用中约定的轴向切片方向。每层先按软组织/骨阈值提取
    身体轮廓，再用毫米尺度的闭运算桥接皮肤和鼻孔处的小间隙，最后
    填充轮廓内部。这样候选掩膜在头部轮廓外的空气会被识别出来，鼻腔
    内部的低 HU 腔体仍位于包络内。此函数不修改磁盘文件。
    """
    if np.asarray(hu).ndim != 3:
        raise PipelineError("failed", "CT 体数据不是三维数组，无法清理体外空气", ["重新导入该病例的 DICOM 序列"])
    if len(spacing) != 3 or any(float(value) <= 0 for value in spacing):
        raise PipelineError("failed", "CT 体素间距无效，无法建立体内包络", ["重新导入该病例并完成输入质控"])
    # 只在轴 1/2 平面做形态学处理，避免按切片厚度误放大闭运算。
    in_plane = min(float(spacing[1]), float(spacing[2]))
    radius = max(1, int(np.ceil(float(closure_mm) / in_plane)))
    structure = _disk_structure(radius)
    body = np.asarray(hu > float(body_hu), dtype=bool)
    envelope = np.empty(body.shape, dtype=bool)
    for index in range(body.shape[0]):
        closed = ndimage.binary_closing(body[index], structure=structure)
        envelope[index] = ndimage.binary_fill_holes(closed)
    return envelope, radius


def _remove_external_air_array(
    mask: np.ndarray,
    hu: np.ndarray,
    spacing: tuple[float, float, float],
    ap_axis: int,
    *,
    body_hu: float = EXTERNAL_AIR_BODY_HU,
    closure_mm: float = EXTERNAL_AIR_CLOSURE_MM,
) -> tuple[np.ndarray, dict]:
    """从候选掩膜中移除体外空气，并返回可审计统计信息。

    体外空气不能只靠边界连通域删除，因为鼻腔本身通过鼻孔/鼻咽与
    体数据边界相连。这里先限制到 CT 身体包络，再裁掉非前后轴最外层
    的边界体素；包络内的连通组件全部保留，交给后续人工复核和精修。
    身体包络 HU 阈值应等于当前气道候选的上限，避免皮肤部分容积伪影
    被双方同时选中。
    若没有足够的内部体素可保留，函数会抛出 ``blocked``，调用方不会
    写入空或不可信的结果。
    """
    mask = np.asarray(mask, dtype=bool)
    hu = np.asarray(hu)
    if mask.ndim != 3 or hu.ndim != 3 or mask.shape != hu.shape:
        raise PipelineError(
            "failed",
            "候选掩膜与 CT 尺寸不一致，无法自动去除体外空气",
            ["重新执行气道分割，确保没有改变体数据尺寸"],
        )
    if isinstance(ap_axis, bool) or int(ap_axis) not in range(3):
        raise PipelineError("failed", "气道前后轴信息无效，无法自动去除体外空气", ["重新执行气道分割后再试"])
    ap_axis = int(ap_axis)
    original_voxels = int(mask.sum())
    if original_voxels == 0:
        raise PipelineError("blocked", "当前候选掩膜为空，无法自动去除体外空气", ["先完成气道分割"])

    envelope, radius = _body_envelope(hu, spacing, body_hu, closure_mm)
    inside = mask & envelope
    outside_voxels = int((mask & ~envelope).sum())

    # 统计包络内连通域，但不要按边界删除整个组件：鼻腔主腔与开口相连，
    # 这样做会把真正需要保留的主气道一起删掉。
    labels, components = ndimage.label(inside, structure=np.ones((3, 3, 3), dtype=bool))
    touching_non_ap = np.zeros(int(components) + 1, dtype=bool)
    for axis in range(3):
        if axis == ap_axis:
            continue
        if axis == 0:
            boundary = np.concatenate((labels[0].ravel(), labels[-1].ravel()))
        elif axis == 1:
            boundary = np.concatenate((labels[:, 0, :].ravel(), labels[:, -1, :].ravel()))
        else:
            boundary = np.concatenate((labels[:, :, 0].ravel(), labels[:, :, -1].ravel()))
        touching_non_ap[np.unique(boundary)] = True
    touching_non_ap[0] = False
    retained = inside.copy()
    boundary_trimmed = np.zeros_like(retained, dtype=bool)
    for axis in range(3):
        if axis == ap_axis:
            continue
        first = [slice(None)] * 3
        last = [slice(None)] * 3
        first[axis] = 0
        last[axis] = -1
        boundary_trimmed[tuple(first)] |= retained[tuple(first)]
        boundary_trimmed[tuple(last)] |= retained[tuple(last)]
    retained[boundary_trimmed] = False
    retained_voxels = int(retained.sum())

    detail = {
        "method": "ct_body_envelope_then_non_ap_boundary_components",
        "body_hu_threshold": float(body_hu),
        "closure_radius_mm": float(closure_mm),
        "closure_radius_pixels": int(radius),
        "candidate_voxels": original_voxels,
        "body_envelope_voxels": int(envelope.sum()),
        "inside_envelope_voxels": int(inside.sum()),
        "outside_envelope_voxels": outside_voxels,
        "components_inside_envelope": int(components),
        "components_touching_non_ap_boundary": int(np.count_nonzero(touching_non_ap)),
        "components_removed_at_non_ap_boundary": 0,
        "boundary_voxels_trimmed": int(boundary_trimmed.sum()),
        "retained_components": int(components),
        "retained_voxels": retained_voxels,
        "removed_voxels": int(original_voxels - retained_voxels),
        "removed_volume_ml": round(float(original_voxels - retained_voxels) * float(np.prod(spacing)) / 1000.0, 3),
    }
    if retained_voxels < EXTERNAL_AIR_MIN_RETAINED_VOXELS:
        raise PipelineError(
            "blocked",
            "自动去除体外空气后没有足够的内部气道区域，未修改当前掩膜",
            ["请使用画笔/橡皮在三平面视图中手动精修", "确认青色区域只覆盖鼻腔内部后再保存"],
            detail,
        )

    before_flags = _review_flags(mask, ap_axis)
    after_flags = _review_flags(retained, ap_axis)
    detail.update(
        {
            "boundary_by_axis_before": before_flags["mask_boundary_by_axis"],
            "boundary_by_axis_after": after_flags["mask_boundary_by_axis"],
            "external_air_suspected_after": after_flags["external_air_suspected"],
            "quality": "review_required",
        }
    )
    # 若清理后仍然保留非前后轴边界，继续自动删除会有误删主气道风险；
    # 返回当前安全结果并明确要求人工复核，而不是声称已经完全清理。
    return retained.astype(np.uint8), detail


def _outside_body_envelope_counts(
    mask: np.ndarray,
    candidate: np.ndarray,
    hu: np.ndarray,
    spacing: tuple[float, float, float],
    body_hu: float,
) -> tuple[int, int]:
    """确认前逐层检查两个掩膜是否仍落在目标体内包络之外。"""
    if mask.shape != candidate.shape or mask.shape != hu.shape:
        raise PipelineError("failed", "掩膜与 CT 尺寸不一致，无法检查体外空气", ["重新导入或重新分割该病例"])
    radius = max(1, int(np.ceil(EXTERNAL_AIR_CLOSURE_MM / min(float(spacing[1]), float(spacing[2])))))
    structure = _disk_structure(radius)
    main_outside = 0
    candidate_outside = 0
    for index in range(mask.shape[0]):
        body = hu[index] > body_hu
        envelope = ndimage.binary_fill_holes(ndimage.binary_closing(body, structure=structure))
        outside = ~envelope
        main_outside += int(np.count_nonzero(mask[index] & outside))
        candidate_outside += int(np.count_nonzero(candidate[index] & outside))
    return main_outside, candidate_outside


def _mask_metrics(mask: np.ndarray, spacing: tuple[float, float, float], ap_axis: int) -> dict:
    coords = np.where(mask)
    if not coords[0].size:
        raise PipelineError("failed", "修正掩膜没有气道体素", ["在 Slicer 中确认标签层已导出且气道区域非空"])
    voxel_volume = float(np.prod(spacing))
    bbox_mm = [round(float((axis_coords.max() - axis_coords.min() + 1) * spacing[i]), 2) for i, axis_coords in enumerate(coords)]
    ap_min = int(coords[ap_axis].min())
    ap_max = int(coords[ap_axis].max())

    def plane_area(plane: int) -> float:
        take = [slice(None)] * 3
        take[ap_axis] = slice(plane, plane + 1)
        inplane = [spacing[i] for i in range(3) if i != ap_axis]
        return float(mask[tuple(take)].sum()) * float(np.prod(inplane))

    return {
        "voxels": int(mask.sum()),
        "volume_ml": round(float(mask.sum()) * voxel_volume / 1000.0, 3),
        "bbox_mm": bbox_mm,
        "ap_length_mm": round(float((ap_max - ap_min + 1) * spacing[ap_axis]), 2),
        "anterior_opening_area_mm2": round(plane_area(ap_min), 2),
        "posterior_opening_area_mm2": round(plane_area(ap_max), 2),
    }


def _validate_mask(case_id: str, source_path: Path) -> tuple[np.ndarray, dict]:
    try:
        import SimpleITK as sitk
    except Exception as exc:  # pragma: no cover - 依赖页负责提示安装
        raise PipelineError("failed", f"无法读取修正掩膜：SimpleITK 不可用（{exc}）", ["在风见项目虚拟环境中安装 SimpleITK"]) from exc

    if source_path.suffix.lower() not in {".nrrd", ".nii", ".mha", ".mhd"} and not source_path.name.lower().endswith(".nii.gz"):
        raise PipelineError("failed", "修正掩膜格式不支持", ["请从 Slicer 导出 NRRD、NIfTI、MHA 或 MHD 标签图"])
    try:
        image = sitk.ReadImage(str(source_path))
        values = np.asarray(sitk.GetArrayFromImage(image))
    except Exception as exc:
        raise PipelineError("failed", f"无法读取修正掩膜：{exc}", ["确认导出的是标签图而不是 CT 灰度图"]) from exc

    geometry = _read_volume_geometry(case_id)
    expected_shape = tuple(int(v) for v in geometry["shape"])
    if values.ndim != 3 or tuple(values.shape) != expected_shape:
        raise PipelineError(
            "failed",
            f"修正掩膜尺寸 {list(values.shape)} 与 CT 尺寸 {list(expected_shape)} 不一致",
            ["在 Slicer 中使用当前病例导出的 CT 和候选掩膜编辑，不要改变体数据尺寸"],
        )
    if not np.isfinite(values).all() or np.any(values < 0):
        raise PipelineError("failed", "修正掩膜包含无效标签值", ["只保留背景 0 和气道标签（大于 0）"])
    unique = np.unique(values)
    if unique.size > 2:
        raise PipelineError("failed", "修正掩膜包含多个标签，无法判断哪个是气道", ["在 Slicer 中只保留气道标签后再导出"])
    mask = values > 0
    if not mask.any():
        raise PipelineError("failed", "修正掩膜为空", ["确认气道标签可见并重新导出"])

    expected_spacing = tuple(float(v) for v in geometry["spacing_mm"])[::-1]
    expected_origin = tuple(float(v) for v in geometry["origin_mm"])
    expected_direction = _direction_for_sitk(geometry["axis_dirs"])
    if not np.allclose(image.GetSpacing(), expected_spacing, rtol=0, atol=1e-3):
        raise PipelineError("failed", "修正掩膜与 CT 的体素间距不一致", ["不要在 Slicer 中重采样或改变当前病例的体数据几何"])
    if not np.allclose(image.GetOrigin(), expected_origin, rtol=0, atol=1e-2):
        raise PipelineError("failed", "修正掩膜与 CT 的原点不一致", ["使用风见导出的当前病例 CT 作为 Slicer 背景图"])
    if not np.allclose(image.GetDirection(), expected_direction, rtol=0, atol=1e-3):
        raise PipelineError("failed", "修正掩膜与 CT 的方向不一致", ["不要对 Slicer 中的标签图单独改变方向"])

    return mask.astype(bool), {"spacing": tuple(float(v) for v in geometry["spacing_mm"]), "shape": expected_shape}


def _replace_mask_and_record(
    mask_path: Path,
    mask_temp: Path,
    segmentation_path: Path,
    segmentation_temp: Path,
) -> None:
    """Replace the mask and its JSON record as one recoverable filesystem operation.

    There is no cross-file transaction on a normal filesystem. Keep both old files
    until both replacements succeed, and restore them if the second replacement
    fails. This prevents a new mask from being paired with stale segmentation
    metadata after an interrupted import.
    """
    token = secrets.token_hex(6)
    mask_backup = mask_path.with_name(f".{mask_path.name}.{token}.bak")
    segmentation_backup = segmentation_path.with_name(f".{segmentation_path.name}.{token}.bak")
    mask_moved = False
    segmentation_moved = False
    mask_installed = False
    segmentation_installed = False
    try:
        if mask_path.exists():
            os.replace(mask_path, mask_backup)
            mask_moved = True
        if segmentation_path.exists():
            os.replace(segmentation_path, segmentation_backup)
            segmentation_moved = True

        os.replace(mask_temp, mask_path)
        mask_installed = True
        os.replace(segmentation_temp, segmentation_path)
        segmentation_installed = True
    except Exception:
        # Remove any newly installed file before restoring the original pair.
        if mask_installed:
            mask_path.unlink(missing_ok=True)
        if segmentation_installed:
            segmentation_path.unlink(missing_ok=True)
        if mask_moved and mask_backup.exists():
            os.replace(mask_backup, mask_path)
        if segmentation_moved and segmentation_backup.exists():
            os.replace(segmentation_backup, segmentation_path)
        raise
    finally:
        mask_temp.unlink(missing_ok=True)
        segmentation_temp.unlink(missing_ok=True)
        mask_backup.unlink(missing_ok=True)
        segmentation_backup.unlink(missing_ok=True)


def _replace_masks_and_record(
    mask_path: Path,
    mask_temp: Path,
    candidate_path: Path,
    candidate_temp: Path,
    segmentation_path: Path,
    segmentation_temp: Path,
) -> None:
    """原子替换主气道、完整候选和登记文件。"""
    token = secrets.token_hex(6)
    paths = (
        (mask_path, mask_temp),
        (candidate_path, candidate_temp),
        (segmentation_path, segmentation_temp),
    )
    backups: list[tuple[Path, Path, bool]] = []
    installed: list[Path] = []
    try:
        for path, _temp in paths:
            backup = path.with_name(f".{path.name}.{token}.bak")
            moved = False
            if path.exists():
                os.replace(path, backup)
                moved = True
            backups.append((path, backup, moved))
        for path, temp in paths:
            os.replace(temp, path)
            installed.append(path)
    except Exception:
        for path in installed:
            path.unlink(missing_ok=True)
        for path, backup, moved in reversed(backups):
            if moved and backup.exists():
                os.replace(backup, path)
        raise
    finally:
        for _path, temp in paths:
            temp.unlink(missing_ok=True)
        for _path, backup, _moved in backups:
            backup.unlink(missing_ok=True)


def import_corrected_mask(case_id: str) -> dict:
    """读取 Slicer 导出的固定复核文件，并使旧下游产物失效。"""
    root = case_dir(case_id)
    source = root / CORRECTED_MASK_REL
    if not source.is_file():
        raise PipelineError(
            "blocked",
            "还没有找到修正掩膜",
            [
                "点击“高级复核”在 Slicer 中编辑气道标签",
                f"在 Slicer 中导出为病例目录下的 {CORRECTED_MASK_REL}",
                "回到网页再次点击“读取修正掩膜”",
            ],
        )

    mask, geometry = _validate_mask(case_id, source)
    seg_dir = root / "segment"
    seg_dir.mkdir(parents=True, exist_ok=True)
    seg_path = seg_dir / "segmentation.json"
    try:
        segmentation = json.loads(seg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError("failed", "原分割记录不存在或损坏，无法登记修正掩膜", ["先重新执行气道分割"]) from exc
    ap_axis = int(segmentation.get("params", {}).get("ap_axis", 1))
    current_mask, current_candidate = _current_masks(root, tuple(int(v) for v in geometry["shape"]))
    _ensure_edit_backups(root, current_mask, current_candidate)
    flags = _review_flags(mask, ap_axis)
    segmentation["need_human_confirm"] = True
    segmentation["review_flags"] = flags
    segmentation["candidate_review_flags"] = flags
    segmentation["metrics"] = _mask_metrics(mask, geometry["spacing"], ap_axis)
    segmentation["candidate_metrics"] = segmentation["metrics"]
    segmentation["human_review"] = {
        **segmentation.get("human_review", {}),
        "status": "已导入修正掩膜，待复核",
        "source": CORRECTED_MASK_REL,
        "imported_at": _now(),
    }
    segmentation["candidate_cleaning"] = {"status": "manual_import", "source": CORRECTED_MASK_REL}
    segmentation["candidate_mask_available"] = True
    segmentation.pop("external_air_validation", None)
    segmentation.pop("external_air_undo", None)
    segmentation.pop("external_air_cleanup", None)
    segmentation.pop("candidate_external_air_cleanup", None)
    segmentation["mask_source"] = "slicer_manual_correction"
    segmentation["mask_version"] = int(segmentation.get("mask_version", 0)) + 1

    mask_path = seg_dir / "mask.npy"
    candidate_path = seg_dir / "candidate_mask.npy"
    temp_path = seg_dir / f"mask.npy.{secrets.token_hex(4)}.tmp"
    candidate_temp = seg_dir / f"candidate_mask.npy.{secrets.token_hex(4)}.tmp"
    tmp_json = seg_path.with_name(f"{seg_path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with temp_path.open("wb") as handle:
            np.save(handle, mask.astype(np.uint8))
            handle.flush()
            os.fsync(handle.fileno())
        with candidate_temp.open("wb") as handle:
            np.save(handle, mask.astype(np.uint8))
            handle.flush()
            os.fsync(handle.fileno())
        with tmp_json.open("w", encoding="utf-8") as handle:
            json.dump(segmentation, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_masks_and_record(mask_path, temp_path, candidate_path, candidate_temp, seg_path, tmp_json)
    except Exception:
        temp_path.unlink(missing_ok=True)
        candidate_temp.unlink(missing_ok=True)
        tmp_json.unlink(missing_ok=True)
        raise

    invalidate_downstream(case_id, "修正掩膜已导入，需人工确认后重新生成")
    message = "修正掩膜已导入，等待人工确认"
    if flags["external_air_suspected"]:
        message = "修正掩膜仍触及体数据边界，请继续修正"
    update_stage(
        case_id,
        "segment",
        "warn",
        message=message,
        artifacts=["segment/mask.npy", "segment/candidate_mask.npy", "segment/segmentation.json", CORRECTED_MASK_REL],
        data={
            "volume_ml": segmentation["metrics"]["volume_ml"],
            "ap_length_mm": segmentation["metrics"]["ap_length_mm"],
            "need_human_confirm": True,
            "external_air_suspected": flags["external_air_suspected"],
            "mask_boundary_by_axis": flags["mask_boundary_by_axis"],
            "mask_source": "slicer_manual_correction",
            "mask_version": segmentation["mask_version"],
            "candidate_mask_available": True,
            "candidate_metrics": segmentation["candidate_metrics"],
            "candidate_cleaning": segmentation["candidate_cleaning"],
            "candidate_external_air_cleanup": None,
        },
    )
    return {"status": "imported", "message": message, "metrics": segmentation["metrics"], "review_flags": flags}


def _commit_mask_change(
    case_id: str,
    mask: np.ndarray,
    segmentation: dict,
    candidate: np.ndarray | None = None,
) -> None:
    """原子替换主气道、完整候选与登记文件。"""
    seg_dir = case_dir(case_id) / "segment"
    seg_dir.mkdir(parents=True, exist_ok=True)
    mask_path = seg_dir / "mask.npy"
    candidate_path = seg_dir / "candidate_mask.npy"
    seg_path = seg_dir / "segmentation.json"
    temp_path = seg_dir / f"mask.npy.{secrets.token_hex(4)}.tmp"
    candidate_temp = seg_dir / f"candidate_mask.npy.{secrets.token_hex(4)}.tmp"
    tmp_json = seg_path.with_name(f"{seg_path.name}.{secrets.token_hex(4)}.tmp")
    if candidate is None:
        candidate = np.asarray(mask, dtype=np.uint8)
    try:
        with temp_path.open("wb") as handle:
            np.save(handle, np.asarray(mask, dtype=np.uint8))
            handle.flush()
            os.fsync(handle.fileno())
        with candidate_temp.open("wb") as handle:
            np.save(handle, np.asarray(candidate, dtype=np.uint8))
            handle.flush()
            os.fsync(handle.fileno())
        with tmp_json.open("w", encoding="utf-8") as handle:
            json.dump(segmentation, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_masks_and_record(mask_path, temp_path, candidate_path, candidate_temp, seg_path, tmp_json)
    except Exception:
        temp_path.unlink(missing_ok=True)
        candidate_temp.unlink(missing_ok=True)
        tmp_json.unlink(missing_ok=True)
        raise


def _mask_change_result(case_id: str, segmentation: dict, message: str, action: str) -> dict:
    root = case_dir(case_id)
    ap_axis = int(segmentation.get("params", {}).get("ap_axis", 1))
    flags = segmentation.get("review_flags", {})
    candidate_flags = segmentation.get("candidate_review_flags", flags)
    candidate_path = root / "segment" / "candidate_mask.npy"
    candidate_available = bool(segmentation.get("candidate_mask_available", False)) and candidate_path.exists()
    candidate_metrics = segmentation.get("candidate_metrics", segmentation.get("metrics", {}))
    invalidate_downstream(case_id, message)
    update_stage(
        case_id,
        "segment",
        "warn",
        message=message,
        artifacts=["segment/mask.npy", "segment/candidate_mask.npy", "segment/segmentation.json"],
        data={
            "volume_ml": segmentation["metrics"]["volume_ml"],
            "ap_length_mm": segmentation["metrics"]["ap_length_mm"],
            "need_human_confirm": True,
            "external_air_suspected": bool(flags.get("external_air_suspected")),
            "candidate_external_air_suspected": bool(candidate_flags.get("external_air_suspected")),
            "mask_boundary_by_axis": flags.get("mask_boundary_by_axis", []),
            "candidate_mask_boundary_by_axis": candidate_flags.get("mask_boundary_by_axis", []),
            "mask_source": segmentation.get("mask_source", "browser_manual_edit"),
            "mask_version": segmentation.get("mask_version"),
            "last_edit_action": action,
            "candidate_mask_available": candidate_available,
            "candidate_metrics": candidate_metrics,
            "candidate_components_total": candidate_metrics.get("components_total"),
            "candidate_cleaning": segmentation.get("candidate_cleaning", {}),
            "candidate_external_air_cleanup": segmentation.get("candidate_external_air_cleanup"),
            "main_component_selection": segmentation.get("main_component_selection", {}),
        },
    )
    return {
        "status": "edited" if action not in {"reset", "rerun"} else "reset",
        "action": action,
        "message": message,
        "metrics": segmentation["metrics"],
        "review_flags": flags,
        "candidate_review_flags": candidate_flags,
        "candidate_metrics": candidate_metrics,
        "mask_version": segmentation.get("mask_version"),
        "need_human_confirm": True,
        "candidate_available": candidate_available,
    }


def edit_mask(
    case_id: str,
    view: str,
    index: int,
    action: str,
    points: Sequence[Mapping[str, object]],
    radius: int = 0,
) -> dict:
    """在当前 CT 切片上编辑掩膜，不接受整幅掩膜上传或任意文件路径。"""
    root = case_dir(case_id)
    geometry = _read_volume_geometry(case_id)
    expected_shape = tuple(int(v) for v in geometry["shape"])
    seg_path, segmentation = _load_segmentation(case_id)
    height, width, _ = _validate_edit_input(expected_shape, view, index, action, points, radius)
    mask, candidate = _current_masks(root, expected_shape)
    _ensure_edit_backups(root, mask, candidate)
    target = _slice_view(mask, view, int(index))
    candidate_target = _slice_view(candidate, view, int(index))
    if action == "fill_slice":
        _fill_polygon(target, points, True)
        _fill_polygon(candidate_target, points, True)
    else:
        _paint_disks(target, points, int(radius), action == "paint")
        _paint_disks(candidate_target, points, int(radius), action == "paint")

    ap_axis = int(segmentation.get("params", {}).get("ap_axis", 1))
    flags = _review_flags(mask, ap_axis)
    candidate_flags = _review_flags(candidate, ap_axis)
    segmentation["need_human_confirm"] = True
    segmentation["review_flags"] = flags
    segmentation["candidate_review_flags"] = candidate_flags
    segmentation["metrics"] = _mask_metrics(mask, tuple(float(v) for v in geometry["spacing_mm"]), ap_axis)
    segmentation["candidate_metrics"] = _mask_metrics(candidate, tuple(float(v) for v in geometry["spacing_mm"]), ap_axis)
    try:
        version = int(segmentation.get("mask_version", 0)) + 1
    except (TypeError, ValueError):
        version = 1
    segmentation["mask_version"] = version
    segmentation["mask_source"] = "browser_manual_edit"
    segmentation["human_review"] = {
        **segmentation.get("human_review", {}),
        "status": "浏览器手动编辑，待复核",
        "method": "风见三平面切片编辑",
        "last_action": action,
        "edited_at": _now(),
    }
    _commit_mask_change(case_id, mask, segmentation, candidate)
    message = "已保存切片编辑，气道掩膜需要重新人工复核"
    if flags["external_air_suspected"] or candidate_flags["external_air_suspected"]:
        message = "已保存切片编辑，但掩膜仍触及体数据边界，请继续修正"
    return _mask_change_result(case_id, segmentation, message, action)


def auto_remove_external_air(case_id: str) -> dict:
    """自动移除可识别的体外空气，并保留原候选掩膜供“重置”回滚。

    这是辅助复核操作，不代表已经完成医学意义上的分割确认；即使清理
    后不再触及边界，也始终把 ``need_human_confirm`` 保持为 ``True``。
    """
    root = case_dir(case_id)
    geometry = _read_volume_geometry(case_id)
    expected_shape = tuple(int(v) for v in geometry["shape"])
    seg_path, segmentation = _load_segmentation(case_id)
    mask, candidate = _current_masks(root, expected_shape)
    _ensure_edit_backups(root, mask, candidate)

    volume = load_volume(case_id)
    ap_axis = int(segmentation.get("params", {}).get("ap_axis", 1))
    body_hu = float(segmentation.get("params", {}).get("hu_high", EXTERNAL_AIR_BODY_HU))
    spacing = tuple(float(value) for value in geometry["spacing_mm"])
    cleaned_candidate, candidate_cleanup = _remove_external_air_array(
        candidate,
        volume.array,
        spacing,
        ap_axis,
        body_hu=body_hu,
    )
    cleaned_candidate_bool = cleaned_candidate.astype(bool)
    if candidate_cleanup["removed_voxels"] == 0 and not external_air_cleanup_pending(segmentation):
        return {
            "status": "unchanged",
            "message": "未发现本工具可清除的体外空气，掩膜未变化；请继续逐层复核",
            "candidate_available": bool(segmentation.get("candidate_mask_available", False)),
            "removed_voxels": 0,
            "candidate_removed_voxels": 0,
            "cleanup": {"removed_voxels": 0, "removed_volume_ml": 0.0},
            "candidate_cleanup": candidate_cleanup,
        }
    # The original main component may be the much larger outside-air region.
    # Select it afresh from the cleaned complete candidate, never from the raw
    # main mask. Keep all cleaned side branches in candidate_mask.npy.
    from .segmentation import _component_stats, _select_main_component_from_candidate, _structure, fill_enclosed_cavities

    params = segmentation.get("params", {})
    selected, selection = _select_main_component_from_candidate(
        cleaned_candidate_bool,
        spacing,
        ap_axis,
        anterior_sign(volume.meta, ap_axis),
        float(params.get("min_component_mm3", 200.0)),
        float(params.get("min_ap_coverage", 0.6)),
        float(params.get("max_anterior_gap_mm", 5.0)),
    )
    if not selected.any():
        raise PipelineError("blocked", "清理后没有可用的主气道连通域，原始掩膜未修改", ["检查 CT 候选范围后重试，或在三平面视图中手动修正"])
    cavity_info = {"cavities_filled": 0, "cavity_volume_mm3": 0.0, "max_cavity_mm3": float(params.get("max_cavity_mm3", 5.0))}
    if bool(params.get("fill_enclosed_cavities", True)) and external_air_cleanup_pending(segmentation) and not segmentation.get("mask_source"):
        selected, cavity_info = fill_enclosed_cavities(selected, spacing, float(params.get("max_cavity_mm3", 5.0)))
    cleaned_bool = selected.astype(bool)
    removed_main = int(np.count_nonzero(mask.astype(bool) & ~cleaned_bool))
    added_main = int(np.count_nonzero(cleaned_bool & ~mask.astype(bool)))
    cleanup = {
        "method": "main_reselected_from_clean_candidate",
        "candidate_voxels": int(mask.sum()),
        "retained_voxels": int(cleaned_bool.sum()),
        "removed_voxels": removed_main,
        "added_voxels": added_main,
        "removed_volume_ml": round(removed_main * float(np.prod(spacing)) / 1000.0, 3),
        "quality": "review_required",
    }
    flags = _review_flags(cleaned_bool, ap_axis)
    candidate_flags = _review_flags(cleaned_candidate_bool, ap_axis)
    previous_state = {
        "candidate_cleaning": segmentation.get("candidate_cleaning"),
        "external_air_cleanup": segmentation.get("external_air_cleanup"),
        "candidate_external_air_cleanup": segmentation.get("candidate_external_air_cleanup"),
        "candidate_metrics": segmentation.get("candidate_metrics"),
        "candidate_components": segmentation.get("candidate_components"),
        "main_component_selection": segmentation.get("main_component_selection"),
        "cavity_filling": segmentation.get("cavity_filling"),
        "params": dict(params),
    }
    segmentation["need_human_confirm"] = True
    segmentation["review_flags"] = flags
    segmentation["candidate_review_flags"] = candidate_flags
    segmentation["metrics"] = _mask_metrics(cleaned_bool, spacing, ap_axis)
    segmentation["candidate_metrics"] = _mask_metrics(cleaned_candidate_bool, spacing, ap_axis)
    candidate_labels, candidate_count = ndimage.label(
        cleaned_candidate_bool, structure=_structure(int(params.get("connectivity", 3)))
    )
    candidate_components = _component_stats(candidate_labels, candidate_count, spacing)
    segmentation["candidate_components"] = candidate_components
    segmentation["candidate_metrics"].update({
        "components_total": int(candidate_count),
        "largest_component_voxels": int(candidate_components[0]["voxels"]) if candidate_components else 0,
        "largest_component_volume_ml": round(float(candidate_components[0]["volume_mm3"]) / 1000.0, 3) if candidate_components else 0.0,
    })
    segmentation["candidate_mask_available"] = True
    segmentation["main_component_selection"] = selection
    segmentation["cavity_filling"] = cavity_info
    main_ap = np.flatnonzero(np.any(cleaned_bool, axis=tuple(axis for axis in range(3) if axis != ap_axis)))
    params["anterior_plane"] = int(main_ap.min())
    params["posterior_plane"] = int(main_ap.max())
    segmentation["candidate_cleaning"] = {"status": "ok", "source": "manual_button", **candidate_cleanup}
    segmentation["external_air_cleanup"] = cleanup
    segmentation["candidate_external_air_cleanup"] = candidate_cleanup
    segmentation["mask_source"] = "browser_external_air_cleanup"
    segmentation["human_review"] = {
        **segmentation.get("human_review", {}),
        "status": "已自动去除可识别体外空气，待人工复核",
        "method": "CT 体内包络 + 非前后轴边界残留清理",
        "last_action": "remove_external_air",
        "edited_at": _now(),
    }
    try:
        version = int(segmentation.get("mask_version", 0)) + 1
    except (TypeError, ValueError):
        version = 1
    seg_dir = root / "segment"
    _atomic_save_npy(seg_dir / PRE_CLEAN_MAIN_MASK_NAME, mask)
    _atomic_save_npy(seg_dir / PRE_CLEAN_FULL_MASK_NAME, candidate)
    segmentation["external_air_undo"] = {"after_version": version, "before_version": version - 1, "before_state": previous_state}
    segmentation.pop("external_air_validation", None)
    segmentation["mask_version"] = version
    _commit_mask_change(case_id, cleaned_bool, segmentation, cleaned_candidate_bool)

    message = "已更新体外空气清理候选；请重新核对鼻孔、鼻咽端面及鼻窦，再确认气道范围"
    if flags["external_air_suspected"] or candidate_flags["external_air_suspected"]:
        message = "已移除包络外体外空气，但掩膜仍触及非前后边界，请继续人工精修"
    result = _mask_change_result(case_id, segmentation, message, "remove_external_air")
    result["cleanup"] = cleanup
    result["candidate_cleanup"] = candidate_cleanup
    result["removed_voxels"] = cleanup["removed_voxels"]
    result["review_flags"] = flags
    result["candidate_review_flags"] = candidate_flags
    result["candidate_removed_voxels"] = candidate_cleanup["removed_voxels"]
    return result


def undo_external_air(case_id: str) -> dict:
    """只撤销紧接着的那次自动清理，保留此前的手动编辑。"""
    root = case_dir(case_id)
    geometry = _read_volume_geometry(case_id)
    expected_shape = tuple(int(v) for v in geometry["shape"])
    _seg_path, segmentation = _load_segmentation(case_id)
    undo = segmentation.get("external_air_undo", {})
    if undo.get("after_version") != segmentation.get("mask_version"):
        raise PipelineError("blocked", "当前掩膜在自动清理后已再次改变，不能直接撤销", ["请使用重置候选，或继续人工编辑当前掩膜"])
    seg_dir = root / "segment"
    main_path = seg_dir / PRE_CLEAN_MAIN_MASK_NAME
    candidate_path = seg_dir / PRE_CLEAN_FULL_MASK_NAME
    if not main_path.exists() or not candidate_path.exists():
        raise PipelineError("blocked", "找不到本次清理前的掩膜副本", ["请使用重置候选，或继续人工编辑当前掩膜"])
    mask = _load_mask_array(main_path, expected_shape, "清理前主气道掩膜")
    candidate = _load_mask_array(candidate_path, expected_shape, "清理前完整候选掩膜")
    spacing = tuple(float(value) for value in geometry["spacing_mm"])
    ap_axis = int(segmentation.get("params", {}).get("ap_axis", 1))
    segmentation["metrics"] = _mask_metrics(mask, spacing, ap_axis)
    segmentation["candidate_metrics"] = _mask_metrics(candidate, spacing, ap_axis)
    before_state = undo.get("before_state")
    if isinstance(before_state, dict):
        for name in (
            "candidate_cleaning",
            "external_air_cleanup",
            "candidate_external_air_cleanup",
            "candidate_components",
            "main_component_selection",
            "cavity_filling",
        ):
            if name in before_state:
                if before_state[name] is None:
                    segmentation.pop(name, None)
                else:
                    segmentation[name] = before_state[name]
        if isinstance(before_state.get("candidate_metrics"), dict):
            segmentation["candidate_metrics"].update({
                key: before_state["candidate_metrics"][key]
                for key in ("components_total", "largest_component_voxels", "largest_component_volume_ml")
                if key in before_state["candidate_metrics"]
            })
        if isinstance(before_state.get("params"), dict):
            segmentation["params"] = before_state["params"]
    elif segmentation.get("candidate_source") == "threshold_window_cropped_uncleaned":
        segmentation["candidate_cleaning"] = {
            "status": "pending_manual",
            "method": "ct_body_envelope_then_non_ap_boundary_components",
            "reason": "等待用户点击自动去除体外空气",
        }
        segmentation.pop("external_air_cleanup", None)
        segmentation.pop("candidate_external_air_cleanup", None)
    segmentation["review_flags"] = _review_flags(mask, ap_axis)
    segmentation["candidate_review_flags"] = _review_flags(candidate, ap_axis)
    segmentation["need_human_confirm"] = True
    segmentation["mask_source"] = "browser_external_air_undo"
    segmentation["mask_version"] = int(segmentation["mask_version"]) + 1
    segmentation["human_review"] = {
        **segmentation.get("human_review", {}),
        "status": "已撤销自动清理，待人工复核",
        "last_action": "undo_external_air",
        "edited_at": _now(),
    }
    segmentation.pop("external_air_undo", None)
    segmentation.pop("external_air_validation", None)
    _commit_mask_change(case_id, mask, segmentation, candidate)
    return _mask_change_result(case_id, segmentation, "已恢复自动清理前的掩膜，请重新复核", "undo_external_air")


def reset_mask(case_id: str) -> dict:
    """恢复首次编辑前的主气道与完整候选掩膜。"""
    root = case_dir(case_id)
    geometry = _read_volume_geometry(case_id)
    expected_shape = tuple(int(v) for v in geometry["shape"])
    mask_path = root / "segment" / "mask.npy"
    candidate_path = root / "segment" / "candidate_mask.npy"
    main_backup = root / "segment" / MAIN_MASK_BACKUP_NAME
    full_backup = root / "segment" / FULL_MASK_BACKUP_NAME
    if not mask_path.exists():
        from . import segmentation

        try:
            payload = segmentation.run_segment(case_id, {})
        except PipelineError:
            raise
        return {
            "status": "reset",
            "action": "rerun",
            "message": "没有保存的候选副本，已重新生成候选气道掩膜，请人工复核",
            "metrics": payload.get("metrics", {}),
            "review_flags": payload.get("review_flags", {}),
            "mask_version": payload.get("mask_version"),
            "need_human_confirm": True,
            "candidate_available": bool((root / "segment" / "candidate_mask.npy").exists()),
        }

    seg_path, segmentation = _load_segmentation(case_id)
    current_mask, current_candidate = _current_masks(root, expected_shape)
    _ensure_edit_backups(root, current_mask, current_candidate)
    if not main_backup.exists():
        raise PipelineError("blocked", "没有保存可恢复的初始主气道掩膜", ["重新执行气道分割后再试"])
    mask = _load_mask_array(main_backup, expected_shape, "初始主气道掩膜")
    if full_backup.exists():
        candidate = _load_mask_array(full_backup, expected_shape, "初始完整候选掩膜")
    else:
        candidate = _load_mask_array(candidate_path, expected_shape, "当前完整候选掩膜") if candidate_path.exists() else mask.copy()
    ap_axis = int(segmentation.get("params", {}).get("ap_axis", 1))
    flags = _review_flags(candidate, ap_axis)
    main_flags = _review_flags(mask, ap_axis)
    segmentation["need_human_confirm"] = True
    segmentation["review_flags"] = main_flags
    segmentation["candidate_review_flags"] = flags
    segmentation["metrics"] = _mask_metrics(mask, tuple(float(v) for v in geometry["spacing_mm"]), ap_axis)
    segmentation["candidate_metrics"] = _mask_metrics(candidate, tuple(float(v) for v in geometry["spacing_mm"]), ap_axis)
    from .segmentation import _component_stats, _structure

    spacing = tuple(float(v) for v in geometry["spacing_mm"])
    candidate_labels, candidate_count = ndimage.label(
        candidate.astype(bool), structure=_structure(int(segmentation.get("params", {}).get("connectivity", 3)))
    )
    candidate_components = _component_stats(candidate_labels, candidate_count, spacing)
    segmentation["candidate_components"] = candidate_components
    segmentation["candidate_metrics"].update({
        "components_total": int(candidate_count),
        "largest_component_voxels": int(candidate_components[0]["voxels"]) if candidate_components else 0,
        "largest_component_volume_ml": round(float(candidate_components[0]["volume_mm3"]) / 1000.0, 3) if candidate_components else 0.0,
    })
    segmentation.pop("external_air_cleanup", None)
    segmentation.pop("candidate_external_air_cleanup", None)
    segmentation.pop("external_air_undo", None)
    segmentation.pop("external_air_validation", None)
    if segmentation.get("candidate_source") == "threshold_window_cropped_uncleaned":
        segmentation["candidate_cleaning"] = {
            "status": "pending_manual",
            "method": "ct_body_envelope_then_non_ap_boundary_components",
            "reason": "等待用户点击自动去除体外空气",
        }
        segmentation["main_component_selection"] = {
            "method": "raw_threshold_component",
            "status": "pending_manual",
            "chosen_component": segmentation.get("chosen_component"),
        }
        segmentation["cavity_filling"] = {
            "status": "pending_manual",
            "cavities_filled": 0,
            "cavity_volume_mm3": 0.0,
            "max_cavity_mm3": float(segmentation.get("params", {}).get("max_cavity_mm3", 5.0)),
        }
        main_ap = np.flatnonzero(np.any(mask.astype(bool), axis=tuple(axis for axis in range(3) if axis != ap_axis)))
        segmentation["params"]["anterior_plane"] = int(main_ap.min())
        segmentation["params"]["posterior_plane"] = int(main_ap.max())
    elif segmentation.get("candidate_source") == "threshold_window_cropped_and_body_envelope_cleaned":
        segmentation["candidate_cleaning"] = {"status": "ok", "source": "reset_initial_clean_candidate"}
    try:
        version = int(segmentation.get("mask_version", 0)) + 1
    except (TypeError, ValueError):
        version = 1
    segmentation["mask_version"] = version
    segmentation["mask_source"] = "mask_candidate_reset"
    segmentation["human_review"] = {
        **segmentation.get("human_review", {}),
        "status": "已恢复候选掩膜，待复核",
        "method": "恢复自动分割候选",
        "reset_at": _now(),
    }
    _commit_mask_change(case_id, mask, segmentation, candidate)
    message = "已恢复自动分割候选掩膜，请重新检查三平面边界"
    if main_flags["external_air_suspected"] or flags["external_air_suspected"]:
        message = "已恢复候选掩膜，但仍疑似包含体外空气，请继续修正"
    return _mask_change_result(case_id, segmentation, message, "reset")


def export_mask(case_id: str, export_format: str = "nrrd", scope: str = "full_candidate") -> Path:
    """导出指定范围掩膜；默认导出完整候选，避免复核时漏掉鼻窦/旁支。"""
    root = case_dir(case_id)
    geometry = _read_volume_geometry(case_id)
    _seg_path, segmentation = _load_segmentation(case_id)
    if external_air_cleanup_pending(segmentation):
        raise PipelineError("blocked", "完整候选尚未清理体外空气，不能导出掩膜", ["先点击“自动去除体外空气”，再逐层复核并确认气道范围"])
    normalized_scope = str(scope).strip().lower()
    if normalized_scope not in {"full_candidate", "main_airway"}:
        raise PipelineError("failed", f"不支持的导出范围：{scope}", ["导出范围只能是 full_candidate 或 main_airway"])
    expected_shape = tuple(int(v) for v in geometry["shape"])
    candidate_path = root / "segment" / "candidate_mask.npy"
    mask_path = candidate_path if normalized_scope == "full_candidate" else root / "segment" / "mask.npy"
    if normalized_scope == "full_candidate" and not bool(segmentation.get("candidate_mask_available", False)):
        raise PipelineError("blocked", "当前病例没有可导出的完整候选掩膜", ["重新执行气道分割并完成体内空气清理", "或选择主鼻腔范围导出"])
    if not mask_path.exists():
        raise PipelineError("blocked", "还没有可导出的气道掩膜", ["先完成气道分割或手动编辑"])
    mask = _load_mask_array(mask_path, expected_shape, "当前气道掩膜")
    try:
        version = int(segmentation.get("mask_version", 0))
    except (TypeError, ValueError):
        version = 0
    output_dir = root / "review"
    output_dir.mkdir(parents=True, exist_ok=True)
    normalized = export_format.lower().strip()
    if normalized == "npy":
        output = output_dir / f"airway_{normalized_scope}_mask_v{version}.npy"
        _atomic_save_npy(output, mask)
        return output
    if normalized != "nrrd":
        raise PipelineError("failed", f"不支持的导出格式：{export_format}", ["导出格式只能是 nrrd 或 npy"])
    try:
        import SimpleITK as sitk
    except Exception as exc:
        raise PipelineError(
            "failed",
            f"无法导出 NRRD：SimpleITK 不可用（{exc}）",
            ["安装 SimpleITK 后重试", "或将导出格式改为 npy 下载"],
        ) from exc
    image = sitk.GetImageFromArray(mask.astype(np.uint8))
    image.SetSpacing(tuple(float(v) for v in geometry["spacing_mm"])[::-1])
    image.SetOrigin(tuple(float(v) for v in geometry["origin_mm"]))
    image.SetDirection(_direction_for_sitk(geometry["axis_dirs"]))
    output = output_dir / f"airway_{normalized_scope}_mask_v{version}.nrrd"
    temp = output_dir / f".{output.name}.{secrets.token_hex(4)}.tmp.nrrd"
    try:
        sitk.WriteImage(image, str(temp), useCompression=False)
        os.replace(temp, output)
    finally:
        temp.unlink(missing_ok=True)
    return output


def confirm_mask(case_id: str) -> dict:
    """记录用户逐层检查通过；有体外空气风险时拒绝确认。"""
    root = case_dir(case_id)
    seg_path = root / "segment" / "segmentation.json"
    mask_path = root / "segment" / "mask.npy"
    candidate_path = root / "segment" / "candidate_mask.npy"
    if not seg_path.exists() or not mask_path.exists():
        raise PipelineError("blocked", "还没有可确认的气道掩膜", ["先完成气道分割"])
    segmentation = json.loads(seg_path.read_text(encoding="utf-8"))
    if external_air_cleanup_pending(segmentation):
        raise PipelineError(
            "blocked",
            "尚未执行体外空气清理，不能确认气道范围",
            ["先点击“自动去除体外空气”，再逐层复核鼻孔、鼻咽及鼻窦"],
        )
    mask = np.load(mask_path, mmap_mode="r")
    candidate = np.load(candidate_path, mmap_mode="r") if candidate_path.exists() else mask
    ap_axis = int(segmentation.get("params", {}).get("ap_axis", 1))
    flags = _review_flags(mask, ap_axis)
    candidate_flags = _review_flags(candidate, ap_axis)
    if flags["external_air_suspected"] or candidate_flags["external_air_suspected"]:
        raise PipelineError(
            "blocked",
            "当前掩膜仍疑似包含体外空气，不能确认",
            ["先在高级复核工具中修正掩膜并导入", "确认三平面 CT 中青色区域只落在鼻腔内部后再试"],
            {"stage": "segment", "review_flags": flags, "candidate_review_flags": candidate_flags},
        )
    volume = load_volume(case_id)
    body_hu = float(segmentation.get("params", {}).get("hu_high", EXTERNAL_AIR_BODY_HU))
    main_outside, candidate_outside = _outside_body_envelope_counts(
        mask, candidate, volume.array, volume.spacing, body_hu
    )
    if main_outside or candidate_outside:
        raise PipelineError(
            "blocked",
            "掩膜仍有贴近头部外缘或体外的候选空气，不能确认",
            ["先使用“自动去除体外空气”，再逐层复核鼻孔、鼻咽和鼻窦", "如清理误删真实气道，撤销本次清理后用画笔精修"],
            {"main_outside_voxels": main_outside, "candidate_outside_voxels": candidate_outside},
        )
    review = dict(segmentation.get("human_review", {}))
    review.update({"status": "已确认", "confirmed_at": _now(), "method": "风见三平面 CT 逐层复核"})
    segmentation["human_review"] = review
    segmentation["need_human_confirm"] = False
    segmentation["review_flags"] = flags
    segmentation["candidate_review_flags"] = candidate_flags
    segmentation["external_air_validation"] = {
        "mask_version": int(segmentation.get("mask_version", 0)),
        "body_hu_threshold": body_hu,
        "outside_voxels": main_outside,
        "candidate_outside_voxels": candidate_outside,
    }
    invalidate_downstream(case_id, "气道掩膜已人工确认，需重新生成下游产物")
    tmp_json = seg_path.with_name(f"{seg_path.name}.{secrets.token_hex(4)}.tmp")
    try:
        tmp_json.write_text(json.dumps(segmentation, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_json.replace(seg_path)
    finally:
        tmp_json.unlink(missing_ok=True)
    next_step = (
        "可以生成 STL 与几何质检"
        if get_case(case_id).get("synthetic", False)
        else "下一步请人工选择并保存鼻孔端和鼻咽端裁切面，再生成 STL 与几何质检"
    )
    update_stage(
        case_id,
        "segment",
        "ok",
        message=f"气道掩膜已人工确认，{next_step}",
        artifacts=["segment/mask.npy", "segment/segmentation.json"],
        data={
            "need_human_confirm": False,
            "external_air_suspected": False,
            "candidate_external_air_suspected": False,
            "candidate_mask_available": candidate_path.exists(),
            "human_review_status": "已确认",
        },
    )
    return {"status": "confirmed", "message": f"气道掩膜已确认，{next_step}"}
