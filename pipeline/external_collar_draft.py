"""Build a read-only, case-bound draft of finite air outside both nostrils.

This entry point never persists candidates or changes a confirmed airway,
port, surface, or CFD result. The returned arrays are not CFD inputs.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Sequence

import numpy as np

from .cases import case_dir
from .errors import PipelineError
from .external_collar import build_external_collar_candidates
from .mask_review import EXTERNAL_AIR_CLOSURE_MM, _body_envelope, has_current_external_air_validation
from .segmentation_learning import case_operation
from .volume import load_volume


FORMAT = "external_nostril_collar_case_draft_v1"


def _blocked(reason: str) -> None:
    raise PipelineError(
        "blocked", reason,
        ["重新确认当前气道掩膜和 CT，再生成外侧空气候选；草稿不能直接用于 CFD"],
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise PipelineError("failed", "候选来源文件无法读取", ["检查病例体数据和掩膜文件是否完整"]) from exc
    return digest.hexdigest()


def _segmentation_bytes(path: Path) -> tuple[bytes, dict]:
    try:
        data = path.read_bytes()
        record = json.loads(data)
    except (OSError, ValueError) as exc:
        raise PipelineError("blocked", "没有可用的气道分割记录", ["先完成分割和气道范围确认"]) from exc
    if not isinstance(record, dict):
        _blocked("气道分割记录格式无效")
    return data, record


def _threshold(value: object, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        _blocked(f"气道分割记录缺少有效的{label}")
    return float(value)


def _source_stamp(path: Path) -> tuple[int, int, int]:
    try:
        stamp = path.stat()
    except OSError as exc:
        raise PipelineError("failed", "CT 来源文件已不可用", ["检查病例体数据是否完整"]) from exc
    return stamp.st_size, stamp.st_mtime_ns, stamp.st_ctime_ns


@case_operation
def build_case_external_collar_draft(
    case_id: str,
    *,
    expected_mask_version: int,
    left_seed_ijk: Sequence[int],
    right_seed_ijk: Sequence[int],
    max_extent_mm: float,
) -> dict:
    """Recompute uncropped CT air into isolated bilateral candidates.

    The original saved candidate mask was cropped on the anterior-posterior
    axis, so it is not a source for exterior air. The caller explicitly
    supplies the reviewed mask version, two in-mask seeds, and extent.
    """
    root = case_dir(case_id)
    segment_path = root / "segment" / "segmentation.json"
    mask_path = root / "segment" / "mask.npy"
    volume_path = root / "volume" / "hu.npy"
    volume_meta_path = root / "volume" / "meta.json"
    first_record_bytes, segmentation = _segmentation_bytes(segment_path)
    version = segmentation.get("mask_version")
    if (type(expected_mask_version) is not int or type(version) is not int
            or version != expected_mask_version):
        _blocked("当前气道掩膜版本与请求不一致")
    review = segmentation.get("human_review")
    if (not isinstance(review, dict) or review.get("status") != "已确认"
            or segmentation.get("need_human_confirm") is not False
            or not has_current_external_air_validation(segmentation)):
        _blocked("当前气道掩膜尚未完成对应版本的人工确认和体外空气检查")

    params = segmentation.get("params")
    if not isinstance(params, dict):
        _blocked("气道分割记录缺少 HU 阈值来源")
    hu_low = _threshold(params.get("hu_low"), "HU 下限")
    hu_high = _threshold(params.get("hu_high"), "HU 上限")
    if hu_low > hu_high:
        _blocked("气道分割记录的 HU 上下限颠倒")
    body_hu = _threshold(
        segmentation["external_air_validation"].get("body_hu_threshold"),
        "体内包络 HU 阈值",
    )
    if body_hu < hu_high:
        _blocked("体内包络阈值低于气道空气阈值，来源不一致")

    volume_stamp = _source_stamp(volume_path)
    meta_stamp = _source_stamp(volume_meta_path)
    volume_sha256 = _file_sha256(volume_path)
    meta_sha256 = _file_sha256(volume_meta_path)
    try:
        mask_sha256 = _file_sha256(mask_path)
        mask = np.load(mask_path, allow_pickle=False)
        volume = load_volume(case_id)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PipelineError("failed", "无法读取当前掩膜或 CT 体数据", ["检查病例产物是否完整"]) from exc
    if mask.ndim != 3 or mask.shape != volume.shape or not mask.any():
        _blocked("已确认掩膜与 CT 尺寸不一致或为空")
    if not np.isfinite(volume.array).all():
        _blocked("CT 体数据含非有限 HU 值，不能提取外侧空气")
    raw_air = (volume.array >= hu_low) & (volume.array <= hu_high)
    envelope, closure_radius_pixels = _body_envelope(
        volume.array, volume.spacing, body_hu, EXTERNAL_AIR_CLOSURE_MM,
    )
    candidates = build_external_collar_candidates(
        mask, raw_air, envelope, volume.spacing, volume.axis_dirs,
        left_seed_ijk, right_seed_ijk, max_extent_mm,
    )

    if (_segmentation_bytes(segment_path)[0] != first_record_bytes
            or _file_sha256(mask_path) != mask_sha256
            or _source_stamp(volume_path) != volume_stamp
            or _source_stamp(volume_meta_path) != meta_stamp
            or _file_sha256(volume_path) != volume_sha256
            or _file_sha256(volume_meta_path) != meta_sha256):
        _blocked("候选计算期间 CT 或掩膜已变化，草稿已作废")

    candidates.update({
        "format": FORMAT,
        "case_id": case_id,
        "source": {
            "mask_version": version,
            "mask_sha256": mask_sha256,
            "segmentation_sha256": hashlib.sha256(first_record_bytes).hexdigest(),
            "volume_sha256": volume_sha256,
            "volume_meta_sha256": meta_sha256,
            "segmentation_algorithm_version": segmentation.get("algorithm_version"),
            "raw_air": "volume/hu.npy:uncropped_hu_window",
            "hu_low": hu_low,
            "hu_high": hu_high,
            "left_seed_ijk": [int(value) for value in left_seed_ijk],
            "right_seed_ijk": [int(value) for value in right_seed_ijk],
            "body_envelope": "ct_axial_slice_closing_fill_holes",
            "body_hu_threshold": body_hu,
            "closure_radius_mm": EXTERNAL_AIR_CLOSURE_MM,
            "closure_radius_pixels": closure_radius_pixels,
        },
    })
    return candidates


__all__ = ["FORMAT", "build_case_external_collar_draft"]
