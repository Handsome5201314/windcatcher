from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import cases, cfd_prep
from pipeline.cases import case_dir, create_case
from pipeline.cfd_prep import _openings_by_end
from pipeline.errors import PipelineError


@pytest.mark.parametrize("layers", [False, True])
def test_snappy_dictionary_always_has_openfoam_layer_controls(layers):
    body = cfd_prep._snappy_dict((0.0, 0.0, 0.0), 0.8, layers)
    assert body.count("addLayersControls\n{") == 1
    assert "addLayers\n{" not in body
    assert "minMedialAxisAngle 90;" in body
    assert f"addLayers {'true' if layers else 'false'};" in body


def test_microfluidica_image_uses_esi_flavour_by_default(monkeypatch):
    monkeypatch.setattr("pipeline.openfoam_runner.configured_image", lambda: "microfluidica/openfoam:latest")
    assert cfd_prep._default_openfoam_flavour() == "esi"


def test_generated_cfd_solver_uses_stable_initial_discretization():
    # The generated templates are validated through the source contract here;
    # this guards against reintroducing the unstable high-order transport scheme.
    source = Path(cfd_prep.__file__).read_text(encoding="utf-8")
    assert "div(phi,U)      bounded Gauss upwind;" in source
    assert "U               0.4;" in source
    assert "p               0.2;" in source


@pytest.mark.parametrize("inlet_patches", [("inlet",), ("inlet_left", "inlet_right")])
def test_surface_monitors_use_selection_syntax_for_openfoam_flavour(inlet_patches):
    names = ("inletFlowRate", "outletFlowRate", "wallFlowRate", "inletPressure", "outletPressure")
    template = "functions\n{\n" + "".join(
        f"    {name}\n    {{\n        type            surfaceFieldValue;\n"
        f"        select          patch;\n        patch           {name.split('FlowRate')[0].split('Pressure')[0].lower()};\n"
        "    }\n"
        for name in names
    ) + "}\n"

    esi = cfd_prep._control_dict_for_inlets(template, inlet_patches, "esi")
    org = cfd_prep._control_dict_for_inlets(template, inlet_patches, "org")
    count = 7 if len(inlet_patches) == 2 else 5
    assert esi.count("regionType      patch;") == count
    assert esi.count("name            ") == count
    assert "select          patch;" not in esi
    assert org.count("select          patch;") == count
    assert "regionType" not in org
    if len(inlet_patches) == 2:
        assert "name            inlet_left;" in esi
        assert "name            inlet_right;" in esi


def test_tiny_openings_are_not_used_as_boundary_conditions():
    geom_qc = {
        "openings": [
            {"end": "anterior", "area_mm2": 0.43},
            {"end": "anterior", "area_mm2": 8.2},
            {"end": "posterior", "area_mm2": 5.4},
        ]
    }

    anterior, posterior, tiny = _openings_by_end(geom_qc)

    assert [item["area_mm2"] for item in anterior] == [8.2]
    assert [item["area_mm2"] for item in posterior] == [5.4]
    assert [item["area_mm2"] for item in tiny] == [0.43]


def test_full_candidate_with_disconnected_surface_blocks_cfd_prep(tmp_path, monkeypatch):
    monkeypatch.setattr("pipeline.config.CASES_DIR", tmp_path)
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = create_case("synthetic", "<合成测试>", synthetic=True)
    root = case_dir(case_id)
    (root / "surface" / "geom_qc.json").write_text(
        json.dumps(
            {
                "model_scope": "full_candidate",
                "connected_components": 2,
                "verdict": "warn",
                "reasons": ["全候选包含断开分量"],
            }
        ),
        encoding="utf-8",
    )
    (root / "surface" / "surface.json").write_text(
        json.dumps({"params": {"model_scope": "full_candidate"}}),
        encoding="utf-8",
    )

    with pytest.raises(PipelineError) as caught:
        cfd_prep.prepare_case(case_id)

    assert caught.value.status == "blocked"
    assert "断开的表面组件" in caught.value.reason


@pytest.mark.parametrize(
    ("surface_meta", "expected_reason"),
    [
        ({"mask_version": 2, "endplanes": None}, "CT 端面"),
        ({"mask_version": 2, "endplanes": {"source": "automatic", "mask_version": 2}}, "CT 端面"),
        (
            {"mask_version": 1, "endplanes": {"source": "user_confirmed_ct_planes", "mask_version": 2}},
            "版本已过期",
        ),
        (
            {"mask_version": 2, "endplanes": {"source": "user_confirmed_ct_planes", "mask_version": 1}},
            "版本已过期",
        ),
    ],
)
def test_real_case_requires_current_user_confirmed_ct_endplanes(
    tmp_path, monkeypatch, surface_meta, expected_reason
):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = create_case("local_path", "<合成测试>", synthetic=False)
    root = case_dir(case_id)
    (root / "surface" / "geom_qc.json").write_text(
        json.dumps({"model_scope": "main_airway", "verdict": "pass", "openings": []}), encoding="utf-8"
    )
    (root / "surface" / "surface.json").write_text(json.dumps(surface_meta), encoding="utf-8")
    (root / "segment" / "segmentation.json").write_text(
        json.dumps({"mask_version": 2, "need_human_confirm": False,
                    "review_flags": {"external_air_suspected": False}}),
        encoding="utf-8",
    )

    with pytest.raises(PipelineError) as caught:
        cfd_prep.prepare_case(case_id)

    assert caught.value.status == "blocked"
    assert expected_reason in caught.value.reason
    assert not (root / "cfd" / "case_meta.json").exists()


@pytest.mark.parametrize("synthetic", [False, True])
@pytest.mark.parametrize("source", ["user_confirmed_ct_planes", "user_confirmed_oblique_planes"])
def test_current_endplane_gate_passes_to_existing_opening_check(tmp_path, monkeypatch, synthetic, source):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = create_case("synthetic" if synthetic else "local_path", "<合成测试>", synthetic=synthetic)
    root = case_dir(case_id)
    (root / "surface" / "geom_qc.json").write_text(
        json.dumps({"model_scope": "main_airway", "verdict": "pass", "openings": []}), encoding="utf-8"
    )
    surface_meta = (
        {"mask_version": 2, "endplanes": {"source": source, "mask_version": 2}}
        if not synthetic else {"endplanes": None}
    )
    (root / "surface" / "surface.json").write_text(json.dumps(surface_meta), encoding="utf-8")
    (root / "segment" / "segmentation.json").write_text(
        json.dumps({"mask_version": 2, "need_human_confirm": False,
                    "review_flags": {"external_air_suspected": False}}),
        encoding="utf-8",
    )

    with pytest.raises(PipelineError) as caught:
        cfd_prep.prepare_case(case_id)

    assert caught.value.status == "blocked"
    assert "缺少可用开口" in caught.value.reason


@pytest.mark.parametrize("synthetic", [False, True])
@pytest.mark.parametrize(
    ("source", "clip_format"),
    [
        ("user_confirmed_local_oblique_planes", None),
        ("user_confirmed_oblique_planes", "oblique_mesh_clip_local_v2"),
    ],
)
def test_local_single_branch_geometry_never_enters_bilateral_cfd_prep(
    tmp_path, monkeypatch, synthetic, source, clip_format,
):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = create_case("synthetic" if synthetic else "local_path", "<合成测试>", synthetic=synthetic)
    root = case_dir(case_id)
    (root / "surface" / "geom_qc.json").write_text(
        json.dumps({"model_scope": "main_airway", "verdict": "pass",
                    "openings": [{"end": "anterior", "area_mm2": 20.0},
                                 {"end": "posterior", "area_mm2": 30.0}]}),
        encoding="utf-8",
    )
    (root / "surface" / "surface.json").write_text(json.dumps({
        "mask_version": 2,
        "endplanes": {"source": source, "mask_version": 2},
        "oblique_clip": {"format": clip_format} if clip_format else None,
    }), encoding="utf-8")
    (root / "segment" / "segmentation.json").write_text(
        json.dumps({"mask_version": 2, "need_human_confirm": False,
                    "review_flags": {"external_air_suspected": False}}),
        encoding="utf-8",
    )

    with pytest.raises(PipelineError) as caught:
        cfd_prep.prepare_case(case_id)

    assert caught.value.status == "blocked"
    assert "单支段几何" in caught.value.reason
    assert not (root / "cfd" / "case_meta.json").exists()
    assert not (root / "cfd" / "constant" / "triSurface" / "inlet.stl").exists()
