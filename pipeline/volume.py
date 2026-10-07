"""DICOM 序列 → HU 体数据。

只读取与几何、灰度相关的信息；患者标识信息的值不会被写入任何产物。
数组约定：(z, y, x) —— 轴 0 为层面方向，轴 1 为行方向，轴 2 为列方向。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .errors import PipelineError

ANTERIOR_LPS = (0.0, -1.0, 0.0)  # DICOM 患者坐标系 LPS：+X 左、+Y 后、+Z 上


@dataclass
class Volume:
    """HU 体数据与其几何描述。"""

    array: np.ndarray                      # float32，(z, y, x)，单位 HU
    spacing: tuple[float, float, float]    # 每个数组轴的体素间距（mm），顺序 (z, y, x)
    origin: tuple[float, float, float]     # 体素 (0,0,0) 的患者坐标（mm，LPS）
    axis_dirs: tuple[tuple[float, float, float], ...]  # 每个数组轴在患者坐标系下的单位方向
    meta: dict = field(default_factory=dict)

    @property
    def shape(self) -> tuple[int, int, int]:
        z, y, x = self.array.shape
        return int(z), int(y), int(x)


def _cross(a, b) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _dot(a, b) -> float:
    return sum(float(x) * float(y) for x, y in zip(a, b))


def _looks_like_hu(arr: np.ndarray) -> bool:
    """判断是否已经是 HU 量纲（空气应接近 -1000）。"""
    return float(np.min(arr)) < -200.0


def _read_rescale(dicom_dir: Path) -> tuple[float, float]:
    """读取第一个文件的 RescaleSlope / RescaleIntercept。"""
    import pydicom

    for path in sorted(dicom_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            ds = pydicom.dcmread(str(path), stop_before_pixels=True)
        except Exception:
            continue
        slope = float(getattr(ds, "RescaleSlope", 1.0) or 1.0)
        intercept = float(getattr(ds, "RescaleIntercept", 0.0) or 0.0)
        return slope, intercept
    return 1.0, 0.0


def _load_with_simpleitk(dicom_dir: Path) -> Volume:
    import SimpleITK as sitk

    reader = sitk.ImageSeriesReader()
    files = reader.GetGDCMSeriesFileNames(str(dicom_dir))
    if not files:
        raise PipelineError("failed", "SimpleITK 未在该目录下找到 DICOM 序列", ["确认目录内是同一序列的 .dcm 文件"])
    reader.SetFileNames(files)
    image = reader.Execute()

    arr = sitk.GetArrayFromImage(image)          # (z, y, x)
    sx, sy, sz = image.GetSpacing()              # ITK 轴顺序 (x, y, z)
    d = image.GetDirection()                     # 列主序 3x3：列为 x/y/z 轴方向
    # SimpleITK 的 direction 是按行展开的 3x3 矩阵，矩阵列对应图像 x/y/z 轴；
    # GetArrayFromImage 反转数组轴为 z/y/x，因此这里按列映射回 numpy 轴。
    axis_dirs = (
        (d[2], d[5], d[8]),   # 数组轴 0（z）
        (d[1], d[4], d[7]),   # 数组轴 1（y）
        (d[0], d[3], d[6]),   # 数组轴 2（x）
    )
    spacing = (float(sz), float(sy), float(sx))

    slope, intercept = _read_rescale(dicom_dir)
    hu_note = "simpleitk_native"
    values = arr.astype(np.float32)
    if not _looks_like_hu(values) and (abs(slope - 1.0) > 1e-6 or abs(intercept) > 1e-6):
        values = values * slope + intercept
        hu_note = "pydicom_rescale_applied"
    elif not _looks_like_hu(values):
        hu_note = "rescale_missing_or_already_hu_unverified"

    meta = {
        "loader": "SimpleITK",
        "files": len(files),
        "shape": list(values.shape),
        "spacing_mm": list(spacing),
        "origin_mm": list(image.GetOrigin()),
        "axis_dirs": [list(v) for v in axis_dirs],
        "hu_min": float(np.min(values)),
        "hu_max": float(np.max(values)),
        "hu_transform": hu_note,
        "source_dir": str(dicom_dir),
    }
    return Volume(array=values, spacing=spacing, origin=tuple(image.GetOrigin()), axis_dirs=axis_dirs, meta=meta)


def _load_with_pydicom(dicom_dir: Path) -> Volume:
    import pydicom

    datasets = []
    for path in sorted(dicom_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            ds = pydicom.dcmread(str(path))
        except Exception:
            continue
        if not hasattr(ds, "PixelData"):
            continue
        datasets.append((path, ds))
    if not datasets:
        raise PipelineError(
            "failed",
            "目录内没有可读取像素的 DICOM 文件",
            ["确认目录内是未加密的 DICOM 文件", "压缩传输语法（JPEG2000 等）需要 SimpleITK 或额外解码器"],
        )

    first = datasets[0][1]
    row_cos = tuple(float(v) for v in getattr(first, "ImageOrientationPatient", [1, 0, 0, 0, 1, 0])[0:3])
    col_cos = tuple(float(v) for v in getattr(first, "ImageOrientationPatient", [1, 0, 0, 0, 1, 0])[3:6])
    normal = _cross(row_cos, col_cos)

    def sort_key(item):
        ds = item[1]
        ipp = getattr(ds, "ImagePositionPatient", None)
        if ipp is not None:
            return _dot(tuple(float(v) for v in ipp), normal)
        return float(getattr(ds, "InstanceNumber", 0) or 0)

    datasets.sort(key=sort_key)

    slices = []
    for _, ds in datasets:
        arr = ds.pixel_array.astype(np.float32)
        slope = float(getattr(ds, "RescaleSlope", 1.0) or 1.0)
        intercept = float(getattr(ds, "RescaleIntercept", 0.0) or 0.0)
        slices.append(arr * slope + intercept)
    values = np.stack(slices, axis=0)

    pixel_spacing = getattr(first, "PixelSpacing", [1.0, 1.0])
    row_spacing = float(pixel_spacing[0])   # 相邻行之间的距离 → 数组轴 1
    col_spacing = float(pixel_spacing[1])   # 相邻列之间的距离 → 数组轴 2
    slice_thickness = float(getattr(first, "SliceThickness", 0.0) or 0.0)

    z_positions = []
    for _, ds in datasets:
        ipp = getattr(ds, "ImagePositionPatient", None)
        if ipp is not None:
            z_positions.append(_dot(tuple(float(v) for v in ipp), normal))
    if len(z_positions) >= 2:
        diffs = np.abs(np.diff(np.asarray(z_positions, dtype=float)))
        slice_spacing = float(np.median(diffs[diffs > 0])) if np.any(diffs > 0) else slice_thickness
    else:
        slice_spacing = slice_thickness
    if slice_spacing <= 0:
        slice_spacing = slice_thickness if slice_thickness > 0 else 1.0

    ipp0 = getattr(datasets[0][1], "ImagePositionPatient", [0.0, 0.0, 0.0])
    origin = tuple(float(v) for v in ipp0)
    axis_dirs = (normal, col_cos, row_cos)
    spacing = (slice_spacing, row_spacing, col_spacing)

    meta = {
        "loader": "pydicom",
        "files": len(datasets),
        "shape": list(values.shape),
        "spacing_mm": list(spacing),
        "origin_mm": list(origin),
        "axis_dirs": [list(v) for v in axis_dirs],
        "hu_min": float(np.min(values)),
        "hu_max": float(np.max(values)),
        "hu_transform": "pydicom_rescale_applied",
        "slice_thickness_tag": slice_thickness,
        "computed_slice_spacing_mm": slice_spacing,
        "source_dir": str(dicom_dir),
    }
    return Volume(array=values, spacing=spacing, origin=origin, axis_dirs=axis_dirs, meta=meta)


def load_series(dicom_dir: str | Path) -> Volume:
    """载入 DICOM 序列为 HU 体数据。优先 SimpleITK（支持压缩传输语法），回退 pydicom。"""
    path = Path(dicom_dir)
    if not path.exists():
        raise PipelineError("failed", f"目录不存在：{path}", ["检查路径是否正确"])
    try:
        return _load_with_simpleitk(path)
    except PipelineError:
        raise
    except Exception as exc:
        try:
            volume = _load_with_pydicom(path)
        except PipelineError:
            raise
        except Exception as fallback_exc:
            raise PipelineError(
                "failed",
                f"无法读取该目录的 DICOM 序列：{exc}",
                [
                    "确认目录内是同一序列的 DICOM 文件",
                    f"pydicom 回退也失败：{fallback_exc}",
                ],
            ) from fallback_exc
        volume.meta["simpleitk_error"] = str(exc)
        return volume


def save_volume(case_id: str, volume: Volume) -> Path:
    from .cases import case_dir

    root = case_dir(case_id)
    root.mkdir(parents=True, exist_ok=True)
    np.save(root / "volume" / "hu.npy", volume.array.astype(np.float32))
    meta_path = root / "volume" / "meta.json"
    meta = dict(volume.meta)
    meta.update(
        {
            "spacing_mm": list(volume.spacing),
            "origin_mm": list(volume.origin),
            "axis_dirs": [list(v) for v in volume.axis_dirs],
        }
    )
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta_path


def load_volume(case_id: str) -> Volume:
    from .cases import case_dir

    root = case_dir(case_id)
    array_path = root / "volume" / "hu.npy"
    meta_path = root / "volume" / "meta.json"
    if not array_path.exists() or not meta_path.exists():
        raise PipelineError(
            "failed",
            "该病例还没有体数据",
            ["先执行导入与质控阶段，生成 volume/hu.npy"],
        )
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return Volume(
        # 逐层浏览时不能反复把数百 MB 的体数据读进内存；memmap 保持接口不变，
        # 只有实际访问的切片才会被操作系统载入。
        array=np.load(array_path, mmap_mode="r"),
        spacing=tuple(float(v) for v in meta["spacing_mm"]),
        origin=tuple(float(v) for v in meta["origin_mm"]),
        axis_dirs=tuple(tuple(float(x) for x in v) for v in meta["axis_dirs"]),
        meta=meta,
    )


def resolve_ap_axis(meta: dict) -> tuple[int, str]:
    """判定前后（A-P）轴：鼻孔端在前、鼻咽端在后。

    返回 (轴索引, 判定依据)。方向标签缺失时回退到轴 1（行方向）并明确标注需人工确认。
    """
    axis_dirs = meta.get("axis_dirs")
    if not axis_dirs:
        return 1, "缺少方向信息，回退到行方向轴（需人工确认鼻孔/鼻咽端）"
    scores = [abs(_dot(tuple(float(x) for x in v), ANTERIOR_LPS)) for v in axis_dirs]
    idx = int(np.argmax(scores))
    if scores[idx] < 0.7:
        return idx, f"方向标签与患者前后轴匹配度仅 {scores[idx]:.2f}，需人工确认鼻孔与鼻咽端"
    return idx, "依据 ImageOrientationPatient 判定"


def anterior_sign(meta: dict, axis: int) -> float:
    """返回该轴上"索引增大是否指向前方"的符号（+1 增大即向前，-1 增大即向后）。"""
    axis_dirs = meta.get("axis_dirs")
    if not axis_dirs:
        return -1.0
    d = _dot(tuple(float(x) for x in axis_dirs[axis]), ANTERIOR_LPS)
    return 1.0 if d > 0 else -1.0
