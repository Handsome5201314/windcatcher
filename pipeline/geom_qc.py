"""几何质检：把 SOP 5 与 FAQ 里的失败模式变成可检查项。

检查项：游离碎片、开口（鼻孔/鼻咽）数量、退化面、重复顶点、非流形边、
法向一致性、体积与掩膜对照。自交检测需要 VTK/PyVista，本机未安装时明确标记"未检测"。
"""

from __future__ import annotations

import json
from collections import defaultdict

import numpy as np

from .config import MIN_OPENING_AREA_MM2
from .errors import PipelineError
from .surface import select_largest_component
from .volume import ANTERIOR_LPS, load_volume


def _edge_key(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def _boundary_loops(faces: np.ndarray, vertices: np.ndarray) -> list[dict]:
    """找出开口边界环（只被一个面使用的边所组成的闭环）。"""
    edges: dict[tuple[int, int], list[int]] = defaultdict(list)
    for face_id, face in enumerate(faces):
        for a, b in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0])):
            edges[_edge_key(int(a), int(b))].append(face_id)

    boundary = [key for key, ids in edges.items() if len(ids) == 1]
    if not boundary:
        return []

    adjacency: dict[int, list[int]] = defaultdict(list)
    for a, b in boundary:
        adjacency[a].append(b)
        adjacency[b].append(a)

    visited_edges = set()
    loops = []
    for start_a, start_b in boundary:
        if _edge_key(start_a, start_b) in visited_edges:
            continue
        loop = [start_a, start_b]
        visited_edges.add(_edge_key(start_a, start_b))
        prev, current = start_a, start_b
        while True:
            nexts = [n for n in adjacency[current] if n != prev and _edge_key(current, n) not in visited_edges]
            if not nexts:
                break
            nxt = nexts[0]
            visited_edges.add(_edge_key(current, nxt))
            loop.append(nxt)
            prev, current = current, nxt
            if current == start_a:
                break
        loops.append(loop)
    return [{"vertex_indices": loop, "points": vertices[loop]} for loop in loops]


def _loop_geometry(loop_points: np.ndarray, ap_coord_func) -> dict:
    """计算环的顶点数、质心、面积（投影到最佳拟合平面）与前后位置。"""
    centered = loop_points - loop_points.mean(axis=0)
    if len(loop_points) >= 3:
        _, _, vh = np.linalg.svd(centered, full_matrices=False)
        normal = vh[-1]
        basis_u = vh[0]
        basis_v = vh[1]
        u = centered @ basis_u
        v = centered @ basis_v
        area = 0.5 * float(np.abs(np.dot(u, np.roll(v, -1)) - np.dot(np.roll(u, -1), v)))
    else:
        normal = np.zeros(3)
        area = 0.0
    centroid = loop_points.mean(axis=0)
    return {
        "vertices": int(len(loop_points)),
        "points": [[round(float(c), 6) for c in point] for point in loop_points],
        "centroid_lps_mm": [round(float(x), 2) for x in centroid],
        "area_mm2": round(area, 2),
        "plane_normal": [round(float(x), 3) for x in normal],
        "ap_position": ap_coord_func(centroid),
    }


def _cap_geometry(vertices: np.ndarray, faces: np.ndarray, face_indices: np.ndarray, end: str, ap_coord_func) -> dict | None:
    """从 oblique 裁切保存的封盖三角面生成一个可审计的开口记录。

    斜切后的正式 STL 是闭合壳，不能再靠边界边寻找开口。封盖面索引由
    surface 阶段写入 inlet/outlet_triangles.npy，是这类模型的开口权威来源。
    """
    indices = np.asarray(face_indices, dtype=np.int64).reshape(-1)
    if not len(indices):
        return None
    if np.any(indices < 0) or np.any(indices >= len(faces)):
        raise PipelineError("failed", f"{end}封盖索引超出表面范围", ["重新生成 STL 与几何质检"])
    triangles = np.asarray(vertices[faces[indices]], dtype=float)
    cross = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    areas = 0.5 * np.linalg.norm(cross, axis=1)
    usable = areas > 1e-9
    if not np.any(usable):
        return None
    triangles = triangles[usable]
    areas = areas[usable]
    normal = np.sum(cross[usable], axis=0)
    length = float(np.linalg.norm(normal))
    if length > 1e-12:
        normal = normal / length
    points = np.unique(np.round(triangles.reshape(-1, 3), 6), axis=0)
    centroid = np.average(triangles.mean(axis=1), axis=0, weights=areas)
    return {
        "vertices": int(len(points)),
        "points": [[round(float(c), 6) for c in point] for point in points],
        "centroid_lps_mm": [round(float(x), 2) for x in centroid],
        "area_mm2": round(float(areas.sum()), 2),
        "plane_normal": [round(float(x), 3) for x in normal],
        "ap_position": ap_coord_func(centroid),
        "end": end,
        "is_tiny": bool(float(areas.sum()) < MIN_OPENING_AREA_MM2),
        "source": "oblique_cap_triangles",
        "face_indices": [int(value) for value in indices[usable]],
    }


def _signed_volume(vertices: np.ndarray, faces: np.ndarray) -> float:
    v0 = vertices[faces[:, 0]]
    v1 = vertices[faces[:, 1]]
    v2 = vertices[faces[:, 2]]
    return float(np.sum(np.einsum("ij,ij->i", v0, np.cross(v1, v2))) / 6.0)


def _volume_with_caps(
    vertices: np.ndarray,
    faces: np.ndarray,
    loops: list[dict],
    ap_min: float,
    ap_max: float,
) -> float:
    """开口表面不能直接套用散度定理，先把每个开口用扇形三角封起来再算体积。

    封盖方向按该开口所在端（鼻孔端朝前、鼻咽端朝后）判定，避免方向反了导致体积抵消。
    """
    anterior = np.asarray(ANTERIOR_LPS, dtype=float)
    span = max(ap_max - ap_min, 1e-6)
    total = _signed_volume(vertices, faces)

    for loop in loops:
        pts = np.asarray(loop["points"], dtype=float)
        if len(pts) < 3:
            continue
        centroid = pts.mean(axis=0)
        ap_position = float(np.dot(centroid, anterior))
        desired = anterior if ap_position >= ap_min + 0.5 * span else -anterior

        normal = np.zeros(3, dtype=float)
        for i in range(len(pts)):
            a = pts[i] - centroid
            b = pts[(i + 1) % len(pts)] - centroid
            normal += np.cross(a, b)
        if np.linalg.norm(normal) < 1e-12:
            continue
        normal = normal / np.linalg.norm(normal)
        if float(np.dot(normal, desired)) < 0:
            pts = pts[::-1]

        for i in range(len(pts)):
            a = centroid
            b = pts[i]
            c = pts[(i + 1) % len(pts)]
            total += float(np.dot(a, np.cross(b, c))) / 6.0
    return abs(total)


def check_mesh(
    stl_dir: str,
    mask: np.ndarray | None = None,
    spacing=None,
    origin=None,
    axis_dirs=None,
    ap_axis: int = 1,
) -> dict:
    """对已生成的表面做质检。stl_dir 指向保存 vertices.npy / faces.npy 的目录。"""
    from pathlib import Path

    root = Path(stl_dir)
    v_path = root / "vertices.npy"
    f_path = root / "faces.npy"
    if not (v_path.exists() and f_path.exists()):
        raise PipelineError("failed", "还没有表面数据（surface/vertices.npy）", ["先执行表面生成阶段"])
    vertices = np.load(v_path)
    faces = np.load(f_path)

    # 重复顶点（量化到 1e-6 mm）
    quantized = np.round(vertices, 6)
    unique_rows = np.unique(quantized, axis=0)
    duplicate_vertices = int(len(quantized) - len(unique_rows))

    # 退化面
    v0 = vertices[faces[:, 0]]
    v1 = vertices[faces[:, 1]]
    v2 = vertices[faces[:, 2]]
    cross = np.cross(v1 - v0, v2 - v0)
    areas = 0.5 * np.linalg.norm(cross, axis=1)
    degenerate_faces = int(np.sum(areas < 1e-9))

    # 边统计
    edge_counts: dict[tuple[int, int], int] = defaultdict(int)
    for face in faces:
        for a, b in ((int(face[0]), int(face[1])), (int(face[1]), int(face[2])), (int(face[2]), int(face[0]))):
            edge_counts[_edge_key(a, b)] += 1
    boundary_edges = int(sum(1 for c in edge_counts.values() if c == 1))
    non_manifold_edges = int(sum(1 for c in edge_counts.values() if c > 2))

    # 连通分量（按顶点共享边）
    parent = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for face in faces:
        union(int(face[0]), int(face[1]))
        union(int(face[1]), int(face[2]))
    # 只统计真正被面引用的顶点，避免把未使用的孤立顶点算成碎片
    used_vertices = np.unique(faces.reshape(-1))
    components = len({find(int(i)) for i in used_vertices}) if len(used_vertices) else 0

    anterior = np.asarray(ANTERIOR_LPS, dtype=float)

    def ap_coord(point) -> float:
        return round(float(np.dot(point, anterior)), 2)

    loops = _boundary_loops(faces, vertices)
    loop_info = [_loop_geometry(np.asarray(loop["points"]), ap_coord) for loop in loops]
    loop_info.sort(key=lambda item: item["area_mm2"], reverse=True)

    # Oblique clipping closes the shell with explicit inlet/outlet caps. Those
    # caps replace boundary loops as the source of truth for opening counts.
    cap_openings = []
    cap_files = ((root / "inlet_triangles.npy", "anterior"), (root / "outlet_triangles.npy", "posterior"))
    for cap_path, end in cap_files:
        if not cap_path.exists():
            continue
        try:
            cap_indices = np.load(cap_path)
        except (OSError, ValueError) as exc:
            raise PipelineError("failed", f"{end}封盖记录无法读取", ["重新生成 STL 与几何质检"]) from exc
        record = _cap_geometry(vertices, faces, cap_indices, end, ap_coord)
        if record is not None:
            cap_openings.append(record)
    has_cap_openings = bool(cap_openings)
    if has_cap_openings:
        loop_info = cap_openings

    # 旧的开放表面按患者前后轴分组；斜切闭合表面已经在封盖文件中分组。
    if not has_cap_openings:
        ap_all = vertices @ anterior
        ap_min, ap_max = float(ap_all.min()), float(ap_all.max())
        span = max(ap_max - ap_min, 1e-6)
        # ap_position = 质心与"前方"方向向量的点积，越大越靠前（鼻孔端）
        for item in loop_info:
            pos = item["ap_position"]
            if pos >= ap_max - 0.15 * span:
                item["end"] = "anterior"
            elif pos <= ap_min + 0.15 * span:
                item["end"] = "posterior"
            else:
                item["end"] = "middle"
            item["is_tiny"] = item["area_mm2"] < MIN_OPENING_AREA_MM2
    openings_by_end = {
        "anterior": sum(1 for i in loop_info if i["end"] == "anterior"),
        "posterior": sum(1 for i in loop_info if i["end"] == "posterior"),
        "middle": sum(1 for i in loop_info if i["end"] == "middle"),
    }
    usable_openings_by_end = {
        end: sum(1 for i in loop_info if i["end"] == end and not i["is_tiny"])
        for end in openings_by_end
    }

    # 法向一致性（需要掩膜）
    orientation = {"status": "skipped", "reason": "未提供掩膜，无法判定内外方向"}
    if mask is not None and spacing is not None and origin is not None and axis_dirs is not None:
        dirs = np.asarray(axis_dirs, dtype=float)
        spacing = np.asarray(spacing, dtype=float)
        eps = float(np.min(spacing)) * 0.5

        def to_voxel(points: np.ndarray) -> np.ndarray:
            delta = np.asarray(points, dtype=float) - np.asarray(origin, dtype=float)
            idx = np.stack([(delta @ dirs[a]) / spacing[a] for a in range(3)], axis=-1)
            return np.rint(idx).astype(int)

        def inside(points: np.ndarray) -> np.ndarray:
            idx = to_voxel(points)
            ok = np.all((idx >= 0) & (idx < np.asarray(mask.shape)), axis=1)
            out = np.zeros(len(points), dtype=bool)
            for i, (index, valid) in enumerate(zip(idx, ok)):
                if valid:
                    out[i] = bool(mask[tuple(index)])
            return out

        sample_count = min(len(faces), 2000)
        step = max(len(faces) // sample_count, 1)
        sampled = faces[::step]
        centroids = vertices[sampled].mean(axis=1)
        normals = np.cross(
            vertices[sampled[:, 1]] - vertices[sampled[:, 0]],
            vertices[sampled[:, 2]] - vertices[sampled[:, 0]],
        )
        lengths = np.linalg.norm(normals, axis=1)
        lengths[lengths == 0] = 1.0
        normals = normals / lengths[:, None]
        outward = inside(centroids + normals * eps) == False  # noqa: E712 - 需要逐元素比较
        inward = inside(centroids - normals * eps) == True    # noqa: E712
        consistent = int(np.sum(outward & inward))
        ratio = consistent / max(len(sampled), 1)
        orientation = {
            "status": "checked",
            "sampled_faces": int(len(sampled)),
            "outward_consistent_ratio": round(float(ratio), 3),
            "note": "沿法向外侧取样应在气道外、内侧取样应在气道内",
        }

    # 体积对照（开口表面需先封盖再计算）
    volume_check = {"status": "skipped", "reason": "未提供掩膜"}
    if mask is not None and spacing is not None:
        ap_all = vertices @ np.asarray(ANTERIOR_LPS, dtype=float)
        if has_cap_openings:
            mesh_volume_ml = abs(_signed_volume(vertices, faces)) / 1000.0
        else:
            ap_all = vertices @ np.asarray(ANTERIOR_LPS, dtype=float)
            mesh_volume_ml = (
                _volume_with_caps(vertices, faces, loops, float(ap_all.min()), float(ap_all.max())) / 1000.0
            )
        mask_volume_ml = float(mask.sum()) * float(np.prod(spacing)) / 1000.0
        deviation = abs(mesh_volume_ml - mask_volume_ml) / mask_volume_ml * 100 if mask_volume_ml else 0.0
        volume_check = {
            "status": "checked",
            "mesh_volume_ml": round(mesh_volume_ml, 3),
            "mask_volume_ml": round(mask_volume_ml, 3),
            "deviation_percent": round(float(deviation), 2),
        }

    reasons: list[str] = []
    verdict = "pass"
    if has_cap_openings and boundary_edges > 0:
        verdict = "fail"
        reasons.append(
            f"模型已包含入口/出口封盖，但完整壳仍有 {boundary_edges} 条未闭合边："
            "存在额外孔或封盖接缝缺口，修复并重新质检后才能生成 CFD 算例"
        )
    if len(loop_info) == 0:
        verdict = "fail"
        reasons.append("没有检测到开口：鼻孔与鼻咽很可能都被封死了（SOP FAQ Q2）")
    else:
        if openings_by_end["middle"] > 0:
            verdict = "fail"
            reasons.append(
                f"有 {openings_by_end['middle']} 个开口位于气道中段（既不在鼻孔端也不在鼻咽端）："
                "疑似鼻窦旁路或表面破洞，需要人工修复后再求解"
            )
        if usable_openings_by_end["anterior"] == 0 or usable_openings_by_end["posterior"] == 0:
            verdict = "fail"
            tiny_by_end = {
                end: [
                    f"{item['area_mm2']:.2f} mm²"
                    for item in loop_info
                    if item["end"] == end and item["is_tiny"]
                ]
                for end in ("anterior", "posterior")
            }
            reasons.append(
                f"缺少可用端面：鼻孔端 {usable_openings_by_end['anterior']} 个 / "
                f"鼻咽端 {usable_openings_by_end['posterior']} 个（技术过滤阈值 ≥ {MIN_OPENING_AREA_MM2:g} mm²）。"
                f"检测到的小孔：鼻孔端 {', '.join(tiny_by_end['anterior']) or '无'}；"
                f"鼻咽端 {', '.join(tiny_by_end['posterior']) or '无'}。"
                "小孔不参与 CFD 入口/出口封盖；需检查实际裁切位置和端面。"
            )
        elif openings_by_end["middle"] == 0 and verdict == "pass":
            reasons.append(
                f"开口技术初筛通过：鼻孔端 {usable_openings_by_end['anterior']} 个、"
                f"鼻咽端 {usable_openings_by_end['posterior']} 个；仍需人工确认端面位置与解剖范围"
            )

    if components > 1:
        verdict = "warn" if verdict == "pass" else verdict
        reasons.append(f"表面有 {components} 个连通分量，可能存在游离碎片（SOP 5 要求删除 Loose）")
    if degenerate_faces > 0:
        verdict = "warn" if verdict == "pass" else verdict
        reasons.append(f"存在 {degenerate_faces} 个退化面（零面积），建议清理后重跑")
    if non_manifold_edges > 0:
        if has_cap_openings:
            verdict = "fail"
            reasons.append(f"封盖后的完整壳仍有 {non_manifold_edges} 条非流形边，修复后才能生成 CFD 算例")
        else:
            verdict = "warn" if verdict == "pass" else verdict
            reasons.append(f"存在 {non_manifold_edges} 条非流形边，网格生成可能失败")
    if duplicate_vertices > 0:
        reasons.append(f"存在 {duplicate_vertices} 个重复顶点位置（建议 Merge by Distance）")

    result = {
        "faces": int(len(faces)),
        "vertices": int(len(vertices)),
        "connected_components": int(components),
        "boundary_edges": boundary_edges,
        "openings": loop_info,
        "opening_count": len(loop_info),
        "openings_by_end": openings_by_end,
        "usable_openings_by_end": usable_openings_by_end,
        "opening_area_threshold_mm2": MIN_OPENING_AREA_MM2,
        "degenerate_faces": degenerate_faces,
        "duplicate_vertices": duplicate_vertices,
        "non_manifold_edges": non_manifold_edges,
        "orientation": orientation,
        "volume_check": volume_check,
        "self_intersection": {
            "status": "not_run",
            "reason": "当前几何质检尚未执行精确自交检查；请在 ParaView / Blender 中人工复核",
        },
        "verdict": verdict,
        "blocking": verdict == "fail",
        "reasons": reasons,
    }
    return result


def run_geom_qc(case_id: str) -> dict:
    """对病例执行几何质检并落盘。"""
    from pathlib import Path

    from .cases import case_dir, update_stage

    volume = load_volume(case_id)
    root = case_dir(case_id)
    surface_meta_path = root / "surface" / "surface.json"
    surface_meta = {}
    if surface_meta_path.exists():
        try:
            surface_meta = json.loads(surface_meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            surface_meta = {}
    model_scope = str(surface_meta.get("params", {}).get("model_scope", "main_airway"))
    effective_mask_path = root / "surface" / "effective_mask.npy"
    mask_name = "candidate_mask.npy" if model_scope == "full_candidate" else "mask.npy"
    mask_path = effective_mask_path if effective_mask_path.exists() else root / "segment" / mask_name
    mask_source = mask_path.relative_to(root).as_posix()
    mask = np.load(mask_path) if mask_path.exists() else None
    # 质检必须对与 surface 相同的范围计算体积。主气道模式沿用最大
    # 6-连通组件；full_candidate 保留候选的所有组件，不能在质检阶段
    # 再次静默删除鼻窦/旁支。
    if mask is not None and mask_path != effective_mask_path and model_scope != "full_candidate":
        mask, _ = select_largest_component(mask)

    result = check_mesh(
        str(root / "surface"),
        mask=mask,
        spacing=volume.spacing,
        origin=volume.origin,
        axis_dirs=volume.axis_dirs,
    )
    result["model_scope"] = model_scope
    result["mask_source"] = mask_source
    result["full_candidate_review"] = model_scope == "full_candidate"
    (root / "surface" / "geom_qc.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    status = "ok" if result["verdict"] == "pass" else ("warn" if result["verdict"] == "warn" else "failed")
    update_stage(
        case_id,
        "surface",
        status,
        message=f"几何质检：{result['verdict']}（开口 {result['opening_count']} 个，面数 {result['faces']}）",
        artifacts=["surface/geom_qc.json"],
        data={
            "opening_count": result["opening_count"],
            "opening_anterior": result["openings_by_end"].get("anterior", 0),
            "opening_posterior": result["openings_by_end"].get("posterior", 0),
            "opening_middle": result["openings_by_end"].get("middle", 0),
            "usable_opening_anterior": result["usable_openings_by_end"].get("anterior", 0),
            "usable_opening_posterior": result["usable_openings_by_end"].get("posterior", 0),
            "opening_area_threshold_mm2": result["opening_area_threshold_mm2"],
            "faces": result["faces"],
            "boundary_edges": result["boundary_edges"],
            "verdict": result["verdict"],
            "volume_deviation_percent": result["volume_check"].get("deviation_percent"),
            "volume_check": result["volume_check"],
            "reasons": result["reasons"],
            "model_scope": model_scope,
            "mask_source": mask_source,
            "full_candidate_review": model_scope == "full_candidate",
        },
    )
    return result
