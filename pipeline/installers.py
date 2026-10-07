"""受控的本机依赖安装任务。

安装目标全部来自固定白名单，页面不能传入任意命令。真正执行前由前端再次显示确认，
任务状态保存在内存中供环境页轮询；安装完成后应重新探测能力。
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from .config import APP_DIR

_LOCK = threading.Lock()
_JOBS: dict[str, dict] = {}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _job(job_id: str, **changes) -> dict:
    with _LOCK:
        item = _JOBS[job_id]
        item.update(changes)
        item["updated_at"] = _now()
        return dict(item)


def _python_executable() -> str:
    path = APP_DIR / ".venv" / "Scripts" / "python.exe"
    return str(path) if path.exists() else shutil.which("python") or "python"


def _openfoam_image() -> str:
    image = os.environ.get("OPENFOAM_IMAGE") or os.environ.get("IMAGE") or ""
    if image:
        return image.strip()
    marker = APP_DIR / ".openfoam_image"
    if marker.exists():
        return marker.read_text(encoding="utf-8").strip()
    return ""


def catalog() -> dict[str, dict]:
    """返回可展示给前端的安装目标，不暴露任意 shell 命令。"""
    return {
        "python:numpy": {"kind": "pip", "label": "安装 Python 依赖", "description": "安装到风见自己的 .venv"},
        "python:scipy": {"kind": "pip", "label": "安装 Python 依赖", "description": "安装到风见自己的 .venv"},
        "python:scikit-image": {"kind": "pip", "label": "安装 Python 依赖", "description": "安装到风见自己的 .venv"},
        "python:pydicom": {"kind": "pip", "label": "安装 Python 依赖", "description": "安装到风见自己的 .venv"},
        "python:SimpleITK": {"kind": "pip", "label": "安装 Python 依赖", "description": "安装到风见自己的 .venv"},
        "python:vtk": {"kind": "pip", "label": "安装 Python 依赖", "description": "安装到风见自己的 .venv"},
        "python:Pillow": {"kind": "pip", "label": "安装 Python 依赖", "description": "安装到风见自己的 .venv"},
        "python:requests": {"kind": "pip", "label": "安装 Python 依赖", "description": "安装到风见自己的 .venv"},
        "python:fastapi": {"kind": "pip", "label": "安装 Python 依赖", "description": "安装到风见自己的 .venv"},
        "python:uvicorn": {"kind": "pip", "label": "安装 Python 依赖", "description": "安装到风见自己的 .venv"},
        "paraview": {"kind": "winget", "label": "安装 ParaView", "description": "使用 Windows 官方 winget 源"},
        "blender": {"kind": "winget", "label": "安装 Blender", "description": "使用 Windows 官方 winget 源"},
        "docker": {"kind": "winget", "label": "安装 Docker Desktop", "description": "安装后仍需启动 Docker Desktop"},
        "openfoam-image": {"kind": "docker-image", "label": "拉取 OpenFOAM 镜像", "description": "从镜像仓库下载项目配置的本地镜像"},
    }


def _command_for(target: str) -> tuple[list[str], str]:
    if target.startswith("python:"):
        package = target.split(":", 1)[1]
        if package not in {"numpy", "scipy", "scikit-image", "pydicom", "SimpleITK", "vtk", "Pillow", "requests", "fastapi", "uvicorn"}:
            raise ValueError("不允许安装该 Python 包")
        return ([_python_executable(), "-m", "pip", "install", "--index-url", "https://pypi.org/simple", package], "Python 依赖")

    winget_ids = {
        "paraview": "Kitware.ParaView",
        "blender": "BlenderFoundation.Blender",
        "docker": "Docker.DockerDesktop",
    }
    if target in winget_ids:
        return (
            [
                "winget",
                "install",
                "--id",
                winget_ids[target],
                "--exact",
                "--accept-source-agreements",
                "--accept-package-agreements",
            ],
            winget_ids[target],
        )

    if target == "openfoam-image":
        image = _openfoam_image()
        if not image:
            raise ValueError("没有配置 OpenFOAM 镜像名")
        return (["docker", "pull", image], image)

    raise ValueError("不允许安装该目标")


def start(target: str) -> dict:
    if target not in catalog():
        raise ValueError("不允许安装该目标")
    job_id = f"install-{secrets.token_hex(4)}"
    item = {
        "job_id": job_id,
        "target": target,
        "status": "queued",
        "message": "已加入安装队列",
        "started_at": None,
        "updated_at": _now(),
    }
    with _LOCK:
        _JOBS[job_id] = item
    return dict(item)


def get(job_id: str) -> dict | None:
    with _LOCK:
        item = _JOBS.get(job_id)
        return dict(item) if item else None


def run(job_id: str) -> None:
    item = get(job_id)
    if not item:
        return
    target = item["target"]
    try:
        command, label = _command_for(target)
    except ValueError as exc:
        _job(job_id, status="failed", message=str(exc), finished_at=_now())
        return

    if shutil.which(command[0]) is None and not Path(command[0]).exists():
        _job(job_id, status="failed", message=f"本机找不到 {command[0]}，无法执行安装", finished_at=_now())
        return

    _job(job_id, status="running", message=f"正在执行 {label}，可能需要几分钟", started_at=_now())
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3600,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        _job(job_id, status="failed", message="安装等待超过 60 分钟，已停止等待", finished_at=_now())
        return
    except OSError as exc:
        _job(job_id, status="failed", message=f"无法启动安装程序：{exc}", finished_at=_now())
        return

    output = (proc.stdout or "") + (proc.stderr or "")
    tail = output[-2000:].strip()
    if proc.returncode == 0:
        _job(job_id, status="completed", message="安装命令已完成，请重新探测环境", output_tail=tail, finished_at=_now())
    else:
        _job(job_id, status="failed", message=f"安装命令退出码 {proc.returncode}", output_tail=tail, finished_at=_now())
