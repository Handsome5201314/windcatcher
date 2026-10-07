"""风见（Windcatcher）全局配置与 SOP 默认参数。

SOP 依据：项目原始资料 / 飞书知识库 / 鼻腔气流CFD模拟SOP_2.0_修订版.md
所有阈值都以参数形式留痕，禁止把任何参考样本的数值当作默认值。
"""

from __future__ import annotations

from pathlib import Path

# 目录
APP_DIR = Path(__file__).resolve().parent.parent          # 开发/windcatcher
PROJECT_ROOT = APP_DIR.parents[1]                          # 鼻腔气流CFD比赛
CASES_DIR = APP_DIR / "cases"                              # 本地病例产物（禁止入库/外传）
TEST_DATA_DIR = APP_DIR / "tests" / "phantom_ct"           # 合成测试 CT（非患者数据）

# 仅用于排除体素离散化产生的微小边界环，不代表临床上合格的鼻孔/鼻咽端面面积。
MIN_OPENING_AREA_MM2 = 1.0

# 阶段顺序，状态机依赖
STAGES = (
    "import",
    "anonymize",
    "qc",
    "segment",
    "surface",
    "cfd_prep",
    "cfd_run",
    "report",
)

# SOP 与工况默认值
SOP_DEFAULTS = {
    # 分割阈值（HU）：SOP 4.3 下限 -1024、上限约 -300
    "hu_low": -1024,
    "hu_high": -300,
    # 输入质控（SOP 3.2）：层厚 ≤1mm 合格，>3mm 判不合格
    "max_slice_thickness_mm": 1.0,
    "hard_fail_slice_thickness_mm": 3.0,
    # 工况（SOP 6.4）：成人静息经鼻气流约 15 L/min（双侧合计）
    "total_flow_lpm": 15.0,
    # 空气物性（SOP 6.3）：ρ≈1.2 kg/m³，μ≈1.8e-5 Pa·s → ν≈1.5e-5 m²/s
    "air_nu": 1.5e-5,
    "air_rho": 1.2,
    # 网格与几何
    "background_cell_mm": 1.0,
    "cfd_margin_mm": 5.0,
    "target_face_count_min": 30000,
    "target_face_count_max": 100000,
}

# 需要检查的识别性 DICOM 标签（只记录"是否存在"，绝不记录取值）
IDENTIFYING_TAGS = (
    "PatientName",
    "PatientID",
    "PatientBirthDate",
    "PatientSex",
    "PatientAge",
    "PatientAddress",
    "PatientTelephoneNumbers",
    "InstitutionName",
    "InstitutionAddress",
    "ReferringPhysicianName",
    "AccessionNumber",
    "StudyID",
    "StudyDate",
    "SeriesDate",
    "OperatorsName",
    "RequestingPhysician",
)

# 去标识时清空/改写的标签
ANONYMIZE_CLEAR_TAGS = (
    "PatientName",
    "PatientBirthDate",
    "PatientAddress",
    "PatientTelephoneNumbers",
    "InstitutionName",
    "InstitutionAddress",
    "ReferringPhysicianName",
    "AccessionNumber",
    "OperatorsName",
    "RequestingPhysician",
    "StudyID",
)

DEIDENTIFICATION_METHOD = "风见本地去标识 v1（清空直接标识符，保留几何与像素数据）"

# 产品定位声明，出现在报告与界面
DISCLAIMER = (
    "研究原型，仅用于鼻腔气流建模与结果复核；不用于诊断、治疗或手术决策。"
    "稳态求解表示指定边界条件下的稳定流场，不等于逐帧模拟真实呼吸周期。"
)


def ensure_dirs() -> None:
    """确保病例根目录存在。"""
    CASES_DIR.mkdir(parents=True, exist_ok=True)
