"""生成 OpenFOAM 算例（网格 + 边界条件 + 求解控制），不运行求解器。

前置闸门：几何质检必须给出"鼻孔端 + 鼻咽端开口"，否则不生成算例。
入口面积由封盖三角面求和得到，平均速度 U = Q / A 是算出来的，不是填出来的。
模板按 OpenFOAM 分支（org / esi）生成，本机未安装 OpenFOAM，故标记为"未验证"。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy import ndimage

from .config import MIN_OPENING_AREA_MM2, SOP_DEFAULTS, resolve_parallel_plan
from .errors import PipelineError
from .volume import ANTERIOR_LPS, load_volume

def _openings_by_end(geom_qc: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """按端分组可用开口，并把离散化毛刺排除在边界条件之外。"""
    anterior, posterior, tiny = [], [], []
    for item in geom_qc.get("openings", []):
        is_tiny = item.get("area_mm2", 0) < MIN_OPENING_AREA_MM2
        if is_tiny:
            tiny.append(item)
            continue
        if item.get("end") == "anterior":
            anterior.append(item)
        elif item.get("end") == "posterior":
            posterior.append(item)
    return anterior, posterior, tiny


def _triangulate_loop(points: np.ndarray, outward: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """把开口环三角化：投影到最佳拟合平面后以质心做扇形三角化。

    凹形环用扇形三角化会略有出入，因此在结果里标注需人工/桌面端复核。
    连续重复点产生的退化三角形会被剔除。
    """
    centroid = points.mean(axis=0)
    centered = points - centroid
    _, _, vh = np.linalg.svd(centered, full_matrices=False)
    normal = vh[-1]
    if float(np.dot(normal, outward)) < 0:
        normal = -normal
    triangles = []
    for i in range(len(points)):
        a = centroid
        b = points[i]
        c = points[(i + 1) % len(points)]
        if np.linalg.norm(b - c) < 1e-9 or np.linalg.norm(a - b) < 1e-9 or np.linalg.norm(a - c) < 1e-9:
            continue  # 连续重复点 → 退化三角形，剔除
        triangles.append([a, b, c])
    return np.asarray(triangles, dtype=float), normal


def _clean_shell(regions: dict[str, np.ndarray]) -> tuple[dict[str, np.ndarray], dict]:
    """清洗封闭壳：焊接重复顶点、剔除退化面、按顶点集合去重、BFS 统一朝向。

    snappyHexMesh 的 inside 细化和 patch 分配都要求壳闭合、法向一致；
    这里的清洗统计会写进 case_meta.json 留痕。
    """
    names: list[str] = []
    flats: list[np.ndarray] = []
    for name, tris in regions.items():
        if len(tris) == 0:
            continue
        flats.append(np.asarray(tris, dtype=float).reshape(-1, 3))
        names.extend([name] * len(tris))
    if not flats:
        return {k: np.zeros((0, 3, 3)) for k in regions}, {"error": "没有三角形"}

    points = np.vstack(flats)
    keys = np.round(points, 6)  # 1e-6 mm 精度焊接
    uniq, inverse = np.unique(keys, axis=0, return_inverse=True)
    faces = inverse.reshape(-1, 3)
    names = np.asarray(names)

    # 剔除退化面：重复顶点或零面积
    tri_pts = uniq[faces]
    area = 0.5 * np.linalg.norm(
        np.cross(tri_pts[:, 1] - tri_pts[:, 0], tri_pts[:, 2] - tri_pts[:, 0]), axis=1
    )
    keep = (
        (faces[:, 0] != faces[:, 1])
        & (faces[:, 1] != faces[:, 2])
        & (faces[:, 0] != faces[:, 2])
        & (area > 1e-9)
    )
    dropped_degenerate = int((~keep).sum())
    faces, names = faces[keep], names[keep]

    # 按顶点集合去重（封盖与壁面在焊缝处的重复三角形）
    seen: set[tuple[int, int, int]] = set()
    keep2 = np.zeros(len(faces), dtype=bool)
    for i, face in enumerate(faces):
        key = tuple(sorted(face.tolist()))
        if key in seen:
            continue
        seen.add(key)
        keep2[i] = True
    dropped_duplicate = int((~keep2).sum())
    faces, names = faces[keep2], names[keep2]

    # BFS 统一朝向：共享边必须方向相反
    from collections import defaultdict

    und: dict[frozenset, list[int]] = defaultdict(list)
    for i, face in enumerate(faces):
        for a, b in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0])):
            und[frozenset((int(a), int(b)))].append(i)

    def directed(face, flipped):
        f = face[::-1] if flipped else face
        return ((int(f[0]), int(f[1])), (int(f[1]), int(f[2])), (int(f[2]), int(f[0])))

    flip: list[bool | None] = [None] * len(faces)
    flip[0] = False
    stack = [0]
    while stack:
        i = stack.pop()
        for a, b in directed(faces[i], flip[i]):
            for j in und[frozenset((a, b))]:
                if j == i or flip[j] is not None:
                    continue
                dirs_j = directed(faces[j], False)
                if (a, b) in dirs_j:
                    flip[j] = True  # 同向 → 翻转邻居
                else:
                    flip[j] = False
                stack.append(j)
    for i in range(len(flip)):
        if flip[i] is None:
            flip[i] = False

    oriented = np.asarray([f[::-1] if flip[i] else f for i, f in enumerate(faces)])

    # 有符号体积定全局外向
    v0, v1, v2 = uniq[oriented[:, 0]], uniq[oriented[:, 1]], uniq[oriented[:, 2]]
    volume = float(np.sum(np.einsum("ij,ij->i", v0, np.cross(v1, v2))) / 6.0)
    if volume < 0:
        oriented = oriented[:, ::-1]
        volume = -volume

    # 闭合性统计
    edge_count: dict[frozenset, int] = defaultdict(int)
    for face in oriented:
        for a, b in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0])):
            edge_count[frozenset((int(a), int(b)))] += 1
    open_edges = sum(1 for c in edge_count.values() if c == 1)
    nonmanifold_edges = sum(1 for c in edge_count.values() if c > 2)

    cleaned: dict[str, np.ndarray] = {}
    for name in regions:
        mask = names == name
        cleaned[name] = np.stack(
            [uniq[oriented[mask][:, 0]], uniq[oriented[mask][:, 1]], uniq[oriented[mask][:, 2]]], axis=1
        ) if mask.any() else np.zeros((0, 3, 3))

    stats = {
        "triangles_in": int(len(points) // 3),
        "triangles_out": int(len(oriented)),
        "dropped_degenerate": dropped_degenerate,
        "dropped_duplicate": dropped_duplicate,
        "vertices_welded": int(len(uniq)),
        "open_edges": open_edges,
        "nonmanifold_edges": nonmanifold_edges,
        "signed_volume_mm3": round(volume, 2),
        "closed": open_edges == 0 and nonmanifold_edges == 0,
    }
    return cleaned, stats


def _write_ascii_stl(path: Path, solid_name: str, triangles: np.ndarray) -> None:
    """写单实体 ASCII STL（LF 换行、米制坐标）。

    Windows 的 CRLF 会污染实体名，必须 LF；坐标从患者毫米换算成米，
    与 blockMesh（convertToMeters 0.001）一致。
    """
    lines = [f"solid {solid_name}"]
    for tri in np.asarray(triangles, dtype=float) / 1000.0:
        v0, v1, v2 = tri
        normal = np.cross(v1 - v0, v2 - v0)
        length = np.linalg.norm(normal)
        normal = normal / length if length else np.zeros(3)
        lines.append(f" facet normal {normal[0]:.6g} {normal[1]:.6g} {normal[2]:.6g}")
        lines.append("  outer loop")
        for vertex in (v0, v1, v2):
            lines.append(f"   vertex {vertex[0]:.9g} {vertex[1]:.9g} {vertex[2]:.9g}")
        lines.append("  endloop")
        lines.append(" endfacet")
    lines.append(f"endsolid {solid_name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="ascii", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def _location_in_mesh(mask: np.ndarray, spacing, origin, axis_dirs) -> np.ndarray:
    """取掩膜内部最大内切点，换算到米（snappyHexMesh 的 locationInMesh）。"""
    distance = ndimage.distance_transform_edt(mask.astype(bool), sampling=[float(s) for s in spacing])
    idx = np.unravel_index(int(np.argmax(distance)), distance.shape)
    dirs = np.asarray(axis_dirs, dtype=float)
    spacing_arr = np.asarray(spacing, dtype=float)
    point_mm = np.asarray(origin, dtype=float) + sum(
        float(idx[a]) * spacing_arr[a] * dirs[a] for a in range(3)
    )
    return point_mm / 1000.0


def _blockmesh_dict(min_xyz, max_xyz, cell_mm: float) -> str:
    size = max_xyz - min_xyz
    nx = max(int(np.ceil(size[0] / cell_mm)), 1)
    ny = max(int(np.ceil(size[1] / cell_mm)), 1)
    nz = max(int(np.ceil(size[2] / cell_mm)), 1)
    return f"""/* 由风见生成：背景六面体网格，单元尺寸 {cell_mm} mm */
FoamFile
{{
    version 2.0;
    format ascii;
    class dictionary;
    object blockMeshDict;
}}
convertToMeters 0.001;

vertices
(
    ({min_xyz[0]:.4f} {min_xyz[1]:.4f} {min_xyz[2]:.4f})
    ({max_xyz[0]:.4f} {min_xyz[1]:.4f} {min_xyz[2]:.4f})
    ({max_xyz[0]:.4f} {max_xyz[1]:.4f} {min_xyz[2]:.4f})
    ({min_xyz[0]:.4f} {max_xyz[1]:.4f} {min_xyz[2]:.4f})
    ({min_xyz[0]:.4f} {min_xyz[1]:.4f} {max_xyz[2]:.4f})
    ({max_xyz[0]:.4f} {min_xyz[1]:.4f} {max_xyz[2]:.4f})
    ({max_xyz[0]:.4f} {max_xyz[1]:.4f} {max_xyz[2]:.4f})
    ({min_xyz[0]:.4f} {max_xyz[1]:.4f} {max_xyz[2]:.4f})
);

blocks
(
    hex (0 1 2 3 4 5 6 7) ({nx} {ny} {nz}) simpleGrading (1 1 1)
);

edges ();

boundary
(
    background
    {{
        type patch;
        faces
        (
            (0 1 2 3) (4 5 6 7) (0 1 5 4) (2 3 7 6) (1 2 6 5) (0 3 7 4)
        );
    }}
);
"""


def _snappy_dict(location_m, cell_mm: float, layers: bool) -> str:
    layer_block = ""
    if layers:
        layer_block = """
    addLayers
    {
        relativeSizes true;
        layers { wall { nSurfaceLayers 3; } }
        expansionRatio 1.2;
        finalLayerThickness 0.5;
        minThickness 0.1;
        nGrow 0;
        featureAngle 60;
        nRelaxIter 3;
        nSmoothSurfaceNormals 1;
        nSmoothThickness 5;
        maxFaceThicknessRatio 0.5;
        maxThicknessToMedialRatio 0.3;
        minMedianAxisAngle 90;
        nBufferCellsNoExtrude 0;
        nLayerIter 50;
    }
"""
    level = 2 if cell_mm >= 0.8 else 3
    return f"""/* 由风见生成：snappyHexMesh 设置 */
FoamFile
{{
    version 2.0;
    format ascii;
    class dictionary;
    object snappyHexMeshDict;
}}

castellatedMesh true;
snap true;
addLayers {'true' if layers else 'false'};
mergeTolerance 1e-6;

geometry
{{
    wall.stl
    {{
        type triSurfaceMesh;
        name wall;
    }}
    inlet.stl
    {{
        type triSurfaceMesh;
        name inlet;
    }}
    outlet.stl
    {{
        type triSurfaceMesh;
        name outlet;
    }}
    nasal_cfd.stl
    {{
        type triSurfaceMesh;
        name airwayShell;
    }}
}};

castellatedMeshControls
{{
    maxLocalCells 1000000;
    maxGlobalCells 3000000;
    minRefinementCells 10;
    maxLoadUnbalance 0.10;
    nCellsBetweenLevels 3;
    features ();
    refinementSurfaces
    {{
        wall   {{ level ({level} {level + 1}); patchInfo {{ type wall; }} }}
        inlet  {{ level ({level} {level + 1}); patchInfo {{ type patch; }} }}
        outlet {{ level ({level} {level + 1}); patchInfo {{ type patch; }} }}
    }}
    resolveFeatureAngle 30;
    refinementRegions
    {{
        airwayShell {{ mode inside; levels ((0.001 {level})); }}
    }}
    locationInMesh ({location_m[0]:.6f} {location_m[1]:.6f} {location_m[2]:.6f});
    allowFreeStandingZoneFaces true;
}}

snapControls
{{
    nSmoothPatch 3;
    tolerance 2.0;
    nSolveIter 30;
    nRelaxIter 5;
    nFeatureSnapIter 10;
    implicitFeatureSnap false;
    explicitFeatureSnap true;
    multiRegionFeatureSnap false;
}}

meshQualityControls
{{
    maxNonOrtho 65;
    maxBoundarySkewness 20;
    maxInternalSkewness 4;
    maxConcave 80;
    minVol 1e-13;
    minTetQuality 1e-15;
    minArea -1;
    minTwist 0.02;
    minDeterminant 0.001;
    minFaceWeight 0.02;
    minVolRatio 0.01;
    minTriangleTwist -1;
    nSmoothScale 4;
    errorReduction 0.75;
}}

mergePatchPairs ();
{layer_block}
"""


HEADER = """FoamFile
{{
    version 2.0;
    format ascii;
    class {cls};
    object {name};
}}
"""


def _write(path: Path, cls: str, name: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER.format(cls=cls, name=name) + "\n" + body, encoding="utf-8")


def prepare_case(case_id: str, params: dict | None = None) -> dict:
    """生成 OpenFOAM 算例目录，返回工况与产物清单。"""
    from .cases import case_dir, get_case, update_stage

    p = dict(SOP_DEFAULTS)
    p.update(
        {
            "openfoam_flavour": "org",        # org=OpenFOAM.org(v11) / esi=OpenFOAM ESI(v2312)
            "turbulence_model": "laminar",    # SOP：入门先层流，定量分析再 kOmegaSST
            "inlet_mode": "flowRate",         # flowRate / pressure
            "inlet_pressure_pa": 18.0,        # 吸气：入口相对出口为正压差；出口参考压力为 0
            "add_layers": False,
        }
    )
    if params:
        p.update({k: v for k, v in params.items() if v is not None})
    try:
        parallel_plan = resolve_parallel_plan({
            "profile": p.get("parallel_profile", p.get("parallel", {}).get("profile") if isinstance(p.get("parallel"), dict) else None),
            "custom_subdomains": p.get("custom_subdomains", p.get("parallel", {}).get("custom_subdomains") if isinstance(p.get("parallel"), dict) else None),
        })
        mpi_subdomains = parallel_plan["requested_subdomains"]
    except ValueError as exc:
        raise PipelineError("blocked", str(exc), ["选择有效的计算资源模式或 MPI 进程数后重新生成算例"]) from exc
    try:
        rho = float(p["air_rho"])
        inlet_pressure_pa = float(p["inlet_pressure_pa"])
    except (TypeError, ValueError) as exc:
        raise PipelineError("failed", "空气密度与入口压力必须是有限数值") from exc
    if not np.isfinite(rho) or rho <= 0 or not np.isfinite(inlet_pressure_pa):
        raise PipelineError("failed", "空气密度必须大于 0，入口压力必须为有限数值")
    if p["inlet_mode"] == "pressure" and inlet_pressure_pa <= 0:
        raise PipelineError("blocked", "吸气算例的入口压力必须高于出口参考压力 0 Pa", ["使用正的入口与出口压差，避免将吸气入口变成流出边界"])

    root = case_dir(case_id)
    geom_path = root / "surface" / "geom_qc.json"
    if not geom_path.exists():
        raise PipelineError("blocked", "还没有几何质检结果", ["先执行表面生成与几何质检"], {"stage": "surface"})
    geom_qc = json.loads(geom_path.read_text(encoding="utf-8"))
    surface_meta_path = root / "surface" / "surface.json"
    surface_meta = {}
    if surface_meta_path.exists():
        try:
            surface_meta = json.loads(surface_meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            surface_meta = {}
    seg_path = root / "segment" / "segmentation.json"
    seg = json.loads(seg_path.read_text(encoding="utf-8")) if seg_path.exists() else {}
    endplane_meta = None
    if not get_case(case_id).get("synthetic", False):
        endplane_meta = surface_meta.get("endplanes")
        if not isinstance(endplane_meta, dict) or endplane_meta.get("source") not in {
            "user_confirmed_ct_planes", "user_confirmed_oblique_planes",
        }:
            raise PipelineError(
                "blocked",
                "真实病例缺少已确认的 CT 端面或 3D 端面，不能生成 CFD 算例",
                ["回到 3D 视窗选择鼻孔端与鼻咽端裁切面", "保存端面后重新生成 STL 与几何质检"],
                {"stage": "surface", "endplane_source": endplane_meta.get("source") if isinstance(endplane_meta, dict) else None},
            )
        current_mask_version = seg.get("mask_version")
        if (
            current_mask_version is None
            or surface_meta.get("mask_version") != current_mask_version
            or endplane_meta.get("mask_version") != current_mask_version
        ):
            raise PipelineError(
                "blocked",
                "CT 端面或 STL 的掩膜版本已过期，不能生成 CFD 算例",
                ["回到 CT 复核视窗重新确认端面", "使用当前掩膜重新生成 STL 与几何质检"],
                {
                    "stage": "surface",
                    "current_mask_version": current_mask_version,
                    "surface_mask_version": surface_meta.get("mask_version"),
                    "endplane_mask_version": endplane_meta.get("mask_version"),
                },
            )
    model_scope = str(
        geom_qc.get("model_scope")
        or surface_meta.get("params", {}).get("model_scope", "main_airway")
    )
    if model_scope == "full_candidate" and int(geom_qc.get("connected_components", 1)) > 1:
        raise PipelineError(
            "blocked",
            "全候选模型包含断开的表面组件，不能直接生成 CFD 算例",
            [
                "在三平面/3D 视图中确认断开区域是否为真实鼻窦或旁支",
                "若要进行主鼻腔 CFD，请切换为主气道范围后重新生成 STL",
                "若要保留鼻窦，请先确认窦口连通并在 Blender/SimScale 中修复几何",
            ],
            {
                "stage": "surface",
                "model_scope": model_scope,
                "connected_components": int(geom_qc.get("connected_components", 0)),
                "reasons": geom_qc.get("reasons", []),
            },
        )
    if geom_qc.get("verdict") == "fail":
        raise PipelineError(
            "blocked",
            "几何质检未通过，不生成 CFD 算例",
            geom_qc.get("reasons", []) + ["修复几何后重跑 surface 与几何质检"],
            {"verdict": geom_qc.get("verdict")},
        )

    if seg.get("need_human_confirm", True):
        raise PipelineError(
            "blocked",
            "气道掩膜尚未人工确认，不能生成 CFD 算例",
            ["先在三平面 CT 中确认气道范围", "确认后重新生成 STL 与几何质检"],
            {"stage": "segment", "human_review": seg.get("human_review", {})},
        )
    review_flags = seg.get("review_flags", {})
    mask_name = "candidate_mask.npy" if model_scope == "full_candidate" else "mask.npy"
    mask_for_review = root / "segment" / mask_name
    if (not review_flags or model_scope == "full_candidate") and mask_for_review.exists():
        mask_probe = np.load(mask_for_review, mmap_mode="r")
        ap_axis = int(seg.get("params", {}).get("ap_axis", 1))
        boundary_by_axis = [
            bool(mask_probe[0].any() or mask_probe[-1].any()),
            bool(mask_probe[:, 0, :].any() or mask_probe[:, -1, :].any()),
            bool(mask_probe[:, :, 0].any() or mask_probe[:, :, -1].any()),
        ]
        review_flags = {
            "mask_boundary_by_axis": boundary_by_axis,
            "external_air_suspected": any(
                touched for axis, touched in enumerate(boundary_by_axis) if axis != ap_axis
            ),
        }
    if review_flags.get("external_air_suspected"):
        raise PipelineError(
            "blocked",
            "候选掩膜疑似包含体外空气，不能生成 CFD 算例",
            [
                "先在 CT 复核视窗逐层确认并修正气道掩膜",
                "确认后重新执行气道分割、STL 与几何质检",
            ],
            {"stage": "segment", "review_flags": review_flags},
        )

    anterior, posterior, skipped = _openings_by_end(geom_qc)
    if not anterior or not posterior:
        raise PipelineError(
            "blocked",
            f"缺少可用开口（鼻孔端 {len(anterior)} 个 / 鼻咽端 {len(posterior)} 个），无法定义入口与出口",
            [
                "检查鼻孔/鼻咽端面是否被封死（SOP FAQ Q2）",
                "检查是否存在鼻窦旁路导致中段开口",
                "必要时在 Blender / Slicer 中人工修复后重跑",
            ],
            {"openings_by_end": geom_qc.get("openings_by_end")},
        )

    vertices = np.load(root / "surface" / "vertices.npy")
    faces = np.load(root / "surface" / "faces.npy")
    oblique_caps = endplane_meta.get("source") == "user_confirmed_oblique_planes" if isinstance(endplane_meta, dict) else False
    inlet_cap_indices = outlet_cap_indices = np.zeros((0,), dtype=np.int32)
    if oblique_caps:
        try:
            inlet_cap_indices = np.asarray(np.load(root / "surface" / "inlet_triangles.npy"), dtype=np.int32).reshape(-1)
            outlet_cap_indices = np.asarray(np.load(root / "surface" / "outlet_triangles.npy"), dtype=np.int32).reshape(-1)
        except (OSError, ValueError) as exc:
            raise PipelineError("blocked", "3D 端面封盖记录缺失", ["重新生成 STL 与几何质检后再生成 CFD 算例"]) from exc
        all_cap_indices = np.concatenate([inlet_cap_indices, outlet_cap_indices])
        if (
            len(inlet_cap_indices) == 0 or len(outlet_cap_indices) == 0
            or np.any(all_cap_indices < 0) or np.any(all_cap_indices >= len(faces))
        ):
            raise PipelineError("blocked", "3D 端面没有形成完整入口/出口封盖", ["调整两张切面后重新生成 STL 与几何质检"])
        cap_set = set(int(value) for value in all_cap_indices)
        wall_indices = np.asarray([index for index in range(len(faces)) if index not in cap_set], dtype=np.int32)
        wall_tris = vertices[faces[wall_indices]]
        inlet_tris = vertices[faces[inlet_cap_indices]]
        outlet_tris = vertices[faces[outlet_cap_indices]]
    else:
        wall_tris = np.stack([vertices[faces[:, 0]], vertices[faces[:, 1]], vertices[faces[:, 2]]], axis=1)

    inlet_area_mm2 = 0.0
    outlet_area_mm2 = 0.0
    if not oblique_caps:
        anterior_dir = np.asarray(ANTERIOR_LPS, dtype=float)
        inlet_parts, outlet_parts = [], []
        for item in anterior:
            pts = np.asarray(item["points"], dtype=float)
            tris, _ = _triangulate_loop(pts, anterior_dir)
            inlet_parts.append(tris)
            inlet_area_mm2 += float(item["area_mm2"])
        for item in posterior:
            pts = np.asarray(item["points"], dtype=float)
            tris, _ = _triangulate_loop(pts, -anterior_dir)
            outlet_parts.append(tris)
            outlet_area_mm2 += float(item["area_mm2"])
        inlet_tris = np.concatenate(inlet_parts, axis=0) if inlet_parts else np.zeros((0, 3, 3))
        outlet_tris = np.concatenate(outlet_parts, axis=0) if outlet_parts else np.zeros((0, 3, 3))
    else:
        inlet_area_mm2 = float(np.sum(0.5 * np.linalg.norm(np.cross(inlet_tris[:, 1] - inlet_tris[:, 0], inlet_tris[:, 2] - inlet_tris[:, 0]), axis=1)))
        outlet_area_mm2 = float(np.sum(0.5 * np.linalg.norm(np.cross(outlet_tris[:, 1] - outlet_tris[:, 0], outlet_tris[:, 2] - outlet_tris[:, 0]), axis=1)))

    # 清洗封闭壳（焊接/去退化/去重/统一朝向），再交给 STL 写出
    cleaned, clean_stats = _clean_shell(
        {"wall": wall_tris, "inlet": inlet_tris, "outlet": outlet_tris}
    )
    wall_tris = cleaned["wall"]
    inlet_tris = cleaned["inlet"]
    outlet_tris = cleaned["outlet"]

    stl_dir = root / "cfd" / "constant" / "triSurface"
    _write_ascii_stl(stl_dir / "wall.stl", "wall", wall_tris)
    _write_ascii_stl(stl_dir / "inlet.stl", "inlet", inlet_tris)
    _write_ascii_stl(stl_dir / "outlet.stl", "outlet", outlet_tris)
    merged = np.concatenate(
        [wall_tris, inlet_tris, outlet_tris], axis=0
    ) if len(outlet_tris) else np.concatenate([wall_tris, inlet_tris], axis=0)
    _write_ascii_stl(stl_dir / "nasal_cfd.stl", "airwayShell", merged)

    # 工况计算：Q（m³/s）→ 平均速度 U = Q / A
    flow_lpm = float(p["total_flow_lpm"])
    flow_m3s = flow_lpm / 60000.0
    inlet_area_m2 = inlet_area_mm2 * 1e-6
    outlet_area_m2 = outlet_area_mm2 * 1e-6
    if inlet_area_m2 <= 0:
        raise PipelineError("failed", "入口面积为 0，无法计算入口速度", ["检查鼻孔端开口是否正确保留"])
    mean_velocity = flow_m3s / inlet_area_m2

    # locationInMesh：掩膜内部最大内切点（米）
    volume = load_volume(case_id)
    effective_mask_path = root / "surface" / "effective_mask.npy"
    mask = np.load(effective_mask_path if effective_mask_path.exists() else root / "segment" / mask_name)
    location_m = _location_in_mesh(mask, volume.spacing, volume.origin, volume.axis_dirs)

    all_points = np.concatenate(
        [wall_tris.reshape(-1, 3), inlet_tris.reshape(-1, 3), outlet_tris.reshape(-1, 3)]
    )
    margin = float(p["cfd_margin_mm"])
    min_xyz = all_points.min(axis=0) - margin
    max_xyz = all_points.max(axis=0) + margin

    cfd_root = root / "cfd"
    (cfd_root / "0").mkdir(parents=True, exist_ok=True)
    (cfd_root / "system").mkdir(parents=True, exist_ok=True)
    (cfd_root / "constant").mkdir(parents=True, exist_ok=True)

    (cfd_root / "system" / "blockMeshDict").write_text(
        _blockmesh_dict(min_xyz, max_xyz, float(p["background_cell_mm"])), encoding="utf-8"
    )
    (cfd_root / "system" / "snappyHexMeshDict").write_text(
        _snappy_dict(location_m, float(p["background_cell_mm"]), bool(p["add_layers"])), encoding="utf-8"
    )

    inlet_u = (
        f"""    inlet
    {{
        type            flowRateInletVelocity;
        volumetricFlowRate {flow_m3s:.8g};
        value           uniform (0 0 0);
    }}
"""
        if p["inlet_mode"] == "flowRate"
        else f"""    inlet
    {{
        type            pressureInletVelocity;
        value           uniform (0 0 0);
    }}
"""
    )
    inlet_p = (
        """    inlet
    {
        type            zeroGradient;
    }
"""
        if p["inlet_mode"] == "flowRate"
        else f"""    inlet
    {{
        type            fixedValue;
        value           uniform {inlet_pressure_pa / rho:.6g};
    }}
"""
    )

    _write(
        cfd_root / "0" / "U",
        "volVectorField",
        "U",
        f"""dimensions [0 1 -1 0 0 0 0];
internalField uniform (0 0 0);
boundaryField
{{
{inlet_u}    outlet
    {{
        type            zeroGradient;
    }}
    wall
    {{
        type            noSlip;
    }}
    background
    {{
        type            zeroGradient;
    }}
}}
""",
    )

    _write(
        cfd_root / "0" / "p",
        "volScalarField",
        "p",
        f"""dimensions [0 2 -2 0 0 0 0];
internalField uniform 0;
boundaryField
{{
{inlet_p}    outlet
    {{
        type            fixedValue;
        value           uniform 0;
    }}
    wall
    {{
        type            zeroGradient;
    }}
    background
    {{
        type            zeroGradient;
    }}
}}
""",
    )

    turbulence = str(p["turbulence_model"])
    if turbulence != "laminar":
        # 湍流场初值需由 CFD 工程师确认，这里给出常用量级并明确标注
        k_value = 1.5 * (mean_velocity * 0.05) ** 2
        omega_value = max(mean_velocity / 0.01, 1.0)
        _write(cfd_root / "0" / "k", "volScalarField", "k",
               f"""dimensions [0 2 -2 0 0 0 0];
internalField uniform {k_value:.6g};
boundaryField
{{
    inlet  {{ type fixedValue; value uniform {k_value:.6g}; }}
    outlet {{ type zeroGradient; }}
    wall   {{ type kqRWallFunction; value uniform {k_value:.6g}; }}
    background {{ type zeroGradient; }}
}}
""")
        _write(cfd_root / "0" / "omega", "volScalarField", "omega",
               f"""dimensions [0 0 -1 0 0 0 0];
internalField uniform {omega_value:.6g};
boundaryField
{{
    inlet  {{ type fixedValue; value uniform {omega_value:.6g}; }}
    outlet {{ type zeroGradient; }}
    wall   {{ type omegaWallFunction; value uniform {omega_value:.6g}; }}
    background {{ type zeroGradient; }}
}}
""")
        _write(cfd_root / "0" / "nut", "volScalarField", "nut",
               """dimensions [0 2 -1 0 0 0 0];
internalField uniform 0;
boundaryField
{
    inlet  { type calculated; value uniform 0; }
    outlet { type calculated; value uniform 0; }
    wall   { type nutkWallFunction; value uniform 0; }
    background { type calculated; value uniform 0; }
}
""")

    if p["openfoam_flavour"] == "org":
        _write(
            cfd_root / "constant" / "physicalProperties",
            "dictionary",
            "physicalProperties",
            f"""viscosityModel constant;
nu              {float(p['air_nu']):.6g};
""",
        )
        sim = "laminar" if turbulence == "laminar" else "RAS"
        body = f"simulationType {sim};\n"
        if sim == "RAS":
            body += f"""
RAS
{{
    model {turbulence};
    turbulence on;
    printCoeffs on;
}}
"""
        _write(cfd_root / "constant" / "momentumTransport", "dictionary", "momentumTransport", body)
    else:
        _write(
            cfd_root / "constant" / "transportProperties",
            "dictionary",
            "transportProperties",
            f"""transportModel Newtonian;
nu              {float(p['air_nu']):.6g};
""",
        )
        sim = "laminar" if turbulence == "laminar" else "RAS"
        body = f"simulationType {sim};\n"
        if sim == "RAS":
            body += f"""
RAS
{{
    RASModel {turbulence};
    turbulence on;
    printCoeffs on;
}}
"""
        _write(cfd_root / "constant" / "turbulenceProperties", "dictionary", "turbulenceProperties", body)

    _write(
        cfd_root / "system" / "controlDict",
        "dictionary",
        "controlDict",
        """application     simpleFoam;
startFrom       startTime;
startTime       0;
stopAt          endTime;
endTime         1000;
deltaT          1;
writeControl    timeStep;
writeInterval   100;
purgeWrite      3;
writeFormat     ascii;
writePrecision  8;
writeCompression off;
timeFormat      general;
timePrecision   6;
runTimeModifiable true;

functions
{
    inletFlowRate
    {
        type            surfaceFieldValue;
        libs            ("libfieldFunctionObjects.so");
        writeControl    timeStep;
        writeInterval   1;
        writeFields     no;
        log             yes;
        select          patch;
        patch           inlet;
        operation       sum;
        fields          (phi);
    }
    outletFlowRate
    {
        type            surfaceFieldValue;
        libs            ("libfieldFunctionObjects.so");
        writeControl    timeStep;
        writeInterval   1;
        writeFields     no;
        log             yes;
        select          patch;
        patch           outlet;
        operation       sum;
        fields          (phi);
    }
    wallFlowRate
    {
        type            surfaceFieldValue;
        libs            ("libfieldFunctionObjects.so");
        writeControl    timeStep;
        writeInterval   1;
        writeFields     no;
        log             yes;
        select          patch;
        patch           wall;
        operation       sum;
        fields          (phi);
    }
    inletPressure
    {
        type            surfaceFieldValue;
        libs            ("libfieldFunctionObjects.so");
        writeControl    timeStep;
        writeInterval   1;
        writeFields     no;
        log             yes;
        select          patch;
        patch           inlet;
        operation       areaAverage;
        fields          (p);
    }
    outletPressure
    {
        type            surfaceFieldValue;
        libs            ("libfieldFunctionObjects.so");
        writeControl    timeStep;
        writeInterval   1;
        writeFields     no;
        log             yes;
        select          patch;
        patch           outlet;
        operation       areaAverage;
        fields          (p);
    }
}
""",
    )

    _write(
        cfd_root / "system" / "fvSchemes",
        "dictionary",
        "fvSchemes",
        """ddtSchemes { default steadyState; }
gradSchemes { default Gauss linear; }
divSchemes
{
    default         none;
    div(phi,U)      bounded Gauss linearUpwind grad(U);
    div((nuEff*dev2(T(grad(U))))) Gauss linear;
    div(phi,k)      bounded Gauss upwind;
    div(phi,omega)  bounded Gauss upwind;
}
laplacianSchemes { default Gauss linear corrected; }
interpolationSchemes { default linear; }
snGradSchemes { default corrected; }
""",
    )

    _write(
        cfd_root / "system" / "decomposeParDict",
        "dictionary",
        "decomposeParDict",
        f"""numberOfSubdomains {mpi_subdomains};
method          scotch;
distributed     no;
roots           ();
""",
    )

    _write(
        cfd_root / "system" / "fvSolution",
        "dictionary",
        "fvSolution",
        f"""solvers
{{
    p
    {{
        solver          GAMG;
        tolerance       1e-07;
        relTol          0.01;
        smoother        GaussSeidel;
    }}
    "(U|k|omega)"
    {{
        solver          smoothSolver;
        smoother        symGaussSeidel;
        tolerance       1e-08;
        relTol          0.01;
    }}
}}

SIMPLE
{{
    nNonOrthogonalCorrectors 1;
    pRefPoint       ({location_m[0]:.6f} {location_m[1]:.6f} {location_m[2]:.6f});
    pRefValue       0;
    residualControl
    {{
        p               1e-04;
        U               1e-05;
    }}
}}

relaxationFactors
{{
    equations
    {{
        U               0.7;
    }}
    fields
    {{
        p               0.3;
    }}
}}
""",
    )

    (cfd_root / "run_wsl.sh").write_text(
        """#!/usr/bin/env bash
# 在 WSL / Linux 的 OpenFOAM 环境里跑本算例；未安装 OpenFOAM 时本脚本不会凭空产生结果。
set -e
cd "$(dirname "$0")"
mkdir -p logs
blockMesh > logs/blockMesh.log 2>&1
snappyHexMesh -overwrite > logs/snappyHexMesh.log 2>&1
checkMesh > logs/checkMesh.log 2>&1
simpleFoam > logs/simpleFoam.log 2>&1
echo "求解结束，请检查 logs/ 与 postProcessing/"
""",
        encoding="utf-8",
    )

    (cfd_root / "run_docker.sh").write_text(
        """#!/usr/bin/env bash
# 用本地 Docker 镜像跑本算例；镜像名需由用户指定，本工具不会自动拉取。
# 用法：IMAGE=<openfoam 镜像> ./run_docker.sh
set -e
if [ -z "$IMAGE" ]; then echo "请先设置 IMAGE 环境变量（例如 IMAGE=your/openfoam:tag）"; exit 2; fi
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
docker run --rm -v "$CASE_DIR":/case -w /case "$IMAGE" bash -c \\
  "blockMesh > logs/blockMesh.log 2>&1 && snappyHexMesh -overwrite > logs/snappyHexMesh.log 2>&1 && checkMesh > logs/checkMesh.log 2>&1 && simpleFoam > logs/simpleFoam.log 2>&1"
""",
        encoding="utf-8",
    )

    meta = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "solver": "simpleFoam",
        "parallel": {
            **parallel_plan,
            "enabled": mpi_subdomains > 1,
            "method": "scotch",
        },
        "openfoam_flavour": p["openfoam_flavour"],
        "turbulence_model": turbulence,
        "inlet_mode": p["inlet_mode"],
        "total_flow_lpm": flow_lpm,
        "total_flow_m3s": flow_m3s,
        "inlet_area_mm2": round(inlet_area_mm2, 2),
        "inlet_area_m2": inlet_area_m2,
        "outlet_area_mm2": round(outlet_area_mm2, 2),
        "mean_inlet_velocity_mps": mean_velocity,
        "nu": float(p["air_nu"]),
        "rho_kg_m3": rho,
        "pressure_field_unit": "m2/s2",
        "pressure_dimensions": [0, 2, -2, 0, 0, 0, 0],
        "inlet_pressure_pa": inlet_pressure_pa if p["inlet_mode"] == "pressure" else None,
        "outlet_reference_pressure_pa": 0.0,
        "background_cell_mm": float(p["background_cell_mm"]),
        "margin_mm": margin,
        "add_layers": bool(p["add_layers"]),
        "location_in_mesh_m": [round(float(x), 6) for x in location_m],
        "stl_units": "meters",
        "stl_cleaning": clean_stats,
        "skipped_tiny_openings": [
            {"area_mm2": item.get("area_mm2"), "end": item.get("end")} for item in skipped
        ],
        "openings_used": {"anterior": len(anterior), "posterior": len(posterior)},
        "validation": {
            "status": "未验证",
            "note": "算例按所选 OpenFOAM 分支生成；尚未求解验证。"
            "边界条件与求解设置需由 CFD 工程师复核，且需做粗/中/细网格敏感性检查。",
        },
        "notes": [
            "入口为双侧鼻孔合计流量；若只模拟单侧，请将 total_flow_lpm 设为 7.5",
            "稳态求解 ≠ 呼吸周期动态仿真",
            "开口封盖采用平面扇形三角化，凹形环需在 ParaView / Blender 中复核",
        ],
    }
    (cfd_root / "case_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    update_stage(
        case_id,
        "cfd_prep",
        "ok",
        message=f"已生成 OpenFOAM 算例（入口面积 {inlet_area_mm2:.1f} mm²，平均速度 {mean_velocity:.2f} m/s）；尚未求解",
        artifacts=[
            "cfd/constant/triSurface/wall.stl",
            "cfd/constant/triSurface/inlet.stl",
            "cfd/constant/triSurface/outlet.stl",
            "cfd/constant/triSurface/nasal_cfd.stl",
            "cfd/system/blockMeshDict",
            "cfd/system/snappyHexMeshDict",
            "cfd/system/decomposeParDict",
            "cfd/0/U",
            "cfd/0/p",
            "cfd/case_meta.json",
        ],
        data={
            "total_flow_lpm": flow_lpm,
            "inlet_area_mm2": round(inlet_area_mm2, 2),
            "mean_inlet_velocity_mps": round(mean_velocity, 3),
            "solver": "simpleFoam",
            "parallel": meta["parallel"],
            "solver_gpu_note": meta.get("parallel", {}).get("capabilities", {}).get("solver_gpu_note"),
            "solved": False,
        },
    )
    return meta
