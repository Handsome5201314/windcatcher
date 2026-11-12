"""纯函数端口标注契约与几何校验。

端口草稿是三维入口标注的中间数据，不负责保存病例或修改现有端面接口。
平面只记录定位参考；真正的端口由选中的网格面和其边界环定义。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import math

import numpy as np

from .errors import PipelineError


PORT_FORMAT = "airway_port_draft_v1"
PORT_SIDES = frozenset({"left", "right", "posterior"})
PROVENANCE_SOURCES = frozenset({"mesh_annotation", "ct_projection", "manual_import"})
_HEX64 = set("0123456789abcdefABCDEF")


def _fail(reason: str, remediation: list[str], detail: dict | None = None) -> None:
    raise PipelineError("blocked", reason, remediation, detail)


def _vector(value: object, label: str) -> np.ndarray:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != 3:
        _fail(f"{label}必须是 3 个数值", ["重新生成端口草稿"])
    try:
        vector = np.asarray([float(item) for item in value], dtype=float)
    except (TypeError, ValueError) as exc:
        raise PipelineError("blocked", f"{label}包含非数值", ["重新生成端口草稿"]) from exc
    if not np.all(np.isfinite(vector)):
        _fail(f"{label}包含非有限数值", ["重新生成端口草稿"])
    return vector


def _plane(value: object) -> tuple[np.ndarray, np.ndarray]:
    if not isinstance(value, Mapping):
        _fail("端口平面缺少坐标和法向", ["在三维视窗重新定位端口"])
    point = _vector(value.get("point_lps_mm"), "端口平面位置")
    normal = _vector(value.get("normal_lps"), "端口平面法向")
    length = float(np.linalg.norm(normal))
    if length < 1e-8:
        _fail("端口平面法向长度为零", ["重新定位端口平面"])
    return point, normal / length


def _face_ids(value: object) -> list[int]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
        _fail("端口没有选中的网格面", ["在三维模型上选择至少一个入口面"])
    result: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, np.integer)) or int(item) < 0:
            _fail("端口网格面 ID 必须是非负整数", ["重新选择三维端口面"])
        result.append(int(item))
    if len(set(result)) != len(result):
        _fail("端口网格面 ID 存在重复", ["重新生成端口面集合"])
    return result


def _boundary(value: object) -> np.ndarray:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        _fail("端口边界环格式无效", ["在三维模型上画出闭合入口边界"])
    points = np.asarray([_vector(item, "端口边界点") for item in value], dtype=float)
    if len(points) >= 2 and np.linalg.norm(points[0] - points[-1]) <= 1e-6:
        points = points[:-1]
    if len(points) < 3:
        _fail("端口边界环至少需要 3 个不重复点", ["闭合圈选完整鼻孔入口"])
    if any(np.linalg.norm(points[index] - points[(index + 1) % len(points)]) <= 1e-6
           for index in range(len(points))):
        _fail("端口边界环含有重复相邻点", ["重新绘制入口边界"])
    if len(np.unique(np.round(points, 6), axis=0)) < 3:
        _fail("端口边界环退化", ["重新绘制完整入口边界"])
    return points


def _projected_area(boundary: np.ndarray, normal: np.ndarray) -> float:
    """用 Newell 法计算三维多边形在法向平面上的面积。"""
    cross_sum = np.zeros(3, dtype=float)
    for first, second in zip(boundary, np.roll(boundary, -1, axis=0)):
        cross_sum += np.cross(first, second)
    return float(abs(np.dot(cross_sum, normal)) * 0.5)


def _centroid(boundary: np.ndarray) -> np.ndarray:
    return np.mean(boundary, axis=0)


def _provenance(value: object) -> dict:
    if not isinstance(value, Mapping):
        _fail("端口缺少来源记录", ["保留标注来源、掩膜版本和网格版本后再试"])
    source = value.get("source")
    if source not in PROVENANCE_SOURCES:
        _fail(
            "端口来源类型无效",
            ["来源必须是 mesh_annotation、ct_projection 或 manual_import"],
        )
    required_strings = ("geometry_version", "algorithm_version", "created_at")
    for key in required_strings:
        item = value.get(key)
        if not isinstance(item, str) or not item.strip():
            _fail(f"端口来源缺少 {key}", ["补齐端口标注来源记录"])
    try:
        datetime.fromisoformat(str(value["created_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PipelineError("blocked", "端口来源时间格式无效", ["使用 ISO 8601 时间记录端口草稿"]) from exc
    mask_version = value.get("mask_version")
    if isinstance(mask_version, bool) or not isinstance(mask_version, (int, np.integer)) or int(mask_version) < 0:
        _fail("端口来源缺少有效 mask_version", ["绑定当前气道掩膜版本后再试"])
    result = dict(value)
    result["source"] = str(source)
    result["mask_version"] = int(mask_version)
    if "mesh_hash" in value:
        mesh_hash = value["mesh_hash"]
        if not isinstance(mesh_hash, str) or len(mesh_hash) != 64 or any(char not in _HEX64 for char in mesh_hash):
            _fail("端口 mesh_hash 无效", ["使用当前网格的 SHA-256 记录"])
        result["mesh_hash"] = mesh_hash.lower()
    return result


def validate_port_draft(
    draft: Mapping[str, object],
    *,
    plane_tolerance_mm: float = 0.75,
    area_tolerance_ratio: float = 0.15,
) -> dict:
    """校验并规范化一个未保存的三维端口草稿。

    返回新字典，不修改调用方对象。该函数不读取病例、不写磁盘，也不
    宣称端口已经适合 CFD；它只确保 UI 传来的结构完整且内部自洽。
    """
    if not isinstance(draft, Mapping):
        _fail("端口草稿必须是对象", ["重新生成端口草稿"])
    if draft.get("format") != PORT_FORMAT:
        _fail(f"不支持的端口草稿格式：{draft.get('format')}", ["刷新工作台后重新生成端口草稿"])
    side = draft.get("side")
    if side not in PORT_SIDES:
        _fail("端口 side 无效", ["端口 side 只能是 left、right 或 posterior"])
    if isinstance(plane_tolerance_mm, bool) or not math.isfinite(float(plane_tolerance_mm)) or float(plane_tolerance_mm) <= 0:
        raise ValueError("plane_tolerance_mm must be positive")
    if isinstance(area_tolerance_ratio, bool) or not math.isfinite(float(area_tolerance_ratio)) or not 0 < float(area_tolerance_ratio) < 1:
        raise ValueError("area_tolerance_ratio must be between 0 and 1")

    plane_point, plane_normal = _plane(draft.get("plane"))
    seed = _vector(draft.get("seed_lps_mm"), "端口 seed")
    faces = _face_ids(draft.get("selected_face_ids"))
    boundary = _boundary(draft.get("boundary_loop_lps_mm"))
    distances = (boundary - plane_point) @ plane_normal
    max_distance = float(np.max(np.abs(distances)))
    if max_distance > float(plane_tolerance_mm):
        _fail(
            "端口边界不在记录的切面上",
            ["重新标注完整入口，或更新端口平面"] ,
            {"max_boundary_plane_distance_mm": round(max_distance, 6), "tolerance_mm": float(plane_tolerance_mm)},
        )
    area = _projected_area(boundary, plane_normal)
    if area <= 1e-6:
        _fail("端口边界面积为零", ["重新圈选完整鼻孔入口"])
    provided_area = draft.get("area_mm2")
    if isinstance(provided_area, bool) or not isinstance(provided_area, (int, float, np.integer, np.floating)):
        _fail("端口缺少有效 area_mm2", ["由实际端口边界重新计算面积"])
    provided_area = float(provided_area)
    if not math.isfinite(provided_area) or provided_area <= 0:
        _fail("端口 area_mm2 必须大于零", ["重新生成端口草稿"])
    relative_error = abs(provided_area - area) / max(area, 1e-6)
    if relative_error > float(area_tolerance_ratio):
        _fail(
            "端口记录面积与边界环不一致",
            ["重新生成端口边界和面积，避免 UI 与实际裁切不一致"],
            {"boundary_area_mm2": round(area, 6), "recorded_area_mm2": provided_area,
             "relative_error": round(relative_error, 6)},
        )
    centroid = _vector(draft.get("centroid_lps_mm", _centroid(boundary).tolist()), "端口中心")
    centroid_distance = abs(float((centroid - plane_point) @ plane_normal))
    if centroid_distance > float(plane_tolerance_mm):
        _fail("端口中心不在记录的切面上", ["重新生成端口中心或更新端口平面"])
    provenance = _provenance(draft.get("provenance"))
    result = dict(draft)
    result.update({
        "format": PORT_FORMAT,
        "side": str(side),
        "plane": {"point_lps_mm": plane_point.tolist(), "normal_lps": plane_normal.tolist()},
        "seed_lps_mm": seed.tolist(),
        "selected_face_ids": faces,
        "boundary_loop_lps_mm": boundary.tolist(),
        "centroid_lps_mm": centroid.tolist(),
        "area_mm2": provided_area,
        "boundary_area_mm2": round(area, 6),
        "provenance": provenance,
    })
    return result


def validate_port_set(ports: Sequence[Mapping[str, object]], *, require_bilateral: bool = False) -> list[dict]:
    """校验一组端口草稿，并阻止 side 重复或面集合互相重叠。"""
    if isinstance(ports, (str, bytes)) or not isinstance(ports, Sequence) or not ports:
        _fail("端口草稿列表为空", ["先标注至少一个鼻孔或鼻咽入口"])
    validated = [validate_port_draft(item) for item in ports]
    sides = [item["side"] for item in validated]
    if len(set(sides)) != len(sides):
        _fail("端口 side 重复", ["每个鼻孔和鼻咽端只能保留一个端口草稿"])
    face_owner: dict[int, str] = {}
    for item in validated:
        for face_id in item["selected_face_ids"]:
            previous = face_owner.setdefault(face_id, item["side"])
            if previous != item["side"]:
                _fail("不同端口复用了同一个网格面", ["拆分重叠端口后再试"], {"face_id": face_id})
    if require_bilateral and not {"left", "right", "posterior"}.issubset(set(sides)):
        _fail("双侧 CFD 端口不完整", ["分别标注左鼻孔、右鼻孔和鼻咽端口"])
    return validated


__all__ = ["PORT_FORMAT", "PORT_SIDES", "validate_port_draft", "validate_port_set"]
