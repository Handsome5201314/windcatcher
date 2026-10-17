"""鼻塞模型资产处理。

这一模块只处理可追溯的几何资产，不把鼻塞模型当作医疗器械建议。上传的
原始 STL 永远原样保留；后续 CFD 只能读取通过几何预检且已经人工审核的
摆位版本。模块不依赖 VTK，便于在没有桌面几何软件的服务环境中完成上传
和预检。
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from .errors import PipelineError

MODEL_MAX_BYTES = 256 * 1024 * 1024
MODEL_MAX_FACES = 1_000_000
MAX_COORDINATE_MM = 10_000_000.0
VERTEX_MERGE_DECIMALS = 9
UNIT_TO_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "inch": 25.4}
_VERTEX_RE = re.compile(
    r"^\s*vertex\s+([^\s]+)\s+([^\s]+)\s+([^\s]+)\s*$",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class Mesh:
    """三角形网格，顶点已经去重，面索引从零开始。"""

    vertices: tuple[tuple[float, float, float], ...]
    faces: tuple[tuple[int, int, int], ...]
    encoding: str


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _finite_float(raw: str | float, label: str) -> float:
    if isinstance(raw, bool):
        raise PipelineError("failed", f"STL 中存在无效的{label}", ["检查模型坐标是否为数值"])
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise PipelineError("failed", f"STL 中存在无法解析的{label}", ["导出纯三角面 STL 后重新上传"]) from exc
    if not math.isfinite(value):
        raise PipelineError("failed", f"STL 中存在非有限的{label}", ["检查模型是否包含 NaN 或无穷坐标"])
    return value


def _vertex_key(vertex: Sequence[float]) -> tuple[float, float, float]:
    # 允许 ASCII/Binary STL 在同一条边上出现极小的浮点舍入差异。
    return tuple(round(float(value), VERTEX_MERGE_DECIMALS) for value in vertex)  # type: ignore[return-value]


def _mesh_from_triangles(triangles: Iterable[Sequence[Sequence[float]]], encoding: str) -> Mesh:
    vertices: list[tuple[float, float, float]] = []
    index_by_key: dict[tuple[float, float, float], int] = {}
    faces: list[tuple[int, int, int]] = []
    for face_index, triangle in enumerate(triangles, start=1):
        if face_index > MODEL_MAX_FACES:
            raise PipelineError("failed", f"STL 三角面超过 {MODEL_MAX_FACES} 个限制", ["降低模型面数后重新上传"])
        if len(triangle) != 3:
            raise PipelineError("failed", "STL 三角面顶点数量不完整", ["导出纯三角面 STL 后重新上传"])
        indexes = []
        for raw_vertex in triangle:
            if len(raw_vertex) != 3:
                raise PipelineError("failed", "STL 顶点坐标不完整", ["导出纯三角面 STL 后重新上传"])
            vertex = tuple(_finite_float(value, "顶点坐标") for value in raw_vertex)
            key = _vertex_key(vertex)
            index = index_by_key.get(key)
            if index is None:
                index = len(vertices)
                index_by_key[key] = index
                vertices.append(vertex)  # type: ignore[arg-type]
            indexes.append(index)
        # 退化面在几何预检中应当可解释地暴露，而不是被静默丢掉。
        faces.append((indexes[0], indexes[1], indexes[2]))
    if not faces:
        raise PipelineError("failed", "STL 中没有三角面", ["确认上传的是有效的三角面 STL"])
    return Mesh(tuple(vertices), tuple(faces), encoding)


def _parse_binary(data: bytes) -> Mesh | None:
    if len(data) < 84:
        return None
    try:
        count = struct.unpack_from("<I", data, 80)[0]
    except struct.error:
        return None
    expected = 84 + 50 * count
    # 只有长度完全吻合时才按二进制 STL 解析，避免把 ASCII 文件头误判成面数。
    if count == 0 or expected != len(data):
        return None
    if count > MODEL_MAX_FACES:
        raise PipelineError("failed", f"STL 三角面超过 {MODEL_MAX_FACES} 个限制", ["降低模型面数后重新上传"])
    triangles = []
    offset = 84
    for _ in range(count):
        try:
            values = struct.unpack_from("<12fH", data, offset)
        except struct.error as exc:
            raise PipelineError("failed", "二进制 STL 面数据不完整", ["重新导出 STL 后上传"]) from exc
        triangles.append((values[3:6], values[6:9], values[9:12]))
        offset += 50
    return _mesh_from_triangles(triangles, "binary")


def _parse_ascii(data: bytes) -> Mesh:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PipelineError("failed", "无法识别 STL 编码", ["导出标准 ASCII 或 Binary STL 后重新上传"]) from exc
    lines = text.splitlines()
    stripped = [line.strip().lower() for line in lines if line.strip()]
    if not stripped or not stripped[0].startswith("solid") or not any(line.startswith("endsolid") for line in stripped):
        raise PipelineError("failed", "ASCII STL 缺少 solid/endsolid 结构", ["导出标准 ASCII 或 Binary STL 后重新上传"])
    triangles: list[list[tuple[float, float, float]]] = []
    current: list[tuple[float, float, float]] = []
    in_facet = False
    for line in lines:
        marker = line.strip().lower()
        if marker.startswith("facet"):
            if in_facet:
                raise PipelineError("failed", "ASCII STL facet 结构嵌套", ["重新导出标准三角面 STL"])
            in_facet = True
            current = []
            continue
        match = _VERTEX_RE.match(line)
        if not match:
            if marker.startswith("endfacet"):
                if not in_facet or len(current) != 3:
                    raise PipelineError("failed", "ASCII STL facet 中没有恰好三个顶点", ["重新导出标准三角面 STL"])
                triangles.append(current)
                in_facet = False
            continue
        if not in_facet or len(current) >= 3:
            raise PipelineError("failed", "ASCII STL 顶点不在有效 facet 中", ["重新导出标准三角面 STL"])
        current.append(tuple(_finite_float(value, "顶点坐标") for value in match.groups()))
    if in_facet:
        raise PipelineError("failed", "ASCII STL facet 未闭合", ["重新导出标准三角面 STL"])
    if not triangles:
        raise PipelineError(
            "failed",
            "ASCII STL 中没有完整的三角面",
            ["确认导出选项为三角面 STL", "也可以转换为 Binary STL 后上传"],
        )
    return _mesh_from_triangles(triangles, "ascii")


def parse_stl(data: bytes) -> Mesh:
    """解析 ASCII/Binary STL，并拒绝明显不可能的输入。"""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise PipelineError("failed", "上传的 STL 文件为空", ["选择有效的鼻塞 STL 文件"])
    if len(data) > MODEL_MAX_BYTES:
        raise PipelineError(
            "failed",
            f"鼻塞 STL 超过 {MODEL_MAX_BYTES // (1024 * 1024)} MB 限制",
            ["降低模型面数或导出较小的 STL"],
        )
    binary = _parse_binary(bytes(data))
    return binary if binary is not None else _parse_ascii(bytes(data))


def _cross(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _sub(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _triangle_area(a: Sequence[float], b: Sequence[float], c: Sequence[float]) -> float:
    cross = _cross(_sub(b, a), _sub(c, a))
    return 0.5 * math.sqrt(_dot(cross, cross))


def _edge_stats(mesh: Mesh) -> tuple[int, int, int]:
    counts: dict[tuple[int, int], int] = {}
    for a, b, c in mesh.faces:
        for start, end in ((a, b), (b, c), (c, a)):
            edge = (start, end) if start < end else (end, start)
            counts[edge] = counts.get(edge, 0) + 1
    boundary = sum(1 for count in counts.values() if count == 1)
    non_manifold = sum(1 for count in counts.values() if count > 2)
    invalid = sum(1 for count in counts.values() if count <= 0)
    return boundary, non_manifold, invalid


def _connected_components(mesh: Mesh) -> int:
    neighbours: list[set[int]] = [set() for _ in mesh.faces]
    edge_faces: dict[tuple[int, int], list[int]] = {}
    for face_index, (a, b, c) in enumerate(mesh.faces):
        for start, end in ((a, b), (b, c), (c, a)):
            edge = (start, end) if start < end else (end, start)
            edge_faces.setdefault(edge, []).append(face_index)
    for face_indexes in edge_faces.values():
        if len(face_indexes) < 2:
            continue
        # 将同一条边上的面连成星形，避免异常非流形边触发 O(k²) 邻接扩张。
        anchor, *others = face_indexes
        for face_index in others:
            neighbours[anchor].add(face_index)
            neighbours[face_index].add(anchor)
    seen: set[int] = set()
    components = 0
    for start in range(len(mesh.faces)):
        if start in seen:
            continue
        components += 1
        stack = [start]
        seen.add(start)
        while stack:
            current = stack.pop()
            for neighbour in neighbours[current]:
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
    return components


def inspect_mesh(mesh: Mesh, *, unit: str = "mm") -> dict:
    """计算用于上传闸门和后续 CFD 选择的几何质检结果。"""
    unit = str(unit).strip().lower()
    scale = UNIT_TO_MM.get(unit)
    if scale is None:
        raise PipelineError("failed", f"不支持的 STL 单位：{unit}", [f"单位必须是：{', '.join(UNIT_TO_MM)}"])

    vertices = tuple(tuple(value * scale for value in vertex) for vertex in mesh.vertices)
    if not vertices:
        raise PipelineError("failed", "STL 没有顶点", ["选择有效的三角面 STL"])
    for face in mesh.faces:
        if (not isinstance(face, (list, tuple)) or len(face) != 3
                or any(type(index) is not int or index < 0 or index >= len(vertices) for index in face)):
            raise PipelineError("failed", "STL 面索引无效", ["导出标准三角面 STL 后重新上传"])
    if any(not math.isfinite(value) or abs(value) > MAX_COORDINATE_MM for vertex in vertices for value in vertex):
        raise PipelineError("failed", "STL 坐标超出毫米范围限制", ["确认 STL 单位和模型坐标原点后重新上传"])
    minimum = tuple(min(vertex[index] for vertex in vertices) for index in range(3))
    maximum = tuple(max(vertex[index] for vertex in vertices) for index in range(3))
    size = tuple(maximum[index] - minimum[index] for index in range(3))
    characteristic = max(size) if max(size) > 0 else 1.0
    degenerate = 0
    area = 0.0
    signed_volume = 0.0
    reference = vertices[0]
    for a, b, c in mesh.faces:
        va, vb, vc = vertices[a], vertices[b], vertices[c]
        triangle_area = _triangle_area(va, vb, vc)
        area += triangle_area
        if triangle_area <= max(1e-12, characteristic * characteristic * 1e-12):
            degenerate += 1
        signed_volume += _dot(_sub(va, reference), _cross(_sub(vb, reference), _sub(vc, reference))) / 6.0
    boundary, non_manifold, invalid_edges = _edge_stats(mesh)
    components = _connected_components(mesh)
    closed = boundary == 0 and non_manifold == 0 and invalid_edges == 0
    reasons: list[str] = []
    if degenerate:
        reasons.append(f"存在 {degenerate} 个退化三角面")
    if non_manifold:
        reasons.append(f"存在 {non_manifold} 条非流形边")
    if boundary:
        reasons.append(f"存在 {boundary} 条开口边，模型尚未闭合")
    if components > 1:
        reasons.append(f"模型包含 {components} 个不相连面组件")
    if any(value <= 0 for value in size):
        reasons.append("模型包围盒存在零尺寸轴")
    volume = abs(signed_volume)
    if volume <= 1e-9:
        reasons.append("模型体积接近零")
    fatal = bool(degenerate or non_manifold or invalid_edges or any(value <= 0 for value in size) or volume <= 1e-9)
    can_use_for_cfd = closed and components == 1 and not fatal and volume > 0
    verdict = "pass" if can_use_for_cfd else ("warn" if not fatal else "failed")
    return {
        "verdict": verdict,
        "can_use_for_cfd": can_use_for_cfd,
        "closed": closed,
        "face_count": len(mesh.faces),
        "vertex_count": len(mesh.vertices),
        "boundary_edges": boundary,
        "non_manifold_edges": non_manifold,
        "invalid_edges": invalid_edges,
        "degenerate_faces": degenerate,
        "connected_components": components,
        "surface_area_mm2": round(area, 9),
        "volume_mm3": round(volume, 9),
        "bbox_mm": {"min": list(minimum), "max": list(maximum), "size": list(size)},
        "unit": unit,
        "reasons": reasons,
        "self_intersection_checked": False,
        "normal_consistency_checked": False,
        "coordinate_limit_mm": MAX_COORDINATE_MM,
        "vertex_merge_decimals": VERTEX_MERGE_DECIMALS,
        "checked_at": _now(),
    }


def _scale_mesh(mesh: Mesh, scale: float) -> Mesh:
    vertices = tuple(tuple(value * scale for value in vertex) for vertex in mesh.vertices)
    return Mesh(vertices, mesh.faces, mesh.encoding)


def _rotation_matrix(rotation_deg: Sequence[float]) -> tuple[tuple[float, float, float], ...]:
    rx, ry, rz = (math.radians(value) for value in rotation_deg)
    sx, cx = math.sin(rx), math.cos(rx)
    sy, cy = math.sin(ry), math.cos(ry)
    sz, cz = math.sin(rz), math.cos(rz)
    # Rz * Ry * Rx，记录的旋转顺序固定，便于跨前端复现。
    return (
        (cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx),
        (sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx),
        (-sy, cy * sx, cy * cx),
    )


def validate_placement(placement: dict | None) -> dict:
    placement = placement if isinstance(placement, dict) else {}

    def vector(name: str, default: tuple[float, float, float], *, positive: bool = False) -> list[float]:
        raw = placement.get(name, list(default))
        if not isinstance(raw, (list, tuple)) or len(raw) != 3:
            raise PipelineError("failed", f"摆位参数 {name} 必须是 3 个数", ["使用平移、旋转和缩放的三轴数组"])
        output = [_finite_float(value, f"摆位参数 {name}") for value in raw]
        if positive and any(value <= 0 for value in output):
            raise PipelineError("failed", "摆位缩放必须大于 0", ["检查三个方向的缩放值"])
        if any(abs(value) > (1000.0 if positive else 10000.0) for value in output):
            raise PipelineError("failed", f"摆位参数 {name} 超出安全范围", ["确认单位为毫米、角度为度"])
        return output

    return {
        "translation_mm": vector("translation_mm", (0.0, 0.0, 0.0)),
        "rotation_deg": vector("rotation_deg", (0.0, 0.0, 0.0)),
        "scale": vector("scale", (1.0, 1.0, 1.0), positive=True),
    }


def transform_mesh(mesh: Mesh, placement: dict) -> Mesh:
    placement = validate_placement(placement)
    matrix = _rotation_matrix(placement["rotation_deg"])
    scale = placement["scale"]
    translation = placement["translation_mm"]
    transformed = []
    for vertex in mesh.vertices:
        scaled = tuple(vertex[index] * scale[index] for index in range(3))
        transformed.append(tuple(
            sum(matrix[row][column] * scaled[column] for column in range(3)) + translation[row]
            for row in range(3)
        ))
    return Mesh(tuple(transformed), mesh.faces, "binary")


def mesh_to_binary_stl(mesh: Mesh, header: str = "Windcatcher normalized STL") -> bytes:
    if len(mesh.faces) > 0xFFFFFFFF:
        raise PipelineError("failed", "STL 三角面数量超出 Binary STL 限制", ["降低模型面数后重新上传"])
    header_bytes = header.encode("ascii", errors="replace")[:80].ljust(80, b" ")
    chunks = [header_bytes, struct.pack("<I", len(mesh.faces))]
    for a, b, c in mesh.faces:
        va, vb, vc = mesh.vertices[a], mesh.vertices[b], mesh.vertices[c]
        normal = _cross(_sub(vb, va), _sub(vc, va))
        length = math.sqrt(_dot(normal, normal))
        normal = tuple(value / length for value in normal) if length else (0.0, 0.0, 0.0)
        values = (*normal, *va, *vb, *vc)
        if any(not math.isfinite(value) or abs(value) > 3.4028235e38 for value in values):
            raise PipelineError("failed", "STL 坐标无法安全写入 Binary STL", ["缩小模型或检查单位后重新上传"])
        try:
            chunks.append(struct.pack("<12fH", *values, 0))
        except (OverflowError, struct.error) as exc:
            raise PipelineError("failed", "STL 坐标无法安全写入 Binary STL", ["缩小模型或检查单位后重新上传"]) from exc
    return b"".join(chunks)


def normalized_mesh(data: bytes, unit: str) -> tuple[Mesh, dict, bytes]:
    unit = str(unit).strip().lower()
    scale = UNIT_TO_MM.get(unit)
    if scale is None:
        raise PipelineError("failed", f"不支持的 STL 单位：{unit}", [f"单位必须是：{', '.join(UNIT_TO_MM)}"])
    mesh = parse_stl(data)
    normalized = _scale_mesh(mesh, scale)
    # inspect_mesh validates the unit separately and includes the normalized mm bbox.
    qc = inspect_mesh(mesh, unit=unit)
    return normalized, qc, mesh_to_binary_stl(normalized)
