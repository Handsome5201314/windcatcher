"""掩膜 → STL 表面。

SOP 5 的简化与平滑默认关闭：鼻腔窄通道不允许被盲目平滑或简化；
开启时必须记录前后面数与体积变化。

自由切面先对补零的掩膜生成闭合壳，再按用户平面裁切和封盖。
旧正交流程保留前后轴极值延伸，以生成轴向开口。
"""

from __future__ import annotations

import json
import struct
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy import ndimage
from skimage import measure

from .config import SOP_DEFAULTS
from . import endplanes, oblique
from .errors import PipelineError
from .mask_review import has_current_external_air_validation
from .volume import load_volume


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

    notes = []
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
    oblique_plan = bool(plan is not None and plan.get("format") == "oblique_planes_v1")
    # Only the saved free planes may open an oblique model. Extending the
    # original A-P extrema can leave extra wall holes outside those planes.
    requested["extend_ap_edges"] = not oblique_plan
    if oblique_plan:
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
    result = mask_to_stl(
        mask, vol.spacing, vol.origin, vol.axis_dirs, ap_axis=ap_axis, params=requested,
    )
    result["mask_version"] = seg.get("mask_version")
    vertices = result.pop("_vertices")
    faces = result.pop("_faces")
    inlet_triangles = np.zeros((0,), dtype=np.int32)
    outlet_triangles = np.zeros((0,), dtype=np.int32)
    if oblique_plan:
        vertices, faces, inlet_triangles, outlet_triangles, clip_stats = clip_mesh_by_oblique_planes(
            vertices,
            faces,
            plan["anterior_plane"],
            plan["posterior_plane"],
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
            "source": "user_confirmed_oblique_planes" if oblique_plan else "user_confirmed_ct_planes",
        }
        if plan is not None
        else None
    )

    stl_path = root / "surface" / "airway.stl"
    write_binary_stl(stl_path, vertices, faces)
    np.save(root / "surface" / "vertices.npy", vertices)
    np.save(root / "surface" / "faces.npy", faces)
    inlet_path = root / "surface" / "inlet_triangles.npy"
    outlet_path = root / "surface" / "outlet_triangles.npy"
    if oblique_plan:
        np.save(inlet_path, inlet_triangles)
        np.save(outlet_path, outlet_triangles)
    else:
        inlet_path.unlink(missing_ok=True)
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
            ["surface/airway.stl", "surface/surface.json", "surface/inlet_triangles.npy", "surface/outlet_triangles.npy"]
            if oblique_plan
            else ["surface/airway.stl", "surface/surface.json"]
        ),
        data={
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
                "outlet_triangles": int(len(outlet_triangles)),
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
