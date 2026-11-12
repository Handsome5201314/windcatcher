"""Three independently seeded local cuts on one closed airway surface.

The left and right nostril inlets and the nasopharyngeal outlet are selected
by voxel-connected branches. Each exact mesh cut removes only the surface
component matched to its seed. A plane's other intersections are preserved.
"""

from __future__ import annotations

from collections import defaultdict

import numpy as np

from . import oblique
from .config import MIN_OPENING_AREA_MM2
from .errors import PipelineError


def _group_cut_metrics(
    outside: list[np.ndarray], group: list[int], plane_point: np.ndarray,
    normal: np.ndarray, seed: np.ndarray,
) -> dict[str, float]:
    """Measure a candidate wall sheet without changing its geometry."""
    area = 0.0
    max_depth = 0.0
    seed_wall_distance = float("inf")
    for index in group:
        polygon = outside[index]
        vectors = polygon[1:] - polygon[0]
        if len(vectors) >= 2:
            area += float(0.5 * np.linalg.norm(np.cross(vectors[:-1], vectors[1:]), axis=1).sum())
        max_depth = max(max_depth, float(np.max((polygon - plane_point) @ normal)))
        seed_wall_distance = min(
            seed_wall_distance, float(np.min(np.linalg.norm(polygon - seed, axis=1))),
        )
    return {
        "surface_area_mm2": area,
        "max_cut_depth_mm": max_depth,
        "seed_wall_distance_mm": seed_wall_distance,
    }


def _selected_wall_polygons(
    polygons: list[np.ndarray], plane: dict, selected: np.ndarray,
    positive_mask: np.ndarray, volume, side: str, seed_lps_mm,
) -> tuple[list[np.ndarray], int]:
    """Split a mesh once, removing only the seeded positive-side component."""
    from . import surface

    voxel_labels, crop, component_count = oblique._label_positive_components(positive_mask)
    selected_labels = np.unique(voxel_labels[selected[crop]])
    if len(selected_labels) != 1 or int(selected_labels[0]) == 0:
        raise PipelineError("blocked", f"{side}局部体素支段无法唯一标记", ["核查端面与掩膜"])
    selected_label = int(selected_labels[0])
    reverse = {
        "point_lps_mm": plane["point_lps_mm"],
        "normal_lps": (-np.asarray(plane["normal_lps"], dtype=float)).tolist(),
    }
    inside: list[np.ndarray] = []
    outside: list[np.ndarray] = []
    for polygon in polygons:
        retained = surface._clip_polygon_to_plane(polygon, plane)
        deleted = surface._clip_polygon_to_plane(polygon, reverse)
        if len(retained) >= 3:
            inside.append(np.asarray(retained, dtype=float))
        if len(deleted) >= 3:
            outside.append(np.asarray(deleted, dtype=float))
    if not inside or not outside:
        raise PipelineError("blocked", f"{side}切面没有形成可裁切网格", ["把局部切面移入目标管段"])

    # Edge-connected positive-side pieces correspond to independent wall
    # sheets. Vertex-only contacts must not make two nostrils one component.
    _, index = surface._weld_points([p for polygon in outside for p in polygon])
    polygon_ids = [
        [index[tuple(np.rint(p / 1e-6).astype(np.int64).tolist())] for p in polygon]
        for polygon in outside
    ]
    edge_owner: dict[tuple[int, int], int] = {}
    adjacency: dict[int, set[int]] = defaultdict(set)
    for polygon_index, ids in enumerate(polygon_ids):
        for a, b in zip(ids, ids[1:] + ids[:1]):
            if a == b:
                continue
            key = (min(a, b), max(a, b))
            previous = edge_owner.setdefault(key, polygon_index)
            if previous != polygon_index:
                adjacency[polygon_index].add(previous)
                adjacency[previous].add(polygon_index)
    groups: list[list[int]] = []
    visited: set[int] = set()
    for first in range(len(outside)):
        if first in visited:
            continue
        queue = [first]
        visited.add(first)
        group = []
        while queue:
            current = queue.pop()
            group.append(current)
            for neighbor in adjacency[current] - visited:
                visited.add(neighbor)
                queue.append(neighbor)
        groups.append(group)

    axes_mm = np.asarray(volume.axis_dirs, dtype=float) * np.asarray(volume.spacing, dtype=float)[:, None]
    try:
        inverse_axes = np.linalg.inv(axes_mm)
    except np.linalg.LinAlgError as exc:
        raise PipelineError("failed", "CT 体素坐标系不可逆", ["核查 DICOM 几何方向"]) from exc
    origin = np.asarray(volume.origin, dtype=float)
    normal = np.asarray(plane["normal_lps"], dtype=float)
    plane_point = np.asarray(plane["point_lps_mm"], dtype=float)

    def label_near(point: np.ndarray) -> int:
        center = np.rint((point - origin) @ inverse_axes).astype(int)
        ranges = [
            range(max(0, int(c) - 2), min(int(n), int(c) + 3))
            for c, n in zip(center, positive_mask.shape)
        ]
        nearest_by_label: dict[int, float] = {}
        for i in ranges[0]:
            for j in ranges[1]:
                for k in ranges[2]:
                    if not (crop[0].start <= i < crop[0].stop
                            and crop[1].start <= j < crop[1].stop
                            and crop[2].start <= k < crop[2].stop):
                        continue
                    label = int(voxel_labels[
                        i - crop[0].start, j - crop[1].start, k - crop[2].start
                    ])
                    if label:
                        world = origin + np.asarray([i, j, k]) @ axes_mm
                        distance = float(np.linalg.norm(world - point))
                        nearest_by_label[label] = min(
                            nearest_by_label.get(label, float("inf")), distance
                        )
        if not nearest_by_label:
            return 0
        ranked = sorted((distance, label) for label, distance in nearest_by_label.items())
        if ranked[0][0] > 2.5 * float(max(volume.spacing)):
            return 0
        if len(ranked) > 1 and ranked[1][0] - ranked[0][0] < 0.25 * float(min(volume.spacing)):
            return 0
        return ranked[0][1]

    matched: list[int] = []
    for group_index, group in enumerate(groups):
        centroids = [outside[i].mean(axis=0) for i in group]
        order = sorted(
            range(len(group)),
            key=lambda i: float(np.dot(centroids[i] - plane_point, normal)),
            reverse=True,
        )
        samples = [order[int(i)] for i in np.linspace(0, len(order) - 1, min(32, len(order)))]
        votes: dict[int, int] = defaultdict(int)
        for sample in samples:
            label = label_near(centroids[sample])
            if label:
                votes[label] += 1
        if surface._is_selected_local_mesh_group(
            votes, selected_label, group_index=group_index, group_size=len(group),
            sampled_fragments=len(samples), voxel_component_count=component_count,
        ):
            matched.append(group_index)
    metrics = {}
    if len(matched) > 1:
        seed = np.asarray(seed_lps_mm, dtype=float)
        unit_normal = normal / np.linalg.norm(normal)
        metrics = {
            index: _group_cut_metrics(outside, groups[index], plane_point, unit_normal, seed)
            for index in matched
        }
        largest = max(matched, key=lambda index: metrics[index]["surface_area_mm2"])
        main = metrics[largest]
        # A plane can just graze a sub-voxel wall bump of the same voxel
        # branch. Keep that tiny, shallow, seed-distant sheet intact. Never
        # choose the largest sheet merely because several matched: a second
        # substantial opening still needs an explicit contour or new plane.
        max_spacing = float(max(volume.spacing))
        half_min_spacing = 0.5 * float(min(volume.spacing))
        if (main["max_cut_depth_mm"] > 2 * max_spacing
                and main["surface_area_mm2"] > 0
                and all(
                    metrics[index]["surface_area_mm2"] <= 0.01 * main["surface_area_mm2"]
                    and metrics[index]["max_cut_depth_mm"] <= half_min_spacing + 1e-6
                    and metrics[index]["seed_wall_distance_mm"]
                    > main["seed_wall_distance_mm"] + max_spacing
                    for index in matched if index != largest
                )):
            matched = [largest]
    if len(matched) != 1:
        detail = {
            "side": side,
            "selected_mesh_components": len(matched),
            "code": "multiple_substantial_mesh_sheets" if matched else "no_mesh_sheet_match",
        }
        if metrics:
            detail["matched_mesh_sheets"] = [
                {
                    "faces": len(groups[index]),
                    "surface_area_mm2": round(metrics[index]["surface_area_mm2"], 3),
                    "max_cut_depth_mm": round(metrics[index]["max_cut_depth_mm"], 3),
                }
                for index in matched
            ]
        raise PipelineError(
            "blocked", f"{side}定位点与网格支段无法唯一对应",
            ["在目标切面外侧的独立管段重新定位；复杂分叉需要人工轮廓"],
            detail,
        )
    removed = set(groups[matched[0]])
    return inside + [polygon for i, polygon in enumerate(outside) if i not in removed], len(removed)


def _port_boundary_loops(
    all_loops: list[np.ndarray], boundary_edge_count: int, planes: dict,
    seeds: dict, min_spacing: float,
) -> dict[str, list[np.ndarray]]:
    """Attribute every *closed* mesh boundary to its real port before gating.

    A voxel preflight can pass while a tilted plane makes two separate mesh
    openings on one side. Diagnose that side, but never cap or merge the extra
    opening automatically.
    """
    loop_edges = sum(len(loop) for loop in all_loops)
    if loop_edges != boundary_edge_count:
        raise PipelineError(
            "blocked", "三处局部裁切后仍有未闭合的网格边",
            ["核查原始掩膜是否有孔，并调整三处切面后重新查看实际裁切"],
            {"detected_loops": len(all_loops), "boundary_edges": boundary_edge_count,
             "closed_loop_edges": loop_edges},
        )
    loops_by_side: dict[str, list[np.ndarray]] = {"left": [], "right": [], "outlet": []}
    for loop in all_loops:
        matches = [
            side for side, plane in planes.items()
            if max(abs((loop - np.asarray(plane["point_lps_mm"], dtype=float))
                       @ np.asarray(plane["normal_lps"], dtype=float))) <= 1e-5
        ]
        if not matches:
            raise PipelineError(
                "blocked", "双侧局部裁切产生不属于三处端面的网格缺口",
                ["核查原始掩膜水密性、分叉及三处切面是否重叠"],
                {"detected_loops": len(all_loops), "boundary_edges": boundary_edge_count},
            )
        if len(matches) > 1:
            centroid = loop.mean(axis=0)
            ranked = sorted(
                (float(np.linalg.norm(centroid - np.asarray(seeds[side], dtype=float))), side)
                for side in matches
            )
            if ranked[1][0] - ranked[0][0] < 0.5 * min_spacing:
                raise PipelineError(
                    "blocked", "共面局部切面无法唯一归属左右鼻孔",
                    ["把左右切面中心和定位点分别放到独立的鼻孔管段"],
                )
            matches = [ranked[0][1]]
        loops_by_side[matches[0]].append(loop)
    counts = {side: len(loops) for side, loops in loops_by_side.items()}
    offending = [side for side, count in counts.items() if count != 1]
    if offending:
        names = {"left": "左鼻孔", "right": "右鼻孔", "outlet": "鼻咽出口"}
        if len(offending) == 1:
            side = offending[0]
            reason = f"{names[side]}切面形成 {counts[side]} 个闭合环，要求恰好 1 个"
            remediation = ([f"先尝试将{names[side]}切面向外小幅移动，再查看实际裁切"]
                           if side != "outlet" else ["将鼻咽出口切面移到独立连续管段，再查看实际裁切"])
        else:
            reason = "多个端面的闭合环数量不符：" + "、".join(
                f"{names[side]} {counts[side]} 个" for side in offending
            )
            remediation = ["分别调整所列端面到独立连续管段，再查看实际裁切"]
        raise PipelineError(
            "blocked", reason, remediation,
            {"detected_loops": len(all_loops), "boundary_edges": boundary_edge_count,
             "port_loop_counts": counts, "offending_ports": offending},
        )
    return loops_by_side


def clip_mesh_bilateral_local(
    vertices: np.ndarray, faces: np.ndarray, mask: np.ndarray, volume,
    *, left_inlet_plane: dict, right_inlet_plane: dict, posterior_plane: dict,
    left_inlet_seed_lps_mm, right_inlet_seed_lps_mm, posterior_seed_lps_mm,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, dict]:
    """Return a watertight, connected shell and three disjoint cap face sets."""
    from . import surface

    selection, deleted, retained = oblique._bilateral_local_selection(
        mask, volume, left_inlet_plane, right_inlet_plane, posterior_plane,
        left_inlet_seed_lps_mm, right_inlet_seed_lps_mm, posterior_seed_lps_mm,
    )
    if not retained.any():
        raise PipelineError("blocked", "双侧裁切后没有保留气道", ["调整切面和锚点"])
    polygons = [np.asarray(vertices[face], dtype=float) for face in np.asarray(faces, dtype=np.int32)]
    removed_counts: dict[str, int] = {}
    planes = selection["planes"]
    seeds = {
        "left": left_inlet_seed_lps_mm,
        "right": right_inlet_seed_lps_mm,
        "outlet": posterior_seed_lps_mm,
    }
    for side in ("left", "right", "outlet"):
        plane = planes[side]
        offset, coefficients = oblique._distance_coefficients(volume, plane)
        positive = np.zeros_like(np.asarray(mask, dtype=bool))
        for start in range(0, positive.shape[0], 12):
            stop = min(start + 12, positive.shape[0])
            positive[start:stop] = np.asarray(mask[start:stop], dtype=bool) & (
                oblique._distances(offset, coefficients, start, stop, positive.shape) > 1e-8
            )
        polygons, removed_counts[side] = _selected_wall_polygons(
            polygons, plane, deleted[side], positive, volume, side, seeds[side],
        )
    if not polygons:
        raise PipelineError("blocked", "三处局部裁切后网格为空", ["调整切面"])

    wall_vertices, vertex_index = surface._weld_points([p for polygon in polygons for p in polygon])
    wall_faces: list[list[int]] = []
    for polygon in polygons:
        ids = [vertex_index[tuple(np.rint(p / 1e-6).astype(np.int64).tolist())] for p in polygon]
        for i in range(1, len(ids) - 1):
            triangle = [ids[0], ids[i], ids[i + 1]]
            if len(set(triangle)) == 3:
                wall_faces.append(triangle)
    if not wall_faces:
        raise PipelineError("blocked", "三处局部裁切后没有网格面", ["调整切面"])

    edge_counts: dict[tuple[int, int], int] = defaultdict(int)
    for face in wall_faces:
        for a, b in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0])):
            edge_counts[(min(a, b), max(a, b))] += 1
    boundary_segments: list[tuple[np.ndarray, np.ndarray]] = []
    for (a, b), count in edge_counts.items():
        if count != 1:
            continue
        first, second = wall_vertices[[a, b]]
        boundary_segments.append((first, second))
    all_loops = surface._plane_loops(boundary_segments)
    loops_by_side = _port_boundary_loops(
        all_loops, len(boundary_segments), planes, selection["seeds"],
        float(min(volume.spacing)),
    )
    cap_faces: list[list[int]] = []
    cap_groups: dict[str, list[int]] = {"left": [], "right": [], "outlet": []}
    cap_areas: dict[str, float] = {}
    for side, plane in planes.items():
        loops = loops_by_side[side]
        loop = loops[0]
        ids = [vertex_index[tuple(np.rint(p / 1e-6).astype(np.int64).tolist())] for p in loop]
        triangles = surface._triangulate_plane_loop(
            wall_vertices[ids], np.asarray(plane["normal_lps"], dtype=float)
        )
        if not len(triangles):
            raise PipelineError("blocked", f"{side}端面无法封盖", ["调整切面，避开复杂交汇处"])
        for face in np.asarray(ids, dtype=np.int32)[triangles]:
            triangle = [int(value) for value in face]
            points = wall_vertices[triangle]
            direction = np.cross(points[1] - points[0], points[2] - points[0])
            if float(np.dot(direction, plane["normal_lps"])) < 0:
                triangle[1], triangle[2] = triangle[2], triangle[1]
            cap_groups[side].append(len(wall_faces) + len(cap_faces))
            cap_faces.append(triangle)
    output_faces = np.asarray(wall_faces + cap_faces, dtype=np.int32)
    for side, indices in cap_groups.items():
        triangles = wall_vertices[output_faces[np.asarray(indices, dtype=np.int32)]]
        cap_areas[side] = float(0.5 * np.linalg.norm(
            np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]), axis=1
        ).sum())
        if cap_areas[side] < MIN_OPENING_AREA_MM2:
            raise PipelineError(
                "blocked", f"{side}真实网格端面面积过小",
                ["调整切面，在更连续的气道管段重新预览"],
                {"area_mm2": round(cap_areas[side], 3)},
            )
    final_edges: dict[tuple[int, int], list[int]] = defaultdict(list)
    for face_index, face in enumerate(output_faces):
        for a, b in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0])):
            final_edges[(min(int(a), int(b)), max(int(a), int(b)))].append(face_index)
    if any(len(owners) != 2 for owners in final_edges.values()):
        raise PipelineError("blocked", "三端面封盖后网格仍有额外孔或非流形边", ["检查掩膜和切面"])
    neighbors: dict[int, list[int]] = defaultdict(list)
    for first, second in final_edges.values():
        neighbors[first].append(second)
        neighbors[second].append(first)
    visited = {0}
    queue = [0]
    while queue:
        for neighbor in neighbors[queue.pop()]:
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)
    if len(visited) != len(output_faces):
        raise PipelineError(
            "blocked", "双侧入口与鼻咽出口没有形成单一连通网格",
            ["修正掩膜的游离片和左右鼻腔连通性后重试"],
        )
    stats = {
        "format": "oblique_mesh_clip_bilateral_local_v3",
        "retained_surface_faces": len(wall_faces),
        "selected_delete_surface_fragments": sum(removed_counts.values()),
        "left_inlet_loops": 1, "right_inlet_loops": 1, "outlet_loops": 1,
        "left_inlet_cap_faces": len(cap_groups["left"]),
        "right_inlet_cap_faces": len(cap_groups["right"]),
        "outlet_cap_faces": len(cap_groups["outlet"]),
        "left_inlet_area_mm2": round(cap_areas["left"], 3),
        "right_inlet_area_mm2": round(cap_areas["right"], 3),
        "outlet_area_mm2": round(cap_areas["outlet"], 3),
        "closed_caps": True,
    }
    return (
        wall_vertices, output_faces,
        np.asarray(cap_groups["left"], dtype=np.int32),
        np.asarray(cap_groups["right"], dtype=np.int32),
        np.asarray(cap_groups["outlet"], dtype=np.int32),
        stats,
    )
