"""Read the three actual CFD caps as a versioned formal-port manifest.

Browser annotations are deliberately excluded: only the saved bilateral
local mesh and the same cap-index arrays consumed by CFD can produce ports.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

import numpy as np

from . import endplanes
from .cases import case_dir
from .errors import PipelineError
from .mask_review import has_current_external_air_validation


FORMAT = "airway_formal_ports_v1"
_CAPS = {
    "left": ("inlet_left_triangles.npy", "left_inlet_plane", "left_inlet_area_mm2"),
    "right": ("inlet_right_triangles.npy", "right_inlet_plane", "right_inlet_area_mm2"),
    "posterior": ("outlet_triangles.npy", "posterior_plane", "outlet_area_mm2"),
}


def _blocked(reason: str, action: str = "重新生成主鼻腔双侧局部 STL 与几何质检") -> None:
    raise PipelineError("blocked", reason, [action])


def _read_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PipelineError("blocked", f"{label}缺失或损坏", ["重新生成对应病例产物"]) from exc
    if not isinstance(value, dict):
        _blocked(f"{label}格式无效")
    return value


def _read_array(path: Path, label: str) -> np.ndarray:
    try:
        return np.load(path, allow_pickle=False, mmap_mode="r")
    except (OSError, ValueError) as exc:
        raise PipelineError("blocked", f"{label}缺失或损坏", ["重新生成主鼻腔 STL 后重试"]) from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_stl(path: Path, vertices: np.ndarray, faces: np.ndarray) -> str:
    """Ensure the displayed STL has exactly the coordinates CFD will load."""
    try:
        if path.stat().st_size != 84 + 50 * len(faces):
            _blocked("三维 STL 与 CFD 网格面数不一致")
        with path.open("rb") as handle:
            handle.seek(80)
            count = int.from_bytes(handle.read(4), "little")
        if count != len(faces):
            _blocked("三维 STL 与 CFD 网格面数不一致")
        dtype = np.dtype([
            ("normal", "<f4", (3,)), ("v0", "<f4", (3,)),
            ("v1", "<f4", (3,)), ("v2", "<f4", (3,)), ("attr", "<u2"),
        ])
        records = np.memmap(path, dtype=dtype, mode="r", offset=84, shape=(len(faces),))
        for start in range(0, len(faces), 8192):
            stop = min(start + 8192, len(faces))
            indices = np.asarray(faces[start:stop], dtype=np.int64)
            for axis, name in enumerate(("v0", "v1", "v2")):
                expected = np.asarray(vertices[indices[:, axis]], dtype=np.float32)
                if not np.array_equal(records[name][start:stop], expected):
                    _blocked("三维 STL 与 CFD 网格坐标不一致")
        del records
        return _sha256_file(path)
    except (OSError, ValueError) as exc:
        raise PipelineError("blocked", "三维 STL 缺失或无法读取", ["重新生成主鼻腔 STL"]) from exc


def _loop_indices(cap_faces: np.ndarray) -> list[int]:
    counts: Counter[tuple[int, int]] = Counter()
    for a, b, c in np.asarray(cap_faces, dtype=np.int64):
        for first, second in ((a, b), (b, c), (c, a)):
            first, second = int(first), int(second)
            if first == second:
                _blocked("封盖含退化三角面")
            counts[(min(first, second), max(first, second))] += 1
    if any(value > 2 for value in counts.values()):
        _blocked("封盖存在非流形边")
    boundary = [edge for edge, value in counts.items() if value == 1]
    neighbors: dict[int, set[int]] = defaultdict(set)
    for a, b in boundary:
        neighbors[a].add(b)
        neighbors[b].add(a)
    if len(boundary) < 3 or len(neighbors) != len(boundary) or any(len(items) != 2 for items in neighbors.values()):
        _blocked("封盖没有单一闭合边界环")
    start = min(neighbors)
    order = [start]
    previous = None
    current = start
    while True:
        choices = sorted(neighbors[current] - ({previous} if previous is not None else set()))
        if not choices or (previous is not None and len(choices) != 1):
            _blocked("封盖边界无法唯一遍历")
        nxt = choices[0]
        if nxt == start:
            break
        if nxt in order or len(order) >= len(neighbors):
            _blocked("封盖边界含额外环或交叉")
        order.append(nxt)
        previous, current = current, nxt
    if len(order) != len(neighbors):
        _blocked("封盖存在多个边界环")
    return order


def _port(case_id: str, scope: str, mask_version: int, mesh_hash: str,
          vertices: np.ndarray, faces: np.ndarray, ids: np.ndarray,
          side: str, plane: dict, recorded_area: object, geometry_version: str) -> dict:
    if ids.ndim != 1 or not len(ids) or ids.dtype.kind not in "iu":
        _blocked(f"{side}封盖面索引无效")
    if np.any(ids < 0) or np.any(ids >= len(faces)) or len(np.unique(ids)) != len(ids):
        _blocked(f"{side}封盖面索引越界或重复")
    triangles = np.asarray(faces[ids], dtype=np.int64)
    points = np.asarray(vertices[triangles], dtype=np.float64)
    crosses = np.cross(points[:, 1] - points[:, 0], points[:, 2] - points[:, 0])
    areas = 0.5 * np.linalg.norm(crosses, axis=1)
    if not np.all(np.isfinite(areas)) or np.any(areas <= 1e-10):
        _blocked(f"{side}封盖含零面积或非有限三角面")
    area = float(areas.sum())
    try:
        point = np.asarray(plane["point_lps_mm"], dtype=np.float64)
        normal = np.asarray(plane["normal_lps"], dtype=np.float64)
        expected_area = float(recorded_area)
    except (KeyError, TypeError, ValueError) as exc:
        raise PipelineError("blocked", f"{side}缺少正式端面或面积记录", ["重新生成双侧局部 STL"]) from exc
    if point.shape != (3,) or normal.shape != (3,) or not np.all(np.isfinite(point)) or not np.all(np.isfinite(normal)):
        _blocked(f"{side}端面位置或方向无效")
    normal_length = float(np.linalg.norm(normal))
    if normal_length < 1e-8:
        _blocked(f"{side}端面法向为零")
    normal /= normal_length
    combined = crosses.sum(axis=0)
    length = float(np.linalg.norm(combined))
    if length <= 1e-8 or float(np.dot(combined / length, normal)) < 0.95:
        _blocked(f"{side}封盖方向与端面不一致")
    if not np.isfinite(expected_area) or not np.isclose(area, expected_area, rtol=0.001, atol=0.02):
        _blocked(f"{side}封盖面积与裁切记录不一致")
    boundary = np.asarray(vertices[_loop_indices(triangles)], dtype=np.float64)
    if np.max(np.abs((boundary - point) @ normal)) > 0.001:
        _blocked(f"{side}封盖边界偏离端面")
    indices_hash = hashlib.sha256(np.asarray(ids, dtype="<i8").tobytes()).hexdigest()
    port_id = "port_" + hashlib.sha256(
        f"{case_id}:{scope}:{mask_version}:{mesh_hash}:{side}:{indices_hash}".encode("utf-8")
    ).hexdigest()
    return {
        "port_id": port_id,
        "side": side,
        "boundary_loop_lps_mm": boundary.tolist(),
        "cap_face_ids": [int(item) for item in ids],
        "cap_face_count": int(len(ids)),
        "cap_index_sha256": indices_hash,
        "area_mm2": round(area, 6),
        "normal_lps": normal.tolist(),
        "plane_point_lps_mm": point.tolist(),
        "provenance": {
            "source": "saved_bilateral_local_cap",
            "context_source": "cleaned_confirmed_mask",
            "endplane_format": endplanes.BILATERAL_LOCAL_FORMAT,
            "geometry_algorithm_version": geometry_version,
            "mask_version": mask_version,
            "mesh_hash": mesh_hash,
        },
    }


def build_formal_port_manifest(case_id: str, scope: str = "main_airway") -> dict:
    """Recompute from saved mesh, cap indices, endplanes and current mask."""
    if scope != "main_airway":
        _blocked("双侧正式端口仅支持主鼻腔气流模型", "切换到主鼻腔气流模型")
    root = case_dir(case_id)
    seg = _read_json(root / "segment" / "segmentation.json", "分割记录")
    plan = endplanes.load_plan(case_id, scope)
    meta = _read_json(root / "surface" / "surface.json", "表面记录")
    surface_plan = meta.get("endplanes")
    clip = meta.get("oblique_clip")
    if not isinstance(plan, dict) or plan.get("format") != endplanes.BILATERAL_LOCAL_FORMAT:
        _blocked("当前病例尚无正式双侧局部端面", "先预检并保存左右鼻孔及鼻咽端面")
    mask_version = seg.get("mask_version")
    if type(mask_version) is not int or plan.get("mask_version") != mask_version or meta.get("mask_version") != mask_version:
        _blocked("端口所用掩膜版本已过期", "基于当前掩膜重新预检、保存端面并生成 STL")
    if seg.get("need_human_confirm", True) or not has_current_external_air_validation(seg):
        _blocked("当前掩膜尚未确认体外空气清理，不能作为正式端口来源",
                 "核对当前掩膜及体外空气后重新生成 STL 与几何质检")
    params = meta.get("params")
    if not isinstance(params, dict) or params.get("model_scope") != scope:
        _blocked("端口与当前建模范围不一致")
    if not isinstance(surface_plan, dict) or any(surface_plan.get(key) != value for key, value in plan.items()):
        _blocked("STL 使用的端面方案与当前保存方案不一致")
    expected_source = (
        "automated_bilateral_local_oblique_planes"
        if plan.get("confirmation_source") == "automated_technical_gate"
        and plan.get("human_confirmed") is False
        else "user_confirmed_bilateral_local_oblique_planes"
    )
    if (plan.get("confirmation_source") is not None or plan.get("human_confirmed") is False):
        if not endplanes.automated_plan_is_valid(plan):
            _blocked("自动端面技术来源不完整", "重新生成并核对三端口")
    if surface_plan.get("source") != expected_source:
        _blocked("STL 缺少正式双侧局部裁切来源")
    if not isinstance(clip, dict) or clip.get("format") != "oblique_mesh_clip_bilateral_local_v3" or clip.get("closed_caps") is not True:
        _blocked("STL 未形成三个真实局部封盖")
    surface_root = root / "surface"
    vertices = _read_array(surface_root / "vertices.npy", "CFD 顶点网格")
    faces = _read_array(surface_root / "faces.npy", "CFD 三角面网格")
    if vertices.ndim != 2 or vertices.shape[1] != 3 or vertices.dtype.kind != "f" or not np.all(np.isfinite(vertices)):
        _blocked("CFD 顶点网格无效")
    if faces.ndim != 2 or faces.shape[1] != 3 or faces.dtype.kind not in "iu" or not len(faces):
        _blocked("CFD 三角面网格无效")
    if np.any(faces < 0) or np.any(faces >= len(vertices)):
        _blocked("CFD 三角面索引越界")
    mesh_hash = _verify_stl(surface_root / "airway.stl", vertices, faces)
    cap_arrays = {side: _read_array(surface_root / name, f"{side}封盖面索引")
                  for side, (name, _, _) in _CAPS.items()}
    if any(value.ndim != 1 or value.dtype.kind not in "iu" for value in cap_arrays.values()):
        _blocked("正式封盖面索引格式无效")
    combined_ids = np.concatenate([np.asarray(value) for value in cap_arrays.values()])
    if len(np.unique(combined_ids)) != len(combined_ids):
        _blocked("三个正式端口的封盖面重叠")
    version = str(meta.get("geometry_algorithm_version") or "unknown")
    if any(not isinstance(plan.get(plane_key), dict) for _, plane_key, _ in _CAPS.values()):
        _blocked("正式端面方案缺少一个或多个端口平面")
    ports = [_port(case_id, scope, mask_version, mesh_hash, vertices, faces,
                   cap_arrays[side], side, plan[plane_key], clip.get(area_key), version)
             for side, (_, plane_key, area_key) in _CAPS.items()]
    return {
        "format": FORMAT,
        "status": "formal_geometry_only",
        "case_id": case_id,
        "scope": scope,
        "mask_version": mask_version,
        "mesh_hash": mesh_hash,
        "context_source": "cleaned_confirmed_mask",
        "ports": ports,
        "quality_note": "端口来自正式裁切网格；CFD 和临床适用性仍须另行质检。",
    }


def get_formal_port_state(case_id: str, scope: str = "main_airway") -> dict:
    """Use not_generated only for genuinely missing work; reject stale/corrupt work."""
    if scope != "main_airway":
        _blocked("双侧正式端口仅支持主鼻腔气流模型", "切换到主鼻腔气流模型")
    root = case_dir(case_id)
    segmentation_path = root / "segment" / "segmentation.json"
    if not segmentation_path.exists():
        return {"status": "not_generated", "ports": [], "reason": "气道分割尚未完成",
                "scope": scope, "mask_version": None}
    seg = _read_json(segmentation_path, "分割记录")
    plan = endplanes.load_plan(case_id, scope)
    if plan is None:
        return {"status": "not_generated", "ports": [], "reason": "当前尚无已保存的双侧局部端面",
                "scope": scope, "mask_version": seg.get("mask_version")}
    if not isinstance(plan, dict) or plan.get("format") != endplanes.BILATERAL_LOCAL_FORMAT:
        _blocked("当前保存的是旧版或单支段端面，不是双侧正式端口",
                 "重新预检并保存左右鼻孔及鼻咽端面；旧结果不会被伪装成双侧 CFD")
    surface_root = root / "surface"
    if not (surface_root / "surface.json").exists():
        return {"status": "not_generated", "ports": [], "reason": "正式 STL 尚未生成",
                "scope": scope, "mask_version": seg.get("mask_version")}
    manifest = build_formal_port_manifest(case_id, scope)
    if not (surface_root / "geom_qc.json").exists():
        return {**manifest, "status": "not_generated", "ports": [], "reason": "几何质检尚未运行"}
    qc_path = surface_root / "geom_qc.json"
    source_files = [surface_root / name for name in (
        "surface.json", "airway.stl", "vertices.npy", "faces.npy",
        "inlet_left_triangles.npy", "inlet_right_triangles.npy", "outlet_triangles.npy",
    )]
    if qc_path.stat().st_mtime_ns < max(path.stat().st_mtime_ns for path in source_files):
        _blocked("几何质检早于当前正式网格，结果已过期", "重新运行几何质检")
    qc = _read_json(qc_path, "几何质检记录")
    bilateral_qc = qc.get("bilateral_local_caps")
    if not isinstance(bilateral_qc, dict):
        _blocked("双侧正式端口缺少对应的几何质检记录")
    if (qc.get("model_scope") != scope or qc.get("faces") != len(_read_array(surface_root / "faces.npy", "CFD 三角面网格"))
            or bilateral_qc.get("enabled") is not True
            or bilateral_qc.get("components_by_side") != {"left": 1, "right": 1, "outlet": 1}
            or qc.get("usable_openings_by_end") != {"anterior": 2, "posterior": 1, "middle": 0}
            or qc.get("verdict") != "pass" or qc.get("boundary_edges") != 0
            or qc.get("connected_components") != 1):
        _blocked("正式端口几何质检未通过或与当前网格不一致", "重新运行几何质检并核查三个开口")
    return {**manifest, "status": "ready", "quality_verdict": "pass"}


__all__ = ["FORMAT", "build_formal_port_manifest", "get_formal_port_state"]
