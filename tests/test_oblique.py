"""任意旋转双平面的合成管道回归验证。"""

from __future__ import annotations

import json

import numpy as np
import pytest

from pipeline import cases, endplanes, oblique, surface
from pipeline.errors import PipelineError
from pipeline.volume import Volume, save_volume


@pytest.fixture
def tube():
    shape = (24, 40, 24)
    mask = np.zeros(shape, dtype=np.uint8)
    mask[7:17, 5:35, 7:17] = 1
    volume = Volume(
        array=np.zeros(shape, dtype=np.float32),
        spacing=(0.8, 0.8, 0.8),
        origin=(0.0, 0.0, 0.0),
        axis_dirs=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    )
    anterior = {"point_lps_mm": [9.6, 8.0, 9.6], "normal_lps": [0.20, -0.98, 0.08]}
    posterior = {"point_lps_mm": [9.6, 24.0, 9.6], "normal_lps": [-0.13, 0.99, 0.04]}
    return mask, volume, anterior, posterior


def test_oblique_pair_retains_tube_and_reports_preflight(tube):
    mask, volume, anterior, posterior = tube

    checked = oblique.validate_plane_pair(mask, volume, anterior, posterior, main_airway=True)
    retained = oblique.clip_mask_oblique(mask, volume, checked["anterior_plane"], checked["posterior_plane"])

    assert checked["retained_voxels"] == int(retained.sum())
    assert checked["retained_components"] == 1
    assert checked["anterior_area_estimate_mm2"] > 1.0
    assert checked["posterior_area_estimate_mm2"] > 1.0
    assert np.isclose(np.linalg.norm(checked["anterior_plane"]["normal_lps"]), 1.0)
    assert retained.sum() < mask.sum()
    assert checked["retained_volume_ml"] == round(float(retained.sum()) * 0.8**3 / 1000.0, 3)


def test_oblique_pair_rejects_reversed_cut_normals(tube):
    mask, volume, anterior, posterior = tube
    anterior["normal_lps"] = [0.0, 1.0, 0.0]

    with pytest.raises(PipelineError, match="法向"):
        oblique.validate_plane_pair(mask, volume, anterior, posterior, main_airway=True)


@pytest.mark.parametrize("main_airway", [True, False])
def test_oblique_pair_rejects_planes_crossing_inside_airway(tube, main_airway):
    mask, volume, _, _ = tube
    # Both normals point to their respective anatomical outside and the AP
    # ordering is valid, but the two tilted planes meet inside the tube.
    anterior = {"point_lps_mm": [9.6, 12.0, 9.6], "normal_lps": [0.4, -0.8, 0.4]}
    posterior = {"point_lps_mm": [9.6, 15.2, 9.6], "normal_lps": [-0.4, 0.8, 0.4]}

    with pytest.raises(PipelineError, match="相交|过近"):
        oblique.validate_plane_pair(mask, volume, anterior, posterior, main_airway=main_airway)


def test_oblique_pair_rejects_parallel_planes_with_unresolved_gap(tube):
    mask, volume, _, _ = tube
    anterior = {"point_lps_mm": [9.6, 12.0, 9.6], "normal_lps": [0.0, -1.0, 0.0]}
    posterior = {"point_lps_mm": [9.6, 12.8, 9.6], "normal_lps": [0.0, 1.0, 0.0]}

    with pytest.raises(PipelineError, match="间距过小"):
        oblique.validate_plane_pair(mask, volume, anterior, posterior, main_airway=True)


def test_oblique_surface_only_opens_at_user_planes_with_extreme_protrusion(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = cases.create_case("local_path", "<合成 CT 测试>", synthetic=False)
    root = cases.case_dir(case_id)
    mask = np.zeros((16, 20, 14), dtype=np.uint8)
    mask[4:14, 5:15, 4:9] = 1
    mask[5, 2:5, 5:7] = 1
    save_volume(case_id, Volume(
        array=np.zeros(mask.shape, dtype=np.float32),
        spacing=(1.0, 1.0, 1.0), origin=(0.0, 0.0, 0.0),
        axis_dirs=((0.0, 0.0, 1.0), (0.0, 1.0, 0.0), (1.0, 0.0, 0.0)),
        meta={"shape": list(mask.shape)},
    ))
    np.save(root / "segment" / "mask.npy", mask)
    record = {
        "params": {"ap_axis": 1, "hu_high": -300.0},
        "mask_version": 1, "need_human_confirm": False,
        "review_flags": {"external_air_suspected": False},
        "external_air_validation": {
            "mask_version": 1, "body_hu_threshold": -300.0,
            "outside_voxels": 0, "candidate_outside_voxels": 0,
        },
    }
    (root / "segment" / "segmentation.json").write_text(json.dumps(record), encoding="utf-8")
    anterior = {"point_lps_mm": [6.0, 5.0, 9.5], "normal_lps": [0.0, -1.0, 1.0]}
    posterior = {"point_lps_mm": [6.0, 12.0, 8.0], "normal_lps": [0.0, 1.0, 0.0]}
    endplanes.save_oblique_endplanes(case_id, "main_airway", anterior, posterior, 1)

    result = surface.run_surface(case_id)

    faces = np.load(root / "surface" / "faces.npy")
    edges = np.sort(np.concatenate((faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]])), axis=1)
    _, counts = np.unique(edges, axis=0, return_counts=True)
    assert np.count_nonzero(counts == 1) == 0
    assert np.all(counts == 2)
    assert result["params"]["extend_ap_edges"] is False
    assert result["manifold_prep"]["padding_mode"] == "closed_zero_padding"
    assert result["endplanes"]["source"] == "user_confirmed_oblique_planes"
    assert result["oblique_clip"]["inlet_cap_faces"] > 0
    assert result["oblique_clip"]["outlet_cap_faces"] > 0
