"""气道分割：SOP 阈值 + 连通域基线（可解释 baseline，不是 AI 模型）。

SOP 4.3 阈值 -1024 ~ -300 HU；4.4 用连通域保留鼻孔→鼻咽主气道；
4.5 鼻孔端保留开口、鼻咽端切平。所有参数留痕，默认标记为"需人工复核"。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from scipy import ndimage

from .config import SOP_DEFAULTS
from .errors import PipelineError
from .volume import Volume, anterior_sign, load_volume, resolve_ap_axis


def _structure(connectivity: int) -> np.ndarray:
    return ndimage.generate_binary_structure(3, connectivity)


def fill_enclosed_cavities(mask: np.ndarray, spacing, max_cavity_mm3: float = 5.0) -> tuple[np.ndarray, dict]:
    """填充被气道完全包围的微小空腔（离散化或噪声造成的孤立点）。

    只处理不与包围盒外背景连通、且体积小于阈值的空腔；数量与体积会写进产物留痕。
    这是可关闭、可复核的清理步骤，不等于平滑或简化，也不触碰主腔边界与开口。
    """
    coords = np.where(mask)
    bbox = tuple(slice(int(c.min()), int(c.max()) + 1) for c in coords)
    sub = mask[bbox].copy()
    background = ~sub
    labeled, count = ndimage.label(background, structure=np.ones((3, 3, 3)))
    if count == 0:
        return mask, {"cavities_filled": 0, "cavity_volume_mm3": 0.0, "max_cavity_mm3": max_cavity_mm3}

    border_labels = set(np.unique(labeled[0])) | set(np.unique(labeled[-1]))
    border_labels |= set(np.unique(labeled[:, 0, :])) | set(np.unique(labeled[:, -1, :]))
    border_labels |= set(np.unique(labeled[:, :, 0])) | set(np.unique(labeled[:, :, -1]))
    border_labels.discard(0)

    voxel_volume = float(np.prod([float(s) for s in spacing]))
    sizes = ndimage.sum(np.ones_like(labeled, dtype=np.float64), labeled, range(1, count + 1))
    filled_voxels = 0
    filled_volume = 0.0
    for index, size in enumerate(sizes, start=1):
        if index in border_labels:
            continue
        volume = float(size) * voxel_volume
        if volume <= max_cavity_mm3:
            sub[labeled == index] = True
            filled_voxels += int(size)
            filled_volume += volume
    mask = mask.copy()
    mask[bbox] = sub
    return mask, {
        "cavities_filled": int(filled_voxels),
        "cavity_volume_mm3": round(filled_volume, 2),
        "max_cavity_mm3": float(max_cavity_mm3),
    }


def _component_stats(labeled: np.ndarray, count: int, spacing) -> list[dict]:
    voxel_volume = float(np.prod(spacing))
    sizes = np.bincount(labeled.ravel(), minlength=count + 1)[1:]
    order = np.argsort(sizes)[::-1]
    stats = []
    for label in order:
        idx = int(label) + 1
        stats.append(
            {
                "component_id": idx,
                "voxels": int(sizes[idx - 1]),
                "volume_mm3": float(sizes[idx - 1] * voxel_volume),
            }
        )
    return stats


def _select_main_component_from_candidate(
    candidate: np.ndarray,
    spacing,
    ap_axis: int,
    sign: int,
    min_component_mm3: float,
    min_ap_coverage: float,
    max_anterior_gap_mm: float,
) -> tuple[np.ndarray, dict]:
    """从已清理的完整候选中选出主鼻腔对照掩膜。

    阈值分割时最大的连通域可能是体外空气。完整候选经过 CT 体内包络
    清理后，主鼻腔对照必须重新选组件；完整候选本身仍原样保留，不在此
    处删除鼻窦或旁支。
    """
    labels, count = ndimage.label(candidate, structure=_structure(3))
    if count == 0:
        return candidate.astype(bool), {"method": "clean_candidate_largest_component", "status": "empty"}

    spacing = tuple(float(value) for value in spacing)
    voxel_volume = float(np.prod(spacing))
    coords_all = np.where(candidate)
    global_min = int(coords_all[ap_axis].min())
    global_max = int(coords_all[ap_axis].max())
    global_span = max(global_max - global_min + 1, 1)
    ap_spacing = float(spacing[ap_axis])
    stats = _component_stats(labels, count, spacing)
    # ``find_objects`` reuses the labelled volume's bounding boxes. Calling
    # ``np.where(labels == id)`` for every tiny component would rescan the full
    # CT for each component and is prohibitively expensive on real scans.
    objects = ndimage.find_objects(labels)
    candidates = []
    for stat in stats:
        component_box = objects[int(stat["component_id"]) - 1]
        if component_box is None:
            continue
        comp_min = int(component_box[ap_axis].start)
        comp_max = int(component_box[ap_axis].stop) - 1
        span = comp_max - comp_min + 1
        front_index = comp_max if sign > 0 else comp_min
        global_front = global_max if sign > 0 else global_min
        candidates.append(
            {
                **stat,
                "ap_span_voxels": span,
                "ap_coverage": round(float(span / global_span), 3),
                "anterior_gap_mm": round(float(abs(front_index - global_front) * ap_spacing), 2),
            }
        )

    qualified = [
        item
        for item in candidates
        if item["volume_mm3"] >= float(min_component_mm3)
        and item["ap_coverage"] >= float(min_ap_coverage)
        and item["anterior_gap_mm"] <= float(max_anterior_gap_mm)
    ]
    chosen = max(qualified or candidates, key=lambda item: item["volume_mm3"])
    selected = labels == int(chosen["component_id"])
    return selected, {
        "method": "clean_candidate_qualified_component" if qualified else "clean_candidate_largest_component",
        "status": "ok",
        "chosen_component": chosen,
        "components_total": int(count),
        "qualified_components": int(len(qualified)),
    }


def segment_airway(volume: Volume, params: dict | None = None) -> dict:
    """执行阈值 + 连通域分割，返回掩膜与可复核的指标。"""
    p = dict(SOP_DEFAULTS)
    p.update(
        {
            "connectivity": 3,
            "anterior_plane": None,
            "posterior_plane": None,
            "min_component_mm3": 200.0,
            "min_ap_coverage": 0.6,
            "max_anterior_gap_mm": 5.0,
            "fill_enclosed_cavities": True,
            "max_cavity_mm3": 5.0,
        }
    )
    if params:
        p.update({k: v for k, v in params.items() if v is not None})

    hu = volume.array.astype(np.float32)
    mask = (hu >= float(p["hu_low"])) & (hu <= float(p["hu_high"]))
    if not mask.any():
        raise PipelineError(
            "failed",
            f"阈值区间 [{p['hu_low']}, {p['hu_high']}] HU 内没有任何体素",
            ["检查该 CT 是否为可用的薄层鼻窦 CT", "调整阈值并重跑（SOP 默认 -1024 ~ -300）"],
        )

    labeled, count = ndimage.label(mask, structure=_structure(int(p["connectivity"])))
    if count == 0:
        raise PipelineError("failed", "阈值掩膜内没有连通域", ["调整阈值区间后重试"])

    spacing = tuple(float(s) for s in volume.spacing)
    voxel_volume = float(np.prod(spacing))
    stats = _component_stats(labeled, count, spacing)

    ap_axis, ap_source = resolve_ap_axis(volume.meta)
    ap_spacing = spacing[ap_axis]
    sign = anterior_sign(volume.meta, ap_axis)

    # 掩膜在 A-P 轴上的整体跨度（体素索引）
    ap_coords = np.flatnonzero(np.any(mask, axis=tuple(axis for axis in range(3) if axis != ap_axis)))
    global_min, global_max = int(ap_coords.min()), int(ap_coords.max())
    global_span = max(global_max - global_min + 1, 1)

    axis0, axis1, axis2 = (i for i in range(3))
    candidates = []
    for stat in stats[:10]:
        comp = labeled == stat["component_id"]
        coords = np.where(comp)
        comp_min = int(coords[ap_axis].min())
        comp_max = int(coords[ap_axis].max())
        span = comp_max - comp_min + 1
        coverage = span / global_span
        # 前端距离：以"索引增大是否向前"换算成解剖前后的实际距离
        front_index = comp_max if sign > 0 else comp_min
        global_front = global_max if sign > 0 else global_min
        anterior_gap_mm = abs(front_index - global_front) * ap_spacing
        area_inplane = float(comp.sum()) * voxel_volume / (span * ap_spacing)
        candidates.append(
            {
                **stat,
                "ap_span_voxels": span,
                "ap_coverage": round(float(coverage), 3),
                "anterior_gap_mm": round(float(anterior_gap_mm), 2),
                "mean_cross_section_mm2": round(area_inplane, 2),
            }
        )

    qualified = [
        c
        for c in candidates
        if c["volume_mm3"] >= float(p["min_component_mm3"])
        and c["ap_coverage"] >= float(p["min_ap_coverage"])
        and c["anterior_gap_mm"] <= float(p["max_anterior_gap_mm"])
    ]
    if not qualified:
        raise PipelineError(
            "failed",
            "未找到从鼻孔贯通到鼻咽的主气道连通域",
            [
                "检查扫描范围是否覆盖鼻孔至鼻咽",
                "检查阈值区间是否把鼻窦旁路/外部空气连成一片",
                "降低 min_ap_coverage 或改用 6-连通（connectivity=1）后重试",
                "在 3D Slicer 中用 Islands 工具人工确认主气道后导出掩膜",
            ],
            {"components_found": len(candidates), "top_candidates": candidates[:5]},
        )

    chosen = max(qualified, key=lambda c: c["volume_mm3"])
    comp = labeled == chosen["component_id"]

    # 端面裁剪（SOP 4.5）：默认取连通域端点，明确标注需人工确认
    comp_box = ndimage.find_objects(labeled)[int(chosen["component_id"]) - 1]
    comp_min = int(comp_box[ap_axis].start)
    comp_max = int(comp_box[ap_axis].stop) - 1
    planes_auto = p["anterior_plane"] is None or p["posterior_plane"] is None
    anterior_plane = int(p["anterior_plane"]) if p["anterior_plane"] is not None else comp_min
    posterior_plane = int(p["posterior_plane"]) if p["posterior_plane"] is not None else comp_max
    anterior_plane = max(anterior_plane, comp_min)
    posterior_plane = min(max(posterior_plane, anterior_plane), comp_max)

    crop = np.zeros(mask.shape, dtype=bool)
    slicer = [slice(None)] * 3
    slicer[ap_axis] = slice(anterior_plane, posterior_plane + 1)
    crop[tuple(slicer)] = True
    # Keep the complete threshold candidate inside the A-P extent for browser
    # review. The user explicitly starts body-envelope cleanup in the next step.
    cropped = comp & crop
    candidate_cropped = mask & crop
    candidate_cleaning = {
        "status": "pending_manual",
        "method": "ct_body_envelope_then_non_ap_boundary_components",
        "reason": "等待用户点击自动去除体外空气",
    }
    candidate_available = bool(candidate_cropped.any())
    candidate_labels, candidate_count = ndimage.label(
        candidate_cropped, structure=_structure(int(p["connectivity"]))
    )
    candidate_components = _component_stats(candidate_labels, candidate_count, spacing)
    main_component_selection = {
        "method": "raw_threshold_component",
        "status": "pending_manual",
        "chosen_component": chosen,
    }
    candidate_axes = [
        np.flatnonzero(np.any(candidate_cropped, axis=tuple(other for other in range(3) if other != axis)))
        for axis in range(3)
    ]
    candidate_bbox_mm = [
        round(float((axis_coords.max() - axis_coords.min() + 1) * spacing[axis]), 2)
        for axis, axis_coords in enumerate(candidate_axes)
    ]
    candidate_voxels = int(candidate_cropped.sum())
    candidate_metrics = {
        "voxels": candidate_voxels,
        "volume_ml": round(candidate_voxels * voxel_volume / 1000.0, 3),
        "bbox_mm": candidate_bbox_mm,
        "components_total": int(candidate_count),
        "largest_component_voxels": int(candidate_components[0]["voxels"])
        if candidate_components
        else 0,
        "largest_component_volume_ml": round(
            float(candidate_components[0]["volume_mm3"]) / 1000.0, 3
        )
        if candidate_components
        else 0.0,
    }
    if not cropped.any():
        raise PipelineError("failed", "裁剪后的气道掩膜为空", ["检查鼻孔面与鼻咽面参数是否切反或超出范围"])

    cavity_info = {
        "status": "pending_manual",
        "cavities_filled": 0,
        "cavity_volume_mm3": 0.0,
        "max_cavity_mm3": float(p["max_cavity_mm3"]),
    }

    # 开口面积：两个端面所在层的横截面积
    def plane_area(plane_value: int) -> float:
        take = [slice(None)] * 3
        take[ap_axis] = slice(plane_value, plane_value + 1)
        inplane_spacing = [spacing[i] for i in range(3) if i != ap_axis]
        return float(cropped[tuple(take)].sum()) * float(np.prod(inplane_spacing))

    anterior_area = plane_area(anterior_plane)
    posterior_area = plane_area(posterior_plane)

    voxel_count = int(cropped.sum())
    boundary_by_axis = [
        bool(cropped[0].any() or cropped[-1].any()),
        bool(cropped[:, 0, :].any() or cropped[:, -1, :].any()),
        bool(cropped[:, :, 0].any() or cropped[:, :, -1].any()),
    ]
    external_air_suspected = any(
        touched for axis, touched in enumerate(boundary_by_axis) if axis != ap_axis
    )
    bbox_mm = []
    for axis in range(3):
        axis_coords = np.flatnonzero(np.any(cropped, axis=tuple(other for other in range(3) if other != axis)))
        bbox_mm.append(round(float((axis_coords.max() - axis_coords.min() + 1) * spacing[axis]), 2))

    ap_length_mm = round(float((posterior_plane - anterior_plane + 1) * ap_spacing), 2)

    result = {
        "params": {
            "hu_low": float(p["hu_low"]),
            "hu_high": float(p["hu_high"]),
            "connectivity": int(p["connectivity"]),
            "anterior_plane": anterior_plane,
            "posterior_plane": posterior_plane,
            "ap_axis": ap_axis,
            "min_component_mm3": float(p["min_component_mm3"]),
            "min_ap_coverage": float(p["min_ap_coverage"]),
            "max_anterior_gap_mm": float(p["max_anterior_gap_mm"]),
            "fill_enclosed_cavities": bool(p["fill_enclosed_cavities"]),
            "max_cavity_mm3": float(p["max_cavity_mm3"]),
        },
        "ap_axis_source": ap_source,
        "planes_auto": bool(planes_auto),
        "need_human_confirm": True,
        "review_flags": {
            "mask_boundary_by_axis": boundary_by_axis,
            "external_air_suspected": external_air_suspected,
        },
        "human_review": {
            "status": "未复核",
            "items": [
                "鼻孔端面位置（是否切在鼻孔开口处）",
                "鼻咽端面位置（是否沿冠状面切平）",
                "鼻窦旁路是否已被剔除",
                "ENS 宽大腔隙与鼻甲残留区域边界是否正确",
            ],
        },
        "cavity_filling": cavity_info,
        "chosen_component": chosen,
        "candidates": candidates[:5],
        "candidate_source": "threshold_window_cropped_uncleaned",
        "candidate_cleaning": candidate_cleaning,
        "main_component_selection": main_component_selection,
        "candidate_mask_available": candidate_available,
        "candidate_metrics": candidate_metrics,
        "candidate_components": candidate_components,
        "candidate_review_flags": {
            "mask_boundary_by_axis": [
                bool(candidate_cropped[0].any() or candidate_cropped[-1].any()),
                bool(candidate_cropped[:, 0, :].any() or candidate_cropped[:, -1, :].any()),
                bool(candidate_cropped[:, :, 0].any() or candidate_cropped[:, :, -1].any()),
            ],
            "external_air_suspected": any(
                bool(candidate_cropped.take(0, axis=axis).any() or candidate_cropped.take(-1, axis=axis).any())
                for axis in range(3)
                if axis != ap_axis
            ),
        },
        "candidate_mask": candidate_cropped.astype(np.uint8),
        "components_total": int(count),
        "components_rejected": max(int(count) - 1, 0),
        "metrics": {
            "voxels": voxel_count,
            "volume_ml": round(voxel_count * voxel_volume / 1000.0, 3),
            "bbox_mm": bbox_mm,
            "ap_length_mm": ap_length_mm,
            "anterior_opening_area_mm2": round(anterior_area, 2),
            "posterior_opening_area_mm2": round(posterior_area, 2),
        },
        "mask": cropped.astype(np.uint8),
    }
    return result


def run_segment(case_id: str, params: dict | None = None) -> dict:
    """对病例执行分割，写 products 到 segment/ 并更新状态。"""
    from .cases import case_dir, invalidate_downstream, update_stage

    volume = load_volume(case_id)
    result = segment_airway(volume, params)
    root = case_dir(case_id)
    (root / "segment").mkdir(parents=True, exist_ok=True)
    # The viewer uses this monotonic value to invalidate any already loaded slices.
    version = 1
    previous = root / "segment" / "segmentation.json"
    if previous.exists():
        try:
            version = int(json.loads(previous.read_text(encoding="utf-8")).get("mask_version", 0)) + 1
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            version = 1
    result["mask_version"] = version
    np.save(root / "segment" / "mask.npy", result.pop("mask"))
    np.save(root / "segment" / "candidate_mask.npy", result.pop("candidate_mask"))
    candidate_mask_available = bool(result.get("candidate_mask_available"))
    original_candidate_path = root / "segment" / "candidate_mask_original.npy"
    if candidate_mask_available:
        np.save(
            original_candidate_path,
            np.load(root / "segment" / "candidate_mask.npy", mmap_mode="r"),
        )
    else:
        original_candidate_path.unlink(missing_ok=True)

    payload = {k: v for k, v in result.items()}
    (root / "segment" / "segmentation.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 重新分割改变了掩膜基准，旧 CT 端面选择不能沿用到新版本。
    (root / "segment" / "endplanes.json").unlink(missing_ok=True)
    # A fresh segmentation starts a new reset/undo history. Old snapshots may
    # have the same CT shape but contain masks from a different threshold run.
    for stale_name in (
        "mask_candidate.npy",
        "mask_before_external_air.npy",
        "candidate_before_external_air.npy",
    ):
        (root / "segment" / stale_name).unlink(missing_ok=True)

    # 重新分割后，旧 STL/CFD/报告都不再能代表当前掩膜，先统一失效。
    invalidate_downstream(case_id, "气道掩膜已重新生成，需重新确认并生成下游产物")

    review_flags = result.get("review_flags", {})
    external_air_suspected = bool(review_flags.get("external_air_suspected"))
    message = (
        "已生成未清理的阈值候选，且触及体数据边界；请先点击自动去除体外空气，再逐层复核"
        if external_air_suspected
        else "已生成未清理的阈值候选；请先点击自动去除体外空气，再逐层复核"
    )
    update_stage(
        case_id,
        "segment",
        "warn",
        message=message,
        artifacts=[
            "segment/mask.npy",
            "segment/candidate_mask.npy",
            *(["segment/candidate_mask_original.npy"] if candidate_mask_available else []),
            "segment/segmentation.json",
        ],
        data={
            "volume_ml": result["metrics"]["volume_ml"],
            "ap_length_mm": result["metrics"]["ap_length_mm"],
            "hu_range": [result["params"]["hu_low"], result["params"]["hu_high"]],
            "need_human_confirm": True,
            "external_air_suspected": external_air_suspected,
            "mask_boundary_by_axis": review_flags.get("mask_boundary_by_axis", []),
            "candidate_external_air_suspected": bool(
                result.get("candidate_review_flags", {}).get("external_air_suspected", False)
            ),
            "candidate_mask_boundary_by_axis": result.get("candidate_review_flags", {}).get(
                "mask_boundary_by_axis", []
            ),
            "mask_version": payload["mask_version"],
            "candidate_mask_available": candidate_mask_available,
            "candidate_metrics": result["candidate_metrics"],
            "candidate_components_total": result["candidate_metrics"]["components_total"],
            "candidate_cleaning": result["candidate_cleaning"],
            "main_component_selection": result.get("main_component_selection", {}),
        },
    )
    result["mask"] = None
    return payload
