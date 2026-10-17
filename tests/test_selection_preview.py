from __future__ import annotations

import asyncio
import hashlib
import json
import struct

import numpy as np
import pytest
from fastapi import HTTPException
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from skimage import measure

from pipeline import cases, endplanes, selection_preview
from pipeline.errors import PipelineError
from pipeline.surface import write_binary_stl
from pipeline.volume import Volume, load_volume, save_volume
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


def _sorted_rows(values):
    keys = np.round(values, decimals=6)
    return values[np.lexsort(tuple(keys[:, axis] for axis in reversed(range(values.shape[1]))))]


def _triangle_coordinates(vertices, faces):
    triangles = np.asarray([_sorted_rows(triangle) for triangle in vertices[faces]])
    return _sorted_rows(triangles.reshape(len(triangles), 9))


@pytest.mark.parametrize("ap_axis", [0, 1, 2])
def test_selection_mesh_matches_native_voxels_and_rotated_lps_coordinates(ap_axis):
    mask = np.zeros((16, 20, 18), dtype=np.uint8)
    mask[3:12, 4:15, 5:12] = 1
    # A one-voxel branch and an indentation must keep their original boundaries.
    mask[7, 9, 12:16] = 1
    mask[3:7, 8, 5] = 0
    angle = np.deg2rad(31.0)
    volume = Volume(
        array=np.zeros_like(mask, dtype=np.float32),
        spacing=(0.625, 0.488, 1.1),
        origin=(-21.25, 18.5, 73.0),
        axis_dirs=((0.0, 0.0, -1.0), (np.cos(angle), np.sin(angle), 0.0),
                   (-np.sin(angle), np.cos(angle), 0.0)),
        meta={},
    )
    reference_vertices, reference_faces, _, _ = measure.marching_cubes(
        np.pad(mask, 1), level=0.5, allow_degenerate=False,
    )
    # The reference uses the full original array, independent of bbox cropping.
    expected = np.asarray(volume.origin) + ((reference_vertices - 1) * volume.spacing) @ np.asarray(volume.axis_dirs)

    vertices, faces = selection_preview._preview_mesh(mask, volume, ap_axis)

    assert vertices.shape == expected.shape
    assert faces.shape == reference_faces.shape
    np.testing.assert_allclose(_sorted_rows(vertices), _sorted_rows(expected), atol=1e-6, rtol=0)
    np.testing.assert_allclose(
        _triangle_coordinates(vertices, faces), _triangle_coordinates(expected, reference_faces),
        atol=1e-6, rtol=0,
    )


def test_selection_mesh_does_not_bridge_one_voxel_gap(confirmed_case):
    case_id, _root, _seg = confirmed_case
    volume = load_volume(case_id)
    mask = np.zeros(volume.shape, dtype=np.uint8)
    mask[2:10, 5:26, 4:7] = 1
    mask[2:10, 5:26, 8:11] = 1

    vertices, faces = selection_preview._preview_mesh(mask, volume, 1)
    edges = np.concatenate((faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]))
    adjacency = coo_matrix((np.ones(len(edges)), (edges[:, 0], edges[:, 1])), shape=(len(vertices), len(vertices)))

    assert connected_components(adjacency, directed=False, return_labels=False) == 2


def test_selection_model_does_not_reuse_legacy_downsampled_cache(confirmed_case):
    case_id, root, _seg = confirmed_case
    stamp = tuple((path.stat().st_size, path.stat().st_mtime_ns) for path in (
        root / "segment" / "mask.npy", root / "segment" / "segmentation.json", root / "volume" / "meta.json",
    ))
    legacy_fingerprint = hashlib.sha256(json.dumps(("main_airway", 3, stamp), separators=(",", ":")).encode()).hexdigest()[:16]
    legacy_path = root / "preview" / f"selection-model-main_airway-3-{legacy_fingerprint}.stl"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    coarse_mask = np.load(root / "segment" / "mask.npy")[::2, :, ::2]
    legacy_vertices, legacy_faces, _, _ = measure.marching_cubes(np.pad(coarse_mask, 1), level=0.5)
    write_binary_stl(legacy_path, legacy_vertices, legacy_faces)
    legacy_bytes = legacy_path.read_bytes()

    path, version = selection_preview.selection_model(case_id, "main_airway", 3)

    assert version == 3
    assert path != legacy_path
    assert _stl_faces(path) > _stl_faces(legacy_path)
    assert legacy_path.read_bytes() == legacy_bytes


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


def test_selection_model_memory_failure_is_readable_and_can_retry_without_coarsening(confirmed_case, monkeypatch):
    case_id, root, _seg = confirmed_case
    before = cases.get_case(case_id)

    def out_of_memory(*_args, **_kwargs):
        raise MemoryError("native mesh allocation failed")

    with monkeypatch.context() as patch:
        patch.setattr(selection_preview.measure, "marching_cubes", out_of_memory)
        with pytest.raises(HTTPException) as caught:
            asyncio.run(api_selection_model(case_id, "main_airway", 3))

    assert caught.value.status_code == 400
    assert caught.value.detail["status"] == "failed"
    assert "内存不足" in caught.value.detail["reason"]
    assert "未降低模型精度" in caught.value.detail["reason"]
    assert caught.value.detail["remediation"]
    assert not list((root / "preview").glob("*.stl"))
    assert cases.get_case(case_id) == before

    response = asyncio.run(api_selection_model(case_id, "main_airway", 3))
    assert response.media_type == "model/stl"
    assert _stl_faces(response.path) > 0
    assert cases.get_case(case_id) == before


def _sample_planes():
    return (
        {"point_lps_mm": [12.0, -13.0, 30.0], "normal_lps": [0.0, -1.0, 0.2]},
        {"point_lps_mm": [12.0, 8.0, 30.0], "normal_lps": [0.0, 1.0, 0.0]},
    )


def test_single_endplane_can_be_saved_before_the_other_and_blocks_modeling(confirmed_case):
    case_id, root, seg = confirmed_case
    anterior, posterior = _sample_planes()
    first = endplanes.save_single_endplane(case_id, "main_airway", "anterior", anterior, 3)
    assert first["anterior_plane"]["point_lps_mm"] == anterior["point_lps_mm"]
    assert first["posterior_plane"] is None
    assert first["complete"] is False
    assert endplanes.get_endplanes(case_id)["saved"]["anterior_plane"] == first["anterior_plane"]
    from pipeline.volume import load_volume
    with pytest.raises(PipelineError, match="尚未.*保存|联合检查"):
        endplanes.current_plan(case_id, "main_airway", seg, load_volume(case_id), np.load(root / "segment" / "mask.npy"))
    second = endplanes.save_single_endplane(case_id, "main_airway", "posterior", posterior, 3)
    assert second["anterior_plane"] == first["anterior_plane"]
    assert second["posterior_plane"] == posterior
    assert second["complete"] is True


@pytest.mark.parametrize("side", ["anterior", "posterior"])
def test_saving_one_end_does_not_replace_the_saved_opposite_end(confirmed_case, side):
    case_id, _root, _seg = confirmed_case
    anterior, posterior = _sample_planes()
    original = endplanes.save_oblique_endplanes(case_id, "main_airway", anterior, posterior, 3)
    replacement = {"point_lps_mm": [12.0, -10.0 if side == "anterior" else 5.0, 30.0], "normal_lps": [0.0, -1.0 if side == "anterior" else 1.0, 0.0]}
    saved = endplanes.save_single_endplane(case_id, "main_airway", side, replacement, 3)
    opposite = "posterior" if side == "anterior" else "anterior"
    assert saved[f"{opposite}_plane"] == original[f"{opposite}_plane"]
    assert saved[f"{side}_plane"] == replacement
    assert saved["complete"] is True


def test_independent_save_records_pair_conflict_without_losing_either_end(confirmed_case):
    case_id, _root, _seg = confirmed_case
    anterior, posterior = _sample_planes()
    endplanes.save_single_endplane(case_id, "main_airway", "anterior", anterior, 3)
    crossing = {**posterior, "point_lps_mm": [12.0, -14.0, 30.0]}
    saved = endplanes.save_single_endplane(case_id, "main_airway", "posterior", crossing, 3)
    assert saved["complete"] is False
    assert "前方" in saved["validation_error"]["reason"]
    assert saved["posterior_plane"] == crossing
    corrected = endplanes.save_single_endplane(case_id, "main_airway", "posterior", posterior, 3)
    assert corrected["complete"] is True
    assert "validation_error" not in corrected


def test_single_save_does_not_inherit_opposite_end_from_stale_mask(confirmed_case):
    case_id, root, seg = confirmed_case
    anterior, posterior = _sample_planes()
    endplanes.save_oblique_endplanes(case_id, "main_airway", anterior, posterior, 3)
    seg["mask_version"] = seg["external_air_validation"]["mask_version"] = 4
    (root / "segment" / "segmentation.json").write_text(json.dumps(seg), encoding="utf-8")
    saved = endplanes.save_single_endplane(case_id, "main_airway", "posterior", posterior, 4)
    assert saved["anterior_plane"] is None
    assert saved["complete"] is False
    with pytest.raises(PipelineError, match="版本已变化"):
        endplanes.save_single_endplane(case_id, "main_airway", "anterior", anterior, 3)


def test_single_save_rejects_invalid_normal_without_replacing_saved_data(confirmed_case):
    case_id, root, _seg = confirmed_case
    anterior, _posterior = _sample_planes()
    endplanes.save_single_endplane(case_id, "main_airway", "anterior", anterior, 3)
    path = root / "segment" / "endplanes.json"
    before = path.read_bytes()
    with pytest.raises(PipelineError, match="法向"):
        endplanes.save_single_endplane(case_id, "main_airway", "posterior", anterior, 3)
    assert path.read_bytes() == before


def test_single_endplane_api_and_legacy_ct_plan_preserve_the_other_side(confirmed_case):
    from server import SingleEndplaneBody, api_save_single_endplane
    case_id, _root, _seg = confirmed_case
    endplanes.save_endplanes(case_id, "main_airway", 22, 8, 3)
    anterior, _posterior = _sample_planes()
    result = asyncio.run(api_save_single_endplane(case_id, "anterior", SingleEndplaneBody(
        scope="main_airway", mask_version=3, plane=anterior,
    )))
    assert result["posterior_plane"]["point_lps_mm"] == [12.0, 8.0, 30.0]
    assert result["complete"] is True
    with pytest.raises(HTTPException) as caught:
        asyncio.run(api_save_single_endplane(case_id, "invalid", SingleEndplaneBody(
            scope="main_airway", mask_version=3, plane=anterior,
        )))
    assert caught.value.status_code == 400


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
