from __future__ import annotations

import json

import pytest

from pipeline import cases, cfd_prep
from pipeline.cases import case_dir, create_case
from pipeline.cfd_prep import _openings_by_end
from pipeline.errors import PipelineError


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
def test_current_endplane_gate_passes_to_existing_opening_check(tmp_path, monkeypatch, synthetic):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = create_case("synthetic" if synthetic else "local_path", "<合成测试>", synthetic=synthetic)
    root = case_dir(case_id)
    (root / "surface" / "geom_qc.json").write_text(
        json.dumps({"model_scope": "main_airway", "verdict": "pass", "openings": []}), encoding="utf-8"
    )
    surface_meta = (
        {"mask_version": 2, "endplanes": {"source": "user_confirmed_ct_planes", "mask_version": 2}}
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
