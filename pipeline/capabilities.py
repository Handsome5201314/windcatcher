"""本机能力与依赖探测。

原则：没装就是没装，探测不到就是探测不到。
- available：可用，给出版本/路径
- missing：确认缺失，给出安装建议
- unknown：无法判定（例如 wsl.exe 被安全策略拦截），给出人工确认建议
绝不允许把 missing/unknown 当成可用能力。
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from . import openfoam_runner

SLICER_CANDIDATES = (
    r"C:\Program Files\Slicer 5.6.1\Slicer.exe",
    r"C:\Program Files\Slicer 5.7.0\Slicer.exe",
    r"C:\Program Files\Slicer 5.8.0\Slicer.exe",
    r"C:\Program Files\Slicer\Slicer.exe",
    r"C:\Program Files (x86)\Slicer\Slicer.exe",
)

SLICER_GLOB_ROOTS = (
    Path(r"C:\Program Files"),
    Path(r"C:\Program Files (x86)"),
    Path(os.path.expanduser("~")) / "AppData" / "Local" / "NA-MIC",
)

MODEL_CACHE_CANDIDATES = (
    Path(os.path.expanduser("~")) / ".cache" / "nnunetv2",
    Path(os.path.expanduser("~")) / ".nnunetv2",
    Path(__file__).resolve().parent.parent / "models",
)


def _item(
    name: str,
    status: str,
    detail=None,
    remediation=None,
    install_target: str | None = None,
    install_label: str | None = None,
    install_url: str | None = None,
) -> dict:
    item = {
        "name": name,
        "status": status,
        "detail": detail,
        "remediation": list(remediation or []),
    }
    if install_target:
        item["install"] = {
            "target": install_target,
            "label": install_label or "安装",
        }
    elif install_url:
        item["install"] = {
            "url": install_url,
            "label": install_label or "打开安装说明",
        }
    return item


def _probe_packages() -> list[dict]:
    specs = [
        ("numpy", "numpy", "pip install numpy", "python:numpy"),
        ("scipy", "scipy", "pip install scipy", "python:scipy"),
        ("scikit-image", "skimage", "pip install scikit-image", "python:scikit-image"),
        ("pydicom", "pydicom", "pip install pydicom", "python:pydicom"),
        ("SimpleITK", "SimpleITK", "pip install SimpleITK", "python:SimpleITK"),
        ("VTK", "vtk", "pip install vtk", "python:vtk"),
        ("Pillow", "PIL", "pip install pillow", "python:Pillow"),
        ("requests", "requests", "pip install requests", "python:requests"),
        ("fastapi", "fastapi", "pip install fastapi", "python:fastapi"),
        ("uvicorn", "uvicorn", "pip install 'uvicorn[standard]'", "python:uvicorn"),
    ]
    out = []
    for display, module, cmd, install_target in specs:
        try:
            mod = importlib.import_module(module)
            version = getattr(mod, "__version__", "未知版本")
            out.append(_item(f"Python 包 {display}", "available", f"{version}"))
        except Exception:
            out.append(
                _item(
                    f"Python 包 {display}",
                    "missing",
                    None,
                    [f"在本项目虚拟环境中执行 {cmd}", "本项目用 .venv/Scripts/python.exe 运行，装包也用它"],
                    install_target=install_target,
                    install_label="安装依赖",
                )
            )
    return out


def _find_slicer() -> Path | None:
    env_path = os.environ.get("SLICER_PATH")
    if env_path and Path(env_path).exists():
        return Path(env_path)
    for candidate in SLICER_CANDIDATES:
        if Path(candidate).exists():
            return Path(candidate)
    roots = list(SLICER_GLOB_ROOTS)
    if os.name == "nt":
        # 便携版 Slicer 常被解压到非系统盘（例如 E:\3D Slicer 5.12.4）。
        roots.extend(Path(f"{letter}:\\") for letter in "DEFGHIJKLMNOPQRSTUVWXYZ" if Path(f"{letter}:\\").exists())
    for root in roots:
        if not root.exists():
            continue
        try:
            for pattern in ("Slicer*", "3D Slicer*"):
                for folder in root.glob(pattern):
                    exe = folder / "Slicer.exe"
                    if exe.exists():
                        return exe
        except OSError:
            continue
    which = shutil.which("Slicer")
    return Path(which) if which else None


def find_slicer() -> Path | None:
    """返回已安装的 Slicer 可执行文件路径，供本地复核入口复用。"""
    return _find_slicer()


def _find_executable(names: tuple[str, ...]) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def _find_desktop_tool(tool: str) -> str | None:
    """查找桌面工具的 PATH 安装和常见 Windows 安装目录。"""
    command = _find_executable((tool,))
    if command:
        return command
    if os.name != "nt":
        return None

    program_files = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    program_files_x86 = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    roots = [program_files, program_files_x86, Path("E:\\"), Path(os.path.expanduser("~")) / "AppData" / "Local" / "Programs"]
    patterns = {
        "blender": ("Blender Foundation/Blender*/blender.exe", "Blender*/blender.exe"),
        "paraview": ("ParaView*/bin/paraview.exe", "ParaView*/paraview.exe", "paraview.exe"),
    }.get(tool, (f"{tool}.exe",))
    for root in roots:
        if not root.exists():
            continue
        for pattern in patterns:
            try:
                match = next(root.glob(pattern), None)
            except OSError:
                match = None
            if match and match.is_file():
                return str(match)
    return None


def _run(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=False)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except PermissionError as exc:
        return 126, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", "执行超时"
    except OSError as exc:
        return 125, "", str(exc)


def _probe_slicer() -> dict:
    path = _find_slicer()
    if path:
        return _item("3D Slicer", "available", str(path))
    return _item(
        "3D Slicer",
        "missing",
        None,
        [
            "从 slicer.org 下载 Stable Release 安装（不要用 Preview 版）",
            "安装后可用环境变量 SLICER_PATH 指向 Slicer.exe",
            "当前阶段不影响运行：分割走 SOP 阈值/连通域基线，Slicer 用于人工复核与 STL 导出",
        ],
        install_label="打开 3D Slicer 下载页",
        install_url="https://download.slicer.org/",
    )


def _probe_openfoam() -> dict:
    detection = openfoam_runner.detect_backends()
    available = {item["name"]: item for item in detection["backends"] if item["status"] == "available"}
    if "native" in available:
        return _item("OpenFOAM（本机）", "available", available["native"].get("detail"))
    if "wsl" in available:
        return _item("OpenFOAM（WSL）", "available", available["wsl"].get("detail"))
    if "docker" in available:
        return _item(
            "OpenFOAM（Docker）",
            "available",
            available["docker"].get("detail"),
            ["当前通过 Docker 提供 CFD 求解后端，不需要额外安装 Windows 原生 OpenFOAM"],
        )
    remediation = []
    for backend in detection["backends"]:
        remediation.extend(backend.get("remediation") or [])
    remediation.append("在至少一个后端显示可用后，再开始气流求解")
    return _item(
        "OpenFOAM 求解后端",
        "missing",
        None,
        remediation,
        install_target="openfoam-image",
        install_label="拉取 OpenFOAM 镜像",
    )


def _probe_wsl() -> dict:
    code, out, err = _run(["wsl.exe", "-l", "-v"], timeout=20)
    if code == 0:
        # wsl.exe 在部分 Windows 区域设置下以 UTF-16 输出，subprocess 的
        # 本地解码会留下空字符；清理后再放进环境页，避免显示乱码。
        detail = out.replace("\x00", "").replace("\r", "").replace("\n", " | ")[:300]
        return _item("WSL", "available", detail)
    if code in (124, 125, 126, 127):
        return _item(
            "WSL",
            "unknown",
            None,
            [
                "本机执行 wsl.exe 被环境策略/权限拦截，无法判定是否已安装发行版",
                "请在允许执行 wsl.exe 的环境里手动运行 `wsl -l -v` 确认",
                "确认有 Ubuntu 发行版后，可在其中安装 OpenFOAM 供风见调用",
            ],
            install_label="查看 WSL 安装说明",
            install_url="https://learn.microsoft.com/windows/wsl/install",
        )
    return _item(
        "WSL",
        "unknown",
        None,
        [
            f"wsl 探测失败（退出码 {code}）：{err[:200]}",
            "手动运行 `wsl -l -v` 确认是否有 Linux 发行版",
        ],
        install_label="查看 WSL 安装说明",
        install_url="https://learn.microsoft.com/windows/wsl/install",
    )


def _probe_docker() -> dict:
    exe = _find_executable(("docker",))
    if not exe:
        return _item(
            "Docker",
            "missing",
            None,
            ["安装 Docker Desktop（本机已在 Program Files 发现二进制，可能是环境未启动）", "启动后再由用户决定是否拉取 OpenFOAM 镜像"],
            install_target="docker",
            install_label="安装 Docker Desktop",
        )
    code, out, err = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=20)
    if code == 0 and out:
        return _item("Docker", "available", f"守护进程已运行，ServerVersion {out}")
    return _item(
        "Docker",
        "unknown",
        None,
        [
            f"docker 存在但守护进程未响应（退出码 {code}）：{err[:200]}",
            "启动 Docker Desktop 后再试；镜像拉取需用户明确同意，本工具不会自动执行",
        ],
    )


def _probe_desktop_tools() -> list[dict]:
    paraview = _find_desktop_tool("paraview")
    blender = _find_desktop_tool("blender")
    return [
        _item(
            "ParaView",
            "available" if paraview else "missing",
            paraview,
            [] if paraview else ["从 paraview.org 安装 5.11 或更新版本，用于本地检查网格与后处理结果"],
            install_target=None if paraview else "paraview",
            install_label="安装 ParaView",
        ),
        _item(
            "Blender",
            "available" if blender else "missing",
            blender,
            [] if blender else ["从 blender.org 安装 LTS/Stable，用于 SOP 的人工几何复核"],
            install_target=None if blender else "blender",
            install_label="安装 Blender",
        ),
    ]


def _probe_segmentation_models() -> dict:
    found = []
    for path in MODEL_CACHE_CANDIDATES:
        if path.exists():
            try:
                if any(path.iterdir()):
                    found.append(str(path))
            except OSError:
                continue
    if found:
        return _item(
            "AI 分割模型（nnU-Net / 上气道权重）",
            "unknown",
            "发现权重缓存目录：" + ", ".join(found),
            [
                "需要人工确认权重任务是否覆盖鼻腔 CFD 标签（鼻孔/鼻咽/鼻窦旁路）",
                "接入主流程前必须在授权病例上做患者级盲测",
            ],
        )
    return _item(
        "AI 分割模型（nnU-Net / 上气道权重）",
        "missing",
        None,
        [
            "本阶段不使用任何 AI 模型：分割走 SOP 阈值（-1024~-300）+ 连通域基线",
            "候选模型（如 SlicerUpperAirwaySegmentator 的 nnU-Net v2 权重）未安装，"
            "且必须通过授权病例盲测与边界协议核对后才可能接入主流程",
            "NasalSeg 只作为可复现的训练/评估基准，官方仓库无预训练权重",
        ],
    )


def probe_capabilities() -> dict:
    """返回完整的本机能力清单（按类别分组）。"""
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "groups": [
            {"title": "Python 依赖", "items": _probe_packages()},
            {
                "title": "医学影像与几何",
                "items": [_probe_slicer(), *_probe_desktop_tools(), _probe_segmentation_models()],
            },
            {"title": "CFD 求解环境", "items": [_probe_openfoam(), _probe_wsl(), _probe_docker()]},
        ],
        "notes": [
            "未安装的工具不会被当作已运行能力；缺失项只影响对应阶段，不影响其他阶段的结果真实性",
            "CFD 只有在检测到可用求解后端时才会真正求解，否则报告写“未生成 CFD 结果”",
        ],
    }


def main() -> None:
    print(json.dumps(probe_capabilities(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
