from __future__ import annotations

import asyncio
import json
import struct

import numpy as np
import pytest
from fastapi import HTTPException

from pipeline import cases, endplanes, selection_preview
from pipeline.errors import PipelineError
from pipeline.volume import Volume, save_volume
from server import EndplanesBody, api_preview_oblique_endplanes, api_save_endplanes, api_selection_model


@pytest.fixture
def confirmed_case(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = cases.create_case("local_path", "<synthetic CT geometry>", synthetic=False)
    root = cases.case_dir(case_id)
    shape = (12, 30, 14)
    save_volume(
        case_id,
        Volume(
            array=np.zeros(shape, dtype=np.float32),
            spacing=(0.75, 1.5, 0.6),
            origin=(12.0, 20.0, 30.0),
            axis_dirs=((0.0, 0.0, 1.0), (0.0, -1.0, 0.0), (1.0, 0.0, 0.0)),
            meta={"shape": list(shape)},
        ),
    )
    mask = np.zeros(shape, dtype=np.uint8)
    mask[2:10, 5:26, 4:11] = 1
    candidate = mask.copy()
    candidate[2:5, 8:12, 10:13] = 1
    np.save(root / "segment" / "mask.npy", mask)
    np.save(root / "segment" / "candidate_mask.npy", candidate)
    seg = {
        "params": {"ap_axis": 1},
        "mask_version": 3,
        "candidate_mask_available": True,
        "candidate_cleaning": {"status": "ok"},
        "need_human_confirm": False,
        "external_air_validation": {
            "mask_version": 3,
            "body_hu_threshold": -300.0,
            "outside_voxels": 0,
            "candidate_outside_voxels": 0,
        },
    }
    (root / "segment" / "segmentation.json").write_text(json.dumps(seg), encoding="utf-8")
    return case_id, root, seg


def _stl_faces(path):
    data = path.read_bytes()
    count = struct.unpack_from("<I", data, 80)[0]
    assert len(data) == 84 + 50 * count
    return count


def test_preview_without_formal_stl_is_cached_and_does_not_change_stages(confirmed_case, monkeypatch):
    case_id, root, _seg = confirmed_case
    before = cases.get_case(case_id)
    assert not (root / "surface" / "airway.stl").exists()

    path, version = selection_preview.selection_model(case_id, "main_airway", 3)

    assert version == 3
    assert path.parent == root / "preview"
    assert _stl_faces(path) > 0
    assert not (root / "surface" / "airway.stl").exists()
    assert not (root / "surface" / "surface.json").exists()
    assert cases.get_case(case_id) == before

    monkeypatch.setattr(selection_preview, "_preview_mesh", lambda *_args: pytest.fail("cache not reused"))
    assert selection_preview.selection_model(case_id, "main_airway", 3)[0] == path


def test_scope_and_mask_version_never_reuse_stale_preview(confirmed_case):
    case_id, root, seg = confirmed_case
    main_path, _ = selection_preview.selection_model(case_id, "main_airway", 3)
    candidate_path, _ = selection_preview.selection_model(case_id, "full_candidate", 3)
    assert candidate_path != main_path
    assert _stl_faces(candidate_path) > 0

    seg["mask_version"] = 4
    seg["external_air_validation"]["mask_version"] = 4
    (root / "segment" / "segmentation.json").write_text(json.dumps(seg), encoding="utf-8")
    with pytest.raises(PipelineError, match="版本已变化"):
        selection_preview.selection_model(case_id, "main_airway", 3)
    new_path, version = selection_preview.selection_model(case_id, "main_airway", 4)
    assert version == 4
    assert new_path != main_path
    assert _stl_faces(new_path) > 0


def test_preview_requires_confirmed_mask_and_keeps_formal_stl(confirmed_case):
    case_id, root, seg = confirmed_case
    formal_stl = root / "surface" / "airway.stl"
    formal_stl.write_bytes(b"unchanged formal model")
    selection_preview.selection_model(case_id, "main_airway", 3)
    assert formal_stl.read_bytes() == b"unchanged formal model"

    seg["need_human_confirm"] = True
    (root / "segment" / "segmentation.json").write_text(json.dumps(seg), encoding="utf-8")
    with pytest.raises(PipelineError, match="人工确认"):
        selection_preview.selection_model(case_id, "main_airway", 3)
    assert formal_stl.read_bytes() == b"unchanged formal model"


def test_endplane_projection_metadata_matches_lps_mm(confirmed_case):
    case_id, _root, _seg = confirmed_case
    profile = endplanes.get_endplanes(case_id)
    assert profile["ap_origin_lps_mm"] == [12.0, 20.0, 30.0]
    assert profile["ap_direction_lps"] == [0.0, -1.0, 0.0]
    assert profile["ap_spacing_mm"] == 1.5
    point = np.array([12.0, 3.5, 30.0])
    projected = np.dot(point - profile["ap_origin_lps_mm"], profile["ap_direction_lps"]) / profile["ap_spacing_mm"]
    assert projected == 11
    path, _ = selection_preview.selection_model(case_id, "main_airway", 3)
    records = np.frombuffer(path.read_bytes()[84:], dtype=np.dtype([
        ("normal", "<f4", (3,)), ("v0", "<f4", (3,)),
        ("v1", "<f4", (3,)), ("v2", "<f4", (3,)), ("attr", "<u2"),
    ]))
    verts = records["v0"].astype(float)
    indices = (verts - profile["ap_origin_lps_mm"]) @ profile["ap_direction_lps"] / profile["ap_spacing_mm"]
    assert 4 <= indices.min() < 6
    assert 24 < indices.max() <= 26


def test_selection_model_api_reports_binary_and_readable_errors(confirmed_case):
    case_id, root, _seg = confirmed_case
    response = asyncio.run(api_selection_model(case_id, "main_airway", 3))
    assert response.media_type == "model/stl"
    assert response.headers["x-mask-version"] == "3"
    assert response.headers["x-model-scope"] == "main_airway"
    assert response.path.parent == root / "preview"
    with pytest.raises(HTTPException) as caught:
        asyncio.run(api_selection_model(case_id, "invalid", 3))
    assert caught.value.status_code == 400
    assert "建模范围无效" in caught.value.detail["reason"]


def _sample_planes():
    return (
        {"point_lps_mm": [12.0, -13.0, 30.0], "normal_lps": [0.0, -1.0, 0.2]},
        {"point_lps_mm": [12.0, 8.0, 30.0], "normal_lps": [0.0, 1.0, 0.0]},
    )


def test_oblique_plane_save_keeps_mask_and_invalidates_formal_geometry(confirmed_case):
    case_id, root, _seg = confirmed_case
    anterior, posterior = _sample_planes()
    original = np.load(root / "segment" / "mask.npy").copy()
    (root / "surface" / "airway.stl").write_bytes(b"old")
    cases.update_stage(case_id, "surface", "ok", artifacts=["surface/airway.stl"])

    plan = endplanes.save_oblique_endplanes(case_id, "main_airway", anterior, posterior, 3)

    assert plan["format"] == "oblique_planes_v1"
    assert plan["mask_version"] == 3
    assert plan["retained_voxels"] < plan["source_voxels"]
    assert plan["anterior_area_estimate_mm2"] >= 1
    assert plan["posterior_area_estimate_mm2"] >= 1
    np.testing.assert_array_equal(np.load(root / "segment" / "mask.npy"), original)
    assert not (root / "surface" / "airway.stl").exists()
    assert cases.get_case(case_id)["stages"]["surface"]["status"] == "pending"
    assert endplanes.get_endplanes(case_id)["saved"]["format"] == "oblique_planes_v1"
    assert json.loads((root / "segment" / "endplanes.json").read_text(encoding="utf-8"))["main_airway"] == plan


def test_oblique_api_accepts_plane_pair_and_rejects_mixed_contract(confirmed_case):
    case_id, _root, _seg = confirmed_case
    anterior, posterior = _sample_planes()
    saved = asyncio.run(api_save_endplanes(case_id, EndplanesBody(
        scope="main_airway", mask_version=3, anterior_plane=anterior, posterior_plane=posterior,
    )))
    assert saved["format"] == "oblique_planes_v1"
    with pytest.raises(HTTPException) as caught:
        asyncio.run(api_save_endplanes(case_id, EndplanesBody(
            scope="main_airway", mask_version=3, anterior_plane=anterior, posterior_index=8,
        )))
    assert caught.value.status_code == 400
    assert "参数不完整" in caught.value.detail["reason"]


def test_oblique_preview_is_read_only_and_rejects_bad_plane(confirmed_case):
    case_id, root, _seg = confirmed_case
    anterior, posterior = _sample_planes()
    before = cases.get_case(case_id)
    result = asyncio.run(api_preview_oblique_endplanes(case_id, EndplanesBody(
        scope="main_airway", mask_version=3, anterior_plane=anterior, posterior_plane=posterior,
    )))
    assert result["can_save"] is True
    assert result["format"] == "oblique_planes_v1"
    assert result["anterior_area_mm2"] >= 1
    assert result["posterior_area_mm2"] >= 1
    assert result["retained_volume_ml"] > 0
    assert result["retained_components"] >= 1
    assert not (root / "segment" / "endplanes.json").exists()
    assert cases.get_case(case_id) == before

    bad = {**anterior, "normal_lps": [0.0, 1.0, 0.0]}
    with pytest.raises(HTTPException) as caught:
        asyncio.run(api_preview_oblique_endplanes(case_id, EndplanesBody(
            scope="main_airway", mask_version=3, anterior_plane=bad, posterior_plane=posterior,
        )))
    assert caught.value.status_code == 400
    assert caught.value.detail["reason"]
    assert not (root / "segment" / "endplanes.json").exists()
