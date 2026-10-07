"""合成鼻腔气道 CT phantom（用于自测，禁止当作患者数据使用）。

几何：两个前段鼻腔通道（左右粗细不同）→ 中段汇合 → 后段单一鼻咽管道；
另有前部外部空气层、两个鼻窦样空腔和一个游离小气泡，用来检验连通域筛选是否真的在工作。
输出的 DICOM 故意写入识别性标签，用于验证去标识与隐私检查。

运行：`.venv/Scripts/python.exe tests/make_phantom.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.config import TEST_DATA_DIR  # noqa: E402

# 体素与几何
SPACING = 0.5           # mm，各向同性
SHAPE = (96, 128, 128)  # (z 层面, y 前后, x 左右)
ORIGIN_LPS = (-32.0, -30.0, -25.0)

# 气道走向：沿 y（前后）轴，y 小 = 前（鼻孔），y 大 = 后（鼻咽）
NOSTRIL_Y = 8
MERGE_START_Y = 70
MERGE_END_Y = 92
NASOPHARYNX_Y = 122


def _airway_mask() -> np.ndarray:
    nz, ny, nx = SHAPE
    zz = np.arange(nz)[:, None, None]
    yy = np.arange(ny)[None, :, None]
    xx = np.arange(nx)[None, None, :]
    mask = np.zeros(SHAPE, dtype=bool)

    # 后段：单一鼻咽管道
    mask |= ((yy >= MERGE_END_Y) & (yy <= NASOPHARYNX_Y)) & ((xx - 64) ** 2 + (zz - 48) ** 2 <= 7 ** 2)

    # 汇合区：左右通道向中线收拢
    for y in range(MERGE_START_Y, MERGE_END_Y):
        s = (y - MERGE_START_Y) / float(MERGE_END_Y - MERGE_START_Y)
        for base, radius0 in ((44, 6.0), (84, 5.0)):
            center = 64 + (base - 64) * (1 - s)
            radius = radius0 + (8.0 - radius0) * s
            mask |= (yy == y) & ((xx - center) ** 2 + (zz - 48) ** 2 <= radius ** 2)

    # 前段：两条独立通道
    for y in range(NOSTRIL_Y, MERGE_START_Y):
        band = yy == y
        mask |= band & ((xx - 44) ** 2 + (zz - 48) ** 2 <= 6 ** 2)
        mask |= band & ((xx - 84) ** 2 + (zz - 48) ** 2 <= 5 ** 2)

    # 前部外部空气（应被连通域剔除）
    mask |= yy < 6

    # 鼻窦样空腔（与气道不连通，应被剔除）
    mask |= (xx - 40) ** 2 + (yy - 58) ** 2 + (zz - 72) ** 2 <= 7 ** 2
    mask |= (xx - 88) ** 2 + (yy - 58) ** 2 + (zz - 72) ** 2 <= 7 ** 2

    # 游离小气泡（应被剔除）
    mask |= (xx - 100) ** 2 + (yy - 40) ** 2 + (zz - 20) ** 2 <= 3 ** 2

    # 离散化会在管壁留下零星封闭空腔（26 连通意义下），作为"真值几何"先填平（仅针对合成数据）
    from scipy import ndimage  # noqa: PLC0415

    return ndimage.binary_fill_holes(mask, structure=np.ones((3, 3, 3), dtype=bool))


def build_volume(seed: int = 20260923) -> tuple[np.ndarray, np.ndarray]:
    """返回 (HU 体数据 float32, 气道掩膜 bool)。"""
    rng = np.random.default_rng(seed)
    airway = _airway_mask()
    hu = np.full(SHAPE, 40.0, dtype=np.float32)          # 软组织基底
    hu += rng.normal(0.0, 15.0, SHAPE).astype(np.float32)
    hu[airway] = -1000.0 + rng.normal(0.0, 12.0, int(airway.sum())).astype(np.float32)
    return hu.astype(np.float32), airway


def _write_series(hu: np.ndarray, out_dir: Path) -> int:
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

    out_dir.mkdir(parents=True, exist_ok=True)
    study_uid = generate_uid()
    series_uid = generate_uid()
    stored = np.rint(hu + 1024.0).astype(np.int16)   # RescaleIntercept=-1024 还原 HU

    for k in range(hu.shape[0]):
        file_meta = FileMetaDataset()
        file_meta.MediaStorageSOPClassUID = CTImageStorage
        file_meta.MediaStorageSOPInstanceUID = generate_uid()
        file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

        out_path = out_dir / f"slice_{k:04d}.dcm"
        ds = FileDataset(str(out_path), {}, file_meta=file_meta, preamble=b"\0" * 128)
        ds.SpecificCharacterSet = "ISO_IR 192"
        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        ds.Modality = "CT"
        ds.SeriesNumber = 1
        ds.InstanceNumber = k + 1
        ds.SeriesDescription = "风见合成测试 phantom（非患者数据）"
        ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
        ds.PatientName = "Phantom^Synthetic"
        ds.PatientID = "SYNTH-PHANTOM-001"
        ds.PatientBirthDate = "19800101"
        ds.PatientSex = "O"
        ds.InstitutionName = "风见合成测试"
        ds.AccessionNumber = "SYNTH-0001"
        ds.ReferringPhysicianName = ""
        ds.Rows = int(hu.shape[1])
        ds.Columns = int(hu.shape[2])
        ds.PixelSpacing = [SPACING, SPACING]
        ds.SliceThickness = SPACING
        ds.SpacingBetweenSlices = SPACING
        ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
        ds.ImagePositionPatient = [
            ORIGIN_LPS[0],
            ORIGIN_LPS[1],
            ORIGIN_LPS[2] + k * SPACING,
        ]
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 1
        ds.RescaleSlope = 1.0
        ds.RescaleIntercept = -1024.0
        ds.WindowCenter = -500.0
        ds.WindowWidth = 2000.0
        ds.PixelData = stored[k].tobytes()
        ds.save_as(str(out_path), enforce_file_format=True)
    return hu.shape[0]


def build_phantom(out_dir: Path | None = None) -> Path:
    """生成合成 CT 序列，返回目录路径。"""
    out_dir = Path(out_dir) if out_dir else TEST_DATA_DIR
    hu, airway = build_volume()
    count = _write_series(hu, out_dir)
    np.save(out_dir / "phantom_airway_mask.npy", airway)
    print(f"[phantom] 已生成 {count} 层合成 CT：{out_dir}")
    print(
        f"[phantom] 气道掩膜体素 {int(airway.sum())}，"
        f"总体积 {airway.sum() * SPACING ** 3 / 1000:.2f} mL（含外部空气、鼻窦与游离气泡）"
    )
    return out_dir


if __name__ == "__main__":
    build_phantom()
