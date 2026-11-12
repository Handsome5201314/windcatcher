from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

from pipeline import cases, cfd_prep, openfoam_runner
from pipeline.errors import PipelineError


def _case(tmp_path, monkeypatch, *, source="user_confirmed_bilateral_local_oblique_planes"):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = cases.create_case("synthetic", "<synthetic bilateral test>", synthetic=True)
    root = cases.case_dir(case_id)
    (root / "segment" / "segmentation.json").write_text(json.dumps({
        "mask_version": 1, "need_human_confirm": False,
        "review_flags": {"external_air_suspected": False},
    }), encoding="utf-8")
    np.save(root / "segment" / "mask.npy", np.ones((3, 3, 3), dtype=np.uint8))
    (root / "surface" / "surface.json").write_text(json.dumps({
        "mask_version": 1,
        "endplanes": {"source": source, "format": "oblique_bilateral_local_v3", "mask_version": 1},
        "oblique_clip": {"format": "oblique_mesh_clip_bilateral_local_v3"},
    }), encoding="utf-8")
    (root / "surface" / "geom_qc.json").write_text(json.dumps({
        "verdict": "pass", "model_scope": "main_airway", "boundary_edges": 0,
        "connected_components": 1,
        "openings": [
            {"end": "anterior", "side": "left", "area_mm2": 2.0},
            {"end": "anterior", "side": "right", "area_mm2": 2.0},
            {"end": "posterior", "side": "outlet", "area_mm2": 2.0},
        ],
    }), encoding="utf-8")
    vertices = np.asarray([
        [0, 0, 0], [2, 0, 0], [0, 2, 0],
        [4, 0, 0], [6, 0, 0], [4, 2, 0],
        [1, 4, 0], [3, 4, 0], [1, 6, 0],
        [0, 0, 2], [2, 0, 2], [0, 2, 2],
    ], dtype=float)
    np.save(root / "surface" / "vertices.npy", vertices)
    np.save(root / "surface" / "faces.npy", np.asarray([
        [9, 10, 11], [0, 1, 2], [3, 4, 5], [6, 7, 8],
    ], dtype=np.int32))
    for name, indices in (
        ("inlet_left", [1]), ("inlet_right", [2]), ("outlet", [3]),
    ):
        np.save(root / "surface" / f"{name}_triangles.npy", np.asarray(indices, dtype=np.int32))
    monkeypatch.setattr(cfd_prep, "_clean_shell", lambda regions: (regions, {"closed": True}))
    monkeypatch.setattr(cfd_prep, "load_volume", lambda _case: SimpleNamespace(
        spacing=(1, 1, 1), origin=(0, 0, 0), axis_dirs=np.eye(3),
    ))
    monkeypatch.setattr(cfd_prep, "_location_in_mesh", lambda *_args: np.asarray([0.001, 0.001, 0.001]))
    return case_id, root


def test_bilateral_default_is_two_pressure_inlets_with_no_assumed_flow(tmp_path, monkeypatch):
    case_id, root = _case(tmp_path, monkeypatch)
    meta = cfd_prep.prepare_case(case_id)
    cfd = root / "cfd"
    assert meta["boundary_layout"] == "bilateral_local_v3"
    assert meta["inlet_mode"] == "pressure"
    assert meta["total_flow_lpm"] is None
    assert meta["mean_inlet_velocity_mps"] is None
    assert meta["geometry_basis"] == "internal_bilateral_nasal_airway_with_stable_opening_planes"
    assert meta["external_air_domain"]["mode"] == "not_used_in_primary_baseline"
    assert meta["template_version"] == cfd_prep.CFD_TEMPLATE_VERSION
    stage_data = cases.get_case(case_id)["stages"]["cfd_prep"]["data"]
    assert stage_data["inlet_mode"] == "pressure"
    assert stage_data["total_flow_lpm"] is None
    assert meta["inlet_patches"] == ["inlet_left", "inlet_right"]
    assert (cfd / "constant/triSurface/inlet_left.stl").exists()
    assert (cfd / "constant/triSurface/inlet_right.stl").exists()
    assert not (cfd / "constant/triSurface/inlet.stl").exists()
    for filename in ("0/U", "0/p", "system/snappyHexMeshDict"):
        body = (cfd / filename).read_text(encoding="utf-8")
        assert "inlet_left" in body and "inlet_right" in body
    controls = (cfd / "system/controlDict").read_text(encoding="utf-8")
    assert "inletLeftFlowRate" in controls and "inletRightFlowRate" in controls
    if meta["openfoam_flavour"] == "esi":
        assert "regionType      patch;" in controls
        assert "name            inlet_left;" in controls
        assert "name            inlet_right;" in controls
    else:
        assert "patch           inlet_left;" in controls
        assert "patch           inlet_right;" in controls


def test_bilateral_flow_rate_requires_explicit_split(tmp_path, monkeypatch):
    case_id, root = _case(tmp_path, monkeypatch)
    with pytest.raises(PipelineError, match="左侧入口流量比例"):
        cfd_prep.prepare_case(case_id, {"inlet_mode": "flowRate"})
    assert not (root / "cfd" / "case_meta.json").exists()
    meta = cfd_prep.prepare_case(case_id, {"inlet_mode": "flowRate", "left_flow_fraction": 0.4})
    assert meta["left_flow_fraction"] == 0.4
    body = (root / "cfd" / "0" / "U").read_text(encoding="utf-8")
    assert "volumetricFlowRate 0.0001;" in body
    assert "volumetricFlowRate 0.00015;" in body


def test_unconfirmed_bilateral_geometry_cannot_prepare(tmp_path, monkeypatch):
    case_id, root = _case(tmp_path, monkeypatch, source="automatic_candidate")
    with pytest.raises(PipelineError, match="版本不匹配"):
        cfd_prep.prepare_case(case_id)
    assert not (root / "cfd" / "case_meta.json").exists()


def test_bilateral_postprocessing_sums_two_measured_inlet_fluxes(tmp_path):
    (tmp_path / "case_meta.json").write_text(json.dumps({
        "boundary_layout": "bilateral_local_v3", "rho_kg_m3": 1.2,
        "inlet_areas_mm2": {"inlet_left": 2.0, "inlet_right": 3.0},
    }), encoding="utf-8")
    samples = {
        "inletLeftFlowRate": -0.0001, "inletRightFlowRate": -0.00015,
        "outletFlowRate": 0.00025, "inletLeftPressure": 20.0,
        "inletRightPressure": 30.0, "outletPressure": 2.0,
    }
    for function, value in samples.items():
        directory = tmp_path / "postProcessing" / function / "0"
        directory.mkdir(parents=True)
        (directory / "surfaceFieldValue.dat").write_text(f"600 {value}\n", encoding="ascii")
    fields = {"latest_time": "600", "fields": {"p": {"dimensions": [0, 2, -2, 0, 0, 0, 0]}}}
    result = openfoam_runner._read_postprocessing(tmp_path, fields)
    assert result["bilateral_inlet_samples_complete"] is True
    assert result["inlet_flow_m3s"] == pytest.approx(-0.00025)
    assert result["inlet_pressure_field"] == pytest.approx(26.0)
    assert result["flow_direction_ok"] is True
