"""鼻塞方案独立 CFD 适配与同工况对比测试。"""

from __future__ import annotations

import json

import numpy as np
import pytest

from pipeline import cases, cfd_prep, config, plug_models, scenario_cfd, scenarios
from pipeline.errors import PipelineError


def _box_mesh(origin=(0.0, 0.0, 0.0), size=10.0):
    x, y, z = origin
    s = float(size)
    vertices = (
        (x, y, z), (x + s, y, z), (x + s, y + s, z), (x, y + s, z),
        (x, y, z + s), (x + s, y, z + s), (x + s, y + s, z + s), (x, y + s, z + s),
    )
    faces = (
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
        (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    )
    return plug_models.Mesh(vertices, faces, "test")


def _prepare_case(tmp_path, monkeypatch):
    root = tmp_path / "cases"
    monkeypatch.setattr(config, "CASES_DIR", root)
    monkeypatch.setattr(cases, "CASES_DIR", root)
    case_id = cases.create_case("synthetic", "<合成测试>", synthetic=True)
    case_root = cases.case_dir(case_id)
    (case_root / "segment" / "segmentation.json").write_text(json.dumps({"mask_version": 1}), encoding="utf-8")
    (case_root / "surface" / "airway.stl").write_bytes(plug_models.mesh_to_binary_stl(_box_mesh()))
    (case_root / "surface" / "surface.json").write_text(json.dumps({"mask_version": 1}), encoding="utf-8")
    cases.update_stage(case_id, "surface", "ok", data={"mask_version": 1})

    cfd_root = case_root / "cfd"
    (cfd_root / "system").mkdir(parents=True, exist_ok=True)
    (cfd_root / "0").mkdir(parents=True, exist_ok=True)
    (cfd_root / "constant" / "triSurface").mkdir(parents=True, exist_ok=True)
    (cfd_root / "case_meta.json").write_text(json.dumps({
        "solver": "simpleFoam", "turbulence_model": "laminar", "inlet_mode": "flowRate",
        "total_flow_lpm": 15.0, "total_flow_m3s": 0.00025, "rho_kg_m3": 1.2, "nu": 1.5e-5,
        "inlet_pressure_pa": 18.0, "outlet_reference_pressure_pa": 0.0,
    }), encoding="utf-8")
    (cfd_root / "system" / "snappyHexMeshDict").write_text(
        "geometry\n{\n    wall.stl { type triSurfaceMesh; name wall; }\n}\n"
        "refinementSurfaces\n{\n    wall { level (2 3); patchInfo { type wall; } }\n}\n"
        "locationInMesh (0.005 0.005 0.005);\n",
        encoding="utf-8",
    )
    fields = {
        "U": "boundaryField\n{\n    wall { type noSlip; }\n    background { type zeroGradient; }\n}\n",
        "p": "boundaryField\n{\n    wall { type zeroGradient; }\n    background { type zeroGradient; }\n}\n",
    }
    for name, text in fields.items():
        (cfd_root / "0" / name).write_text(text, encoding="utf-8")
    for name in ("wall.stl", "inlet.stl", "outlet.stl"):
        (cfd_root / "constant" / "triSurface" / name).write_bytes(
            cfd_prep._write_ascii_stl_bytes(_box_mesh()) if hasattr(cfd_prep, "_write_ascii_stl_bytes") else b""
        )
    base_mesh = _box_mesh()
    base_vertices = np.asarray(base_mesh.vertices, dtype=float)
    base_faces = np.asarray(base_mesh.faces, dtype=int)
    cfd_prep._write_ascii_stl(cfd_root / "constant" / "triSurface" / "nasal_cfd.stl", "airwayShell", base_vertices[base_faces])
    return case_id, case_root


def test_prepare_scenario_adds_internal_plug_patch(tmp_path, monkeypatch):
    case_id, case_root = _prepare_case(tmp_path, monkeypatch)
    created = scenarios.create_scenario(case_id, plug_models.mesh_to_binary_stl(_box_mesh((2, 2, 2), 2)), "mm")
    scenario_id = created["scenario_id"]
    scenarios.update_placement(case_id, scenario_id, {"translation_mm": [0, 0, 0], "rotation_deg": [0, 0, 0], "scale": [1, 1, 1]})
    scenarios.record_review(case_id, scenario_id, approved=True, reviewer_role="engineer")

    result = scenario_cfd.prepare_scenario(case_id, scenario_id)
    cfd_root = case_root / "scenarios" / scenario_id / "versions" / "v0002" / "cfd"
    assert result["prepared"]["cfd_ready"] is True
    assert result["prepared"]["adapter"] == "snappyHexMesh_internal_plug_wall"
    assert "plug.stl" in (cfd_root / "system" / "snappyHexMeshDict").read_text(encoding="utf-8")
    assert (cfd_root / "constant" / "triSurface" / "plug.stl").is_file()
    assert "    plug" in (cfd_root / "0" / "U").read_text(encoding="utf-8")
    assert scenarios.get_scenario(case_id, scenario_id)["cfd"]["status"] == "prepared"


def test_prepare_blocks_plug_outside_airway(tmp_path, monkeypatch):
    case_id, _ = _prepare_case(tmp_path, monkeypatch)
    created = scenarios.create_scenario(case_id, plug_models.mesh_to_binary_stl(_box_mesh((30, 30, 30), 2)), "mm")
    scenario_id = created["scenario_id"]
    scenarios.update_placement(case_id, scenario_id, {"translation_mm": [0, 0, 0], "rotation_deg": [0, 0, 0], "scale": [1, 1, 1]})
    scenarios.record_review(case_id, scenario_id, approved=True, reviewer_role="engineer")
    try:
        scenario_cfd.prepare_scenario(case_id, scenario_id)
    except Exception as exc:
        assert "超出" in str(exc) or "气道封闭壳外" in str(exc)
    else:
        raise AssertionError("outside plug should be blocked")


def test_comparison_keeps_engineering_gate_separate(tmp_path, monkeypatch):
    case_id, case_root = _prepare_case(tmp_path, monkeypatch)
    scenario_id = "plug-20261017-000000-deadbeef"
    scenario_root = case_root / "scenarios" / scenario_id / "versions" / "v0001" / "cfd"
    scenario_root.mkdir(parents=True)
    meta = json.loads((case_root / "cfd" / "case_meta.json").read_text(encoding="utf-8"))
    scenario_meta = {"condition_fingerprint": scenario_cfd._condition_fingerprint(meta)}
    (scenario_root / "scenario_meta.json").write_text(json.dumps(scenario_meta), encoding="utf-8")
    gate = lambda _case_id, **kwargs: {"numeric_ok": True, "eligible": False, "metrics": {"pressure_unit": "Pa", "delta_p_pa": 10.0, "inlet_flow_m3s": -0.00025}}
    monkeypatch.setattr(scenario_cfd.openfoam_runner, "check_result_gate", gate)
    comparison = scenario_cfd._comparison(case_id, scenario_id, 1, scenario_root, scenario_meta)
    assert comparison["status"] == "checked"
    assert comparison["eligibility"]["recommendation_allowed"] is False
    assert comparison["delta"]["pressure_percent"] == 0.0


def test_old_version_cannot_use_current_review(tmp_path, monkeypatch):
    case_id, _ = _prepare_case(tmp_path, monkeypatch)
    created = scenarios.create_scenario(case_id, plug_models.mesh_to_binary_stl(_box_mesh((2, 2, 2), 2)), "mm")
    scenario_id = created["scenario_id"]
    scenarios.update_placement(case_id, scenario_id, {})
    scenarios.record_review(case_id, scenario_id, approved=True, reviewer_role="engineer")
    with pytest.raises(PipelineError, match="当前.*版本"):
        scenario_cfd.prepare_scenario(case_id, scenario_id, version=1)


def test_seed_inside_plug_is_blocked(tmp_path, monkeypatch):
    case_id, _ = _prepare_case(tmp_path, monkeypatch)
    created = scenarios.create_scenario(case_id, plug_models.mesh_to_binary_stl(_box_mesh((4, 4, 4), 2)), "mm")
    scenario_id = created["scenario_id"]
    scenarios.update_placement(case_id, scenario_id, {})
    scenarios.record_review(case_id, scenario_id, approved=True, reviewer_role="engineer")
    with pytest.raises(PipelineError, match="locationInMesh"):
        scenario_cfd.prepare_scenario(case_id, scenario_id)


def test_missing_seed_location_is_blocked(tmp_path, monkeypatch):
    case_id, case_root = _prepare_case(tmp_path, monkeypatch)
    created = scenarios.create_scenario(case_id, plug_models.mesh_to_binary_stl(_box_mesh((2, 2, 2), 2)), "mm")
    scenario_id = created["scenario_id"]
    scenarios.update_placement(case_id, scenario_id, {})
    scenarios.record_review(case_id, scenario_id, approved=True, reviewer_role="engineer")
    path = case_root / "cfd" / "system" / "snappyHexMeshDict"
    path.write_text(path.read_text(encoding="utf-8").replace("locationInMesh (0.005 0.005 0.005);\n", ""), encoding="utf-8")
    with pytest.raises(PipelineError, match="缺少可核对的 locationInMesh"):
        scenario_cfd.prepare_scenario(case_id, scenario_id)


def test_old_comparison_is_stale_when_baseline_result_changes(tmp_path, monkeypatch):
    case_id, case_root = _prepare_case(tmp_path, monkeypatch)
    created = scenarios.create_scenario(case_id, plug_models.mesh_to_binary_stl(_box_mesh((2, 2, 2), 2)), "mm")
    scenario_id = created["scenario_id"]
    scenarios.update_placement(case_id, scenario_id, {})
    scenarios.record_review(case_id, scenario_id, approved=True, reviewer_role="engineer")
    (case_root / "cfd" / "run_status.json").write_text('{"status":"ran","run_id":"old"}', encoding="utf-8")
    prepared = scenario_cfd.prepare_scenario(case_id, scenario_id)
    cfd_root = case_root / "scenarios" / scenario_id / "versions" / "v0002" / "cfd"
    gate = lambda *args, **kwargs: {"numeric_ok": True, "metrics": {}}
    monkeypatch.setattr(scenario_cfd.openfoam_runner, "check_result_gate", gate)
    scenario_cfd._comparison(case_id, scenario_id, 2, cfd_root, prepared["prepared"])
    (case_root / "cfd" / "run_status.json").write_text('{"status":"running","run_id":"new"}', encoding="utf-8")
    comparison = scenario_cfd.get_comparison(case_id, scenario_id, version=2)
    assert comparison["status"] == "stale"
