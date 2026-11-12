"""Read-only oblique CT reslice in patient LPS millimetres. Never persists images."""
from __future__ import annotations

import numpy as np
from PIL import Image
from scipy import ndimage

from . import oblique, preview
from .errors import PipelineError


def section_coordinates(volume, plane: dict, span_mm: float = 120.0, size: int = 384):
    plane = oblique._plane(plane, "当前截面")
    if not np.isfinite(span_mm) or not 10 <= span_mm <= 400:
        raise PipelineError("failed", "截面显示范围须在 10–400 mm 之间", ["重新放置端面"])
    n = np.asarray(plane["normal_lps"])
    up = np.array([0., 0., 1.]) if abs(n[2]) < 0.9 else np.array([0., 1., 0.])
    u = np.cross(up, n)
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    pixels = (np.arange(size, dtype=float) - (size - 1) / 2) * span_mm / size
    world = (np.asarray(plane["point_lps_mm"])[:, None, None]
             + u[:, None, None] * pixels[None, None, :]
             - v[:, None, None] * pixels[None, :, None])
    axes_mm = (np.asarray(volume.axis_dirs) * np.asarray(volume.spacing)[:, None]).T
    coordinates = np.linalg.solve(axes_mm, (world - np.asarray(volume.origin)[:, None, None]).reshape(3, -1)).reshape(3, size, size)
    return coordinates


def render_section(volume, mask, plane: dict, span_mm: float = 120.0, size: int = 384) -> Image.Image:
    coordinates = section_coordinates(volume, plane, span_mm, size)
    hu = ndimage.map_coordinates(volume.array, coordinates, order=1, mode="constant", cval=-1000., prefilter=False)
    selected = ndimage.map_coordinates(mask, coordinates, order=0, mode="constant", cval=0, prefilter=False).astype(bool)
    gray = preview._to_grayscale(hu, preview.WINDOW_LEVEL, preview.WINDOW_WIDTH)
    rgb = np.repeat(gray[..., None], 3, axis=2)
    boundary = selected & ~ndimage.binary_erosion(selected)
    rgb[boundary] = (104, 200, 186)
    return Image.fromarray(rgb)
