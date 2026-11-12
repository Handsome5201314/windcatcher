"""掩膜 → STL 表面。

SOP 5 的简化与平滑默认关闭：鼻腔窄通道不允许被盲目平滑或简化；
开启时必须记录前后面数与体积变化。

自由切面先对补零的掩膜生成闭合壳，再按用户平面裁切和封盖。
旧正交流程保留前后轴极值延伸，以生成轴向开口。
"""

from __future__ import annotations

import json
import struct
import threading
from collections import defaultdict
from contextlib import contextmanager, nullcontext
from pathlib import Path

import numpy as np
from scipy import ndimage
from skimage import measure

from .config import SOP_DEFAULTS
from . import endplanes, oblique
from .errors import PipelineError
from .mask_review import has_current_external_air_validation
from .volume import load_volume


MAX_LOCAL_INLET_FACES = 750_000
MAX_LOCAL_MASK_BOUNDARY_TRANSITIONS = 450_000
GEOMETRY_ALGORITHM_VERSION = "mask-to-stl-oblique-clip-v3"
_LOCAL_GEOMETRY_LOCK = threading.Lock()


@contextmanager
def local_geometry_guard():
    """Admit at most one memory-intensive local-inlet mesh build per worker."""
    if not _LOCAL_GEOMETRY_LOCK.acquire(blocking=False):
        raise PipelineError(
            "blocked", "已有局部端面网格任务正在运行",
            ["等待当前精确预览或 STL 生成完成后重试"],
        )
    try:
        yield
    finally:
        _LOCAL_GEOMETRY_LOCK.release()


def ensure_local_mesh_face_capacity(faces_before_decimation: int) -> None:
    """Reject meshes whose local Python clipping would overrun the worker."""
    count = int(faces_before_decimation)
    if count < 0:
        raise PipelineError("failed", "网格面数无效", ["重新生成气道表面"])
    if count > MAX_LOCAL_INLET_FACES:
        raise PipelineError(
            "blocked", "当前网格过大，暂不能做精确局部斜切",
            ["改用主气道范围或收紧掩膜范围，再重新预览局部端面"],
            {"source_faces": count, "local_inlet_face_limit": MAX_LOCAL_INLET_FACES},
        )


def ensure_local_mask_capacity(mask: np.ndarray) -> int:
    """Bound the marching-cubes input before allocating its full mesh.

    This counts voxel-label transitions, not triangles; the authoritative
    post-generation face gate remains necessary because their ratio varies.
    """
    source = np.asarray(mask, dtype=bool)
    if source.ndim != 3:
        raise PipelineError("failed", "局部端面掩膜维度无效", ["重新复核气道掩膜"])
    transitions = 0
    for axis in range(3):
        lower = [slice(None)] * 3
        upper = [slice(None)] * 3
        lower[axis] = slice(None, -1)
        upper[axis] = slice(1, None)
        transitions += int(np.count_nonzero(source[tuple(lower)] != source[tuple(upper)]))
        edge = [slice(None)] * 3
        edge[axis] = 0
        transitions += int(np.count_nonzero(source[tuple(edge)]))
        edge[axis] = -1
        transitions += int(np.count_nonzero(source[tuple(edge)]))
    if transitions > MAX_LOCAL_MASK_BOUNDARY_TRANSITIONS:
        raise PipelineError(
            "blocked", "局部端面掩膜表面过大，暂不能生成精确预览",
            ["改用主气道范围或收紧掩膜范围后重试"],
            {"mask_boundary_transitions": transitions,
             "local_inlet_boundary_limit": MAX_LOCAL_MASK_BOUNDARY_TRANSITIONS},
        )
    return transitions


def voxel_to_mm(verts: np.ndarray, spacing, origin, axis_dirs) -> np.ndarray:
    """体素索引坐标 → 患者坐标（mm，LPS）。数组轴顺序 (z, y, x)。"""
    dirs = np.asarray(axis_dirs, dtype=float)      # (3, 3)，每行对应数组轴 0/1/2
    spacing = np.asarray(spacing, dtype=float)
    return np.asarray(origin, dtype=float) + verts @ (dirs * spacing[:, None])


def select_largest_component(mask: np.ndarray) -> tuple[np.ndarray, dict]:
    """选择用于建模的 6 连通主气道，并返回可审计的筛选统计。"""
    values = np.asarray(mask, dtype=bool)
    if values.ndim != 3 or not np.any(values):
        raise PipelineError("failed", "掩膜为空，无法生成表面", ["先完成分割阶段"])
    labeled, count = ndimage.label(values, structure=ndimage.generate_binary_structure(3, 1))
    if count <= 1:
        return values, {
            "source_voxels": int(values.sum()),
            "voxels_in": int(values.sum()),
            "voxels_dropped": 0,
            "components": int(count),
            "dropped_components": 0,
        }
    sizes = ndimage.sum(values, labeled, index=range(1, count + 1))
    keep = int(np.argmax(sizes)) + 1
    selected = labeled == keep
    return selected, {
        "source_voxels": int(values.sum()),
        "voxels_in": int(selected.sum()),
        "voxels_dropped": int(values.sum() - selected.sum()),
        "components": int(count),
        "dropped_components": int(count - 1),
    }


def prepare_volume(
    mask: np.ndarray,
    ap_axis: int,
    *,
    keep_largest_component: bool = True,
    extend_ap_edges: bool = True,
) -> tuple[np.ndarray, tuple[int, int, int], dict]:
    """裁剪 + 加边；旧正交流程延伸前后轴，自由切面仅补零保持闭合。

    默认只保留面连通（6 连通）的最大体素块：仅靠对角相连的体素会让
    marching cubes 产生被"掐断"的非流形表面。``keep_largest_component=False``
    用于复核全候选掩膜，此时不删除任何组件，断开组件会原样进入表面并由几何
    质检/CFD 前置闸门处理。

    返回 (准备好的浮点体数据, 各轴相对原掩膜的索引偏移, 处理统计)。
    """
    values = np.asarray(mask, dtype=bool)
    if values.ndim != 3 or not np.any(values):
        raise PipelineError("failed", "掩膜为空，无法生成表面", ["先完成分割阶段"])
    # 先按选择的范围决定是否保留所有组件，再按实际范围裁剪。主气道模式
    # 仍保持原有行为；全候选模式禁止在这里隐式调用 Delete Loose。
    if keep_largest_component:
        selected, component_stats = select_largest_component(values)
    else:
        _, count = ndimage.label(values, structure=ndimage.generate_binary_structure(3, 1))
        selected = values
        component_stats = {
            "source_voxels": int(values.sum()),
            "voxels_in": int(values.sum()),
            "voxels_dropped": 0,
            "components": int(count),
            "dropped_components": 0,
        }
    coords = np.where(selected)
    slices: list[slice] = []
    offsets: list[int] = []
    for axis in range(3):
        lo = int(coords[axis].min())
        hi = int(coords[axis].max())
        slices.append(slice(lo, hi + 1))
        offsets.append(lo - 1)          # 每轴前端补 1 个体素
    cropped = np.asarray(selected[tuple(slices)]).astype(bool).copy()
    stats = component_stats.copy()
    stats["padding_mode"] = "legacy_ap_extension" if extend_ap_edges else "closed_zero_padding"
    cropped = cropped.astype(np.float32)
    padded = np.pad(cropped, [(1, 1)] * 3, mode="constant", constant_values=0.0)

    # 前后轴：把端面复制到最外层，让气道穿过边界（否则 marching cubes 会把开口封死）
    def assign(dst_index: int, src_index: int) -> None:
        dst = [slice(1, -1)] * 3                      # 其余轴跳过补零层
        dst[ap_axis] = slice(dst_index, dst_index + 1)
        src = [slice(None)] * 3
        src[ap_axis] = slice(src_index, src_index + 1)
        padded[tuple(dst)] = cropped[tuple(src)]

    if extend_ap_edges:
        assign(0, 0)
        assign(padded.shape[ap_axis] - 1, cropped.shape[ap_axis] - 1)
    return padded, tuple(offsets), stats


def shifted_origin(origin, spacing, axis_dirs, offsets) -> np.ndarray:
    """按索引偏移换算新的原点（mm）。"""
    dirs = np.asarray(axis_dirs, dtype=float)
    spacing = np.asarray(spacing, dtype=float)
    delta = np.zeros(3, dtype=float)
    for axis in range(3):
        delta = delta + offsets[axis] * spacing[axis] * dirs[axis]
    return np.asarray(origin, dtype=float) + delta


def keep_largest_component(faces: np.ndarray, vertex_count: int) -> tuple[np.ndarray, dict]:
    """只保留最大连通分量（对应 SOP 5 的 Delete Loose：删除未连接的游离几何）。

    返回 (保留的面, 统计信息)。被删除的碎片数量与面数会写进产物留痕。
    """
    parent = list(range(vertex_count))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for face in faces:
        union(int(face[0]), int(face[1]))
        union(int(face[1]), int(face[2]))

    roots = [find(i) for i in range(vertex_count)]
    used_roots = {roots[int(v)] for face in faces for v in face}
    counts: dict[int, int] = {}
    for face in faces:
        root = roots[int(face[0])]
        counts[root] = counts.get(root, 0) + 1
    if not counts:
        return faces, {"components": 0, "removed_components": 0, "removed_faces": 0}

    order = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    keep_root = order[0][0]
    mask = np.array([roots[int(face[0])] == keep_root for face in faces], dtype=bool)
    removed_faces = int(len(faces) - mask.sum())
    return (
        faces[mask],
        {
            "components": len(used_roots),
            "removed_components": max(len(order) - 1, 0),
            "removed_faces": removed_faces,
            "kept_faces": int(mask.sum()),
        },
    )


def write_binary_stl(path: Path, vertices: np.ndarray, faces: np.ndarray) -> None:
    """写二进制 STL。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    v0 = vertices[faces[:, 0]]
    v1 = vertices[faces[:, 1]]
    v2 = vertices[faces[:, 2]]
    normals = np.cross(v1 - v0, v2 - v0)
    lengths = np.linalg.norm(normals, axis=1)
    lengths[lengths == 0] = 1.0
    normals = normals / lengths[:, None]

    records = np.zeros(
        len(faces),
        dtype=np.dtype([
            ("normal", "<f4", (3,)),
            ("v0", "<f4", (3,)),
            ("v1", "<f4", (3,)),
            ("v2", "<f4", (3,)),
            ("attr", "<u2"),
        ]),
    )
    records["normal"] = normals
    records["v0"] = v0
    records["v1"] = v1
    records["v2"] = v2

    header = b"Windcatcher airway surface - local only"
    header = header[:80].ljust(80, b"\0")
    with open(path, "wb") as handle:
        handle.write(header)
        handle.write(struct.pack("<I", len(faces)))
        records.tofile(handle)


def remove_opposite_coincident_faces(faces: np.ndarray) -> tuple[np.ndarray, dict]:
    """Cancel exact opposite face pairs; keep coordinates and all distinct walls.

    Marching cubes can emit a doubled, zero-thickness interior sheet at
    ambiguous voxel configurations. Only pairs with identical vertex IDs
    and opposite winding cancel; same-winding duplicates and more complex
    nonmanifold topology remain subject to the normal quality gates.
    """
    faces = np.asarray(faces, dtype=np.int32)
    if not len(faces):
        return faces, {"removed_faces": 0, "opposite_pairs": 0}
    _, inverse, counts = np.unique(np.sort(faces, axis=1), axis=0, return_inverse=True, return_counts=True)
    pairs = np.flatnonzero(counts == 2)
    keep = np.ones(len(faces), dtype=bool)
    if len(pairs):
        order = np.argsort(inverse, kind="stable")
        starts = np.concatenate(([0], np.cumsum(counts[:-1])))
        first, second = order[starts[pairs]], order[starts[pairs] + 1]
        parity = ((faces[:,0] > faces[:,1]).astype(np.int8)
                  + (faces[:,0] > faces[:,2]).astype(np.int8)
                  + (faces[:,1] > faces[:,2]).astype(np.int8)) % 2
        opposite = parity[first] != parity[second]
        keep[first[opposite]] = False
        keep[second[opposite]] = False
    removed = int((~keep).sum())
    return faces[keep], {"removed_faces": removed, "opposite_pairs": removed // 2}


def mask_to_stl(
    mask: np.ndarray,
    spacing,
    origin,
    axis_dirs,
    ap_axis: int = 1,
    params: dict | None = None,
) -> dict:
    """由掩膜生成 STL，返回顶点/面数与参数留痕。"""
    p = {"smooth": False, "decimate_ratio": None, "step_size": 1,
         "keep_largest_component": True, "extend_ap_edges": True}
    if params:
        p.update({k: v for k, v in params.items() if v is not None})
    model_scope = str(p.get("model_scope", "main_airway")).strip().lower()
    if model_scope not in {"main_airway", "full_candidate", "review"}:
        raise PipelineError(
            "failed",
            f"不支持的建模范围：{model_scope}",
            ["model_scope 只能是 main_airway、full_candidate 或 review"],
        )
    if model_scope == "review":
        model_scope = "full_candidate"
    p["model_scope"] = model_scope
    # Scope is authoritative: review/full-candidate output must never fall
    # back to Delete Loose even when a caller omitted keep_largest_component.
    if model_scope != "main_airway":
        p["keep_largest_component"] = False

    if not np.any(mask):
        raise PipelineError("failed", "掩膜为空，无法生成表面", ["先完成分割阶段"])

    volume, offsets, manifold_stats = prepare_volume(
        mask,
        int(ap_axis),
        keep_largest_component=bool(p["keep_largest_component"]),
        extend_ap_edges=bool(p["extend_ap_edges"]),
    )
    origin_eff = shifted_origin(origin, spacing, axis_dirs, offsets)

    try:
        verts, faces, _, _ = measure.marching_cubes(
            volume, level=0.5, step_size=int(p["step_size"]), allow_degenerate=True
        )
    except Exception as exc:  # pragma: no cover - 依赖内部实现细节
        raise PipelineError("failed", f"marching cubes 失败：{exc}", ["检查掩膜是否为三维二值数组"]) from exc

    vertices_mm = voxel_to_mm(verts, spacing, origin_eff, axis_dirs)
    faces_before = len(faces)
    faces, coincident_cleanup = remove_opposite_coincident_faces(faces)

    notes = []
    if coincident_cleanup["removed_faces"]:
        notes.append(f"已移除 {coincident_cleanup['removed_faces']} 个方向相反且完全重合的零厚度三角面；顶点坐标和原掩膜未改变")
    cleanup = {
        "components": int(manifold_stats.get("components", 1)),
        "removed_components": 0,
        "removed_faces": 0,
        "kept_faces": len(faces),
    }
    if p["keep_largest_component"]:
        faces, cleanup = keep_largest_component(faces, len(vertices_mm))
        if cleanup["removed_components"]:
            notes.append(
                f"已删除 {cleanup['removed_components']} 个游离碎片（{cleanup['removed_faces']} 面）："
                "对应 SOP 5 的 Delete Loose；碎片几何不计入后续 CFD"
            )
    if p["smooth"]:
        notes.append("平滑已开启：需对比修复前后几何，禁止抹平鼻腔窄通道")
        neighbors: list[list[int]] = [[] for _ in range(len(vertices_mm))]
        for face in faces:
            for a, b in ((0, 1), (1, 2), (2, 0)):
                neighbors[face[a]].append(face[b])
                neighbors[face[b]].append(face[a])
        smoothed = vertices_mm.copy()
        for i, neigh in enumerate(neighbors):
            if neigh:
                smoothed[i] = 0.5 * vertices_mm[i] + 0.5 * vertices_mm[neigh].mean(axis=0)
        vertices_mm = smoothed

    if p["decimate_ratio"]:
        ratio = float(p["decimate_ratio"])
        if not 0 < ratio < 1:
            raise PipelineError("failed", "decimate_ratio 必须在 0 与 1 之间", ["例如 0.5 表示保留一半面数"])
        faces = faces[: max(int(faces_before * ratio), 100)]
        notes.append(f"已按 {ratio} 简化面数：{faces_before} → {len(faces)}")

    bbox = [[float(vertices_mm[:, i].min()), float(vertices_mm[:, i].max())] for i in range(3)]
    return {
        "vertices": int(len(vertices_mm)),
        "faces": int(len(faces)),
        "faces_before_decimation": faces_before,
        "bbox_lps_mm": [[round(a, 2), round(b, 2)] for a, b in bbox],
        "params": {
            "smooth": bool(p["smooth"]),
            "decimate_ratio": p["decimate_ratio"],
            "ap_axis": int(ap_axis),
            "keep_largest_component": bool(p["keep_largest_component"]),
            "extend_ap_edges": bool(p["extend_ap_edges"]),
            "model_scope": str(p.get("model_scope", "main_airway")),
        },
        "loose_cleanup": cleanup,
        "coincident_face_cleanup": coincident_cleanup,
        "manifold_prep": manifold_stats,
        "notes": notes,
        "_vertices": vertices_mm,
        "_faces": faces,
    }


def _clip_polygon_to_plane(points: np.ndarray, plane: dict, tolerance: float = 1e-8) -> list[np.ndarray]:
    """Clip one polygon to the retained side of an oblique plane.

    Endplane normals point towards the part that must be deleted, therefore the
    retained side is ``dot(point - plane_point, normal) <= 0``.  The helper is
    deliberately independent of VTK so surface generation also works in the
    minimal Python runtime used by the API.
    """
    if len(points) == 0:
        return []
    point = np.asarray(plane["point_lps_mm"], dtype=float)
    normal = np.asarray(plane["normal_lps"], dtype=float)
    distances = (np.asarray(points, dtype=float) - point) @ normal
    clipped: list[np.ndarray] = []
    for index, current in enumerate(np.asarray(points, dtype=float)):
        following = np.asarray(points[(index + 1) % len(points)], dtype=float)
        current_distance = float(distances[index])
        following_distance = float(distances[(index + 1) % len(points)])
        current_inside = current_distance <= tolerance
        following_inside = following_distance <= tolerance
        if current_inside:
            clipped.append(current)
        if current_inside != following_inside:
            denominator = current_distance - following_distance
            if abs(denominator) > 1e-14:
                fraction = current_distance / denominator
                clipped.append(current + fraction * (following - current))
    if not clipped:
        return []
    cleaned: list[np.ndarray] = []
    for point in clipped:
        if not cleaned or np.linalg.norm(np.asarray(point) - cleaned[-1]) > 1e-7:
            cleaned.append(np.asarray(point, dtype=float))
    if len(cleaned) > 1 and np.linalg.norm(cleaned[0] - cleaned[-1]) <= 1e-7:
        cleaned.pop()
    return cleaned


def _weld_points(points: list[np.ndarray], tolerance: float = 1e-6) -> tuple[np.ndarray, dict[tuple[int, int, int], int]]:
    """Weld generated polygon points while retaining deterministic indices."""
    vertices: list[np.ndarray] = []
    indices: dict[tuple[int, int, int], int] = {}
    for point in points:
        key = tuple(np.rint(np.asarray(point, dtype=float) / tolerance).astype(np.int64).tolist())
        if key not in indices:
            indices[key] = len(vertices)
            vertices.append(np.asarray(point, dtype=float))
    return np.asarray(vertices, dtype=float), indices


def _plane_loops(segments: list[tuple[np.ndarray, np.ndarray]], tolerance: float = 1e-6) -> list[np.ndarray]:
    """Join coplanar triangle intersections into one or more closed loops."""
    if not segments:
        return []
    vertices: list[np.ndarray] = []
    index_by_key: dict[tuple[int, int, int], int] = {}

    def vertex_index(point: np.ndarray) -> int:
        key = tuple(np.rint(np.asarray(point, dtype=float) / tolerance).astype(np.int64).tolist())
        existing = index_by_key.get(key)
        if existing is not None:
            return existing
        index_by_key[key] = len(vertices)
        vertices.append(np.asarray(point, dtype=float))
        return len(vertices) - 1

    edges: set[tuple[int, int]] = set()
    for first, second in segments:
        a, b = vertex_index(first), vertex_index(second)
        if a != b:
            edges.add((min(a, b), max(a, b)))
    if not edges:
        return []
    adjacency: dict[int, set[int]] = defaultdict(set)
    for a, b in edges:
        adjacency[a].add(b)
        adjacency[b].add(a)

    # A regular manifold surface intersects a plane in degree-two loops.  A
    # degree greater than two can occur when a plane passes exactly through a
    # mesh vertex; splitting at that vertex still gives usable loops by taking
    # the first unvisited continuation.
    unused = set(edges)
    loops: list[np.ndarray] = []
    while unused:
        edge = next(iter(unused))
        start, current = edge
        loop = [start, current]
        unused.discard(edge)
        previous = start
        while current != start:
            candidates = [n for n in adjacency[current] if (min(current, n), max(current, n)) in unused]
            if not candidates:
                break
            following = candidates[0]
            unused.discard((min(current, following), max(current, following)))
            previous, current = current, following
            loop.append(current)
        if len(loop) >= 4 and loop[-1] == start:
            loops.append(np.asarray([vertices[index] for index in loop[:-1]], dtype=float))
    return loops


def _project_plane_loop(points: np.ndarray, normal: np.ndarray) -> np.ndarray:
    """Project a 3D loop to a stable 2D basis on its clipping plane."""
    normal = np.asarray(normal, dtype=float)
    normal /= max(float(np.linalg.norm(normal)), 1e-12)
    helper = np.array([1.0, 0.0, 0.0])
    if abs(float(np.dot(helper, normal))) > 0.85:
        helper = np.array([0.0, 1.0, 0.0])
    basis_u = helper - normal * float(np.dot(helper, normal))
    basis_u /= max(float(np.linalg.norm(basis_u)), 1e-12)
    basis_v = np.cross(normal, basis_u)
    centered = points - points.mean(axis=0)
    return np.column_stack((centered @ basis_u, centered @ basis_v))


def _polygon_area_2d(points: np.ndarray) -> float:
    return 0.5 * float(np.sum(points[:, 0] * np.roll(points[:, 1], -1) - points[:, 1] * np.roll(points[:, 0], -1)))


def _triangulate_plane_loop(points: np.ndarray, normal: np.ndarray) -> np.ndarray:
    """Ear-clip a (possibly concave) planar polygon into triangle indices."""
    if len(points) < 3:
        return np.zeros((0, 3), dtype=np.int32)
    projected = _project_plane_loop(points, normal)
    # Remove consecutive duplicate points.  Keep collinear boundary points:
    # they are also vertices of the clipped wall mesh and the cap must use the
    # same boundary edges for a watertight result.
    def cross2(first: np.ndarray, second: np.ndarray) -> float:
        return float(first[0] * second[1] - first[1] * second[0])

    keep = []
    for index in range(len(points)):
        previous = projected[index - 1]
        current = projected[index]
        following = projected[(index + 1) % len(points)]
        if np.linalg.norm(current - previous) < 1e-7:
            continue
        keep.append(index)
    if len(keep) < 3:
        return np.zeros((0, 3), dtype=np.int32)
    projected = projected[keep]
    orientation = 1.0 if _polygon_area_2d(projected) >= 0 else -1.0
    remaining = list(range(len(keep)))
    triangles: list[list[int]] = []

    def point_in_triangle(point, a, b, c) -> bool:
        values = [
            cross2(b - a, point - a),
            cross2(c - b, point - b),
            cross2(a - c, point - c),
        ]
        return min(values) >= -1e-9 or max(values) <= 1e-9

    guard = 0
    while len(remaining) > 3 and guard < len(keep) * len(keep):
        guard += 1
        clipped = False
        for position, current in enumerate(remaining):
            previous = remaining[position - 1]
            following = remaining[(position + 1) % len(remaining)]
            a, b, c = projected[previous], projected[current], projected[following]
            cross = cross2(b - a, c - b)
            if orientation * cross <= 1e-10:
                continue
            if any(
                candidate not in (previous, current, following)
                and point_in_triangle(projected[candidate], a, b, c)
                for candidate in remaining
            ):
                continue
            triangles.append([keep[previous], keep[current], keep[following]])
            del remaining[position]
            clipped = True
            break
        if not clipped:
            # Degenerate/self-touching loops should not make the whole surface
            # fail.  A fan is only used as an explicit last-resort fallback;
            # the caller records that cap area separately for QC.
            anchor = remaining[0]
            triangles.extend([[keep[anchor], keep[index], keep[index + 1]] for index in remaining[1:-1]])
            remaining = []
            break
    if len(remaining) == 3:
        triangles.append([keep[index] for index in remaining])
    result = np.asarray(triangles, dtype=np.int32)
    if len(result):
        cross = np.cross(points[result[:, 1]] - points[result[:, 0]], points[result[:, 2]] - points[result[:, 0]])
        flip = (cross @ np.asarray(normal, dtype=float)) < 0
        result[flip] = result[flip][:, [0, 2, 1]]
    return result


def clip_mesh_by_oblique_planes(
    vertices: np.ndarray,
    faces: np.ndarray,
    anterior_plane: dict,
    posterior_plane: dict,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, dict]:
    """Clip a marching-cubes shell by two arbitrary planes and add caps.

    Returns ``(vertices, faces, inlet_triangles, outlet_triangles, stats)``.
    The cap arrays contain indices into the returned vertex array and are the
    source of truth for later inlet/outlet boundary preparation.
    """
    planes = (anterior_plane, posterior_plane)
    output_polygons: list[np.ndarray] = []
    intersections: list[list[tuple[np.ndarray, np.ndarray]]] = [[], []]
    for face in np.asarray(faces, dtype=np.int32):
        polygon: list[np.ndarray] = [np.asarray(vertices[index], dtype=float) for index in face]
        for plane in planes:
            polygon = _clip_polygon_to_plane(np.asarray(polygon, dtype=float), plane)
            if not polygon:
                break
        if len(polygon) < 3:
            continue
        polygon_array = np.asarray(polygon, dtype=float)
        output_polygons.append(polygon_array)
        for plane_index, plane in enumerate(planes):
            normal = np.asarray(plane["normal_lps"], dtype=float)
            point = np.asarray(plane["point_lps_mm"], dtype=float)
            for index, first in enumerate(polygon_array):
                second = polygon_array[(index + 1) % len(polygon_array)]
                if abs(float(np.dot(first - point, normal))) <= 1e-6 and abs(float(np.dot(second - point, normal))) <= 1e-6:
                    if np.linalg.norm(first - second) > 1e-7:
                        intersections[plane_index].append((first, second))

    if not output_polygons:
        raise PipelineError("blocked", "两张切面没有保留任何网格", ["把切面移动到气道连续区域后重试"])
    generated_points = [point for polygon in output_polygons for point in polygon]
    generated_vertices, generated_indices = _weld_points(generated_points)
    generated_faces: list[list[int]] = []
    offset = 0
    for polygon in output_polygons:
        polygon_ids = [generated_indices[tuple(np.rint(point / 1e-6).astype(np.int64).tolist())] for point in polygon]
        generated_faces.extend(
            [
                [polygon_ids[0], polygon_ids[index], polygon_ids[index + 1]]
                for index in range(1, len(polygon) - 1)
                if len({polygon_ids[0], polygon_ids[index], polygon_ids[index + 1]}) == 3
            ]
        )
        offset += len(polygon)
    cap_faces: list[np.ndarray] = []
    cap_groups: list[list[int]] = [[], []]
    for plane_index, (plane, segments) in enumerate(zip(planes, intersections)):
        normal = np.asarray(plane["normal_lps"], dtype=float)
        loops = _plane_loops(segments)
        for loop in loops:
            loop_ids = [generated_indices[tuple(np.rint(point / 1e-6).astype(np.int64).tolist())] for point in loop]
            triangles = _triangulate_plane_loop(generated_vertices[loop_ids], normal)
            if not len(triangles):
                continue
            triangles = np.asarray(loop_ids, dtype=np.int32)[triangles]
            cap_groups[plane_index].extend(range(len(cap_faces), len(cap_faces) + len(triangles)))
            cap_faces.extend(triangles)
    faces_array = np.asarray(generated_faces + [face.tolist() for face in cap_faces], dtype=np.int32)
    inlet_triangles = np.asarray(cap_groups[0], dtype=np.int32)
    # ``cap_groups[1]`` already starts after all anterior cap faces because
    # its values are taken from ``len(cap_faces)`` before the posterior group
    # is appended.  Adding the anterior count a second time would point past
    # the faces array and silently mislabel the outlet.
    outlet_triangles = np.asarray(cap_groups[1], dtype=np.int32)
    # ``cap_faces`` was appended in plane order, so posterior cap indices need
    # the anterior cap count as an offset in the final face array.
    surface_face_count = len(generated_faces)
    inlet_triangles += surface_face_count
    outlet_triangles += surface_face_count
    if len(inlet_triangles) and int(inlet_triangles.max()) >= len(faces_array):
        raise PipelineError("failed", "入口封盖索引超出裁切网格", ["重新生成表面并检查端面方案"])
    if len(outlet_triangles) and int(outlet_triangles.max()) >= len(faces_array):
        raise PipelineError("failed", "出口封盖索引超出裁切网格", ["重新生成表面并检查端面方案"])

    def triangle_area(face_indices: np.ndarray) -> float:
        if len(face_indices) == 0:
            return 0.0
        triangles = generated_vertices[faces_array[face_indices]]
        return float(
            0.5
            * np.linalg.norm(
                np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]), axis=1
            ).sum()
        )

    return generated_vertices, faces_array, inlet_triangles, outlet_triangles, {
        "format": "oblique_mesh_clip_v1",
        "retained_surface_faces": int(len(generated_faces)),
        "inlet_cap_faces": int(len(inlet_triangles)),
        "outlet_cap_faces": int(len(outlet_triangles)),
        "inlet_area_mm2": round(triangle_area(inlet_triangles), 3),
        "outlet_area_mm2": round(triangle_area(outlet_triangles), 3),
        "closed_caps": bool(len(inlet_triangles) > 0 and len(outlet_triangles) > 0),
    }


def _is_selected_local_mesh_group(
    votes: dict[int, int], selected_label: int, *, group_index: int,
    group_size: int, sampled_fragments: int, voxel_component_count: int,
) -> bool:
    """Only an ambiguous group touching the selected voxel branch blocks.

    Unmatched and unrelated positive-side mesh fragments stay intact.  A
    target branch still needs one confident match; the caller checks that
    exactly one group was selected.
    """
    if selected_label not in votes:
        return False
    winner, count = max(votes.items(), key=lambda item: item[1])
    if winner != selected_label or count < 0.8 * sum(votes.values()):
        raise PipelineError(
            "blocked", "网格局部支段跨越多个气道区域",
            ["把切面移到单一鼻孔管段；薄壁或分叉处需显式局部轮廓"],
            {
                "mesh_component_index": group_index,
                "mesh_component_fragments": group_size,
                "sampled_fragments": sampled_fragments,
                "voxel_component_votes": {str(label): int(votes[label]) for label in sorted(votes)},
                "voxel_positive_components": voxel_component_count,
            },
        )
    return True


def clip_mesh_by_oblique_planes_local(
    vertices: np.ndarray, faces: np.ndarray, anterior_plane: dict, posterior_plane: dict,
    mask: np.ndarray, volume, *, anterior_seed_lps_mm,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, dict]:
    """Open one selected anterior branch while preserving other plane crossings.

    The seed first selects a 6-connected *voxel* component on the deletion side.
    Split surface fragments are then matched to that component by their nearby
    source-mask voxels; nearest surface-to-seed matching is unsafe for a thin
    septum. Ambiguous matches and multiple inlet rings block the export.
    """
    selected, positive_mask, local_stats = oblique._local_delete_component(
        mask, volume, anterior_plane, anterior_seed_lps_mm
    )
    voxel_labels, crop, _ = oblique._label_positive_components(positive_mask)
    selected_label = int(np.unique(voxel_labels[selected[crop]])[0])
    anterior_kept: list[np.ndarray] = []
    anterior_positive: list[np.ndarray] = []
    reverse_anterior = {
        "point_lps_mm": anterior_plane["point_lps_mm"],
        "normal_lps": (-np.asarray(anterior_plane["normal_lps"], dtype=float)).tolist(),
    }
    for face in np.asarray(faces, dtype=np.int32):
        polygon = _clip_polygon_to_plane(np.asarray(vertices[face], dtype=float), posterior_plane)
        if len(polygon) < 3:
            continue
        inside = _clip_polygon_to_plane(np.asarray(polygon, dtype=float), anterior_plane)
        outside = _clip_polygon_to_plane(np.asarray(polygon, dtype=float), reverse_anterior)
        if len(inside) >= 3:
            anterior_kept.append(np.asarray(inside, dtype=float))
        if len(outside) >= 3:
            anterior_positive.append(np.asarray(outside, dtype=float))
    if not anterior_kept or not anterior_positive:
        raise PipelineError("blocked", "局部鼻孔切面没有形成可裁切的网格", ["把切面移入目标鼻孔管段"])

    # Connected components of the mesh on the positive side.  Edge adjacency
    # avoids connecting two wall sheets that merely meet at one vertex.
    _, positive_indices = _weld_points(
        [point for polygon in anterior_positive for point in polygon]
    )
    polygon_ids = [
        [positive_indices[tuple(np.rint(point / 1e-6).astype(np.int64).tolist())] for point in polygon]
        for polygon in anterior_positive
    ]
    adjacency: dict[int, set[int]] = defaultdict(set)
    edge_owner: dict[tuple[int, int], int] = {}
    for index, ids in enumerate(polygon_ids):
        for first, second in zip(ids, ids[1:] + ids[:1]):
            if first == second:
                continue
            edge = (min(first, second), max(first, second))
            previous = edge_owner.setdefault(edge, index)
            if previous != index:
                adjacency[index].add(previous)
                adjacency[previous].add(index)
    groups: list[list[int]] = []
    seen: set[int] = set()
    for start in range(len(anterior_positive)):
        if start in seen:
            continue
        queue = [start]
        seen.add(start)
        group: list[int] = []
        while queue:
            current = queue.pop()
            group.append(current)
            for neighbor in adjacency[current] - seen:
                seen.add(neighbor)
                queue.append(neighbor)
        groups.append(group)

    axes_mm = np.asarray(volume.axis_dirs, dtype=float) * np.asarray(volume.spacing, dtype=float)[:, None]
    inverse_axes = np.linalg.inv(axes_mm)
    origin = np.asarray(volume.origin, dtype=float)
    normal = np.asarray(anterior_plane["normal_lps"], dtype=float)
    plane_point = np.asarray(anterior_plane["point_lps_mm"], dtype=float)

    def label_near(point: np.ndarray) -> int:
        ijk = (point - origin) @ inverse_axes
        center = np.rint(ijk).astype(int)
        near = [
            range(max(0, int(c) - 2), min(int(n), int(c) + 3))
            for c, n in zip(center, positive_mask.shape)
        ]
        ranked: dict[int, float] = {}
        for i in near[0]:
            for j in near[1]:
                for k in near[2]:
                    if not (crop[0].start <= i < crop[0].stop
                            and crop[1].start <= j < crop[1].stop
                            and crop[2].start <= k < crop[2].stop):
                        continue
                    label = int(voxel_labels[
                        i - crop[0].start, j - crop[1].start, k - crop[2].start
                    ])
                    if label:
                        voxel_point = origin + np.asarray([i, j, k]) @ axes_mm
                        distance = float(np.linalg.norm(voxel_point - point))
                        ranked[label] = min(ranked.get(label, float("inf")), distance)
        if not ranked:
            return 0
        nearest = sorted((distance, label) for label, distance in ranked.items())
        if nearest[0][0] > 2.5 * float(max(volume.spacing)):
            return 0
        if len(nearest) > 1 and nearest[1][0] - nearest[0][0] < 0.25 * float(min(volume.spacing)):
            return 0
        return nearest[0][1]

    selected_groups: list[int] = []
    for group_index, group in enumerate(groups):
        centroids = [anterior_positive[index].mean(axis=0) for index in group]
        order = sorted(
            range(len(group)), key=lambda item: float(np.dot(centroids[item] - plane_point, normal)),
            reverse=True,
        )
        samples = [order[int(index)] for index in np.linspace(0, len(order) - 1, min(32, len(order)))]
        votes: dict[int, int] = defaultdict(int)
        for sample in samples:
            label = label_near(centroids[sample])
            if label:
                votes[label] += 1
        if _is_selected_local_mesh_group(
            votes, selected_label, group_index=group_index, group_size=len(group),
            sampled_fragments=len(samples),
            voxel_component_count=local_stats["anterior_candidate_components"],
        ):
            selected_groups.append(group_index)
    if len(selected_groups) != 1:
        raise PipelineError(
            "blocked", "所选鼻孔与网格删除支段无法唯一对应",
            ["重新选取鼻孔外侧锚点；跨支段时需明确局部轮廓"],
            {"selected_mesh_components": len(selected_groups)},
        )
    removed_polygon_indices = set(groups[selected_groups[0]])
    wall_polygons = anterior_kept + [
        polygon for index, polygon in enumerate(anterior_positive) if index not in removed_polygon_indices
    ]
    wall_vertices, wall_indices = _weld_points([point for polygon in wall_polygons for point in polygon])
    wall_faces: list[list[int]] = []
    for polygon in wall_polygons:
        ids = [wall_indices[tuple(np.rint(point / 1e-6).astype(np.int64).tolist())] for point in polygon]
        for index in range(1, len(ids) - 1):
            triangle = [ids[0], ids[index], ids[index + 1]]
            if len(set(triangle)) == 3:
                wall_faces.append(triangle)
    if not wall_faces:
        raise PipelineError("blocked", "局部裁切后网格为空", ["调整切面位置"])

    edge_counts: dict[tuple[int, int], int] = defaultdict(int)
    for face in wall_faces:
        for a, b in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0])):
            edge_counts[(min(a, b), max(a, b))] += 1
    cap_segments: list[list[tuple[np.ndarray, np.ndarray]]] = [[], []]
    for (a, b), count in edge_counts.items():
        if count != 1:
            continue
        first, second = wall_vertices[[a, b]]
        matches = []
        for plane in (anterior_plane, posterior_plane):
            plane_point = np.asarray(plane["point_lps_mm"], dtype=float)
            plane_normal = np.asarray(plane["normal_lps"], dtype=float)
            matches.append(
                max(abs(float(np.dot(first - plane_point, plane_normal))),
                    abs(float(np.dot(second - plane_point, plane_normal)))) <= 1e-5
            )
        if sum(matches) != 1:
            raise PipelineError("blocked", "局部裁切出现非端面网格缺口", ["核查原始掩膜连通性和切面位置"])
        cap_segments[matches.index(True)].append((first, second))
    cap_faces: list[list[int]] = []
    cap_groups: list[list[int]] = [[], []]
    loop_counts: list[int] = []
    for plane_index, (plane, segments) in enumerate(zip((anterior_plane, posterior_plane), cap_segments)):
        loops = _plane_loops(segments)
        loop_counts.append(len(loops))
        if (plane_index == 0 and len(loops) != 1) or not loops:
            raise PipelineError(
                "blocked", "局部鼻孔裁切形成多个入口或无有效出口",
                ["把切面移到单一、连续的鼻孔管段；多环需显式局部轮廓"],
                {"inlet_loops": len(loops) if plane_index == 0 else loop_counts[0],
                 "outlet_loops": len(loops) if plane_index == 1 else None},
            )
        for loop in loops:
            ids = [wall_indices[tuple(np.rint(point / 1e-6).astype(np.int64).tolist())] for point in loop]
            triangles = _triangulate_plane_loop(wall_vertices[ids], np.asarray(plane["normal_lps"], dtype=float))
            if not len(triangles):
                raise PipelineError("blocked", "端面轮廓不能封盖", ["调整切面，避开复杂交汇处"])
            for face in np.asarray(ids, dtype=np.int32)[triangles]:
                triangle = [int(index) for index in face]
                points = wall_vertices[triangle]
                direction = np.cross(points[1] - points[0], points[2] - points[0])
                if float(np.dot(direction, plane["normal_lps"])) < 0:
                    triangle[1], triangle[2] = triangle[2], triangle[1]
                cap_groups[plane_index].append(len(wall_faces) + len(cap_faces))
                cap_faces.append(triangle)
    output_faces = np.asarray(wall_faces + cap_faces, dtype=np.int32)
    final_counts: dict[tuple[int, int], int] = defaultdict(int)
    for face in output_faces:
        for a, b in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0])):
            final_counts[(min(int(a), int(b)), max(int(a), int(b)))] += 1
    if any(count != 2 for count in final_counts.values()):
        raise PipelineError("blocked", "局部切面封盖后网格不水密", ["调整切面，避开分叉或薄壁区域"])
    inlet = np.asarray(cap_groups[0], dtype=np.int32)
    outlet = np.asarray(cap_groups[1], dtype=np.int32)

    def cap_area(indices: np.ndarray) -> float:
        triangles = wall_vertices[output_faces[indices]]
        return float(0.5 * np.linalg.norm(
            np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]), axis=1
        ).sum())

    return wall_vertices, output_faces, inlet, outlet, {
        "format": "oblique_mesh_clip_local_v2",
        "retained_surface_faces": len(wall_faces),
        "selected_delete_surface_fragments": len(removed_polygon_indices),
        "anterior_deleted_voxels": local_stats["anterior_deleted_voxels"],
        "inlet_loops": loop_counts[0],
        "outlet_loops": loop_counts[1],
        "inlet_cap_faces": len(inlet),
        "outlet_cap_faces": len(outlet),
        "inlet_area_mm2": round(cap_area(inlet), 3),
        "outlet_area_mm2": round(cap_area(outlet), 3),
        "closed_caps": True,
    }


def run_surface(case_id: str, params: dict | None = None) -> dict:
    """对病例生成气道 STL。"""
    from .cases import case_dir, get_case, update_stage

    vol = load_volume(case_id)
    requested = dict(params or {})
    model_scope = str(requested.get("model_scope", "main_airway")).strip().lower()
    if model_scope not in {"main_airway", "full_candidate", "review"}:
        raise PipelineError(
            "failed",
            f"不支持的建模范围：{model_scope}",
            ["model_scope 只能是 main_airway、full_candidate 或 review"],
        )
    if model_scope == "review":
        # ``review`` is a readable alias for the complete threshold candidate.
        model_scope = "full_candidate"
    requested["model_scope"] = model_scope
    requested["keep_largest_component"] = model_scope == "main_airway"
    root = case_dir(case_id)
    mask_name = "mask.npy" if model_scope == "main_airway" else "candidate_mask.npy"
    mask_path = root / "segment" / mask_name
    if not mask_path.exists():
        remediation = ["先重新执行气道分割以生成全候选掩膜"] if model_scope != "main_airway" else ["先完成分割阶段"]
        raise PipelineError(
            "blocked",
            f"还没有{('全候选' if model_scope != 'main_airway' else '主气道')}分割掩膜",
            remediation,
            {"stage": "segment", "model_scope": model_scope, "mask_artifact": f"segment/{mask_name}"},
        )
    seg_path = root / "segment" / "segmentation.json"
    ap_axis = 1
    if not seg_path.exists():
        raise PipelineError("blocked", "气道分割记录缺失，不能生成 STL", ["重新执行气道分割并复核掩膜"])
    if seg_path.exists():
        seg = json.loads(seg_path.read_text(encoding="utf-8"))
        ap_axis = int(seg.get("params", {}).get("ap_axis", 1))
        if not has_current_external_air_validation(seg):
            raise PipelineError(
                "blocked",
                "当前版本的气道掩膜缺少有效的体外空气验证，不能生成 STL",
                ["返回 CT 复核视窗检查头颅外缘、鼻孔和鼻咽开口后重新确认掩膜"],
                {"stage": "segment", "mask_version": seg.get("mask_version"), "model_scope": model_scope},
            )
        mask = np.load(mask_path)
        if model_scope == "full_candidate" and not bool(seg.get("candidate_mask_available", False)):
            raise PipelineError(
                "blocked",
                "全候选掩膜未通过体内空气清理，不能生成全候选 STL",
                [
                    "先重新执行气道分割并确认候选清理成功",
                    "或切换为主气道范围继续复核",
                ],
                {
                    "stage": "segment",
                    "model_scope": model_scope,
                    "candidate_cleaning": seg.get("candidate_cleaning", {}),
                },
            )
        if seg.get("need_human_confirm", True):
            raise PipelineError(
                "blocked",
                "气道掩膜尚未人工确认，不能生成 STL",
                ["先在三平面 CT 中复核，确认没有体外空气后再继续", "若候选掩膜错误，请导入修正掩膜"],
                {"stage": "segment", "human_review": seg.get("human_review", {})},
            )
        review_flags = seg.get("review_flags", {})
        if not review_flags:
            boundary_by_axis = [
                bool(mask[0].any() or mask[-1].any()),
                bool(mask[:, 0, :].any() or mask[:, -1, :].any()),
                bool(mask[:, :, 0].any() or mask[:, :, -1].any()),
            ]
            review_flags = {
                "mask_boundary_by_axis": boundary_by_axis,
                "external_air_suspected": any(
                    touched for axis, touched in enumerate(boundary_by_axis) if axis != ap_axis
                ),
            }
        if model_scope == "main_airway" and review_flags.get("external_air_suspected"):
            raise PipelineError(
                "blocked",
                "候选掩膜触及非前后轴体数据边界，疑似包含体外空气，未生成 STL",
                [
                    "先在 CT 复核视窗逐层确认并修正气道掩膜",
                    "也可以在 3D Slicer 中用 Islands/手动编辑确认主气道后再导入",
                ],
                {"stage": "segment", "review_flags": review_flags},
            )

    plan = endplanes.current_plan(case_id, model_scope, seg, vol, mask)
    if plan is None and not bool(get_case(case_id).get("synthetic", False)):
        raise PipelineError(
            "blocked", "当前建模范围还没有人工确认的鼻孔端和鼻咽端面",
            ["返回 CT 三平面视窗选择并保存两端，再重新生成 STL"],
            {"stage": "segment", "model_scope": model_scope, "mask_version": seg.get("mask_version")},
        )
    oblique_format = plan.get("format") if plan is not None else None
    local_oblique_plan = oblique_format == "oblique_local_inlet_v2"
    bilateral_local_plan = oblique_format == endplanes.BILATERAL_LOCAL_FORMAT
    precise_local_plan = local_oblique_plan or bilateral_local_plan
    oblique_plan = oblique_format in {
        "oblique_planes_v1", "oblique_local_inlet_v2", endplanes.BILATERAL_LOCAL_FORMAT,
    }
    if precise_local_plan:
        # The read-only actual-mesh preview and the save preflight both use
        # the unmodified full-resolution surface. Reject other mesh settings
        # before writing any artifact so the STL matches the inspected mesh.
        unsupported = (
            ("smooth", requested.get("smooth") not in (None, False)),
            ("step_size", requested.get("step_size") is not None
             and (type(requested["step_size"]) is not int or requested["step_size"] != 1)),
            ("decimate_ratio", requested.get("decimate_ratio") is not None),
        )
        invalid = [name for name, rejected in unsupported if rejected]
        if invalid:
            raise PipelineError(
                "blocked", "局部入口 STL 参数与已预览的精确网格不一致",
                ["使用默认网格参数（smooth=false、step_size=1、decimate_ratio=null）重新生成 STL"],
                {"unsupported_parameters": invalid},
            )
    # Only the saved free planes may open an oblique model. Extending the
    # original A-P extrema can leave extra wall holes outside those planes.
    requested["extend_ap_edges"] = not oblique_plan
    if bilateral_local_plan:
        source_mask = select_largest_component(mask)[0]
        validation = oblique.validate_bilateral_local_planes(
            source_mask, vol, plan["left_inlet_plane"], plan["right_inlet_plane"],
            plan["posterior_plane"], plan["left_inlet_seed_lps_mm"],
            plan["right_inlet_seed_lps_mm"], plan["posterior_seed_lps_mm"],
            main_airway=True,
        )
        effective_mask = oblique.clip_mask_oblique_bilateral_local(
            source_mask, vol, validation["left_inlet_plane"],
            validation["right_inlet_plane"], validation["posterior_plane"],
            left_inlet_seed_lps_mm=validation["left_inlet_seed_lps_mm"],
            right_inlet_seed_lps_mm=validation["right_inlet_seed_lps_mm"],
            posterior_seed_lps_mm=validation["posterior_seed_lps_mm"],
        )
    elif local_oblique_plan:
        source_mask = (
            select_largest_component(mask)[0] if model_scope == "main_airway" else mask
        )
        oblique.validate_local_inlet_pair(
            source_mask, vol, plan["anterior_plane"], plan["posterior_plane"],
            plan["anterior_seed_lps_mm"], main_airway=model_scope == "main_airway",
        )
        effective_mask = oblique.clip_mask_oblique_local(
            source_mask, vol, plan["anterior_plane"], plan["posterior_plane"],
            anterior_seed_lps_mm=plan["anterior_seed_lps_mm"],
        )
    elif oblique_plan:
        # The voxel clip is retained only as a coarse mask for volume and
        # locationInMesh checks.  The authoritative geometry is clipped below
        # in patient millimetres, so a rotated plane cannot be reduced to a CT
        # layer index.
        effective_mask = oblique.clip_mask_oblique(
            mask,
            vol,
            plan["anterior_plane"],
            plan["posterior_plane"],
        )
        if model_scope == "main_airway":
            effective_mask, _ = select_largest_component(effective_mask)
    else:
        effective_mask = (
            endplanes.clip_mask(mask, ap_axis, plan["anterior_index"], plan["posterior_index"])
            if plan is not None else mask
        )
    with local_geometry_guard() if precise_local_plan else nullcontext():
        if precise_local_plan:
            ensure_local_mask_capacity(source_mask)
        result = mask_to_stl(
            mask, vol.spacing, vol.origin, vol.axis_dirs, ap_axis=ap_axis, params=requested,
        )
        result["geometry_algorithm_version"] = GEOMETRY_ALGORITHM_VERSION
        result["mask_version"] = seg.get("mask_version")
        vertices = result.pop("_vertices")
        faces = result.pop("_faces")
        inlet_triangles = np.zeros((0,), dtype=np.int32)
        inlet_left_triangles = np.zeros((0,), dtype=np.int32)
        inlet_right_triangles = np.zeros((0,), dtype=np.int32)
        outlet_triangles = np.zeros((0,), dtype=np.int32)
        if oblique_plan:
            if bilateral_local_plan:
                from .bilateral_local import clip_mesh_bilateral_local

                source_faces = int(result["faces_before_decimation"])
                ensure_local_mesh_face_capacity(source_faces)
                (vertices, faces, inlet_left_triangles, inlet_right_triangles,
                 outlet_triangles, clip_stats) = clip_mesh_bilateral_local(
                    vertices, faces, source_mask, vol,
                    left_inlet_plane=plan["left_inlet_plane"],
                    right_inlet_plane=plan["right_inlet_plane"],
                    posterior_plane=plan["posterior_plane"],
                    left_inlet_seed_lps_mm=plan["left_inlet_seed_lps_mm"],
                    right_inlet_seed_lps_mm=plan["right_inlet_seed_lps_mm"],
                    posterior_seed_lps_mm=plan["posterior_seed_lps_mm"],
                )
                clip_stats["source_surface_faces"] = source_faces
            elif local_oblique_plan:
                source_faces = int(result["faces_before_decimation"])
                ensure_local_mesh_face_capacity(source_faces)
                vertices, faces, inlet_triangles, outlet_triangles, clip_stats = clip_mesh_by_oblique_planes_local(
                    vertices, faces, plan["anterior_plane"], plan["posterior_plane"],
                    source_mask, vol, anterior_seed_lps_mm=plan["anterior_seed_lps_mm"],
                )
                clip_stats["source_surface_faces"] = source_faces
            else:
                vertices, faces, inlet_triangles, outlet_triangles, clip_stats = clip_mesh_by_oblique_planes(
                    vertices, faces, plan["anterior_plane"], plan["posterior_plane"],
                )
            result["oblique_clip"] = clip_stats
            result["faces_before_decimation"] = int(len(faces))
            result["faces"] = int(len(faces))
            result["vertices"] = int(len(vertices))
            result["bbox_lps_mm"] = [
                [round(float(vertices[:, index].min()), 2), round(float(vertices[:, index].max()), 2)]
                for index in range(3)
            ]
    result["endplanes"] = (
        {
            **plan,
            "source": (
                ("automated_bilateral_local_oblique_planes"
                 if plan.get("confirmation_source") == "automated_technical_gate"
                 and plan.get("human_confirmed") is False
                 else "user_confirmed_bilateral_local_oblique_planes") if bilateral_local_plan else
                "user_confirmed_local_oblique_planes" if local_oblique_plan else
                "user_confirmed_oblique_planes" if oblique_plan else "user_confirmed_ct_planes"
            ),
        }
        if plan is not None
        else None
    )

    stl_path = root / "surface" / "airway.stl"
    write_binary_stl(stl_path, vertices, faces)
    np.save(root / "surface" / "vertices.npy", vertices)
    np.save(root / "surface" / "faces.npy", faces)
    inlet_path = root / "surface" / "inlet_triangles.npy"
    inlet_left_path = root / "surface" / "inlet_left_triangles.npy"
    inlet_right_path = root / "surface" / "inlet_right_triangles.npy"
    outlet_path = root / "surface" / "outlet_triangles.npy"
    if bilateral_local_plan:
        inlet_path.unlink(missing_ok=True)
        np.save(inlet_left_path, inlet_left_triangles)
        np.save(inlet_right_path, inlet_right_triangles)
        np.save(outlet_path, outlet_triangles)
    elif oblique_plan:
        inlet_left_path.unlink(missing_ok=True)
        inlet_right_path.unlink(missing_ok=True)
        np.save(inlet_path, inlet_triangles)
        np.save(outlet_path, outlet_triangles)
    else:
        inlet_path.unlink(missing_ok=True)
        inlet_left_path.unlink(missing_ok=True)
        inlet_right_path.unlink(missing_ok=True)
        outlet_path.unlink(missing_ok=True)
    # 几何质检必须对照与本次 STL 相同的掩膜范围；重跑时总是刷新，
    # 避免切换主气道/完整候选后读取上次裁切的残留文件。
    qc_mask = (
        select_largest_component(effective_mask)[0]
        if model_scope == "main_airway" else effective_mask
    )
    np.save(root / "surface" / "effective_mask.npy", np.asarray(qc_mask, dtype=np.uint8))

    (root / "surface" / "surface.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    connected_components = int(result.get("manifold_prep", {}).get("components", 1))
    stage_status = "warn" if model_scope == "full_candidate" or connected_components > 1 else "ok"
    if model_scope == "full_candidate":
        message = (
            f"已导出全候选气道 STL：{result['faces']} 面；保留 {connected_components} 个表面连通分量，"
            "仅供范围复核，CFD 前需确认范围与连通性"
        )
    else:
        message = f"已导出气道 STL：{result['faces']} 面"
    update_stage(
        case_id,
        "surface",
        stage_status,
        message=message,
        artifacts=(
            ["surface/airway.stl", "surface/surface.json", "surface/inlet_left_triangles.npy",
             "surface/inlet_right_triangles.npy", "surface/outlet_triangles.npy"]
            if bilateral_local_plan else
            ["surface/airway.stl", "surface/surface.json", "surface/inlet_triangles.npy", "surface/outlet_triangles.npy"]
            if oblique_plan
            else ["surface/airway.stl", "surface/surface.json"]
        ),
        data={
            "geometry_algorithm_version": GEOMETRY_ALGORITHM_VERSION,
            "faces": result["faces"],
            "vertices": result["vertices"],
            "model_scope": model_scope,
            "mask_source": f"segment/{mask_name}",
            "connected_components": connected_components,
            "dropped_components": int(result.get("manifold_prep", {}).get("dropped_components", 0)),
            "full_candidate_review": model_scope == "full_candidate",
            "endplanes": result["endplanes"],
            "mask_version": seg.get("mask_version"),
            "opening_caps": {
                "inlet_triangles": int(len(inlet_triangles)),
                "inlet_left_triangles": int(len(inlet_left_triangles)),
                "inlet_right_triangles": int(len(inlet_right_triangles)),
                "outlet_triangles": int(len(outlet_triangles)),
                "inlet_left_area_mm2": (
                    result.get("oblique_clip", {}).get("left_inlet_area_mm2") if bilateral_local_plan else None
                ),
                "inlet_right_area_mm2": (
                    result.get("oblique_clip", {}).get("right_inlet_area_mm2") if bilateral_local_plan else None
                ),
                "inlet_area_mm2": (
                    result.get("oblique_clip", {}).get("inlet_area_mm2") if oblique_plan else None
                ),
                "outlet_area_mm2": (
                    result.get("oblique_clip", {}).get("outlet_area_mm2") if oblique_plan else None
                ),
            },
        },
    )
    return result
