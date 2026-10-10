"""风见（Windcatcher）全局配置与 SOP 默认参数。

SOP 依据：项目原始资料 / 飞书知识库 / 鼻腔气流CFD模拟SOP_2.0_修订版.md
所有阈值都以参数形式留痕，禁止把任何参考样本的数值当作默认值。
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path


def _positive_int(value, fallback: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return fallback
    return result if result > 0 else fallback


@lru_cache(maxsize=1)
def detect_compute_capabilities() -> dict:
    """读取本机真实算力，用于选择 OpenFOAM 的 CPU 并行度。

    MPI 的“最强”模式使用物理核心数，避免把超线程当作等价的物理核心；
    nvidia-smi 只负责显示 GPU 是否存在。标准 simpleFoam 没有 CUDA 求解路径，
    因此这里明确记录 GPU 未参与，不能把显卡检测结果冒充加速结果。
    """
    logical = max(1, int(os.cpu_count() or 1))
    physical = None
    try:
        import psutil  # type: ignore
        physical = psutil.cpu_count(logical=False)
    except Exception:
        physical = None
    if not physical and platform.system() == "Windows":
        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfCores -Sum).Sum"],
                capture_output=True, text=True, timeout=3, shell=False,
            )
            physical = int((completed.stdout or "").strip()) if completed.returncode == 0 else None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            physical = None
    physical = min(logical, _positive_int(physical, logical))
    strongest = max(1, physical)
    balanced = max(1, min(strongest, round(strongest * 0.75)))

    memory_gb = None
    try:
        import psutil  # type: ignore
        memory_gb = round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except Exception:
        if platform.system() == "Windows":
            try:
                import ctypes
                class _MemoryStatus(ctypes.Structure):
                    _fields_ = [("length", ctypes.c_ulong), ("memoryLoad", ctypes.c_ulong),
                                ("totalPhys", ctypes.c_ulonglong), ("availPhys", ctypes.c_ulonglong),
                                ("totalPageFile", ctypes.c_ulonglong), ("availPageFile", ctypes.c_ulonglong),
                                ("totalVirtual", ctypes.c_ulonglong), ("availVirtual", ctypes.c_ulonglong),
                                ("availExtended", ctypes.c_ulonglong)]
                status = _MemoryStatus()
                status.length = ctypes.sizeof(_MemoryStatus)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                    memory_gb = round(status.totalPhys / (1024 ** 3), 1)
            except Exception:
                memory_gb = None

    gpu = {"detected": False, "name": None, "memory_gb": None, "used_by_simpleFoam": False}
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            completed = subprocess.run(
                [nvidia_smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3, shell=False,
            )
            line = next((line.strip() for line in (completed.stdout or "").splitlines() if line.strip()), "")
            if completed.returncode == 0 and line:
                name, _, memory = line.partition(",")
                gpu.update({"detected": True, "name": name.strip(), "memory_gb": round(float(memory.strip()) / 1024, 1) if memory.strip().replace('.', '', 1).isdigit() else None})
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return {
        "cpu": {"logical_cores": logical, "physical_cores": physical},
        "memory_gb": memory_gb,
        "gpu": gpu,
        "parallel": {"strongest": strongest, "balanced": balanced, "custom_max": logical},
        "solver_gpu_note": "标准 OpenFOAM simpleFoam 当前只使用 CPU；检测到的 GPU 不会自动参与。",
    }


def resolve_parallel_plan(parallel: dict | None = None) -> dict:
    """把页面配置解析成可留痕的真实 MPI 计划。"""
    parallel = parallel if isinstance(parallel, dict) else {}
    capabilities = detect_compute_capabilities()
    limits = capabilities["parallel"]
    configured = os.environ.get("OPENFOAM_MPI_PROCS")
    profile = str(parallel.get("profile") or parallel.get("parallel_profile") or "").strip().lower()
    if not profile and "requested_subdomains" in parallel:
        profile = "legacy"
    if not profile:
        profile = "strongest"
    if configured is not None:
        profile = "env"
        value = configured
        try:
            count = int(value)
        except (ValueError, TypeError) as exc:
            raise ValueError("OPENFOAM_MPI_PROCS 必须是 1 到 16 的整数") from exc
        # Keep the legacy environment override bounded for old scripts.  The
        # workbench's custom profile uses the detected logical-core limit below.
        if isinstance(value, bool) or str(count) != str(value).strip() or not 1 <= count <= 16:
            raise ValueError("OPENFOAM_MPI_PROCS 必须是 1 到 16 的整数")
    elif parallel.get("enabled") is False and profile == "legacy":
        count = 1
    elif profile == "strongest":
        count = limits["strongest"]
    elif profile == "balanced":
        count = limits["balanced"]
    elif profile == "custom":
        raw = parallel.get("custom_subdomains", parallel.get("requested_subdomains"))
        try:
            count = int(raw)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"自定义 MPI 进程数必须是 1 到 {limits['custom_max']} 的整数") from exc
        if isinstance(raw, bool) or str(count) != str(raw).strip() or not 1 <= count <= limits["custom_max"]:
            raise ValueError(f"自定义 MPI 进程数必须是 1 到 {limits['custom_max']} 的整数")
    elif profile == "legacy":
        value = parallel.get("requested_subdomains", 8)
        try:
            count = int(value)
        except (ValueError, TypeError) as exc:
            raise ValueError("历史算例的 MPI 子域数必须是 1 到 16 的整数") from exc
        if isinstance(value, bool) or str(count) != str(value).strip() or not 1 <= count <= 16:
            raise ValueError("历史算例的 MPI 子域数必须是 1 到 16 的整数")
    else:
        raise ValueError("并行模式必须是 strongest、balanced 或 custom")
    return {
        "profile": profile,
        "requested_subdomains": count,
        "source": "environment" if configured is not None else "workbench",
        "capabilities": capabilities,
    }


def openfoam_mpi_processes(parallel: dict | None = None) -> int:
    """兼容旧调用；新算例通过 ``parallel_profile`` 使用自动适配模式。"""
    if parallel is None or parallel == {}:
        # 保留旧脚本/旧测试的 8 核默认语义；网页新算例明确传 strongest。
        return resolve_parallel_plan({"profile": "legacy", "requested_subdomains": 8})["requested_subdomains"]
    return resolve_parallel_plan(parallel)["requested_subdomains"]

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
