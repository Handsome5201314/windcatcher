"""在已复核的气道掩膜上选择建模前后端面。

端面索引使用原始 CT 数组的零起始层号。这里只保存裁切方案；
`segment/mask.npy` 和 `segment/candidate_mask.npy` 始终保持原样。
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import numpy as np
from scipy import ndimage

from .cases import case_dir, invalidate_downstream
from . import oblique
from .config import MIN_OPENING_AREA_MM2
from .errors import PipelineError
from .mask_review import has_current_external_air_validation
from .volume import ANTERIOR_LPS, load_volume

SCOPES = {"main_airway": "mask.npy", "full_candidate": "candidate_mask.npy"}
VIEWS = {0: "axial", 1: "coronal", 2: "sagittal"}
PLAN_NAME = "endplanes.json"


def _scope(scope: str) -> str:
    if scope not in SCOPES:
        raise PipelineError(
            "failed", "建模范围无效", ["选择主鼻腔气流模型或完整候选范围"], {"scope": scope}
        )
    return scope


def _segmentation(root: Path) -> dict:
    path = root / "segment" / "segmentation.json"
    if not path.exists():
        raise PipelineError("blocked", "还没有气道分割记录", ["先完成分割和掩膜复核"])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError("failed", "气道分割记录无法读取", ["重新执行气道分割并复核"]) from exc
    if not isinstance(data, dict):
        raise PipelineError("failed", "气道分割记录格式错误", ["重新执行气道分割并复核"])
    return data


def _geometry(segmentation: dict, volume) -> tuple[int, int, str]:
    try:
        axis = int(segmentation["params"]["ap_axis"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PipelineError("blocked", "分割记录没有有效的前后轴", ["重新执行气道分割并复核 CT 方向"]) from exc
    if axis not in VIEWS:
        raise PipelineError("failed", "分割记录的前后轴超出三维范围", ["重新执行气道分割"])
    direction = np.asarray(volume.axis_dirs[axis], dtype=float)
    anterior_score = float(np.dot(direction, np.asarray(ANTERIOR_LPS, dtype=float)))
    if not np.isfinite(anterior_score) or abs(anterior_score) < 0.7:
        raise PipelineError(
            "blocked", "CT 前后方向无法可靠判定，不能保存端面",
            ["核查 DICOM ImageOrientationPatient 与分割记录的前后轴"],
            {"ap_axis": axis, "anterior_alignment": round(anterior_score, 3)},
        )
    return axis, 1 if anterior_score > 0 else -1, VIEWS[axis]


def _mask(root: Path, scope: str, shape: tuple[int, int, int]) -> np.ndarray:
    path = root / "segment" / SCOPES[scope]
    if not path.exists():
        raise PipelineError("blocked", "所选建模范围的掩膜不存在", ["重新执行气道分割"])
    try:
        mask = np.load(path, mmap_mode="r")
    except (OSError, ValueError) as exc:
        raise PipelineError("failed", "无法读取所选气道掩膜", ["重新执行气道分割"] ) from exc
    if mask.shape != shape or mask.ndim != 3:
        raise PipelineError("failed", "掩膜尺寸与 CT 不一致", ["重新执行气道分割或导入匹配的修正掩膜"])
    return mask


def _plans(root: Path) -> dict:
    path = root / "segment" / PLAN_NAME
    if not path.exists():
        return {}
    try:
        plans = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError("failed", "已保存的端面方案无法读取", ["检查病例端面方案文件后重试"]) from exc
    if not isinstance(plans, dict):
        raise PipelineError("failed", "端面方案格式错误", ["检查病例端面方案文件后重试"])
    return plans


def load_plan(case_id: str, scope: str) -> dict | None:
    """返回某一建模范围的保存方案；版本检查由调用方执行。"""
    plan = _plans(case_dir(case_id)).get(_scope(scope))
    if plan is not None and not isinstance(plan, dict):
        raise PipelineError("failed", "端面方案格式错误", ["重新选择鼻孔端和鼻咽端面"])
    return plan


def plane_areas_mm2(mask: np.ndarray, axis: int, spacing) -> np.ndarray:
    inplane = float(np.prod([spacing[i] for i in range(3) if i != axis]))
    return np.count_nonzero(mask, axis=tuple(i for i in range(3) if i != axis)) * inplane


def clip_mask(mask: np.ndarray, axis: int, anterior_index: int, posterior_index: int) -> np.ndarray:
    """裁切副本，两端所在层保留，端面外的体素清零。"""
    clipped = np.asarray(mask).copy()
    lo, hi = sorted((anterior_index, posterior_index))
    before = [slice(None)] * 3
    after = [slice(None)] * 3
    before[axis] = slice(None, lo)
    after[axis] = slice(hi + 1, None)
    clipped[tuple(before)] = 0
    clipped[tuple(after)] = 0
    return clipped


def _validate_indices(
    mask: np.ndarray, axis: int, sign: int, spacing, anterior_index: int, posterior_index: int,
) -> tuple[float, float]:
    if type(anterior_index) is not int or type(posterior_index) is not int:
        raise PipelineError("failed", "端面层号必须是整数", ["在 CT 层滑块上选择鼻孔端和鼻咽端"])
    count = mask.shape[axis]
    if not 0 <= anterior_index < count or not 0 <= posterior_index < count:
        raise PipelineError("failed", "端面层号超出 CT 范围", ["在当前 CT 层范围内重新选择端面"])
    if (anterior_index - posterior_index) * sign <= 0:
        raise PipelineError(
            "failed", "鼻孔端必须位于鼻咽端前方，两个端面不能重合",
            ["根据 CT 前后方向重新选择鼻孔端和鼻咽端"],
            {"anterior_sign": sign},
        )
    areas = plane_areas_mm2(mask, axis, spacing)
    anterior_area, posterior_area = float(areas[anterior_index]), float(areas[posterior_index])
    if anterior_area < MIN_OPENING_AREA_MM2 or posterior_area < MIN_OPENING_AREA_MM2:
        raise PipelineError(
            "blocked", "所选端面上的气道面积太小，无法形成可用几何开口",
            ["在 CT 中移动至气道连续且截面清晰的鼻孔端与鼻咽端，再重新选择"],
            {
                "anterior_area_mm2": round(anterior_area, 3),
                "posterior_area_mm2": round(posterior_area, 3),
                "technical_minimum_mm2": MIN_OPENING_AREA_MM2,
            },
        )
    return anterior_area, posterior_area


def _validate_main_connection(
    mask: np.ndarray, axis: int, spacing, anterior_index: int, posterior_index: int,
) -> None:
    """主气道模式会只保留最大 6 连通块，确保选定两端仍在该块上。"""
    lo, hi = sorted((anterior_index, posterior_index))
    ap_slice = [slice(None)] * 3
    ap_slice[axis] = slice(lo, hi + 1)
    slab = np.asarray(mask[tuple(ap_slice)], dtype=bool)
    occupied = np.where(slab)
    if not occupied[0].size:
        raise PipelineError("blocked", "裁切后没有气道", ["重新选择端面或修正掩膜"])
    # 仅标记气道包围盒，避免为完整 512×512 CT 建立巨大整型标签数组。
    bbox = tuple(slice(int(coords.min()), int(coords.max()) + 1) for coords in occupied)
    cropped = slab[bbox]
    labeled, count = ndimage.label(cropped, structure=ndimage.generate_binary_structure(3, 1))
    if count == 0:
        raise PipelineError("blocked", "裁切后没有气道", ["重新选择端面或修正掩膜"])
    sizes = np.bincount(labeled.ravel())
    sizes[0] = 0
    largest = labeled == int(np.argmax(sizes))
    areas = plane_areas_mm2(largest, axis, spacing)
    anterior_local = anterior_index - lo - int(bbox[axis].start)
    posterior_local = posterior_index - lo - int(bbox[axis].start)
    anterior_area = float(areas[anterior_local]) if 0 <= anterior_local < len(areas) else 0.0
    posterior_area = float(areas[posterior_local]) if 0 <= posterior_local < len(areas) else 0.0
    if (anterior_area < MIN_OPENING_AREA_MM2
            or posterior_area < MIN_OPENING_AREA_MM2):
        raise PipelineError(
            "blocked", "两端没有同时落在用于建模的主气道连通区域上",
            ["在 CT 中检查气道是否断开，修正掩膜后重新选择两端"],
            {
                "main_anterior_area_mm2": round(anterior_area, 3),
                "main_posterior_area_mm2": round(posterior_area, 3),
            },
        )


def get_endplanes(case_id: str, scope: str = "main_airway") -> dict:
    scope = _scope(scope)
    root = case_dir(case_id)
    seg = _segmentation(root)
    volume = load_volume(case_id)
    axis, sign, view = _geometry(seg, volume)
    mask = _mask(root, scope, volume.shape)
    areas = plane_areas_mm2(mask, axis, volume.spacing)
    saved = load_plan(case_id, scope)
    version = seg.get("mask_version")
    if saved is not None:
        saved = {**saved, "stale": saved.get("mask_version") != version}
    migration_required = type(version) is not int
    return {
        "scope": scope,
        "axis": axis,
        "view": view,
        "count": int(mask.shape[axis]),
        "anterior_sign": sign,
        "ap_origin_lps_mm": [float(value) for value in volume.origin],
        "ap_direction_lps": [float(value) for value in volume.axis_dirs[axis]],
        "ap_spacing_mm": float(volume.spacing[axis]),
        "mask_version": version,
        "areas_mm2": [round(float(value), 3) for value in areas],
        "saved": saved,
        "can_save": bool(has_current_external_air_validation(seg) and not seg.get("need_human_confirm", True)),
        "migration_required": migration_required,
        "message": (
            "旧病例缺少掩膜版本记录；请重新分割、清理体外空气并确认掩膜后再保存端面"
            if migration_required else None
        ),
    }


def save_endplanes(
    case_id: str, scope: str, anterior_index: int, posterior_index: int, mask_version: int,
) -> dict:
    scope = _scope(scope)
    root = case_dir(case_id)
    seg = _segmentation(root)
    if type(mask_version) is not int or mask_version != seg.get("mask_version"):
        raise PipelineError("blocked", "掩膜版本已变化，端面选择未保存", ["刷新 CT 后重新选择端面"])
    if not has_current_external_air_validation(seg) or seg.get("need_human_confirm", True):
        raise PipelineError(
            "blocked", "气道掩膜尚未清理并人工确认，不能保存建模端面",
            ["先去除体外空气、逐层复核并确认掩膜"],
        )
    if scope == "full_candidate" and not bool(seg.get("candidate_mask_available", False)):
        raise PipelineError("blocked", "完整候选掩膜尚不可用", ["先完成体外空气清理并复核完整候选"])
    volume = load_volume(case_id)
    axis, sign, view = _geometry(seg, volume)
    mask = _mask(root, scope, volume.shape)
    anterior_area, posterior_area = _validate_indices(
        mask, axis, sign, volume.spacing, anterior_index, posterior_index,
    )
    if scope == "main_airway":
        _validate_main_connection(mask, axis, volume.spacing, anterior_index, posterior_index)
    plan = {
        "scope": scope,
        "axis": axis,
        "view": view,
        "anterior_sign": sign,
        "anterior_index": anterior_index,
        "posterior_index": posterior_index,
        "mask_version": mask_version,
        "anterior_area_mm2": round(anterior_area, 3),
        "posterior_area_mm2": round(posterior_area, 3),
    }
    return _persist_plan(case_id, root, scope, plan, mask_version)


def _persist_plan(case_id: str, root: Path, scope: str, plan: dict, mask_version: int) -> dict:
    """原子保存方案；先失效旧 STL/CFD，避免新端面配旧产物。"""
    plans = _plans(root)
    plans[scope] = plan
    path = root / "segment" / PLAN_NAME
    temp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        temp.write_text(json.dumps(plans, ensure_ascii=False, indent=2), encoding="utf-8")
        # 保存新的端面方案之前先使已有 STL/CFD/报告失效。若失效失败，
        # 临时方案会被清理，旧方案保持原状，避免 UI 误读为已提交。
        try:
            invalidate_downstream(case_id, "端面方案已更改，需重新生成 STL 和 CFD 算例")
        except OSError as exc:
            raise PipelineError(
                "failed", "旧 STL/CFD 产物未能完全失效，端面方案没有保存",
                ["检查病例输出文件是否被其他程序占用，然后重试"],
            ) from exc
        if _segmentation(root).get("mask_version") != mask_version:
            raise PipelineError("blocked", "保存期间掩膜版本发生变化", ["刷新 CT 后重新选择端面"])
        try:
            temp.replace(path)
        except OSError as exc:
            raise PipelineError(
                "failed", "端面方案写入失败，旧 STL/CFD 已失效",
                ["检查病例目录的写入权限后重新保存端面，再生成 STL"],
            ) from exc
    finally:
        temp.unlink(missing_ok=True)
    return plan


def _oblique_context(case_id: str, scope: str, mask_version: int):
    """预检与保存共用同一掩膜、版本和人工复核闸门。"""
    scope = _scope(scope)
    root = case_dir(case_id)
    seg = _segmentation(root)
    if type(mask_version) is not int or mask_version != seg.get("mask_version"):
        raise PipelineError("blocked", "掩膜版本已变化，3D 切面没有保存", ["刷新 3D 预览后重新选择两端"])
    if not has_current_external_air_validation(seg) or seg.get("need_human_confirm", True):
        raise PipelineError(
            "blocked", "气道掩膜尚未清理并人工确认，不能保存 3D 切面",
            ["先去除体外空气、逐层复核并确认掩膜"],
        )
    if scope == "full_candidate" and not bool(seg.get("candidate_mask_available", False)):
        raise PipelineError("blocked", "完整候选掩膜尚不可用", ["先完成体外空气清理并复核完整候选"])
    volume = load_volume(case_id)
    axis, sign, view = _geometry(seg, volume)
    mask = _mask(root, scope, volume.shape)
    return scope, root, volume, mask, axis, sign, view


def preview_oblique_endplanes(
    case_id: str, scope: str, anterior_plane: dict, posterior_plane: dict, mask_version: int,
) -> dict:
    """预检 3D 裁切方案，不保存，不使已有 STL/CFD 失效。"""
    scope, root, volume, mask, _, _, _ = _oblique_context(case_id, scope, mask_version)
    validation = oblique.validate_plane_pair(
        mask, volume, anterior_plane, posterior_plane, main_airway=scope == "main_airway",
    )
    if _segmentation(root).get("mask_version") != mask_version:
        raise PipelineError("blocked", "预检期间掩膜版本发生变化", ["刷新 3D 预览后重新选择两端"])
    return {
        "scope": scope, "mask_version": mask_version, "format": "oblique_planes_v1", "can_save": True,
        "anterior_area_mm2": validation["anterior_area_estimate_mm2"],
        "posterior_area_mm2": validation["posterior_area_estimate_mm2"],
        "retained_volume_ml": validation["retained_volume_ml"],
        "retained_components": validation["retained_components"],
        "message": "截面积和体积是体素近似值；生成 STL 后仍需通过几何质检。",
    }


def save_oblique_endplanes(
    case_id: str, scope: str, anterior_plane: dict, posterior_plane: dict, mask_version: int,
) -> dict:
    """保存用户在 3D 中确认的两张可旋转切面，法向均朝删除侧。"""
    scope, root, volume, mask, axis, sign, view = _oblique_context(case_id, scope, mask_version)
    validation = oblique.validate_plane_pair(
        mask, volume, anterior_plane, posterior_plane, main_airway=scope == "main_airway",
    )
    plan = {
        "format": "oblique_planes_v1",
        "scope": scope,
        "axis": axis,
        "view": view,
        "anterior_sign": sign,
        "mask_version": mask_version,
        "anterior_plane": validation["anterior_plane"],
        "posterior_plane": validation["posterior_plane"],
        "source_voxels": validation["source_voxels"],
        "retained_voxels": validation["retained_voxels"],
        "retained_components": validation["retained_components"],
        "retained_volume_ml": validation["retained_volume_ml"],
        "anterior_area_estimate_mm2": validation["anterior_area_estimate_mm2"],
        "posterior_area_estimate_mm2": validation["posterior_area_estimate_mm2"],
    }
    return _persist_plan(case_id, root, scope, plan, mask_version)


def _plane_from_index(volume, axis: int, sign: int, side: str, index: int) -> dict:
    direction = np.asarray(volume.axis_dirs[axis], dtype=float)
    outward = (1 if side == "anterior" else -1) * sign
    return {
        "point_lps_mm": (np.asarray(volume.origin, dtype=float) + index * volume.spacing[axis] * direction).tolist(),
        "normal_lps": (direction * outward).tolist(),
    }


def _oblique_plan_from_existing(
    saved: dict | None, scope: str, volume, axis: int, sign: int, view: str, mask_version: int,
) -> dict:
    """Keep the current peer plane while upgrading an older paired CT plan."""
    plan = {
        "format": "oblique_planes_v1", "scope": scope, "axis": axis, "view": view,
        "anterior_sign": sign, "mask_version": mask_version,
        "anterior_plane": None, "posterior_plane": None,
    }
    if not isinstance(saved, dict) or saved.get("mask_version") != mask_version:
        return plan
    if saved.get("format") == "oblique_planes_v1":
        for side in ("anterior", "posterior"):
            plane = saved.get(f"{side}_plane")
            if plane is not None:
                name = "鼻孔端" if side == "anterior" else "鼻咽端"
                # Validate the persisted peer, but retain its exact coordinates.
                # Re-normalizing here introduces tiny float changes every time the
                # opposite side is saved, which makes an untouched peer appear to
                # have changed in the client.
                oblique._plane(plane, name)
                plan[f"{side}_plane"] = {
                    "point_lps_mm": list(plane["point_lps_mm"]),
                    "normal_lps": list(plane["normal_lps"]),
                }
    elif saved.get("format") is None:
        for side in ("anterior", "posterior"):
            index = saved.get(f"{side}_index")
            if type(index) is int and 0 <= index < volume.shape[axis]:
                plan[f"{side}_plane"] = _plane_from_index(volume, axis, sign, side, index)
    return plan


def _pair_validation(plan: dict, mask: np.ndarray, volume, *, main_airway: bool) -> dict:
    result = {**plan, "complete": False}
    result.pop("validation_error", None)
    if not result.get("anterior_plane") or not result.get("posterior_plane"):
        return result
    # 联合校验会重新归一化法向；保留保存方案中的端面 JSON，避免保存另一端
    # 时让未修改端因浮点舍入产生无意义的变化。
    saved_planes = {
        "anterior_plane": result["anterior_plane"],
        "posterior_plane": result["posterior_plane"],
    }
    try:
        validation = oblique.validate_plane_pair(
            mask, volume, result["anterior_plane"], result["posterior_plane"], main_airway=main_airway,
        )
    except PipelineError as exc:
        result["validation_error"] = exc.to_dict()
        return result
    result.update(validation)
    result.update(saved_planes)
    result["complete"] = True
    return result


def save_single_endplane(case_id: str, scope: str, side: str, plane: dict, mask_version: int) -> dict:
    """Save one side without discarding its peer or requiring a valid pair yet."""
    if side not in {"anterior", "posterior"}:
        raise PipelineError("failed", "端面方向无效", ["分别选择鼻孔端或鼻咽端"])
    scope = _scope(scope)
    _, root, volume, mask, axis, sign, view = _oblique_context(case_id, scope, mask_version)
    normalized = oblique.validate_plane_endpoint(plane, side)
    anterior = np.asarray(ANTERIOR_LPS, dtype=float)
    plane_ap = float(np.dot(normalized["point_lps_mm"], anterior))
    origin_ap = float(np.dot(volume.origin, anterior))
    ap_step = float(np.dot(volume.axis_dirs[axis], anterior) * volume.spacing[axis])
    last_ap = origin_ap + (volume.shape[axis] - 1) * ap_step
    if not min(origin_ap, last_ap) - volume.spacing[axis] <= plane_ap <= max(origin_ap, last_ap) + volume.spacing[axis]:
        raise PipelineError("blocked", "端面位置超出 CT 前后扫描范围", ["把当前端面移回 CT 气道范围后重试"])

    working = np.asarray(mask, dtype=bool)
    if scope == "main_airway":
        from .surface import select_largest_component

        working, _ = select_largest_component(working)
    area = oblique._area_estimate(working, volume, normalized, None)
    if area < MIN_OPENING_AREA_MM2:
        name = "鼻孔端" if side == "anterior" else "鼻咽端"
        raise PipelineError(
            "blocked", f"{name}未形成足够大的截面", ["沿主气道移动当前端面，再单独保存"],
            {"side": side, "area_estimate_mm2": round(area, 3), "technical_minimum_mm2": MIN_OPENING_AREA_MM2},
        )

    plan = _oblique_plan_from_existing(
        load_plan(case_id, scope), scope, volume, axis, sign, view, mask_version,
    )
    plan[f"{side}_plane"] = normalized
    plan[f"{side}_area_estimate_mm2"] = round(area, 3)
    validated = _pair_validation(plan, working, volume, main_airway=scope == "main_airway")
    return _persist_plan(case_id, root, scope, validated, mask_version)


def current_plan(
    case_id: str, scope: str, segmentation: dict, volume, mask: np.ndarray,
) -> dict | None:
    """建模使用的方案必须与当前范围、CT 方向和掩膜版本一致。"""
    plan = load_plan(case_id, scope)
    if plan is None:
        return None
    axis, sign, _ = _geometry(segmentation, volume)
    if (plan.get("mask_version") != segmentation.get("mask_version")
            or plan.get("axis") != axis or plan.get("anterior_sign") != sign):
        raise PipelineError(
            "blocked", "已保存的端面方案与当前掩膜或 CT 方向不匹配",
            ["返回 CT 复核并重新保存鼻孔端和鼻咽端面"],
            {"model_scope": scope, "saved_mask_version": plan.get("mask_version"),
             "current_mask_version": segmentation.get("mask_version")},
        )
    if plan.get("format") == "oblique_planes_v1":
        if not plan.get("complete", True) and plan.get("validation_error"):
            error = plan["validation_error"]
            raise PipelineError(
                "blocked", error.get("reason", "两个端面暂未通过联合检查"),
                error.get("remediation") or ["分别调整并保存鼻孔端和鼻咽端后重试"],
                {"model_scope": scope, "saved_endplanes": plan},
            )
        if not plan.get("anterior_plane") or not plan.get("posterior_plane"):
            raise PipelineError(
                "blocked", "鼻孔端和鼻咽端尚未分别保存完整",
                ["在端面选择面板中分别选择并保存鼻孔端与鼻咽端，再联合核对"],
                {"model_scope": scope, "saved_endplanes": plan},
            )
        oblique.validate_plane_pair(
            mask, volume, plan.get("anterior_plane"), plan.get("posterior_plane"),
            main_airway=scope == "main_airway",
        )
        return plan
    if plan.get("format") is not None:
        raise PipelineError("blocked", "端面方案格式不受支持", ["返回 3D 模型重新选择并保存切面"])
    _validate_indices(
        mask, axis, sign, volume.spacing, plan.get("anterior_index"), plan.get("posterior_index"),
    )
    return plan
