"""Read-only, in-memory mesh preflight for a draft local nasal inlet.

Use the same surface construction and local clipping functions as the saved
surface pipeline. This module never writes a case artifact or stage record.
"""

from __future__ import annotations

import struct

import numpy as np

from . import surface
from .errors import PipelineError


def build_local_inlet_mesh(mask, volume, ap_axis: int, scope: str, validation: dict):
    """Return the exact clipped mesh and its cap checks, without file writes."""
    with surface.local_geometry_guard():
        main_airway = scope == "main_airway"
        source_mask = surface.select_largest_component(mask)[0] if main_airway else mask
        surface.ensure_local_mask_capacity(source_mask)
        result = surface.mask_to_stl(
            mask, volume.spacing, volume.origin, volume.axis_dirs, ap_axis=ap_axis,
            params={
                "model_scope": scope, "keep_largest_component": main_airway,
                "extend_ap_edges": False,
            },
        )
        surface.ensure_local_mesh_face_capacity(result["faces_before_decimation"])
        vertices, faces, inlet, outlet, stats = surface.clip_mesh_by_oblique_planes_local(
            result["_vertices"], result["_faces"],
            validation["anterior_plane"], validation["posterior_plane"],
            source_mask, volume, anterior_seed_lps_mm=validation["anterior_seed_lps_mm"],
        )
        if (not stats.get("closed_caps") or stats.get("inlet_loops") != 1
                or len(inlet) == 0 or len(outlet) == 0 or len(faces) == 0):
            raise PipelineError(
                "blocked", "局部入口网格未形成单一鼻孔入口和水密出口",
                ["调整鼻孔端切面或锚点后重新预检"],
            )
        return vertices, faces, stats


def build_bilateral_local_mesh(mask, volume, ap_axis: int, scope: str, validation: dict):
    """Build the same three-cap full-resolution mesh used by saved surface."""
    from .bilateral_local import clip_mesh_bilateral_local

    if scope != "main_airway":
        raise PipelineError("blocked", "双侧局部端面仅支持主气道范围", ["切换到主气道范围"])
    with surface.local_geometry_guard():
        source_mask, _ = surface.select_largest_component(mask)
        surface.ensure_local_mask_capacity(source_mask)
        result = surface.mask_to_stl(
            mask, volume.spacing, volume.origin, volume.axis_dirs, ap_axis=ap_axis,
            params={
                "model_scope": scope, "keep_largest_component": True,
                "extend_ap_edges": False,
            },
        )
        surface.ensure_local_mesh_face_capacity(result["faces_before_decimation"])
        vertices, faces, left, right, outlet, stats = clip_mesh_bilateral_local(
            result["_vertices"], result["_faces"], source_mask, volume,
            left_inlet_plane=validation["left_inlet_plane"],
            right_inlet_plane=validation["right_inlet_plane"],
            posterior_plane=validation["posterior_plane"],
            left_inlet_seed_lps_mm=validation["left_inlet_seed_lps_mm"],
            right_inlet_seed_lps_mm=validation["right_inlet_seed_lps_mm"],
            posterior_seed_lps_mm=validation["posterior_seed_lps_mm"],
        )
        if (not stats["closed_caps"] or any(len(indices) == 0 for indices in (left, right, outlet))
                or len(faces) == 0):
            raise PipelineError("blocked", "双侧端面预览未形成三个水密封盖", ["调整局部切面后重试"])
        stats["source_face_cleanup"] = result["coincident_face_cleanup"]
        return vertices, faces, stats


def binary_stl(vertices, faces) -> bytes:
    """Serialize a verified in-memory mesh in the standard binary STL layout."""
    points = np.asarray(vertices, dtype=np.float64)
    triangles = np.asarray(faces, dtype=np.int32)
    if (points.ndim != 2 or points.shape[1] != 3 or triangles.ndim != 2
            or triangles.shape[1] != 3 or len(triangles) == 0
            or np.any(triangles < 0) or np.any(triangles >= len(points))
            or not np.isfinite(points).all()):
        raise PipelineError("failed", "预览网格数据无效", ["重新预检局部入口切面"])
    v0, v1, v2 = (points[triangles[:, index]] for index in range(3))
    normals = np.cross(v1 - v0, v2 - v0)
    lengths = np.linalg.norm(normals, axis=1)
    lengths[lengths == 0] = 1.0
    normals /= lengths[:, None]
    records = np.zeros(len(triangles), dtype=np.dtype([
        ("normal", "<f4", (3,)), ("v0", "<f4", (3,)),
        ("v1", "<f4", (3,)), ("v2", "<f4", (3,)), ("attr", "<u2"),
    ]))
    records["normal"] = normals
    records["v0"] = v0
    records["v1"] = v1
    records["v2"] = v2
    header = b"Windcatcher local inlet preview".ljust(80, b"\0")
    return header + struct.pack("<I", len(triangles)) + records.tobytes()
