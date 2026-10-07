"""只读的端面选择用 3D 预览，来源始终是当前已确认的掩膜。

预览会降低横断面分辨率以限制内存和浏览器网格大小；其三角面不参与
正式 STL、几何质检或 CFD。前后轴不降采样，便于把 3D 选点投回 CT 层号。
"""

from __future__ import annotations

import hashlib
import json
import math
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
_TARGET_VOXELS = 6_000_000
_MAX_FACES = 500_000
_MAX_INPLANE_FACTOR = 12


def _bbox(mask: np.ndarray) -> tuple[slice, slice, slice]:
    bounds = []
    for axis in range(3):
        occupied = np.flatnonzero(np.any(mask, axis=tuple(i for i in range(3) if i != axis)))
        if not len(occupied):
            raise PipelineError("blocked", "当前掩膜为空，无法显示 3D 选择预览", ["返回 CT 复核并修正掩膜"])
        bounds.append(slice(int(occupied[0]), int(occupied[-1]) + 1))
    return tuple(bounds)


def _pooled(mask: np.ndarray, bounds: tuple[slice, slice, slice], ap_axis: int, factor: int) -> np.ndarray:
    """仅对非前后轴做块最大值降采样，保留每一个 AP 层。"""
    factors = [1 if axis == ap_axis else factor for axis in range(3)]
    cropped = np.asarray(mask[bounds], dtype=np.uint8)
    pads = [(0, (-cropped.shape[axis]) % factors[axis]) for axis in range(3)]
    padded = np.pad(cropped, pads, mode="constant")
    blocks = padded.reshape(
        padded.shape[0] // factors[0], factors[0],
        padded.shape[1] // factors[1], factors[1],
        padded.shape[2] // factors[2], factors[2],
    )
    return blocks.max(axis=(1, 3, 5))


def _preview_mesh(mask: np.ndarray, volume, ap_axis: int) -> tuple[np.ndarray, np.ndarray]:
    bounds = _bbox(mask)
    size = math.prod(part.stop - part.start for part in bounds)
    factor = max(2, math.ceil(math.sqrt(size / _TARGET_VOXELS)))
    for factor in range(factor, _MAX_INPLANE_FACTOR + 1):
        factors = np.asarray([1 if axis == ap_axis else factor for axis in range(3)], dtype=float)
        pooled = _pooled(mask, bounds, ap_axis, factor)
        padded = np.pad(pooled, [(1, 1)] * 3, mode="constant")
        try:
            vertices, faces, _, _ = measure.marching_cubes(padded, level=0.5, allow_degenerate=False)
        except ValueError as exc:
            raise PipelineError(
                "blocked", "当前掩膜无法生成 3D 选择预览",
                ["返回 CT 复核并修正掩膜"],
            ) from exc
        if len(faces) > _MAX_FACES:
            continue
        # pooled 体素中心对应原始块中心；最外一圈是补零层。
        first_center = np.asarray([part.start for part in bounds], dtype=float) + (factors - 1) / 2
        first_padded = first_center - factors
        dirs = np.asarray(volume.axis_dirs, dtype=float)
        spacing = np.asarray(volume.spacing, dtype=float)
        origin = np.asarray(volume.origin, dtype=float) + (first_padded * spacing) @ dirs
        points_lps_mm = voxel_to_mm(vertices, spacing * factors, origin, volume.axis_dirs)
        return points_lps_mm, faces
    raise PipelineError(
        "blocked", "3D 选择预览超出本机网格大小限制",
        ["先在 CT 中缩小或修正气道候选范围，再重试 3D 预览"],
        {"max_faces": _MAX_FACES},
    )


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
    """返回当前掩膜的低分辨率预览 STL 路径及其版本，不改正式阶段。"""
    with _GENERATE_LOCK:
        root, volume, mask, ap_axis, version, stamp = _state(case_id, scope, mask_version)
        fingerprint = hashlib.sha256(json.dumps((scope, version, stamp), separators=(",", ":")).encode()).hexdigest()[:16]
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
