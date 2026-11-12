"""Conservative, read-only local-inlet proposals from one confirmed case mask.

This is a geometric starting point for a human reviewer, not a learned model or
an anatomical/clinical decision.  It loads the case volume for spatial geometry
but does not use CT intensity values or send them outside this process.  No
other case, learning ledger, or saved endplane artifact is read or written here.
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage

from . import endplanes, local_preview, oblique, surface
from .config import MIN_OPENING_AREA_MM2
from .errors import PipelineError
from .volume import ANTERIOR_LPS

GENERATOR_VERSION = "local_mask_geometry_v2"
BILATERAL_GENERATOR_VERSION = "bilateral_mask_geometry_v2"
_MIN_COMPONENT_AREA_MM2 = max(3.0, MIN_OPENING_AREA_MM2)
# Technical proposal limits, not anatomical or clinical acceptance criteria.
_END_DEPTHS_MM = (2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0)
_MAX_END_DEPTH_MM = 12.0
_MAX_END_DEPTH_FRACTION = 0.20
_MAX_REMOVED_FRACTION = 0.25
_MAX_END_REMOVED_FRACTION = 0.15
_MAX_POSTERIOR_OPTIONS = 2
_MAX_SECTION_DEPTHS_TO_VALIDATE = 4
_MAX_MESH_OPTIONS_PER_SIDE = 3
_MAX_BILATERAL_PREFLIGHT_PAIRS = 4
_MAX_BILATERAL_MESH_ATTEMPTS = 3


def _unavailable(scope: str, mask_version: int, reason: str,
                 generator_version: str = GENERATOR_VERSION) -> dict:
    return {
        "status": "unavailable", "reason": reason, "scope": scope,
        "mask_version": mask_version, "generator_version": generator_version,
        "candidates": [],
    }


def _components(mask: np.ndarray, axis: int, index: int, spacing) -> list[np.ndarray]:
    """Return substantial 2-D connected regions as original (z,y,x) voxels."""
    section = np.take(mask, index, axis=axis)
    labels, count = ndimage.label(section, structure=np.ones((3, 3), dtype=np.uint8))
    pixel_area = float(np.prod([spacing[a] for a in range(3) if a != axis]))
    result = []
    for label in range(1, count + 1):
        pixels = np.argwhere(labels == label)
        if len(pixels) * pixel_area < _MIN_COMPONENT_AREA_MM2:
            continue
        coords = np.insert(pixels, axis, index, axis=1)
        result.append(coords)
    return result


def _point_lps(voxels: np.ndarray, origin: np.ndarray, axes_mm: np.ndarray) -> np.ndarray:
    return origin + voxels.mean(axis=0) @ axes_mm


def _seed_lps(voxels: np.ndarray, origin: np.ndarray, axes_mm: np.ndarray) -> np.ndarray:
    center = voxels.mean(axis=0)
    physical_delta = (voxels - center) @ axes_mm
    closest = voxels[np.argmin(np.sum(physical_delta * physical_delta, axis=1))]
    return origin + closest @ axes_mm


def _end_depths(extent: int, step_mm: float, *, minimum_slices: int) -> list[int]:
    """Try shallow physical distances first, bounded by length and 12 mm."""
    maximum_mm = min(_MAX_END_DEPTH_MM, extent * step_mm * _MAX_END_DEPTH_FRACTION)
    maximum_slices = min(extent - 4, int(np.floor(maximum_mm / step_mm)))
    return list(dict.fromkeys(
        slices for target_mm in _END_DEPTHS_MM
        if (slices := max(minimum_slices, int(np.ceil(target_mm / step_mm)))) <= maximum_slices
    ))


def _later_section_exists(working, volume, axis: int, front_to_back: np.ndarray,
                          start_depth: int, *, component_count: int | None = None) -> bool:
    """Distinguish a too-deep opening from a missing one without 3-D preflight."""
    step_mm = float(volume.spacing[axis])
    stop = min(len(front_to_back) - 4, int(np.floor(2 * _MAX_END_DEPTH_MM / step_mm)))
    for depth in range(start_depth, stop + 1):
        count = len(_components(working, axis, int(front_to_back[depth]), volume.spacing))
        if ((component_count is None and 1 <= count <= 2)
                or (component_count is not None and count == component_count)):
            return True
    return False


def _candidate_plans(
    mask, volume, axis: int, sign: int, scope: str, mask_version: int,
    *, diagnostics: dict | None = None, alternates: dict | None = None,
) -> list[dict]:
    """Rank shallow, low-deletion local drafts before exact mesh preflight."""
    if scope != "main_airway" or sign not in (-1, 1) or axis not in (0, 1, 2):
        return []
    spacing = np.asarray(volume.spacing, dtype=float)
    if spacing.shape != (3,) or not np.isfinite(spacing).all() or np.any(spacing <= 0):
        return []
    source = np.asarray(mask, dtype=bool)
    if source.shape != tuple(volume.shape) or not source.any():
        return []
    working, _ = surface.select_largest_component(source)
    in_plane = tuple(a for a in range(3) if a != axis)
    occupied = np.flatnonzero(np.count_nonzero(working, axis=in_plane))
    if len(occupied) < 12 or np.any(np.diff(occupied) != 1):
        return []
    front_to_back = occupied[::-1] if sign == 1 else occupied
    extent = len(front_to_back)
    origin = np.asarray(volume.origin, dtype=float)
    axes_mm = np.asarray(volume.axis_dirs, dtype=float) * spacing[:, None]
    if axes_mm.shape != (3, 3) or not np.isfinite(axes_mm).all():
        return []
    anterior_direction = np.asarray(ANTERIOR_LPS, dtype=float)
    if float(np.dot(axes_mm[axis] * sign / spacing[axis], anterior_direction)) < 0.7:
        return []

    back_depths = _end_depths(extent, spacing[axis], minimum_slices=2)
    posterior_sections = []
    for back_depth in back_depths:
        index = int(front_to_back[extent - 1 - back_depth])
        regions = _components(working, axis, index, volume.spacing)
        if len(regions) == 1:
            posterior_sections.append((back_depth, {
                "point_lps_mm": _point_lps(regions[0], origin, axes_mm).tolist(),
                "normal_lps": (-anterior_direction).tolist(),
            }))
            if len(posterior_sections) == _MAX_POSTERIOR_OPTIONS:
                break
    if not posterior_sections:
        # Do not move far into the nasopharynx just to force a single outlet.
        # A branched distal tail remains a distinct failure, even if the
        # upstream common cavity eventually has one section.
        reason = "posterior_single_section_missing"
        search_end = min(extent - 4, int(np.floor(_MAX_END_DEPTH_MM / spacing[axis])))
        if back_depths:
            for back_depth in range(max(back_depths) + 1, search_end + 1):
                index = int(front_to_back[extent - 1 - back_depth])
                if len(_components(working, axis, index, volume.spacing)) == 1:
                    reason = "excessive_cut_required"
                    break
        if diagnostics is not None:
            diagnostics["reason"] = reason
        return []

    depths = _end_depths(extent, spacing[axis], minimum_slices=3)
    # A two-branch section must not silently become a single-branch proposal
    # just because a later section has crossed the nasal junction.
    sections = []
    for depth in depths:
        regions = _components(working, axis, int(front_to_back[depth]), volume.spacing)
        if 1 <= len(regions) <= 2:
            sections.append((depth, regions))
    if not sections:
        if diagnostics is not None and _later_section_exists(
            working, volume, axis, front_to_back, max(depths, default=0) + 1
        ):
            diagnostics["reason"] = "excessive_cut_required"
        return []
    branch_count = max(len(regions) for _, regions in sections)
    if branch_count == 1 and _later_section_exists(
        working, volume, axis, front_to_back, max(depths) + 1, component_count=2
    ):
        if diagnostics is not None:
            diagnostics["reason"] = "excessive_cut_required"
        return []
    sections = [(depth, regions) for depth, regions in sections
                if len(regions) == branch_count][:_MAX_SECTION_DEPTHS_TO_VALIDATE]
    ranked: dict[str, list[tuple[int, int, int, dict, dict]]] = {
        label: [] for label in (("left", "right") if branch_count == 2 else ("single",))
    }
    excessive_deletion = False
    for back_depth, posterior in posterior_sections:
        posterior_depth = extent - 1 - back_depth
        for depth, regions in sections:
            if depth + 4 >= posterior_depth or len(regions) != branch_count:
                continue
            front_regions = _components(working, axis, int(front_to_back[depth - 2]), volume.spacing)
            if len(front_regions) != branch_count:
                continue
            centers = [_point_lps(region, origin, axes_mm) for region in regions]
            front_centers = [_point_lps(region, origin, axes_mm) for region in front_regions]
            if branch_count == 2:
                order = sorted(range(2), key=lambda i: centers[i][0], reverse=True)
                if centers[order[0]][0] - centers[order[1]][0] < 2.0:
                    continue
                labels = {order[0]: "left", order[1]: "right"}
            else:
                labels = {0: "single"}
            for index, region in enumerate(regions):
                label = labels[index]
                distances = [float(np.linalg.norm(center - centers[index])) for center in front_centers]
                front_index = int(np.argmin(distances))
                if branch_count == 2 and sorted(distances)[1] - sorted(distances)[0] < 2.0:
                    continue
                direction = front_centers[front_index] - centers[index]
                norm = float(np.linalg.norm(direction))
                if norm < 1e-8 or float(np.dot(direction / norm, anterior_direction)) < 0.8:
                    continue
                anterior = {
                    "point_lps_mm": centers[index].tolist(),
                    "normal_lps": (direction / norm).tolist(),
                }
                seed = _seed_lps(front_regions[front_index], origin, axes_mm).tolist()
                try:
                    checked = oblique.validate_local_inlet_pair(
                        working, volume, anterior, posterior, seed, main_airway=True,
                    )
                except PipelineError:
                    continue
                source_voxels = checked["source_voxels"]
                removed_voxels = source_voxels - checked["retained_voxels"]
                removed_fraction = removed_voxels / source_voxels
                anterior_removed = checked["anterior_deleted_voxels"]
                posterior_removed = removed_voxels - anterior_removed
                if (removed_fraction > _MAX_REMOVED_FRACTION
                        or max(anterior_removed, posterior_removed) / source_voxels
                        > _MAX_END_REMOVED_FRACTION):
                    excessive_deletion = True
                    continue
                item = {
                    "label": label,
                    "plan": {
                        "format": endplanes.LOCAL_INLET_FORMAT, "scope": scope,
                        "mask_version": mask_version,
                        "anterior_plane": checked["anterior_plane"],
                        "posterior_plane": checked["posterior_plane"],
                        "anterior_seed_lps_mm": checked["anterior_seed_lps_mm"],
                    },
                    "retention": {
                        "source_voxels": source_voxels,
                        "retained_voxels": checked["retained_voxels"],
                        "removed_fraction": round(removed_fraction, 4),
                        "anterior_depth_mm": round(float(depth * spacing[axis]), 3),
                        "posterior_depth_mm": round(float(back_depth * spacing[axis]), 3),
                    },
                }
                ranked[label].append((removed_voxels, depth, back_depth, item, checked))

    options: dict[str, list[dict]] = {label: [] for label in ranked}
    for label, candidates in ranked.items():
        per_outlet: dict[int, int] = {}
        for _removed, _depth, back_depth, item, checked in sorted(candidates, key=lambda entry: entry[:3]):
            if len(options[label]) >= _MAX_MESH_OPTIONS_PER_SIDE:
                break
            if per_outlet.get(back_depth, 0) >= 2:
                continue
            try:
                local_preview.build_local_inlet_mesh(working, volume, axis, scope, checked)
            except PipelineError:
                continue
            options[label].append(item)
            per_outlet[back_depth] = per_outlet.get(back_depth, 0) + 1
    if alternates is not None:
        alternates.update(options)
    proposals = [options[label][0] for label in ("left", "right", "single") if options.get(label)]
    if not proposals and excessive_deletion and diagnostics is not None:
        diagnostics["reason"] = "excessive_cut_required"
    return proposals


def generate_auto_candidates(case_id: str, scope: str, mask_version: int) -> dict:
    """Generate review-only drafts from this case's *confirmed* current mask.

    The caller controls authorization and any later proposal-ledger write.  A
    draft never changes the case plan, segmentation, STL, or CFD artifacts.
    """
    if scope != "main_airway":
        return _unavailable(scope, mask_version, "unsupported_scope")
    if not endplanes.local_inlet_available():
        return _unavailable(scope, mask_version, "local_inlet_unavailable")
    try:
        scope, root, volume, mask, axis, sign, _view = endplanes._oblique_context(
            case_id, scope, mask_version,
        )
    except PipelineError:
        return _unavailable(scope, mask_version, "mask_not_ready_or_stale")
    try:
        diagnostics: dict[str, str] = {}
        plans = _candidate_plans(mask, volume, axis, sign, scope, mask_version,
                                 diagnostics=diagnostics)
        current_version = endplanes._segmentation(root).get("mask_version")
    except PipelineError:
        return _unavailable(scope, mask_version, "case_context_unavailable")
    if current_version != mask_version:
        return _unavailable(scope, mask_version, "mask_changed_during_generation")
    if not plans:
        return _unavailable(scope, mask_version,
                            diagnostics.get("reason", "geometry_or_preflight_unavailable"))
    return {
        "status": "available", "scope": scope, "mask_version": mask_version,
        "generator_version": GENERATOR_VERSION, "candidates": plans,
    }


def _posterior_seed_candidates(mask, volume, axis: int, sign: int, plane: dict):
    """Find a voxel seed behind the proposed outlet, never on its cut plane."""
    working, _ = surface.select_largest_component(np.asarray(mask, dtype=bool))
    occupied = np.flatnonzero(np.any(working, axis=tuple(a for a in range(3) if a != axis)))
    front_to_back = occupied[::-1] if sign == 1 else occupied
    origin = np.asarray(volume.origin, dtype=float)
    spacing = np.asarray(volume.spacing, dtype=float)
    directions = np.asarray(volume.axis_dirs, dtype=float)
    axes_mm = directions * spacing[:, None]
    plane_index = float(np.dot(np.asarray(plane["point_lps_mm"], dtype=float) - origin,
                               directions[axis]) / spacing[axis])
    at = int(np.argmin(np.abs(front_to_back - plane_index)))
    for depth in (2, 3, 4):
        if at + depth >= len(front_to_back):
            break
        regions = _components(working, axis, int(front_to_back[at + depth]), volume.spacing)
        if len(regions) == 1:
            yield _seed_lps(regions[0], origin, axes_mm).tolist()


def _bilateral_candidate_plans(mask, volume, axis: int, sign: int,
                               scope: str, mask_version: int,
                               *, diagnostics: dict | None = None) -> list[dict]:
    """Join low-deletion independent proposals only if three real caps work."""
    options: dict[str, list[dict]] = {}
    _candidate_plans(mask, volume, axis, sign, scope, mask_version,
                     diagnostics=diagnostics, alternates=options)
    if not options.get("left") or not options.get("right"):
        return []
    pairs = ((left, right) for left in options["left"] for right in options["right"]
             if left["plan"]["posterior_plane"] == right["plan"]["posterior_plane"])
    ranked_pairs = sorted(pairs, key=lambda pair: (
        pair[0]["retention"]["removed_fraction"] + pair[1]["retention"]["removed_fraction"],
        pair[0]["retention"]["anterior_depth_mm"] + pair[1]["retention"]["anterior_depth_mm"],
    ))
    excessive_deletion = False
    scored = []
    for left_item, right_item in ranked_pairs[:_MAX_BILATERAL_PREFLIGHT_PAIRS]:
        left, right = left_item["plan"], right_item["plan"]
        for posterior_seed in _posterior_seed_candidates(
            mask, volume, axis, sign, left["posterior_plane"]
        ):
            try:
                checked = oblique.validate_bilateral_local_planes(
                    mask, volume,
                    left["anterior_plane"], right["anterior_plane"], left["posterior_plane"],
                    left["anterior_seed_lps_mm"], right["anterior_seed_lps_mm"], posterior_seed,
                    main_airway=True,
                )
            except PipelineError:
                continue
            source_voxels = checked["source_voxels"]
            removed_fraction = (source_voxels - checked["retained_voxels"]) / source_voxels
            end_removed_fraction = max(checked[f"{end}_deleted_voxels"]
                                       for end in ("left", "right", "posterior")) / source_voxels
            if (removed_fraction > _MAX_REMOVED_FRACTION
                    or end_removed_fraction > _MAX_END_REMOVED_FRACTION):
                excessive_deletion = True
                break
            scored.append((source_voxels - checked["retained_voxels"],
                           left_item, right_item, checked))
            break
    for _removed, left_item, right_item, checked in sorted(
        scored, key=lambda entry: (entry[0],
                                   entry[1]["retention"]["anterior_depth_mm"]
                                   + entry[2]["retention"]["anterior_depth_mm"])
    )[:_MAX_BILATERAL_MESH_ATTEMPTS]:
        try:
            _vertices, _faces, stats = local_preview.build_bilateral_local_mesh(
                mask, volume, axis, scope, checked,
            )
        except PipelineError:
            continue
        if not stats.get("closed_caps") or any(stats.get(key) != 1 for key in (
            "left_inlet_loops", "right_inlet_loops", "outlet_loops"
        )):
            continue
        source_voxels = checked["source_voxels"]
        removed_fraction = (source_voxels - checked["retained_voxels"]) / source_voxels
        return [{"label": "bilateral", "plan": {
                "format": endplanes.BILATERAL_LOCAL_FORMAT,
                "scope": scope,
                "mask_version": mask_version,
                "left_inlet_plane": checked["left_inlet_plane"],
                "right_inlet_plane": checked["right_inlet_plane"],
                "posterior_plane": checked["posterior_plane"],
                "left_inlet_seed_lps_mm": checked["left_inlet_seed_lps_mm"],
                "right_inlet_seed_lps_mm": checked["right_inlet_seed_lps_mm"],
                "posterior_seed_lps_mm": checked["posterior_seed_lps_mm"],
        }, "retention": {
                "source_voxels": source_voxels,
                "retained_voxels": checked["retained_voxels"],
                "removed_fraction": round(removed_fraction, 4),
                "left_deleted_voxels": checked["left_deleted_voxels"],
                "right_deleted_voxels": checked["right_deleted_voxels"],
                "posterior_deleted_voxels": checked["posterior_deleted_voxels"],
                "left_depth_mm": left_item["retention"]["anterior_depth_mm"],
                "right_depth_mm": right_item["retention"]["anterior_depth_mm"],
                "posterior_depth_mm": left_item["retention"]["posterior_depth_mm"],
        }}]
    if excessive_deletion and diagnostics is not None:
        diagnostics["reason"] = "excessive_cut_required"
    return []


def generate_bilateral_candidate(case_id: str, scope: str, mask_version: int) -> dict:
    """Return one read-only three-opening draft; never save or certify it."""
    def unavailable(reason: str) -> dict:
        return _unavailable(scope, mask_version, reason, BILATERAL_GENERATOR_VERSION)

    if scope != "main_airway":
        return unavailable("unsupported_scope")
    if not endplanes.bilateral_local_available():
        return unavailable("bilateral_local_unavailable")
    try:
        scope, root, volume, mask, axis, sign, _view = endplanes._oblique_context(
            case_id, scope, mask_version,
        )
    except PipelineError:
        return unavailable("mask_not_ready_or_stale")
    try:
        diagnostics: dict[str, str] = {}
        candidates = _bilateral_candidate_plans(mask, volume, axis, sign, scope, mask_version,
                                                diagnostics=diagnostics)
        current_version = endplanes._segmentation(root).get("mask_version")
    except PipelineError:
        return unavailable("case_context_unavailable")
    if current_version != mask_version:
        return unavailable("mask_changed_during_generation")
    if not candidates:
        return unavailable(diagnostics.get("reason", "three_cap_preflight_unavailable"))
    return {
        "status": "available", "scope": scope, "mask_version": mask_version,
        "generator_version": BILATERAL_GENERATOR_VERSION, "candidates": candidates,
    }


def suggest_outward_bilateral_inlet_shift(
    mask, volume, ap_axis: int, scope: str, *, side: str,
    left_inlet_plane: dict, right_inlet_plane: dict, posterior_plane: dict,
    left_inlet_seed_lps_mm, right_inlet_seed_lps_mm, posterior_seed_lps_mm,
) -> dict:
    """Suggest a shallower *real* three-cap draft without changing angle or data.

    An outward shift preserves more of the selected inlet. The same voxel and
    exact-mesh preflights used for a saved bilateral plan must both pass. A
    single seed never authorizes combining disconnected target components.
    """
    if scope != "main_airway" or side not in {"left", "right"}:
        raise PipelineError("failed", "仅支持主气道左右鼻孔向外微调", ["检查建模范围和鼻孔侧别"])
    if ap_axis not in (0, 1, 2):
        raise PipelineError("failed", "CT 前后轴无效", ["核查 DICOM 几何方向"])
    source, _ = surface.select_largest_component(np.asarray(mask, dtype=bool))
    if source.shape != tuple(volume.shape) or not source.any():
        raise PipelineError("blocked", "主气道掩膜为空或尺寸不符", ["重新复核掩膜"])
    planes = {
        "left": oblique.validate_plane_endpoint(left_inlet_plane, "anterior"),
        "right": oblique.validate_plane_endpoint(right_inlet_plane, "anterior"),
        "outlet": oblique._bilateral_local_outlet_plane(posterior_plane, posterior_seed_lps_mm),
    }
    seeds = {
        "left": left_inlet_seed_lps_mm, "right": right_inlet_seed_lps_mm,
        "outlet": posterior_seed_lps_mm,
    }
    original = planes[side]
    normal = np.asarray(original["normal_lps"], dtype=float)
    point = np.asarray(original["point_lps_mm"], dtype=float)
    try:
        seed = np.asarray(seeds[side], dtype=float)
    except (TypeError, ValueError) as exc:
        raise PipelineError("failed", "鼻孔定位点无效", ["重新选择入口"]) from exc
    if seed.shape != (3,) or not np.isfinite(seed).all():
        raise PipelineError("failed", "鼻孔定位点无效", ["重新选择入口"])
    try:
        original_selected, _, _ = oblique._local_delete_component(
            source, volume, original, seed, role="anterior",
        )
    except PipelineError:
        return {"status": "unavailable", "reason": "original_port_not_selectable",
                "side": side, "tested_offsets_mm": [], "mesh_attempts": 0}
    axes_mm = np.asarray(volume.axis_dirs, dtype=float) * np.asarray(volume.spacing)[:, None]
    selected_world = np.asarray(volume.origin, dtype=float) + np.argwhere(original_selected) @ axes_mm
    outward_distance = (selected_world - point) @ normal
    tested = []
    mesh_attempts = 0
    mesh_budget_exhausted = False
    # Bounded, deterministic outward search. Re-evaluate the *whole* three-end
    # draft at every point; an inlet cannot be accepted independently of its
    # opposite inlet and outlet. Never move farther into the nose to force pass.
    for shift in (2.0, 1.5, 1.0, 0.5):
        eligible = outward_distance > shift + 0.1
        if not eligible.any():
            continue
        trial_seed = selected_world[eligible][np.argmin(
            np.linalg.norm(selected_world[eligible] - seed, axis=1)
        )].tolist()
        moved = dict(planes)
        moved[side] = {
            "point_lps_mm": (point + shift * normal).tolist(),
            "normal_lps": original["normal_lps"],
        }
        tested.append(shift)
        try:
            trial_selected, _, _ = oblique._local_delete_component(
                source, volume, moved[side], trial_seed, role="anterior",
            )
            if not np.any(trial_selected) or np.any(trial_selected & ~original_selected):
                continue
            trial_seeds = {**seeds, side: trial_seed}
            checked = oblique.validate_bilateral_local_planes(
                source, volume, moved["left"], moved["right"], moved["outlet"],
                trial_seeds["left"], trial_seeds["right"], trial_seeds["outlet"],
                main_airway=True,
            )
            source_voxels = checked["source_voxels"]
            removed = source_voxels - checked["retained_voxels"]
            if (removed / source_voxels > _MAX_REMOVED_FRACTION or any(
                checked[f"{end}_deleted_voxels"] / source_voxels > _MAX_END_REMOVED_FRACTION
                for end in ("left", "right", "posterior")
            )):
                continue
            if mesh_attempts >= 2:
                mesh_budget_exhausted = True
                break
            mesh_attempts += 1
            _vertices, _faces, stats = local_preview.build_bilateral_local_mesh(
                source, volume, ap_axis, scope, checked,
            )
            if not stats.get("closed_caps") or any(
                stats.get(key) != 1 for key in (
                    "left_inlet_loops", "right_inlet_loops", "outlet_loops",
                )
            ):
                continue
        except PipelineError:
            continue
        return {
            "status": "proposed", "reason": None, "side": side,
            "shift_mm": {"left": shift if side == "left" else 0.0,
                         "right": shift if side == "right" else 0.0, "outlet": 0.0},
            "plan": {
                "format": endplanes.BILATERAL_LOCAL_FORMAT, "scope": scope,
                "left_inlet_plane": checked["left_inlet_plane"],
                "right_inlet_plane": checked["right_inlet_plane"],
                "posterior_plane": checked["posterior_plane"],
                "left_inlet_seed_lps_mm": checked["left_inlet_seed_lps_mm"],
                "right_inlet_seed_lps_mm": checked["right_inlet_seed_lps_mm"],
                "posterior_seed_lps_mm": checked["posterior_seed_lps_mm"],
            },
            "mesh_preflight": {
                key: stats[key] for key in (
                    "closed_caps", "left_inlet_loops", "right_inlet_loops", "outlet_loops",
                )
            },
            "retained_voxels": checked["retained_voxels"],
            "tested_offsets_mm": tested, "mesh_attempts": mesh_attempts,
        }
    return {"status": "unavailable",
            "reason": "mesh_preflight_budget_exhausted" if mesh_budget_exhausted else "three_cap_preflight_unavailable",
            "side": side, "tested_offsets_mm": tested, "mesh_attempts": mesh_attempts}


__all__ = ["GENERATOR_VERSION", "BILATERAL_GENERATOR_VERSION",
           "generate_auto_candidates", "generate_bilateral_candidate",
           "suggest_outward_bilateral_inlet_shift"]
