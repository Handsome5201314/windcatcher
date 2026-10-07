"""本地去标识：生成匿名化副本，源文件只读。

注意：清空 DICOM 标签不等于去除体数据中的面部轮廓（defacing），这一点会写进结果提示。
"""

from __future__ import annotations

from pathlib import Path

from .config import ANONYMIZE_CLEAR_TAGS, DEIDENTIFICATION_METHOD
from .errors import PipelineError

# 必须保留的几何/灰度标签，缺一不可
GEOMETRY_TAGS = (
    "ImagePositionPatient",
    "ImageOrientationPatient",
    "PixelSpacing",
    "SliceThickness",
    "SpacingBetweenSlices",
    "Rows",
    "Columns",
    "RescaleSlope",
    "RescaleIntercept",
    "SeriesInstanceUID",
    "StudyInstanceUID",
    "SOPInstanceUID",
    "SOPClassUID",
    "InstanceNumber",
    "Modality",
)

# 时间类标签：保留研究时间线有用，但精确到秒的时间戳清空
TIME_TAGS_TO_CLEAR = ("InstanceCreationDate", "InstanceCreationTime", "StudyTime", "SeriesTime")


def anonymize_series(src_dir: str | Path, dst_dir: str | Path, case_id: str) -> dict:
    """把源序列复制成匿名化副本（PatientID 替换为 case_id）。"""
    import pydicom

    src = Path(src_dir)
    dst = Path(dst_dir)
    dst.mkdir(parents=True, exist_ok=True)

    written = 0
    cleared: list[str] = []
    geometry_kept: dict[str, int] = {}
    for index, path in enumerate(sorted(src.rglob("*"))):
        if not path.is_file():
            continue
        try:
            ds = pydicom.dcmread(str(path))
        except Exception:
            continue
        if not hasattr(ds, "PixelData"):
            continue

        for tag in ANONYMIZE_CLEAR_TAGS:
            if hasattr(ds, tag):
                setattr(ds, tag, "")
                cleared.append(tag)
        ds.PatientID = case_id
        ds.PatientName = ""
        ds.DeidentificationMethod = DEIDENTIFICATION_METHOD
        for tag in TIME_TAGS_TO_CLEAR:
            if hasattr(ds, tag):
                setattr(ds, tag, "")

        for tag in GEOMETRY_TAGS:
            if hasattr(ds, tag):
                geometry_kept[tag] = geometry_kept.get(tag, 0) + 1

        ds.save_as(str(dst / f"{index:05d}.dcm"), enforce_file_format=True)
        written += 1

    if written == 0:
        raise PipelineError(
            "failed",
            f"源目录没有可处理的 DICOM 文件：{src}",
            ["确认目录内是 DICOM 序列", "压缩包请先解压"],
        )

    return {
        "source_dir": str(src),
        "output_dir": str(dst),
        "files_written": written,
        "cleared_tags": sorted(set(cleared)),
        "patient_id_replaced_with": case_id,
        "geometry_tags_preserved": geometry_kept,
        "residual_risk": [
            "DICOM 标签已去标识，但 CT 体数据中仍可能存在可识别的面部轮廓",
            "对外共享前需按数据范围评估是否要做 defacing 或裁剪",
        ],
        "method": DEIDENTIFICATION_METHOD,
    }
