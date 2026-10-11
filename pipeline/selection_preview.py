"""按当前已确认掩膜的原始体素分辨率重建端面选择用 3D 模型。

所有轴均保留原始采样，不做降采样、网格简化或几何平滑。仅裁去外围
空白以节省内存；选端模型不修改掩膜或正式 STL、几何质检和 CFD 阶段。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from pathlib import Path

import numpy as np
from skimage import measure

from . import endplanes
from .cases import case_dir
from .errors import PipelineError
from .mask_review import has_current_external_air_validation
from .surface import voxel_to_mm, write_binary_stl
from .volume import load_volume

_GENERATE_LOCK = threading.Lock()
_MODEL_RESOLUTION = "native_voxel_v1"


def _bbox(mask: np.ndarray) -> tuple[slice, slice, slice]:
    bounds = []
    for axis in range(3):
        occupied = np.flatnonzero(np.any(mask, axis=tuple(i for i in range(3) if i != axis)))
        if not len(occupied):
            raise PipelineError("blocked", "当前掩膜为空，无法显示 3D 选择预览", ["返回 CT 复核并修正掩膜"])
        bounds.append(slice(int(occupied[0]), int(occupied[-1]) + 1))
    return tuple(bounds)


def _preview_mesh(mask: np.ndarray, volume, ap_axis: int) -> tuple[np.ndarray, np.ndarray]:
    bounds = _bbox(mask)
    try:
        cropped = np.asarray(mask[bounds], dtype=np.uint8)
        padded = np.pad(cropped, [(1, 1)] * 3, mode="constant")
        vertices, faces, _, _ = measure.marching_cubes(
            padded, level=0.5, step_size=1, allow_degenerate=False,
        )
        # 裁去的仅是空白体素，补零层和裁剪偏移均还原到原始患者坐标。
        offsets = np.asarray([part.start - 1 for part in bounds], dtype=float)
        dirs = np.asarray(volume.axis_dirs, dtype=float)
        spacing = np.asarray(volume.spacing, dtype=float)
        origin = np.asarray(volume.origin, dtype=float) + (offsets * spacing) @ dirs
        points_lps_mm = voxel_to_mm(vertices, spacing, origin, volume.axis_dirs)
        return points_lps_mm, faces
    except MemoryError as exc:
        raise PipelineError(
            "failed", "本机内存不足，无法生成原始分辨率 3D 气道；未降低模型精度",
            ["关闭其他占用内存的程序后重试"],
        ) from exc
    except ValueError as exc:
        raise PipelineError(
            "blocked", "当前掩膜无法生成原始分辨率 3D 气道",
            ["返回 CT 复核并修正掩膜"],
        ) from exc


def _state(case_id: str, scope: str, requested_version: int | None) -> tuple[Path, object, np.ndarray, int, int, tuple]:
    if scope not in endplanes.SCOPES:
        raise PipelineError("failed", "建模范围无效", ["选择主鼻腔气流模型或完整候选范围"], {"scope": scope})
    root = case_dir(case_id)
    segmentation = endplanes._segmentation(root)
    version = segmentation.get("mask_version")
    if type(version) is not int:
        raise PipelineError("blocked", "旧掩膜缺少版本记录，不能生成 3D 选择预览", ["重新分割并确认掩膜"])
    if requested_version is not None and (type(requested_version) is not int or requested_version != version):
        raise PipelineError("blocked", "掩膜版本已变化，3D 选择预览没有加载", ["刷新工作台后重新选择端面"])
    if not has_current_external_air_validation(segmentation) or segmentation.get("need_human_confirm", True):
        raise PipelineError(
            "blocked", "气道掩膜尚未清理并人工确认，不能显示 3D 选择预览",
            ["先去除体外空气并在 CT 中确认气道范围"],
        )
    if scope == "full_candidate" and not bool(segmentation.get("candidate_mask_available", False)):
        raise PipelineError("blocked", "完整候选掩膜尚不可用", ["先完成体外空气清理并复核完整候选"])
    volume = load_volume(case_id)
    ap_axis, _, _ = endplanes._geometry(segmentation, volume)
    mask = endplanes._mask(root, scope, volume.shape)
    mask_path = root / "segment" / endplanes.SCOPES[scope]
    seg_path = root / "segment" / "segmentation.json"
    meta_path = root / "volume" / "meta.json"
    stamp = tuple((path.stat().st_size, path.stat().st_mtime_ns) for path in (mask_path, seg_path, meta_path))
    return root, volume, mask, ap_axis, version, stamp


def selection_model(case_id: str, scope: str = "main_airway", mask_version: int | None = None) -> tuple[Path, int]:
    """返回原始体素分辨率的选端 STL 路径及掩膜版本，不改正式阶段。"""
    with _GENERATE_LOCK:
        root, volume, mask, ap_axis, version, stamp = _state(case_id, scope, mask_version)
        fingerprint = hashlib.sha256(json.dumps((_MODEL_RESOLUTION, scope, version, stamp), separators=(",", ":")).encode()).hexdigest()[:16]
        path = root / "preview" / f"selection-model-{scope}-{version}-{fingerprint}.stl"
        if path.exists() and path.stat().st_size >= 84:
            return path, version
        vertices, faces = _preview_mesh(mask, volume, ap_axis)
        # 掩膜编辑可能在网格计算期间并发完成。旧结果不能落盘或被当作新版本返回。
        _, _, _, _, final_version, final_stamp = _state(case_id, scope, version)
        if final_version != version or final_stamp != stamp:
            raise PipelineError("blocked", "生成 3D 预览期间掩膜发生变化", ["刷新工作台后重新加载 3D 预览"])
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
        try:
            write_binary_stl(temp, vertices, faces)
            _, _, _, _, final_version, final_stamp = _state(case_id, scope, version)
            if final_version != version or final_stamp != stamp:
                raise PipelineError("blocked", "保存 3D 预览期间掩膜发生变化", ["刷新工作台后重新加载 3D 预览"])
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
        return path, version
