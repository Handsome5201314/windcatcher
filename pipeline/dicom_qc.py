"""输入质控：把 SOP 3.2 的 CT 质量要求变成可检查的项。

判定不了的事情（解剖范围是否覆盖鼻尖到鼻咽）明确写成"需人工确认"，不伪装成自动结论。
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import IDENTIFYING_TAGS, SOP_DEFAULTS
from .errors import PipelineError
from .volume import ANTERIOR_LPS, _dot  # noqa: SLF001 - 复用几何工具函数


def _read_headers(dicom_dir: Path) -> list:
    import pydicom

    headers = []
    for path in sorted(dicom_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            ds = pydicom.dcmread(str(path), stop_before_pixels=True)
        except Exception:
            continue
        headers.append(ds)
    return headers


def _series_summary(headers: list) -> list[dict]:
    groups: dict[str, list] = {}
    for ds in headers:
        uid = str(getattr(ds, "SeriesInstanceUID", "未知"))
        groups.setdefault(uid, []).append(ds)

    summaries = []
    for uid, items in groups.items():
        first = items[0]
        pixel_spacing = getattr(first, "PixelSpacing", None)
        thickness = getattr(first, "SliceThickness", None)
        positions = [getattr(ds, "ImagePositionPatient", None) for ds in items]
        positions = [p for p in positions if p is not None]

        si_extent = 0.0
        if len(positions) >= 2:
            values = [float(p[2]) for p in positions]
            si_extent = max(values) - min(values)

        rows = int(getattr(first, "Rows", 0) or 0)
        cols = int(getattr(first, "Columns", 0) or 0)
        iop = getattr(first, "ImageOrientationPatient", None)
        row_cos = tuple(float(v) for v in iop[0:3]) if iop else (1.0, 0.0, 0.0)
        col_cos = tuple(float(v) for v in iop[3:6]) if iop else (0.0, 1.0, 0.0)
        row_spacing = float(pixel_spacing[0]) if pixel_spacing else 0.0
        col_spacing = float(pixel_spacing[1]) if pixel_spacing else 0.0

        ap_extent = 0.0
        ap_note = "缺少方向标签，无法估算前后覆盖范围"
        if iop:
            if abs(_dot(col_cos, ANTERIOR_LPS)) >= abs(_dot(row_cos, ANTERIOR_LPS)):
                ap_extent = rows * row_spacing
                ap_note = "前后方向沿图像行方向"
            else:
                ap_extent = cols * col_spacing
                ap_note = "前后方向沿图像列方向"

        summaries.append(
            {
                "series_instance_uid": uid,
                "series_description": str(getattr(first, "SeriesDescription", "") or ""),
                "modality": str(getattr(first, "Modality", "") or ""),
                "files": len(items),
                "rows": rows,
                "columns": cols,
                "pixel_spacing_mm": [row_spacing, col_spacing],
                "slice_thickness_mm": float(thickness) if thickness else None,
                "si_extent_mm": round(si_extent, 2),
                "in_plane_ap_extent_mm": round(ap_extent, 2),
                "ap_note": ap_note,
                "reconstruction_algorithm": str(
                    getattr(first, "ReconstructionAlgorithm", getattr(first, "ConvolutionKernel", "") or "")
                ),
                "contrast_bolus_agent": str(getattr(first, "ContrastBolusAgent", "") or ""),
            }
        )
    summaries.sort(key=lambda s: s["files"], reverse=True)
    return summaries


def _privacy_flags(headers: list) -> dict:
    present = []
    for tag in IDENTIFYING_TAGS:
        for ds in headers:
            value = getattr(ds, tag, None)
            if value not in (None, ""):
                present.append(tag)
                break
    return {
        "contains_phi": bool(present),
        "identifying_tags_present": sorted(set(present)),
        "note": "去标签不能消除 CT 体数据中的面部轮廓；如涉及对外共享，还需评估 defacing",
    }


def run_qc(dicom_dir: str | Path) -> dict:
    """执行输入质控，返回可序列化结果。"""
    path = Path(dicom_dir)
    headers = _read_headers(path)
    if not headers:
        raise PipelineError(
            "failed",
            "目录内没有可读取的 DICOM 文件",
            ["确认目录内是 DICOM 文件（.dcm / 无扩展名原始文件）", "压缩包请先解压后再导入"],
        )

    series = _series_summary(headers)
    ct_series = [s for s in series if s["modality"].upper() == "CT"] or series
    primary = ct_series[0]

    reasons: list[str] = []
    thickness = primary.get("slice_thickness_mm")
    blocking = False
    verdict = "pass"

    if thickness is None:
        verdict = "warn"
        reasons.append("缺少 SliceThickness 标签，无法自动判定层厚是否满足 ≤1mm 的要求")
    elif thickness > SOP_DEFAULTS["hard_fail_slice_thickness_mm"]:
        verdict = "fail"
        blocking = True
        reasons.append(
            f"层厚 {thickness} mm 超过 {SOP_DEFAULTS['hard_fail_slice_thickness_mm']} mm，"
            "鼻腔细节会大量丢失，SOP 要求 ≤1mm（推荐 0.5mm）"
        )
    elif thickness > SOP_DEFAULTS["max_slice_thickness_mm"]:
        verdict = "warn"
        reasons.append(
            f"层厚 {thickness} mm 大于 SOP 建议的 {SOP_DEFAULTS['max_slice_thickness_mm']} mm，"
            "可以运行但细节会丢失，建议改用薄层鼻窦 CT"
        )
    else:
        reasons.append(f"层厚 {thickness} mm 满足 SOP 要求（≤1mm）")

    if primary["modality"].upper() != "CT":
        verdict = "fail"
        blocking = True
        reasons.append(f"主序列模态为 {primary['modality']}，本流程需要 CT 序列")

    if primary["files"] < 10:
        verdict = "fail"
        blocking = True
        reasons.append(f"序列仅 {primary['files']} 个文件，通常不足以覆盖鼻腔到鼻咽")

    if primary["contrast_bolus_agent"]:
        reasons.append("检测到对比剂信息；SOP 要求平扫即可，增强序列需人工确认是否可用")

    return {
        "source_dir": str(path),
        "series_count": len(series),
        "primary_series": primary,
        "all_series": series,
        "privacy": _privacy_flags(headers),
        "anatomical_coverage": {
            "status": "需人工确认",
            "reason": "自动流程无法判定扫描是否覆盖鼻尖至鼻咽，需要操作者在 3D Slicer 中确认",
            "measured": {
                "in_plane_ap_extent_mm": primary["in_plane_ap_extent_mm"],
                "si_extent_mm": primary["si_extent_mm"],
                "files": primary["files"],
            },
        },
        "verdict": verdict,
        "blocking": blocking,
        "reasons": reasons,
        "thresholds": {
            "max_slice_thickness_mm": SOP_DEFAULTS["max_slice_thickness_mm"],
            "hard_fail_slice_thickness_mm": SOP_DEFAULTS["hard_fail_slice_thickness_mm"],
        },
    }


def save_qc(case_id: str, result: dict) -> Path:
    from .cases import case_dir

    path = case_dir(case_id) / "volume" / "qc.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
