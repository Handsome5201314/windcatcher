"""Replay a frozen, synthetic bilateral-airway local-inlet geometry check.

The fixture has no CT or patient data. Passing this technical regression is
not clinical validation and never authorizes a CFD case or a training sample.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
from skimage import measure

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import cases, cfd_prep, oblique, surface
from pipeline.errors import PipelineError
from pipeline.volume import Volume

FIXTURE = ROOT / "tests" / "fixtures" / "local_inlet_bilateral_v1.json"
ALGORITHM_SOURCES = (
    ROOT / "pipeline" / "oblique.py",
    ROOT / "pipeline" / "surface.py",
    ROOT / "pipeline" / "cfd_prep.py",
)


def _voxel(mask: np.ndarray, point: list[int]) -> int:
    return int(mask[tuple(point)])


def _closed_mesh(faces: np.ndarray) -> bool:
    edges = np.sort(
        np.concatenate((faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]])),
        axis=1,
    )
    _, counts = np.unique(edges, axis=0, return_counts=True)
    return bool(len(counts) and np.all(counts == 2))


def _check_cfd_guard(stats: dict) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="windcatcher-synthetic-inlet-") as temp:
        with patch.object(cases, "CASES_DIR", Path(temp)):
            case_id = "synthetic-local-inlet-fixture"
            case_root = cases.case_dir(case_id)
            (case_root / "surface").mkdir(parents=True)
            (case_root / "surface" / "geom_qc.json").write_text(
                json.dumps({
                    "model_scope": "main_airway", "verdict": "pass",
                    "openings": [
                        {"end": "anterior", "area_mm2": stats["inlet_area_mm2"]},
                        {"end": "posterior", "area_mm2": stats["outlet_area_mm2"]},
                    ],
                }),
                encoding="utf-8",
            )
            (case_root / "surface" / "surface.json").write_text(
                json.dumps({
                    "mask_version": 1,
                    "endplanes": {
                        "source": "user_confirmed_local_oblique_planes",
                        "mask_version": 1,
                    },
                    "oblique_clip": {"format": "oblique_mesh_clip_local_v2"},
                }),
                encoding="utf-8",
            )
            try:
                cfd_prep.prepare_case(case_id)
            except PipelineError as exc:
                blocked = exc.status == "blocked" and "单支段几何" in exc.reason
                no_artifacts = not (case_root / "cfd" / "case_meta.json").exists()
                no_artifacts &= not (
                    case_root / "cfd" / "constant" / "triSurface" / "inlet.stl"
                ).exists()
                return blocked and no_artifacts, exc.reason
            return False, "local single-branch geometry reached CFD preparation"


def evaluate_fixture(fixture_path: Path = FIXTURE) -> dict:
    raw = fixture_path.read_bytes()
    fixture = json.loads(raw)
    algorithm_digest = hashlib.sha256()
    for source in ALGORITHM_SOURCES:
        algorithm_digest.update(source.name.encode("ascii"))
        algorithm_digest.update(b"\0")
        algorithm_digest.update(source.read_bytes())
    result = {
        "fixture_id": fixture["fixture_id"],
        "fixture_version": fixture["fixture_version"],
        "algorithm_version": fixture["algorithm_version"],
        "algorithm_source_sha256": algorithm_digest.hexdigest(),
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "data_kind": "synthetic_only",
        "checks": [],
    }

    def record(name: str, passed: bool, reason: str = "") -> None:
        result["checks"].append({
            "name": name, "passed": bool(passed),
            "reason": "" if passed else reason,
        })

    try:
        shape = tuple(fixture["shape_ijk"])
        mask = np.zeros(shape, dtype=np.uint8)
        for box in fixture["airway_boxes_ijk"]:
            mask[tuple(slice(start, stop) for start, stop in box)] = 1
        volume = Volume(
            array=np.zeros(shape, dtype=np.float32),
            spacing=tuple(fixture["spacing_mm"]),
            origin=tuple(fixture["origin_lps_mm"]),
            axis_dirs=tuple(tuple(axis) for axis in fixture["axis_dirs"]),
        )
        anterior = fixture["anterior_plane"]
        posterior = fixture["posterior_plane"]
        seed = fixture["selected_seed_lps_mm"]
        probes = fixture["probes_ijk"]

        global_mask = oblique.clip_mask_oblique(mask, volume, anterior, posterior)
        record(
            "regression_input_reproduces_cross_cut",
            _voxel(global_mask, probes["unselected_anterior"]) == 0,
            "frozen fixture no longer reproduces the global-plane cross-cut",
        )

        preflight = oblique.validate_local_inlet_pair(
            mask, volume, anterior, posterior, seed, main_airway=True
        )
        record(
            "single_selected_inlet",
            preflight["anterior_opening_components"] == 1,
            "preflight did not identify exactly one selected inlet",
        )
        record(
            "preflight_preserves_unselected_region",
            preflight["unselected_region_changed_voxels"] == 0,
            "preflight reports changes in the unselected region",
        )

        local_mask = oblique.clip_mask_oblique_local(
            mask, volume, anterior, posterior, anterior_seed_lps_mm=seed
        )
        record(
            "selected_anterior_removed",
            _voxel(local_mask, probes["selected_anterior"]) == 0,
            "selected anterior branch was not cut",
        )
        record(
            "unselected_anterior_retained",
            _voxel(local_mask, probes["unselected_anterior"]) == 1,
            "unselected anterior branch was cut",
        )
        record(
            "posterior_boundary_retained_and_cut",
            _voxel(local_mask, probes["retained_posterior"]) == 1
            and _voxel(local_mask, probes["removed_posterior"]) == 0,
            "posterior boundary removed an intended retained voxel or left an outside voxel",
        )

        vertices, faces, _, _ = measure.marching_cubes(mask, level=0.5)
        cut_vertices, cut_faces, inlet, outlet, stats = (
            surface.clip_mesh_by_oblique_planes_local(
                vertices, faces, anterior, posterior, mask, volume,
                anterior_seed_lps_mm=seed,
            )
        )
        record(
            "unselected_surface_retained",
            bool(np.any((cut_vertices[:, 0] > 17) & (cut_vertices[:, 1] < 6)))
            and not np.any((cut_vertices[:, 0] < 11) & (cut_vertices[:, 1] < 6)),
            "mesh lost the unselected branch or kept the selected front segment",
        )
        record(
            "single_inlet_and_closed_caps",
            stats["inlet_loops"] == 1 and stats["closed_caps"] is True
            and len(inlet) > 0 and len(outlet) > 0,
            "mesh did not produce one inlet loop and closed inlet/outlet caps",
        )
        record(
            "watertight_mesh",
            _closed_mesh(cut_faces),
            "at least one mesh edge does not belong to exactly two faces",
        )
        guard_passed, guard_reason = _check_cfd_guard(stats)
        record(
            "local_geometry_blocked_from_bilateral_cfd",
            guard_passed,
            guard_reason,
        )
        result["geometry"] = {
            "inlet_loops": int(stats["inlet_loops"]),
            "inlet_area_mm2": float(stats["inlet_area_mm2"]),
            "outlet_area_mm2": float(stats["outlet_area_mm2"]),
        }
    except Exception as exc:
        record("evaluation_completed", False, f"{type(exc).__name__}: {exc}")

    result["passed_count"] = sum(check["passed"] for check in result["checks"])
    result["failed_count"] = len(result["checks"]) - result["passed_count"]
    result["passed"] = result["failed_count"] == 0
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = evaluate_fixture(args.fixture)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
