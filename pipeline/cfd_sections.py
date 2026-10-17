"""Independent, confirmed section annotations and flux from the final U field.

The cutter interpolates cell U to points. Integration is exact for that piecewise
linear field, including polygon edges and the zero contour; it is not a claim
that the interpolated cut is as conservative as OpenFOAM's boundary phi.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from pathlib import Path

import numpy as np

from . import cfd_results
from .cfd_validation import inspect_latest_fields, strip_comments
from .errors import PipelineError

VERSION = 1
SECTION_ANALYSIS_VERSION = 2
AREA_EPS_MM2 = 1e-9
FLOW_EPS_LPM = 1e-9
REGION_IDS = {f"{side}-{level}" for side in ("left", "right") for level in ("upper", "middle", "lower")}


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError("failed", f"无法读取分析所需的 {path.name}", ["检查文件后重试"]) from exc
    if not isinstance(value, dict):
        raise PipelineError("failed", f"{path.name} 格式错误")
    return value


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _geometry(root: Path) -> tuple[dict, str]:
    meta = _json(root / "volume" / "meta.json")
    segmentation = _json(root / "segment" / "segmentation.json")
    try:
        geometry = {key: meta[key] for key in ("shape", "spacing_mm", "origin_mm", "axis_dirs")}
        axis = segmentation["params"]["ap_axis"]
        version = segmentation["mask_version"]
        directions = np.asarray(geometry["axis_dirs"], dtype=float)
        spacing = np.asarray(geometry["spacing_mm"], dtype=float)
        origin = np.asarray(geometry["origin_mm"], dtype=float)
        shape = geometry["shape"]
    except (KeyError, TypeError, ValueError) as exc:
        raise PipelineError("blocked", "CT 方向或掩膜版本记录缺失", ["复核 CT 与当前掩膜记录"]) from exc
    if (type(axis) is not int or axis not in (0, 1, 2) or type(version) is not int
            or directions.shape != (3, 3) or spacing.shape != (3,) or origin.shape != (3,)
            or not isinstance(shape, list) or len(shape) != 3 or any(type(n) is not int or n <= 0 for n in shape)
            or not all(np.isfinite(array).all() for array in (directions, spacing, origin))
            or np.any(spacing <= 0) or not np.allclose(directions @ directions.T, np.eye(3), atol=1e-4)):
        raise PipelineError("blocked", "CT 几何记录无效，不能确定分析截面的方向")
    anterior_alignment = float(directions[axis] @ np.array([0., -1., 0.]))
    if abs(anterior_alignment) < 0.7:
        raise PipelineError("blocked", "CT 前后轴无法可靠判定", ["复核 CT 方向和分割前后轴"])
    geometry.update(ap_axis=axis, mask_version=version)
    return geometry, _digest(geometry)


def _source(root: Path) -> tuple[Path, str, str]:
    foam, field_mesh_signature, time_name = cfd_results._source(root)
    meta_path = root / "cfd" / "case_meta.json"
    physical_signature = hashlib.sha256(meta_path.read_bytes()).hexdigest() if meta_path.exists() else None
    with (root / "cfd" / time_name / "U").open("rb") as handle:
        header = strip_comments(handle.read(8192).decode("ascii", errors="replace"))
    match = re.search(r"\bdimensions\s*\[([^]]+)\]\s*;", header)
    try:
        dimensions = [int(value) for value in match.group(1).split()] if match else None
    except ValueError as exc:
        raise PipelineError("blocked", "最终 U 场的维度记录无效") from exc
    if dimensions != [0, 1, -1, 0, 0, 0, 0]:
        raise PipelineError("blocked", "最终 U 场没有有效的 m/s 维度，不能积分流量")
    return foam, _digest({"field_mesh": field_mesh_signature, "physical": physical_signature, "version": VERSION}), time_name


def _read_mesh(root: Path, foam: Path, time_name: str):
    vtk = cfd_results.vtk
    if vtk is None:
        raise PipelineError("failed", "当前 Python 环境没有 VTK", ["在工作台虚拟环境安装 VTK"])
    if not foam.exists():
        foam.write_text("", encoding="ascii")
    reader = vtk.vtkOpenFOAMReader()
    reader.SetFileName(str(foam))
    reader.SetSkipZeroTime(True)
    reader.SetCreateCellToPoint(False)
    reader.UpdateInformation()
    times = reader.GetTimeValues()
    if times is None or float(time_name) not in [times.GetValue(i) for i in range(times.GetNumberOfValues())]:
        raise PipelineError("failed", "VTK 无法读取当前最终时间步")
    reader.DisableAllPatchArrays()
    reader.SetPatchArrayStatus("internalMesh", 1)
    reader.DisableAllCellArrays()
    reader.SetCellArrayStatus("U", 1)
    reader.SetTimeValue(float(time_name))
    reader.Update()
    output = reader.GetOutput()
    mesh = output.GetBlock(0) if output.IsA("vtkMultiBlockDataSet") else output
    if mesh is None or not mesh.IsA("vtkDataSet") or mesh.GetNumberOfCells() == 0 or mesh.GetPoints() is None:
        raise PipelineError("failed", "最终时间步没有有效的内部网格")
    velocity = mesh.GetCellData().GetArray("U")
    if velocity is None:
        raise PipelineError("failed", "最终内部网格缺少完整 U 场")
    vectors = cfd_results.vtk_to_numpy(velocity)
    points = cfd_results.vtk_to_numpy(mesh.GetPoints().GetData())
    if vectors.shape != (mesh.GetNumberOfCells(), 3) or not np.isfinite(vectors).all() or not np.isfinite(points).all():
        raise PipelineError("failed", "内部网格与 U 场不匹配或包含非有限值")
    return mesh


def _plane(mesh, geometry: dict, fraction: float) -> dict:
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not math.isfinite(fraction) or not 0 < fraction < 1:
        raise PipelineError("failed", "截面位置必须在鼻腔模型前后范围内部（0 与 1 之间）")
    direction = np.asarray(geometry["axis_dirs"][geometry["ap_axis"]], dtype=float)
    normal = direction * (-1 if direction @ [0., -1., 0.] > 0 else 1)
    u = np.array([1., 0., 0.]) - normal * normal[0]
    u /= np.linalg.norm(u)
    v = np.cross(u, normal)
    v /= np.linalg.norm(v)
    points_mm = cfd_results.vtk_to_numpy(mesh.GetPoints().GetData()) * 1000.
    projection = points_mm @ normal
    coordinate = float(projection.min() + fraction * np.ptp(projection))
    center = (points_mm.min(axis=0) + points_mm.max(axis=0)) / 2
    point = center + normal * (coordinate - center @ normal)
    return {"point_lps_mm": point.tolist(), "normal_lps": normal.tolist(), "u_lps": u.tolist(),
            "v_lps": v.tolist(), "position_fraction": float(fraction),
            "position_coordinate_mm": coordinate,
            "position_bounds_mm": [float(projection.min()), float(projection.max())]}


def _cut(mesh, plane: dict, include_vectors: bool = False) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict]:
    vtk = cfd_results.vtk
    converter = vtk.vtkCellDataToPointData()
    converter.SetInputData(mesh)
    converter.Update()
    cut_plane = vtk.vtkPlane()
    point = np.asarray(plane["point_lps_mm"], dtype=float)
    normal = np.asarray(plane["normal_lps"], dtype=float)
    cut_plane.SetOrigin(*(point * 0.001))
    cut_plane.SetNormal(*normal)
    cutter = vtk.vtkCutter()
    cutter.SetInputData(converter.GetOutput())
    cutter.SetCutFunction(cut_plane)
    triangle = vtk.vtkTriangleFilter()
    triangle.SetInputConnection(cutter.GetOutputPort())
    triangle.Update()
    result = triangle.GetOutput()
    if result.GetNumberOfPoints() == 0 or result.GetNumberOfCells() == 0:
        raise PipelineError("blocked", "所选截面没有切到流体网格", ["移动截面位置后重试"])
    positions = cfd_results.vtk_to_numpy(result.GetPoints().GetData()).astype(float)
    velocity = result.GetPointData().GetArray("U")
    if velocity is None:
        raise PipelineError("failed", "截面插值没有保留 U 场")
    vectors = cfd_results.vtk_to_numpy(velocity)
    if vectors.shape != positions.shape or not np.isfinite(vectors).all() or not np.isfinite(positions).all():
        raise PipelineError("failed", "截面坐标或 U 场无效")
    uv = np.column_stack(((positions * 1000 - point) @ plane["u_lps"], (positions * 1000 - point) @ plane["v_lps"]))
    scalar = vectors @ normal
    indices = np.asarray([[result.GetCell(i).GetPointId(j) for j in range(3)]
                          for i in range(result.GetNumberOfCells()) if result.GetCell(i).GetNumberOfPoints() == 3], dtype=int)
    if not indices.size:
        raise PipelineError("failed", "截面未生成有效三角形")
    section_mesh = {"positions": positions.reshape(-1).tolist(), "indices": indices.reshape(-1).tolist(),
                    "uv_mm": uv.reshape(-1).tolist(), "speeds": np.linalg.norm(vectors, axis=1).tolist(),
                    "normal_velocity_mps": scalar.tolist()}
    if include_vectors:
        section_mesh["velocity_vectors"] = vectors.tolist()
    return uv, scalar, indices, section_mesh


def preview_section(case_root: Path, position_fraction: float) -> dict:
    root = Path(case_root)
    foam, signature, time_name = _source(root)
    geometry, geometry_signature = _geometry(root)
    mesh = _read_mesh(root, foam, time_name)
    plane = _plane(mesh, geometry, position_fraction)
    _, _, _, section_mesh = _cut(mesh, plane)
    if _source(root)[1] != signature or _geometry(root)[1] != geometry_signature:
        raise PipelineError("blocked", "分析期间模型或场发生变化，请刷新截面")
    return {"status": "preview", "plane": plane, "section_mesh": section_mesh,
            "source_signature": signature, "geometry_signature": geometry_signature,
            "units": {"coordinates": "m", "uv": "mm", "flow": "L/min"}}


def _weighted_quantile(values: np.ndarray, weights: np.ndarray, quantile: float) -> float | None:
    valid = np.isfinite(values) & np.isfinite(weights) & (weights > 0)
    if not valid.any():
        return None
    order = np.argsort(values[valid], kind="stable")
    ordered_values = values[valid][order]
    cumulative = np.cumsum(weights[valid][order])
    index = int(np.searchsorted(cumulative, quantile * cumulative[-1], side="left"))
    return float(ordered_values[min(index, len(ordered_values) - 1)])


def _section_velocity_statistics(uv: np.ndarray, vectors: np.ndarray, indices: np.ndarray) -> dict:
    point_speeds = np.linalg.norm(vectors, axis=1)
    triangles = uv[indices]
    twice_area = np.abs(
        (triangles[:, 1, 0] - triangles[:, 0, 0]) * (triangles[:, 2, 1] - triangles[:, 0, 1])
        - (triangles[:, 1, 1] - triangles[:, 0, 1]) * (triangles[:, 2, 0] - triangles[:, 0, 0])
    )
    area_mm2 = twice_area * 0.5
    # Three symmetric barycentric samples per triangle give area-weighted estimates
    # without treating densely tessellated regions as more important.
    barycentric = np.asarray([[2 / 3, 1 / 6, 1 / 6], [1 / 6, 2 / 3, 1 / 6], [1 / 6, 1 / 6, 2 / 3]], dtype=float)
    sampled_vectors = np.einsum("sc,tcd->tsd", barycentric, vectors[indices])
    sampled_speeds = np.linalg.norm(sampled_vectors, axis=2).reshape(-1)
    weights = np.repeat(area_mm2 / 3, 3)
    total_area = float(area_mm2.sum())
    mean = float(np.average(sampled_speeds, weights=weights)) if total_area > AREA_EPS_MM2 else None
    return {
        "min_mps": float(point_speeds.min()) if len(point_speeds) else None,
        "mean_mps": mean,
        "p95_mps": _weighted_quantile(sampled_speeds, weights, 0.95),
        "max_mps": float(point_speeds.max()) if len(point_speeds) else None,
        "sample_count": int(len(sampled_speeds)),
        "method": "三角面内三点面积加权估计；U 由最终网格单元场插值至截面",
    }


def _analysis_signature(result: dict) -> str:
    return _digest({key: value for key, value in result.items() if key != "analysis_signature"})


def analyze_section(case_root: Path, position_fraction: float,
                    expected_source_signature: str | None = None,
                    expected_geometry_signature: str | None = None) -> dict:
    """Compute and persist whole-section flux and speed statistics from the final CFD field."""
    root = Path(case_root)
    foam, signature, time_name = _source(root)
    geometry, geometry_signature = _geometry(root)
    if expected_source_signature and expected_source_signature != signature:
        raise PipelineError("blocked", "截面位置对应的流场已更新，请重新读取后再计算")
    if expected_geometry_signature and expected_geometry_signature != geometry_signature:
        raise PipelineError("blocked", "截面位置对应的 CT 几何已更新，请重新读取后再计算")
    mesh = _read_mesh(root, foam, time_name)
    plane = _plane(mesh, geometry, position_fraction)
    cut_uv, normal_velocity, indices, section_mesh = _cut(mesh, plane, include_vectors=True)
    vectors = np.asarray(section_mesh.pop("velocity_vectors"), dtype=float)
    if vectors.shape != (len(cut_uv), 3) or not np.isfinite(vectors).all():
        raise PipelineError("failed", "截面速度向量缺失或无效，无法计算流速统计")
    integrated = _integrate(cut_uv, normal_velocity, indices, [], plane)
    velocity_summary = _section_velocity_statistics(cut_uv, vectors, indices)
    reasons = ["截面位置可交互调整；速度场由单元 U 插值至切面，网格敏感性和 CFD 工程复核仍需单独完成"]
    if integrated["totals"]["forward_lpm"] <= FLOW_EPS_LPM:
        reasons.append("截面正向通量接近零，无法作为有效正向流量观测")
    if integrated["totals"]["net_lpm"] <= FLOW_EPS_LPM:
        reasons.append("截面净通量未沿 CT 前→后方向流动，请检查工况和截面方向")
    result = {
        "status": "ready", "analysis_type": "whole_section", "version": SECTION_ANALYSIS_VERSION,
        "source_signature": signature, "geometry_signature": geometry_signature,
        "plane": plane, "section_mesh": section_mesh,
        "totals": integrated["totals"], "velocity": velocity_summary,
        "units": {"coordinates": "m", "flow": "L/min", "area": "mm2", "velocity": "m/s"},
        "validation": {"status": "pending", "reasons": reasons,
                       "boundary_reference": _reference(root, integrated["totals"], time_name),
                       "method": "cell_to_point_U; vtk_cut; triangle_area_weighted_velocity; exact_U_dot_n_flux",
                       "timestep": time_name},
        "inlet_condition": _inlet_condition(root),
    }
    result["analysis_signature"] = _analysis_signature(result)
    if _source(root)[1] != signature or _geometry(root)[1] != geometry_signature:
        raise PipelineError("blocked", "分析期间模型或场发生变化，结果未保存，请刷新重试")
    folder = root / "cfd" / "sections"
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / f".analysis-{secrets.token_hex(6)}.tmp"
    target = folder / "analysis.json"
    try:
        temporary.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
        if _source(root)[1] != signature or _geometry(root)[1] != geometry_signature:
            raise PipelineError("blocked", "保存前模型或场发生变化，结果未发布，请刷新重试")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return result


def _area(polygon: np.ndarray) -> float:
    if len(polygon) < 3:
        return 0.
    xy = polygon[:, :2] - polygon[0, :2]
    return abs(float(np.sum(xy[:, 0] * np.roll(xy[:, 1], -1) - xy[:, 1] * np.roll(xy[:, 0], -1)))) / 2


def _halfplane(polygon: np.ndarray, signed: np.ndarray, keep_positive=True) -> np.ndarray:
    """Clip vertices (uv plus interpolated scalar) by a linear half-plane."""
    if len(polygon) == 0:
        return polygon
    signed = signed if keep_positive else -signed
    output = []
    previous, previous_value = polygon[-1], signed[-1]
    for current, current_value in zip(polygon, signed):
        previous_in, current_in = previous_value >= 0, current_value >= 0
        if previous_in != current_in:
            weight = previous_value / (previous_value - current_value)
            output.append(previous + weight * (current - previous))
        if current_in:
            output.append(current)
        previous, previous_value = current, current_value
    return np.asarray(output, dtype=float).reshape(-1, polygon.shape[1])


def _edge_distance(polygon: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    edge = b - a
    delta = polygon[:, :2] - a
    return edge[0] * delta[:, 1] - edge[1] * delta[:, 0]


def _intersect(polygon: np.ndarray, boundary: np.ndarray) -> np.ndarray:
    result = polygon
    for a, b in zip(boundary, np.roll(boundary, -1, axis=0)):
        if len(result) < 3:
            return np.empty((0, polygon.shape[1]))
        result = _halfplane(result, _edge_distance(result, a, b))
    return result


def _subtract(polygon: np.ndarray, boundary: np.ndarray) -> list[np.ndarray]:
    """Disjoint convex pieces outside the convex boundary; no centroid labels."""
    inside, outside = polygon, []
    for a, b in zip(boundary, np.roll(boundary, -1, axis=0)):
        if len(inside) < 3:
            break
        distance = _edge_distance(inside, a, b)
        piece = _halfplane(inside, distance, False)
        if _area(piece) > AREA_EPS_MM2:
            outside.append(piece)
        inside = _halfplane(inside, distance)
    return outside


def _integral(polygon: np.ndarray) -> dict:
    def linear_integral(poly):
        if len(poly) < 3:
            return 0.
        return sum(_area(np.asarray([poly[0], poly[i], poly[i + 1]])) * float((poly[0, 2] + poly[i, 2] + poly[i + 1, 2]) / 3)
                   for i in range(1, len(poly) - 1))
    if len(polygon) < 3:
        return {"forward_lpm": 0., "reverse_lpm": 0., "net_lpm": 0., "area_mm2": 0.}
    positive = _halfplane(polygon, polygon[:, 2])
    negative = _halfplane(polygon, polygon[:, 2], False)
    # mm² × m/s × 1e-6 × 60000 = L/min.
    forward, reverse = linear_integral(positive) * .06, -linear_integral(negative) * .06
    return {"forward_lpm": max(0., forward), "reverse_lpm": max(0., reverse),
            "net_lpm": forward - reverse, "area_mm2": _area(polygon)}


def _regions(annotation: dict) -> list[dict]:
    source = annotation.get("regions")
    if not isinstance(source, list) or not 1 <= len(source) <= 6:
        raise PipelineError("failed", "需要标注 1 至 6 个上／中／下区域")
    regions = []
    for value in source:
        if not isinstance(value, dict):
            raise PipelineError("failed", "区域标注格式错误")
        identity, side, level = value.get("id"), value.get("side"), value.get("level")
        if identity not in REGION_IDS or identity != f"{side}-{level}" or identity in {r["id"] for r in regions}:
            raise PipelineError("failed", "区域 ID、左右或上中下标签无效或重复")
        try:
            polygon = np.asarray(value["polygon_uv_mm"], dtype=float)
        except (KeyError, TypeError, ValueError) as exc:
            raise PipelineError("failed", "区域边界坐标无效") from exc
        if (polygon.ndim != 2 or polygon.shape[1] != 2 or not 3 <= len(polygon) <= 64
                or not np.isfinite(polygon).all() or np.any(np.abs(polygon) > 1e4)
                or _area(polygon) <= AREA_EPS_MM2):
            raise PipelineError("failed", "区域需要 3 至 64 个有限坐标，且面积必须大于零")
        if np.allclose(polygon[0], polygon[-1], atol=1e-10, rtol=0):
            polygon = polygon[:-1]
        edges = np.roll(polygon, -1, axis=0) - polygon
        next_edges = np.roll(edges, -1, axis=0)
        turns = edges[:, 0] * next_edges[:, 1] - edges[:, 1] * next_edges[:, 0]
        if np.any(np.linalg.norm(edges, axis=1) <= 1e-9) or (np.any(turns > 1e-9) and np.any(turns < -1e-9)):
            raise PipelineError("failed", "当前支持凸多边形区域；请移除交叉、重复或凹角边界")
        signed_area = float(np.sum(polygon[:, 0] * np.roll(polygon[:, 1], -1) - polygon[:, 1] * np.roll(polygon[:, 0], -1)))
        if signed_area < 0:
            polygon = polygon[::-1]
        # All vertices must lie on the same inner side of every edge. This also
        # rejects star polygons whose local turn signs happen to agree.
        if any(np.any(_edge_distance(polygon, a, b) < -1e-8) for a, b in zip(polygon, np.roll(polygon, -1, axis=0))):
            raise PipelineError("failed", "区域边界不是简单凸多边形")
        label = value.get("label")
        if not isinstance(label, str) or not label.strip() or len(label) > 80:
            raise PipelineError("failed", "区域名称不能为空且不能超过 80 字")
        regions.append({"id": identity, "side": side, "level": level, "label": label.strip(), "polygon_uv_mm": polygon.tolist()})
    for index, first in enumerate(regions):
        for second in regions[index + 1:]:
            overlap = _intersect(np.asarray(first["polygon_uv_mm"]), np.asarray(second["polygon_uv_mm"]))
            if _area(overlap) > 1e-7:
                raise PipelineError("failed", "区域边界存在重叠，不能重复计算流量", detail={"regions": [first["id"], second["id"]]})
    return regions


def _mesh_piece(target: dict, polygon: np.ndarray, plane: dict):
    if len(polygon) < 3 or _area(polygon) <= AREA_EPS_MM2:
        return
    points = (np.asarray(plane["point_lps_mm"]) + polygon[:, :1] * plane["u_lps"] + polygon[:, 1:2] * plane["v_lps"]) * .001
    offset = len(target["positions"]) // 3
    target["positions"].extend(points.reshape(-1).tolist())
    for index in range(1, len(points) - 1):
        target["indices"].extend((offset, offset + index, offset + index + 1))


def _integrate(uv: np.ndarray, scalar: np.ndarray, indices: np.ndarray, regions: list[dict], plane: dict) -> dict:
    keys = ("forward_lpm", "reverse_lpm", "net_lpm", "area_mm2")
    zero = {key: 0. for key in keys}
    records = [{**{k: v for k, v in region.items() if k != "polygon_uv_mm"}, **zero} for region in regions]
    meshes = [{"id": region["id"], "label": region["label"], "positions": [], "indices": []} for region in regions]
    unassigned, total = {"id": "unassigned", "label": "未标注区域", **zero}, zero.copy()
    unassigned_mesh = {"id": "unassigned", "label": "未标注区域", "positions": [], "indices": []}
    boundaries = [np.asarray(region["polygon_uv_mm"]) for region in regions]
    bounds = [(poly.min(axis=0), poly.max(axis=0)) for poly in boundaries]
    for ids in indices:
        triangle = np.column_stack((uv[ids], scalar[ids]))
        values = _integral(triangle)
        for key in keys:
            total[key] += values[key]
        remaining = [triangle]
        tri_min, tri_max = triangle[:, :2].min(axis=0), triangle[:, :2].max(axis=0)
        for region, region_mesh, boundary, (lo, hi) in zip(records, meshes, boundaries, bounds):
            if np.any(tri_max < lo) or np.any(tri_min > hi):
                continue
            clipped = _intersect(triangle, boundary)
            if _area(clipped) > AREA_EPS_MM2:
                values = _integral(clipped)
                for key in keys:
                    region[key] += values[key]
                _mesh_piece(region_mesh, clipped, plane)
                remaining = [piece for polygon in remaining for piece in _subtract(polygon, boundary)]
        for polygon in remaining:
            values = _integral(polygon)
            for key in keys:
                unassigned[key] += values[key]
            _mesh_piece(unassigned_mesh, polygon, plane)
    denominator = total["forward_lpm"]
    coverage_complete = unassigned["area_mm2"] <= max(1e-7, total["area_mm2"] * 1e-8)
    sides = []
    for side in ("left", "right"):
        side_record = {"id": side, **{key: sum(r[key] for r in records if r["side"] == side) for key in keys}}
        side_record["percent_of_section"] = side_record["forward_lpm"] / denominator * 100 if denominator > FLOW_EPS_LPM else None
        side_record["coverage_status"] = "complete" if coverage_complete else "incomplete"
        sides.append(side_record)
        for region in records:
            if region["side"] == side:
                region["percent_of_side"] = region["forward_lpm"] / side_record["forward_lpm"] * 100 if coverage_complete and side_record["forward_lpm"] > FLOW_EPS_LPM else None
    for record in [*records, unassigned]:
        record["percent_of_section"] = record["forward_lpm"] / denominator * 100 if denominator > FLOW_EPS_LPM else None
    residual = {key: total[key] - unassigned[key] - sum(r[key] for r in records) for key in keys}
    if any(abs(residual[key]) > max(1e-7, abs(total[key]) * 1e-7) for key in keys):
        raise PipelineError("failed", "截面分区积分未保持总量，结果未保存", detail={"partition_residual": residual})
    return {"totals": total, "regions": records, "sides": sides, "unassigned": unassigned,
            "region_meshes": [*meshes, unassigned_mesh], "coverage_complete": coverage_complete,
            "partition_residual": residual}


def _reference(root: Path, total: dict, time_name: str) -> dict:
    fields = inspect_latest_fields(root / "cfd")
    phi = fields.get("fields", {}).get("phi", {})
    patch_flux = phi.get("patch_fluxes_m3s") or {}
    value = patch_flux.get("inlet") if phi.get("complete") else None
    source = f"{time_name}/phi:inlet" if value is not None else None
    if value is None:
        from .openfoam_runner import _read_postprocessing
        sampled = _read_postprocessing(root / "cfd", fields)
        if sampled.get("sample_times", {}).get("inlet_flow_m3s") == float(time_name):
            value, source = sampled.get("inlet_flow_m3s"), sampled.get("sources", {}).get("inlet_flow_m3s")
    if value is None or isinstance(value, bool) or not math.isfinite(value) or value >= -1e-12:
        return {"status": "unavailable", "reason": "没有当前时间步且流入方向正确的入口 phi 流量"}
    reference = -value * 60000
    return {"status": "compared", "inlet_net_lpm": reference, "source": source,
            "section_net_lpm": total["net_lpm"], "integration_discrepancy_percent": abs(total["net_lpm"] - reference) / reference * 100,
            "acceptance_status": "pending", "note": "这是截面 U 插值积分与入口 phi 的差异；需确认截面覆盖所有通路并验证误差，不能代替边界质量守恒检查"}


def _inlet_condition(root: Path) -> dict:
    path = root / "cfd" / "case_meta.json"
    meta = _json(path) if path.exists() else {}
    mode = meta.get("inlet_mode")
    if mode == "flowRate":
        explanation = "当前合并入口使用默认均匀法向流速；左右入口净流量按入口网格面积预设。截面分布描述当前工况，左右占比不能解释为两侧阻力造成的自然分配。"
    elif mode == "pressure":
        explanation = "当前使用压力入口工况，区域比例来自该工况的最终速度场。"
    else:
        explanation = "入口工况记录缺失或未识别，不能据左右占比解释鼻腔阻力差异。"
    return {"mode": mode, "description": explanation}


def compute_sections(case_root: Path, annotation: dict) -> dict:
    root = Path(case_root)
    if not isinstance(annotation, dict) or annotation.get("confirmed") is not True:
        raise PipelineError("blocked", "请先人工确认截面区域标注")
    foam, signature, time_name = _source(root)
    geometry, geometry_signature = _geometry(root)
    if annotation.get("source_signature") != signature or annotation.get("geometry_signature") != geometry_signature:
        raise PipelineError("blocked", "截面预览已过期，请刷新模型和截面后重新标注")
    supplied_plane = annotation.get("plane")
    if not isinstance(supplied_plane, dict):
        raise PipelineError("failed", "截面平面记录缺失")
    mesh = _read_mesh(root, foam, time_name)
    plane = _plane(mesh, geometry, supplied_plane.get("position_fraction"))
    try:
        supplied_vectors = {key: np.asarray(supplied_plane[key], dtype=float)
                            for key in ("point_lps_mm", "normal_lps", "u_lps", "v_lps")}
        plane_matches = all(vector.shape == (3,) and np.isfinite(vector).all()
                            and np.allclose(vector, plane[key], atol=1e-6, rtol=0)
                            for key, vector in supplied_vectors.items())
    except (KeyError, ValueError, TypeError) as exc:
        raise PipelineError("failed", "截面平面记录无效") from exc
    if not plane_matches:
        raise PipelineError("blocked", "截面坐标或方向与当前预览不一致，请重新预览")
    regions = _regions(annotation)
    normalized = {"plane": plane, "regions": regions, "confirmed": True, "source_signature": signature, "geometry_signature": geometry_signature}
    uv, scalar, indices, section_mesh = _cut(mesh, plane)
    integrated = _integrate(uv, scalar, indices, regions, plane)
    reasons = ["截面需经解剖位置与完整通路覆盖复核；网格敏感性和工程复核尚不能由本次后处理确认"]
    if not integrated["coverage_complete"]:
        reasons.append("存在未标注区域，每侧上／中／下比例暂不可用；已标注左右占比仅表示已覆盖区域")
    if integrated["totals"]["forward_lpm"] <= FLOW_EPS_LPM:
        reasons.append("截面正向通量接近零，无法计算占比")
    if integrated["totals"]["net_lpm"] <= FLOW_EPS_LPM:
        reasons.append("截面净通量未沿 CT 前→后方向流动，请检查工况和截面方向")
    result = {"status": "ready", "version": VERSION, "source_signature": signature, "geometry_signature": geometry_signature,
              "analysis_signature": _digest({"version": VERSION, "annotation": normalized}), "annotation": normalized,
              "plane": plane, "section_mesh": section_mesh, **integrated,
              "units": {"coordinates": "m", "uv": "mm", "flow": "L/min", "area": "mm2", "percentage": "%"},
              "percentage_basis": "forward_crossing_flux", "percentage_description": "占比按本截面正向穿越通量 Q+ 计算；回流 Q− 与净流量 Qnet 单列，不表示整个鼻腔体积比例。",
              "inlet_condition": _inlet_condition(root),
              "validation": {"status": "pending", "reasons": reasons, "coverage_status": "complete" if integrated["coverage_complete"] else "incomplete",
                             "boundary_reference": _reference(root, integrated["totals"], time_name),
                             "method": "cell_to_point_U; vtk_cut; exact_convex_clipping; linear_zero_contour_integration", "timestep": time_name}}
    if _source(root)[1] != signature or _geometry(root)[1] != geometry_signature:
        raise PipelineError("blocked", "分析期间模型或场发生变化，结果未保存，请刷新重试")
    folder = root / "cfd" / "sections"
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / f".analysis-{secrets.token_hex(6)}.tmp"
    try:
        temporary.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
        if _source(root)[1] != signature or _geometry(root)[1] != geometry_signature:
            raise PipelineError("blocked", "保存前模型或场发生变化，结果未发布，请刷新重试")
        temporary.replace(folder / "analysis.json")
    finally:
        temporary.unlink(missing_ok=True)
    return result


def get_sections(case_root: Path) -> dict:
    root = Path(case_root)
    path = root / "cfd" / "sections" / "analysis.json"
    if not path.exists():
        try:
            source, geometry = _source(root)[1], _geometry(root)[1]
        except (PipelineError, OSError, ValueError, TypeError) as exc:
            return {"status": "missing", "source_status": "unavailable", "reason": str(exc),
                    "current_source_signature": None, "current_geometry_signature": None}
        return {"status": "missing", "source_signature": source, "geometry_signature": geometry,
                "current_source_signature": source, "current_geometry_signature": geometry}
    try:
        result = _json(path)
        if result.get("analysis_type") == "whole_section":
            if (result.get("version") != SECTION_ANALYSIS_VERSION
                    or result.get("analysis_signature") != _analysis_signature(result)):
                return {"status": "error", "reason": "已保存的截面统计与数据签名不一致，请重新计算"}
        else:
            # Keep old six-region files readable for old reports. The new UI
            # detects the missing analysis_type and immediately replaces them
            # with a whole-section result after the user releases the plane.
            annotation = result.get("annotation")
            if (not isinstance(annotation, dict) or result.get("version") != VERSION
                    or result.get("analysis_signature") != _digest({"version": VERSION, "annotation": annotation})):
                return {"status": "error", "reason": "已保存的截面标注与分析签名不一致，请重新计算"}
        source, geometry = _source(root)[1], _geometry(root)[1]
    except (PipelineError, OSError, ValueError, TypeError) as exc:
        return {"status": "error", "reason": str(exc)}
    if source != result.get("source_signature") or geometry != result.get("geometry_signature"):
        return {"status": "stale", "stale": True, "reason": "求解场、网格、工况或 CT／掩膜版本变化，旧截面数值已停用",
                "position_fraction": (result.get("plane") or {}).get("position_fraction", 0.5),
                "current_source_signature": source, "current_geometry_signature": geometry,
                # Preserve only the old annotation metadata for draft migration;
                # numeric fields remain withheld until a fresh whole-section run.
                **({"annotation": result["annotation"]} if isinstance(result.get("annotation"), dict) else {})}
    return {**result, "current_source_signature": source, "current_geometry_signature": geometry}
