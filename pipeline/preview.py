"""切面预览图：把 HU 体数据与分割掩膜渲染成 PNG（真实产物，不是示意图）。

窗口设置采用鼻腔观片常用参数（WL -500 / WW 2000），掩膜用青色半透明叠加。
"""

from __future__ import annotations

import json
import math
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .config import SOP_DEFAULTS
from .mask_review import external_air_cleanup_pending, has_current_external_air_validation


def _legacy_candidate_cleanup(segmentation: dict) -> bool:
    params = segmentation.get("params")
    if not isinstance(params, dict):
        return False
    air_threshold = params.get("hu_high", SOP_DEFAULTS["hu_high"])
    if type(air_threshold) not in (int, float) or not math.isfinite(air_threshold):
        return False
    for name in ("candidate_external_air_cleanup", "candidate_cleaning"):
        cleanup = segmentation.get(name)
        if not isinstance(cleanup, dict) or cleanup.get("status") == "blocked":
            continue
        body_threshold = cleanup.get("body_hu_threshold")
        if type(body_threshold) in (int, float) and math.isfinite(body_threshold) and body_threshold < air_threshold:
            return True
    return False
WINDOW_LEVEL = -500.0
WINDOW_WIDTH = 2000.0
OVERLAY_COLOR = (45, 212, 191)      # 青绿，与 UI 强调色一致
OVERLAY_ALPHA = 0.45
VIEW_AXES = {"axial": 0, "coronal": 1, "sagittal": 2}
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simsun.ttc",
)


def _font(size: int = 14):
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _validate_view(view: str) -> int:
    try:
        return VIEW_AXES[view]
    except KeyError as exc:
        raise ValueError(f"未知视图：{view}") from exc


def view_count(shape: tuple[int, int, int] | list[int], view: str) -> int:
    """返回某个观察方向可浏览的层数。"""
    return int(shape[_validate_view(view)])


def _slice_arrays(
    hu: np.ndarray,
    mask: np.ndarray | None,
    view: str,
    index: int | None = None,
) -> tuple[np.ndarray, np.ndarray | None, int]:
    """按视图与层号取切片，数组约定为 (z, y, x)。"""
    axis = _validate_view(view)
    count = view_count(hu.shape, view)
    if index is None:
        index = count // 2
    if index < 0 or index >= count:
        raise ValueError(f"{view} 层号超出范围：{index}（共 {count} 层）")
    if axis == 0:
        return hu[index], (mask[index] if mask is not None else None), index
    if axis == 1:
        return hu[:, index, :], (mask[:, index, :] if mask is not None else None), index
    return hu[:, :, index], (mask[:, :, index] if mask is not None else None), index


def _to_grayscale(
    slice_hu: np.ndarray,
    window_level: float = WINDOW_LEVEL,
    window_width: float = WINDOW_WIDTH,
) -> np.ndarray:
    if window_width <= 0:
        raise ValueError("窗宽必须大于 0")
    low = window_level - window_width / 2.0
    high = window_level + window_width / 2.0
    scaled = (slice_hu.astype(np.float32) - low) / (high - low)
    return (np.clip(scaled, 0.0, 1.0) * 255.0).astype(np.uint8)


def viewer_metadata(case_id: str) -> dict:
    """返回浏览器所需的体数据元信息，不返回患者标识或原始路径。"""
    from .cases import case_dir
    from .volume import load_volume

    root = case_dir(case_id)
    volume = load_volume(case_id)
    # 三平面复核默认显示完整候选。主气道掩膜会静默隐藏鼻窦/旁支，
    # 与“先保留、再人工确认”的工作流相冲突。
    candidate_path = root / "segment" / "candidate_mask.npy"
    mask_path = root / "segment" / "mask.npy"
    segmentation = {}
    seg_path = root / "segment" / "segmentation.json"
    if seg_path.exists():
        try:
            segmentation = json.loads(seg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            segmentation = {}
    candidate_available = bool(segmentation.get("candidate_mask_available", False)) and candidate_path.exists()
    displayed_mask_path = candidate_path if candidate_available else mask_path
    cleanup_pending = external_air_cleanup_pending(segmentation)
    candidate_cleanup = segmentation.get("candidate_external_air_cleanup")
    cleanup_stats = (
        {
            "removed_voxels": candidate_cleanup.get("removed_voxels"),
            "removed_volume_ml": candidate_cleanup.get("removed_volume_ml"),
        }
        if isinstance(candidate_cleanup, dict) else None
    )
    validation_current = has_current_external_air_validation(segmentation)
    legacy_candidate_risk = (
        candidate_available
        and _legacy_candidate_cleanup(segmentation)
        and not validation_current
    )
    undo = segmentation.get("external_air_undo")
    can_undo_external_air = (
        isinstance(undo, dict)
        and type(undo.get("after_version")) is int
        and type(segmentation.get("mask_version")) is int
        and undo["after_version"] == segmentation["mask_version"]
        and (root / "segment" / "mask_before_external_air.npy").exists()
        and (root / "segment" / "candidate_before_external_air.npy").exists()
    )
    mask_boundary_touched = False
    mask_external_air_suspected = False
    mask_version = None
    mask_edit_status = "未生成"
    mask_review = {}
    if displayed_mask_path.exists():
        mask = np.load(displayed_mask_path, mmap_mode="r")
        boundary_by_axis = [
            bool(mask[0].any() or mask[-1].any()),
            bool(mask[:, 0, :].any() or mask[:, -1, :].any()),
            bool(mask[:, :, 0].any() or mask[:, :, -1].any()),
        ]
        mask_boundary_touched = any(boundary_by_axis)
        ap_axis = None
        try:
            ap_axis = int(segmentation.get("params", {}).get("ap_axis"))
        except (TypeError, ValueError):
            ap_axis = None
        mask_external_air_suspected = any(
            touched for axis, touched in enumerate(boundary_by_axis) if ap_axis is None or axis != ap_axis
        ) or legacy_candidate_risk
        if segmentation:
            try:
                mask_version = int(segmentation.get("mask_version"))
            except (TypeError, ValueError):
                mask_version = None
            need_confirm = bool(segmentation.get("need_human_confirm", True))
            unvalidated_confirmation = not need_confirm and not validation_current
            mask_edit_status = (
                "待手动清理体外空气" if cleanup_pending else
                "体外空气待复核" if legacy_candidate_risk else
                "需重新复核" if unvalidated_confirmation else
                "待人工复核" if need_confirm else "已确认"
            )
            mask_review = {
                "need_human_confirm": need_confirm or unvalidated_confirmation,
                "human_review_status": segmentation.get("human_review", {}).get("status"),
                "mask_source": segmentation.get("mask_source"),
                "candidate_available": candidate_available,
                "displayed_mask_source": displayed_mask_path.relative_to(root).as_posix(),
                "legacy_candidate_cleanup_risk": legacy_candidate_risk,
                "candidate_cleaning_status": segmentation.get("candidate_cleaning", {}).get("status") if isinstance(segmentation.get("candidate_cleaning"), dict) else None,
                "candidate_external_air_cleanup": cleanup_stats,
            }
    views = {}
    for view, axis in VIEW_AXES.items():
        remaining = [i for i in range(3) if i != axis]
        views[view] = {
            "axis": axis,
            "count": int(volume.shape[axis]),
            "height": int(volume.shape[remaining[0]]),
            "width": int(volume.shape[remaining[1]]),
            "spacing_mm": [float(volume.spacing[i]) for i in remaining],
            "default_index": int(volume.shape[axis] // 2),
        }
    return {
        "shape": list(volume.shape),
        "spacing_mm": [float(v) for v in volume.spacing],
        "views": views,
        "mask_available": displayed_mask_path.exists(),
        "mask_version": mask_version,
        "mask_edit_status": mask_edit_status,
        "mask_candidate_available": candidate_available,
        "mask_source": displayed_mask_path.relative_to(root).as_posix() if displayed_mask_path.exists() else None,
        "mask_review": mask_review,
        "mask_boundary_touched": mask_boundary_touched,
        "mask_external_air_suspected": mask_external_air_suspected,
        "mask_external_air_cleanup_pending": cleanup_pending,
        "can_undo_external_air": can_undo_external_air,
        "window": {"level": WINDOW_LEVEL, "width": WINDOW_WIDTH},
    }


def render_image(
    case_id: str,
    view: str,
    index: int | None = None,
    window_level: float = WINDOW_LEVEL,
    window_width: float = WINDOW_WIDTH,
    overlay: bool = True,
) -> Image.Image:
    """按层渲染 PNG 图像；只读取请求的体数据切片。"""
    from .cases import case_dir
    from .volume import load_volume

    root = case_dir(case_id)
    volume = load_volume(case_id)
    candidate_path = root / "segment" / "candidate_mask.npy"
    mask_path = root / "segment" / "mask.npy"
    candidate_available = False
    seg_path = root / "segment" / "segmentation.json"
    if seg_path.exists():
        try:
            candidate_available = bool(
                json.loads(seg_path.read_text(encoding="utf-8")).get("candidate_mask_available", False)
            )
        except (OSError, json.JSONDecodeError):
            candidate_available = False
    displayed_mask_path = candidate_path if candidate_available and candidate_path.exists() else mask_path
    mask = np.load(displayed_mask_path, mmap_mode="r") if displayed_mask_path.exists() else None

    slice_hu, slice_mask, _ = _slice_arrays(volume.array, mask, view, index)
    gray = _to_grayscale(slice_hu, window_level, window_width)
    height, width = gray.shape
    rgb = np.stack([gray, gray, gray], axis=-1)

    if overlay and slice_mask is not None and slice_mask.any():
        overlay_rgb = np.zeros_like(rgb)
        overlay_rgb[slice_mask.astype(bool)] = OVERLAY_COLOR
        rgb = (rgb * (1 - OVERLAY_ALPHA) + overlay_rgb * OVERLAY_ALPHA).astype(np.uint8)

    image = Image.fromarray(rgb, "RGB")
    max_dimension = 1024
    if max(image.size) > max_dimension:
        scale = max_dimension / max(image.size)
        image = image.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.NEAREST)
    return image


def render_view(
    case_id: str,
    view: str,
    index: int | None = None,
    window_level: float = WINDOW_LEVEL,
    window_width: float = WINDOW_WIDTH,
    overlay: bool = True,
) -> Path:
    """渲染单个视图 PNG，返回绝对路径。"""
    from .cases import case_dir

    image = render_image(case_id, view, index, window_level, window_width, overlay)
    root = case_dir(case_id)
    volume_label = "合成测试数据" if "phantom" in str(json.loads((root / "volume" / "meta.json").read_text(encoding="utf-8")).get("source_dir", "")).lower() else "本机病例"
    bar_height = 26
    canvas = Image.new("RGB", (image.width, image.height + bar_height), (13, 21, 25))
    canvas.paste(image, (0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.text(
        (10, image.height + 6),
        f"风见 · {view.upper()} · 窗位 {int(window_level)}/窗宽 {int(window_width)} · {volume_label}",
        fill=(207, 227, 223),
        font=_font(14),
    )

    out = root / "preview" / f"{view}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, format="PNG")
    return out


def render_all(case_id: str) -> dict:
    """渲染三个视图，返回相对病例目录的路径字典。"""
    from .cases import case_dir

    result = {}
    for view in ("axial", "coronal", "sagittal"):
        path = render_view(case_id, view)
        result[view] = str(path.relative_to(case_dir(case_id)))
    (case_dir(case_id) / "preview" / "index.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result
