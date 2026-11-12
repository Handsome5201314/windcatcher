"""Read-only connected cut regions for the CT picker; never approves anatomy."""
from __future__ import annotations

import base64
from io import BytesIO

import numpy as np
from scipy import ndimage

from . import oblique
from .endplane_section import render_section, section_coordinates
from .errors import PipelineError
from .surface import select_largest_component


def _pixel_runs(pixels: np.ndarray) -> list[list[int]]:
    runs = []
    for row in np.flatnonzero(pixels.any(axis=1)):
        padded = np.pad(pixels[row].astype(np.int8), (1, 1))
        starts = np.flatnonzero(np.diff(padded) == 1)
        ends = np.flatnonzero(np.diff(padded) == -1)
        runs.extend([[int(row), int(a), int(b)] for a, b in zip(starts, ends)])
    return runs


def section_targets(volume, mask, plane, role, span_mm=120.0, size=384, seeds=None):
    """Enumerate every crossing component, including invalid ones.

    Pixel runs belong to actual positive-side 3D components sampled near the
    plane, not the largest two 2D contours. Extra/merged branches require a
    human selection or a different plane. The normal and angle stay unchanged.
    """
    if role not in {"anterior", "posterior"}:
        raise PipelineError("blocked", "未知端面类型", ["重新打开端面编辑器"])
    plane = oblique._plane(plane, "当前截面")
    coordinates = section_coordinates(volume, plane, span_mm, size)
    source, _ = select_largest_component(np.asarray(mask, dtype=bool))
    offset, coefficients = oblique._distance_coefficients(volume, plane)
    positive = np.zeros_like(source, dtype=bool)
    for start in range(0, source.shape[0], 12):
        stop = min(start + 12, source.shape[0])
        positive[start:stop] = source[start:stop] & (
            oblique._distances(offset, coefficients, start, stop, source.shape) > 1e-8
        )
    labels, crop, component_count = oblique._label_positive_components(positive)
    boundary = positive & ndimage.binary_dilation(source & ~positive)
    crossing_counts = np.bincount(labels[boundary[crop]], minlength=component_count + 1)
    crossing = np.unique(labels[boundary[crop]])
    crossing = crossing[crossing > 0]
    if len(crossing) > 64:
        raise PipelineError("blocked", "切面穿过的区域过多", ["把切面移到鼻孔或鼻咽的完整管段"])
    axes = np.asarray(volume.axis_dirs) * np.asarray(volume.spacing)[:, None]
    crop_origin = np.array([part.start for part in crop])
    normal = np.asarray(plane["normal_lps"])
    # Match the significant crossing test used by the bilateral save gate.
    voxel_section_area = float(np.prod(volume.spacing)) / float(np.abs(coefficients).sum())
    step = max(volume.spacing)
    sample = np.zeros((size, size), dtype=np.int32)
    # Reuse the exact CT pixel coordinate system, taking the nearest outward
    # sample only. This map is a selection aid, not an exact mesh cap contour.
    # Padding keeps nearest-neighbour samples within half a voxel of the
    # occupied crop boundary equivalent to sampling the full label volume.
    # mode="constant" on the unpadded crop would incorrectly blank them.
    sampled_labels = np.pad(labels, 1) if labels.size else labels
    section_mask = ndimage.map_coordinates(source.astype(np.uint8), coordinates, order=0, mode="constant", cval=0, prefilter=False).astype(bool)
    for distance in ((0.0, 0.5 * step, step, 1.5 * step) if labels.size else ()):
        shifted = coordinates + (normal * distance @ np.linalg.inv(axes))[:, None, None] - crop_origin[:, None, None] + 1
        sampled = ndimage.map_coordinates(sampled_labels, shifted, order=0, mode="constant", cval=0, prefilter=False)
        use = (sample == 0) & section_mask & (sampled > 0)
        sample[use] = sampled[use]
    candidates = []
    valid_coverage = np.zeros((size, size), dtype=bool)
    for label in crossing.tolist():
        coords = np.column_stack(np.where(boundary[crop] & (labels == label))) + crop_origin
        world = np.asarray(volume.origin) + coords @ axes
        distances = (world - np.asarray(plane["point_lps_mm"])) @ normal
        choices = np.flatnonzero(distances > 0.1)
        seed = None
        crossing_area_mm2 = float(crossing_counts[label]) * voxel_section_area
        error = None
        if crossing_area_mm2 < oblique.MIN_OPENING_AREA_MM2:
            opening = "鼻孔入口" if role == "anterior" else "鼻咽出口"
            error = (f"截面约 {crossing_area_mm2:.2f} mm²，小于 "
                     f"{oblique.MIN_OPENING_AREA_MM2:g} mm² 技术下限；"
                     f"疑似小碎片，不能作为完整{opening}")
        if len(choices):
            # A boundary voxel close to the region centre is deterministic;
            # validate it with the same local-cut routine used at save time.
            center = world.mean(axis=0)
            index = choices[np.argmin(np.linalg.norm(world[choices] - center, axis=1))]
            seed = world[index].tolist()
            if error is None:
                try:
                    selected, _, _ = oblique._local_delete_component(source, volume, plane, seed, role=role)
                    if not np.array_equal(selected[crop], labels == label):
                        error = "候选与实际局部裁切支段不一致，请调整切面"
                except PipelineError as exc:
                    error = exc.to_dict()["reason"]
        elif error is None:
            error = "该区域距离切面过近，请微调位置"
        pixels = sample == label
        if error is None:
            valid_coverage |= pixels
        runs = _pixel_runs(pixels)
        candidates.append({"id": int(label), "seed_lps_mm": seed, "valid": error is None,
                           "reason": error, "runs": runs,
                           "cut_area_estimate_mm2": round(crossing_area_mm2, 3),
                           "center_lps_mm": world.mean(axis=0).tolist()})
    expected = 2 if role == "anterior" else 1
    automatic = []
    if len(candidates) == expected and all(c["valid"] and c["runs"] for c in candidates):
        ordered = sorted(candidates, key=lambda c: c["seed_lps_mm"][0], reverse=True)
        if role == "posterior" or ordered[0]["seed_lps_mm"][0] - ordered[1]["seed_lps_mm"][0] >= 1:
            automatic = [c["id"] for c in ordered]
    selected_ids = []
    selectable_ids = {candidate["id"] for candidate in candidates if candidate["valid"]}
    for seed in seeds or []:
        try:
            selected, _, _ = oblique._local_delete_component(source, volume, plane, seed, role=role)
            found = np.unique(labels[selected[crop]])
            found = found[found > 0]
            if len(found) == 1 and int(found[0]) in selectable_ids:
                selected_ids.append(int(found[0]))
        except PipelineError:
            selected_ids = []
            break
    output = BytesIO()
    render_section(volume, source, plane, span_mm, size).save(output, format="PNG")
    return {"size": size, "image_png": base64.b64encode(output.getvalue()).decode("ascii"),
            "candidates": candidates, "automatic_ids": automatic, "selected_ids": selected_ids, "role": role,
            "uncovered_runs": _pixel_runs(section_mask & ~valid_coverage),
            "uncovered_count": int(np.count_nonzero(section_mask & ~valid_coverage)),
            "note": "标色仅对应可验证的局部体素删除支段；未覆盖像素不等于原始 CT 漏分，最终开口以实际裁切网格为准。"}
