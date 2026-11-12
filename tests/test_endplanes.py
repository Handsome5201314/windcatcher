from __future__ import annotations

import asyncio
import json
import subprocess
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest

from pipeline import cases, endplanes, local_preview, oblique, segmentation, surface
from pipeline.errors import PipelineError
from pipeline.volume import Volume, load_volume, save_volume
from fastapi import HTTPException
from server import (
    EndplanesBody, SingleEndplaneBody, api_get_endplanes, api_preview_oblique_endplanes,
    api_preview_local_inlet_model, api_save_endplanes, api_save_single_endplane,
)
from server import EndplaneSectionBody, api_endplane_section


@pytest.fixture
def confirmed_case(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = cases.create_case("local_path", "<合成 CT 测试>", synthetic=False)
    root = cases.case_dir(case_id)
    shape = (8, 30, 12)
    save_volume(
        case_id,
        Volume(
            array=np.zeros(shape, dtype=np.float32),
            spacing=(1.0, 1.0, 1.0),
            origin=(0.0, 0.0, 0.0),
            axis_dirs=((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
            meta={"shape": list(shape)},
        ),
    )
    mask = np.zeros(shape, dtype=np.uint8)
    mask[2:6, 5:25, 4:8] = 1
    mask[2, 4, 5] = 1     # 旧自动端面只有一个体素，不可用于 CFD
    mask[2, 25, 5] = 1
    np.save(root / "segment" / "mask.npy", mask)
    np.save(root / "segment" / "candidate_mask.npy", mask)
    record = {
        "params": {"ap_axis": 1, "hu_high": -300.0},
        "mask_version": 3,
        "candidate_mask_available": True,
        "candidate_cleaning": {"status": "ok"},
        "need_human_confirm": False,
        "review_flags": {"external_air_suspected": False},
        "human_review": {"status": "已确认"},
        "external_air_validation": {
            "mask_version": 3,
            "body_hu_threshold": -300.0,
            "outside_voxels": 0,
            "candidate_outside_voxels": 0,
        },
    }
    (root / "segment" / "segmentation.json").write_text(json.dumps(record), encoding="utf-8")
    return case_id, root, record, mask


def test_get_profile_is_read_only_and_uses_ct_anterior_direction(confirmed_case):
    case_id, root, _record, _mask = confirmed_case
    before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))

    response = endplanes.get_endplanes(case_id, "main_airway")

    assert response["scope"] == "main_airway"
    assert response["axis"] == 1
    assert response["view"] == "coronal"
    assert response["count"] == len(response["areas_mm2"]) == 30
    assert response["anterior_sign"] == 1
    assert response["mask_version"] == 3
    assert response["areas_mm2"][4] == 1.0
    assert response["areas_mm2"][20] == 16.0
    assert response["saved"] is None
    assert response["can_save"] is True
    assert sorted(path.relative_to(root).as_posix() for path in root.rglob("*")) == before


def test_draft_section_is_read_only_and_versioned(confirmed_case):
    case_id, root, _record, _mask = confirmed_case
    before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    response = asyncio.run(api_endplane_section(case_id, EndplaneSectionBody(
        scope="main_airway", mask_version=3,
        plane={"point_lps_mm": [4, -10, 6], "normal_lps": [0, -1, 0.4]}, span_mm=30,
    )))
    assert response.media_type == "image/png"
    assert response.body.startswith(b"\x89PNG")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-mask-version"] == "3"
    assert response.headers["x-model-scope"] == "main_airway"
    after = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert after == before


def test_draft_section_cannot_bypass_version_or_human_review(confirmed_case):
    case_id, root, record, _mask = confirmed_case
    plane = {"point_lps_mm": [4, -10, 6], "normal_lps": [0, -1, 0]}
    with pytest.raises(PipelineError, match="版本已变化"):
        endplanes.render_endplane_section(case_id, "main_airway", plane, 2, 30)
    record["need_human_confirm"] = True
    (root / "segment" / "segmentation.json").write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(PipelineError, match="人工确认"):
        endplanes.render_endplane_section(case_id, "main_airway", plane, 3, 30)


@pytest.mark.parametrize(
    "front,back,expected",
    [(8, 20, "前方"), (20, 20, "重合"), (26, 8, "太小")],
)
def test_save_rejects_wrong_order_same_plane_and_tiny_old_endpoint(confirmed_case, front, back, expected):
    case_id, root, _record, _mask = confirmed_case
    with pytest.raises(PipelineError, match=expected):
        endplanes.save_endplanes(case_id, "main_airway", front, back, 3)
    assert not (root / "segment" / "endplanes.json").exists()


def test_technical_area_cutoff_accepts_exactly_one_square_mm(confirmed_case):
    case_id, _root, _record, _mask = confirmed_case
    plan = endplanes.save_endplanes(case_id, "main_airway", 25, 8, 3)
    assert plan["anterior_area_mm2"] == 1.0


def test_save_rejects_index_out_of_range_and_disconnected_main_endpoint(confirmed_case):
    case_id, root, _record, mask = confirmed_case
    with pytest.raises(PipelineError, match="超出 CT 范围"):
        endplanes.save_endplanes(case_id, "main_airway", 30, 8, 3)
    with pytest.raises(PipelineError, match="必须是整数"):
        endplanes.save_endplanes(case_id, "main_airway", True, 8, 3)

    mask[2:4, 27, 4:6] = 1  # 面积足够，但与后方主气道断开。
    np.save(root / "segment" / "mask.npy", mask)
    with pytest.raises(PipelineError, match="没有同时落在"):
        endplanes.save_endplanes(case_id, "main_airway", 27, 8, 3)


def test_save_rejects_stale_and_unconfirmed_mask(confirmed_case):
    case_id, root, record, _mask = confirmed_case
    with pytest.raises(PipelineError, match="版本已变化"):
        endplanes.save_endplanes(case_id, "main_airway", 20, 8, 2)
    record["need_human_confirm"] = True
    (root / "segment" / "segmentation.json").write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(PipelineError, match="尚未清理并人工确认"):
        endplanes.save_endplanes(case_id, "main_airway", 20, 8, 3)


def test_legacy_record_without_mask_version_is_readable_but_cannot_save(confirmed_case):
    case_id, root, record, _mask = confirmed_case
    record.pop("mask_version")
    (root / "segment" / "segmentation.json").write_text(json.dumps(record), encoding="utf-8")

    profile = endplanes.get_endplanes(case_id)
    assert len(profile["areas_mm2"]) == profile["count"]
    assert profile["mask_version"] is None
    assert profile["migration_required"] is True
    assert profile["can_save"] is False
    with pytest.raises(PipelineError, match="版本已变化"):
        endplanes.save_endplanes(case_id, "main_airway", 20, 8, 0)


def test_failure_to_invalidate_keeps_previous_plan_and_geometry(confirmed_case, monkeypatch):
    case_id, root, _record, _mask = confirmed_case
    existing = {"main_airway": {"mask_version": 3, "anterior_index": 19, "posterior_index": 9}}
    plan_path = root / "segment" / "endplanes.json"
    plan_path.write_text(json.dumps(existing), encoding="utf-8")
    (root / "surface" / "airway.stl").write_bytes(b"previous")

    def fail_invalidation(*_args):
        raise OSError("synthetic locked STL")

    monkeypatch.setattr(endplanes, "invalidate_downstream", fail_invalidation)
    with pytest.raises(PipelineError, match="没有保存"):
        endplanes.save_endplanes(case_id, "main_airway", 20, 8, 3)
    assert json.loads(plan_path.read_text(encoding="utf-8")) == existing
    assert (root / "surface" / "airway.stl").read_bytes() == b"previous"


def test_save_invalidates_old_geometry_and_surface_uses_clipped_copy(confirmed_case):
    case_id, root, _record, mask = confirmed_case
    cases.update_stage(case_id, "surface", "ok", artifacts=["surface/airway.stl"])
    (root / "surface" / "airway.stl").write_bytes(b"old")

    with pytest.raises(PipelineError, match="还没有人工确认"):
        surface.run_surface(case_id, {"model_scope": "main_airway"})
    plan = endplanes.save_endplanes(case_id, "main_airway", 20, 8, 3)
    assert plan["anterior_index"] == 20
    assert plan["posterior_index"] == 8
    assert not (root / "surface" / "airway.stl").exists()
    assert cases.get_case(case_id)["stages"]["surface"]["status"] == "pending"

    result = surface.run_surface(case_id, {"model_scope": "main_airway"})
    effective = np.load(root / "surface" / "effective_mask.npy")
    assert result["faces"] > 0
    assert result["endplanes"]["source"] == "user_confirmed_ct_planes"
    assert result["mask_version"] == 3
    assert not effective[:, :8, :].any()
    assert not effective[:, 21:, :].any()
    assert effective[:, 8:21, :].any()
    np.testing.assert_array_equal(np.load(root / "segment" / "mask.npy"), mask)
    assert json.loads((root / "surface" / "surface.json").read_text(encoding="utf-8"))["endplanes"] == result["endplanes"]


def test_saved_plan_is_per_scope_and_stale_plan_blocks_surface(confirmed_case):
    case_id, root, record, _mask = confirmed_case
    endplanes.save_endplanes(case_id, "main_airway", 20, 8, 3)
    endplanes.save_endplanes(case_id, "full_candidate", 20, 8, 3)
    plans = json.loads((root / "segment" / "endplanes.json").read_text(encoding="utf-8"))
    assert set(plans) == {"main_airway", "full_candidate"}

    record["mask_version"] = 4
    record["external_air_validation"]["mask_version"] = 4
    (root / "segment" / "segmentation.json").write_text(json.dumps(record), encoding="utf-8")
    assert endplanes.get_endplanes(case_id, "main_airway")["saved"]["stale"] is True
    with pytest.raises(PipelineError, match="不匹配"):
        surface.run_surface(case_id, {"model_scope": "main_airway"})


def test_no_plan_is_still_allowed_for_synthetic_legacy_path(confirmed_case):
    case_id, root, _record, _mask = confirmed_case
    case = cases.get_case(case_id)
    case["synthetic"] = True
    cases._write_case(case_id, case)

    result = surface.run_surface(case_id, {"model_scope": "main_airway"})

    assert result["endplanes"] is None
    assert (root / "surface" / "effective_mask.npy").exists()


def test_switching_from_saved_planes_to_synthetic_legacy_refreshes_effective_mask(confirmed_case):
    case_id, root, _record, _mask = confirmed_case
    case = cases.get_case(case_id)
    case["synthetic"] = True
    cases._write_case(case_id, case)
    endplanes.save_endplanes(case_id, "main_airway", 20, 8, 3)
    surface.run_surface(case_id, {"model_scope": "main_airway"})
    clipped = np.load(root / "surface" / "effective_mask.npy")
    (root / "segment" / "endplanes.json").unlink()

    surface.run_surface(case_id, {"model_scope": "main_airway"})
    restored = np.load(root / "surface" / "effective_mask.npy")
    assert restored.sum() > clipped.sum()


def test_positive_and_negative_ct_axis_directions(confirmed_case):
    case_id, root, _record, _mask = confirmed_case
    meta_path = root / "volume" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["axis_dirs"][1] = [0.0, 1.0, 0.0]
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    profile = endplanes.get_endplanes(case_id)
    assert profile["anterior_sign"] == -1
    with pytest.raises(PipelineError, match="前方"):
        endplanes.save_endplanes(case_id, "main_airway", 20, 8, 3)
    plan = endplanes.save_endplanes(case_id, "main_airway", 8, 20, 3)
    assert plan["anterior_index"] == 8
    assert plan["posterior_index"] == 20


def test_api_get_and_post_share_zero_based_contract(confirmed_case):
    case_id, _root, _record, _mask = confirmed_case
    profile = asyncio.run(api_get_endplanes(case_id, "main_airway"))
    assert profile["view"] == "coronal"
    assert len(profile["areas_mm2"]) == profile["count"]
    saved = asyncio.run(api_save_endplanes(
        case_id,
        EndplanesBody(scope="main_airway", anterior_index=20, posterior_index=8, mask_version=3),
    ))
    assert saved["anterior_index"] == 20
    assert asyncio.run(api_get_endplanes(case_id, "main_airway"))["saved"]["stale"] is False
    with pytest.raises(HTTPException) as caught:
        asyncio.run(api_get_endplanes(case_id, "invalid"))
    assert caught.value.status_code == 400


def _local_inlet_request(**overrides):
    values = {
        "format": endplanes.LOCAL_INLET_FORMAT, "scope": "main_airway", "mask_version": 3,
        "anterior_plane": {"point_lps_mm": [4, -20, 6], "normal_lps": [0, -1, 0]},
        "posterior_plane": {"point_lps_mm": [4, -8, 6], "normal_lps": [0, 1, 0]},
        "anterior_seed_lps_mm": [4, -23, 6],
    }
    values.update(overrides)
    return EndplanesBody(**values)


def _local_inlet_validation(_mask, _volume, anterior, posterior, seed, *, main_airway):
    assert main_airway is True
    return {
        "anterior_plane": anterior, "posterior_plane": posterior,
        "anterior_seed_lps_mm": seed, "source_voxels": 320, "retained_voxels": 280,
        "retained_components": 1, "retained_volume_ml": 0.28,
        "anterior_deleted_voxels": 40, "anterior_unselected_positive_voxels": 8,
        "anterior_unselected_deleted_voxels": 0, "anterior_candidate_components": 2,
        "anterior_opening_components": 1, "anterior_area_estimate_mm2": 16.0,
        "posterior_area_estimate_mm2": 16.0,
    }


def test_local_inlet_v2_previews_without_writing_and_saves_exact_seed(confirmed_case, monkeypatch):
    case_id, root, record, mask = confirmed_case
    monkeypatch.setattr(oblique, "validate_local_inlet_pair", _local_inlet_validation, raising=False)
    mesh_stats = {"format": "oblique_mesh_clip_local_v2", "closed_caps": True,
                  "inlet_loops": 1, "outlet_loops": 1,
                  "inlet_cap_faces": 1, "outlet_cap_faces": 1}
    monkeypatch.setattr(local_preview, "build_local_inlet_mesh", lambda *_args: (
        np.asarray([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=float),
        np.asarray([[0, 1, 2]], dtype=np.int32), mesh_stats,
    ))
    request = _local_inlet_request()
    profile = asyncio.run(api_get_endplanes(case_id, "main_airway"))
    assert endplanes.LOCAL_INLET_FORMAT in profile["supported_formats"]
    preview = asyncio.run(api_preview_oblique_endplanes(case_id, request))
    assert preview["format"] == endplanes.LOCAL_INLET_FORMAT
    assert preview["can_save"] is False
    assert preview["can_preview_mesh"] is True
    assert preview["anterior_seed_lps_mm"] == request.anterior_seed_lps_mm
    assert preview["anterior_deleted_voxels"] == 40
    assert preview["anterior_opening_components"] == 1
    assert preview["unselected_region_changed_voxels"] == 0
    assert preview["anterior_area_mm2"] == 16.0
    assert preview["voxel_preflight"]["anterior_opening_components"] == 1
    assert preview["mesh_preflight"] is False
    assert not (root / "segment" / "endplanes.json").exists()

    cases.update_stage(case_id, "surface", "ok", artifacts=["surface/airway.stl"])
    (root / "surface" / "airway.stl").write_bytes(b"old")
    saved = asyncio.run(api_save_endplanes(case_id, request))
    assert saved["format"] == endplanes.LOCAL_INLET_FORMAT
    assert saved["complete"] is True
    assert saved["anterior_seed_lps_mm"] == request.anterior_seed_lps_mm
    assert not (root / "surface" / "airway.stl").exists()
    assert endplanes.current_plan(case_id, "main_airway", record, load_volume(case_id),
                                  mask)["format"] == endplanes.LOCAL_INLET_FORMAT
    assert asyncio.run(api_get_endplanes(case_id, "main_airway"))["saved"]["format"] == endplanes.LOCAL_INLET_FORMAT

    with pytest.raises(PipelineError, match="不能覆盖"):
        endplanes.save_endplanes(case_id, "main_airway", 20, 8, 3)
    with pytest.raises(PipelineError, match="不能覆盖"):
        endplanes.save_single_endplane(case_id, "main_airway", "posterior", request.posterior_plane, 3)
    assert endplanes.load_plan(case_id, "main_airway")["anterior_seed_lps_mm"] == request.anterior_seed_lps_mm


def test_local_inlet_model_preview_is_binary_stl_and_has_no_case_side_effects(confirmed_case, monkeypatch):
    case_id, root, _record, _mask = confirmed_case
    monkeypatch.setattr(oblique, "validate_local_inlet_pair", _local_inlet_validation)

    def clip_mesh(vertices, faces, *_args, **_kwargs):
        return vertices, faces, np.asarray([0]), np.asarray([1]), {
            "closed_caps": True, "inlet_loops": 1, "outlet_loops": 1,
            "inlet_cap_faces": 1, "outlet_cap_faces": 1,
        }

    monkeypatch.setattr(surface, "clip_mesh_by_oblique_planes_local", clip_mesh)
    before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    stages_before = cases.get_case(case_id)["stages"].copy()
    response = asyncio.run(api_preview_local_inlet_model(case_id, _local_inlet_request()))
    count = struct.unpack("<I", response.body[80:84])[0]
    assert response.media_type == "model/stl"
    assert count > 0
    assert len(response.body) == 84 + count * 50
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-mask-version"] == "3"
    assert response.headers["x-model-scope"] == "main_airway"
    after = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert after == before
    assert cases.get_case(case_id)["stages"] == stages_before


def test_local_inlet_real_preview_and_saved_surface_share_mesh(confirmed_case):
    case_id, root, _record, _mask = confirmed_case
    request = _local_inlet_request()
    coarse = asyncio.run(api_preview_oblique_endplanes(case_id, request))
    assert coarse["can_preview_mesh"] is True
    draft = asyncio.run(api_preview_local_inlet_model(case_id, request))
    assert len(draft.body) > 84
    saved = asyncio.run(api_save_endplanes(case_id, request))
    assert saved["mesh_preflight"]["closed_caps"] is True
    result = surface.run_surface(case_id, {"model_scope": "main_airway"})
    formal = (root / "surface" / "airway.stl").read_bytes()
    assert result["params"]["extend_ap_edges"] is False
    assert draft.body[80:] == formal[80:]


def test_local_inlet_rejects_mesh_settings_that_differ_from_preview_before_writing(confirmed_case):
    case_id, root, _record, _mask = confirmed_case
    asyncio.run(api_save_endplanes(case_id, _local_inlet_request()))
    (root / "surface" / "airway.stl").write_bytes(b"previous STL")
    before = {path.relative_to(root).as_posix(): path.read_bytes()
              for path in root.rglob("*") if path.is_file()}
    for override, key in (({"smooth": True}, "smooth"),
                          ({"step_size": 2}, "step_size"),
                          ({"decimate_ratio": 0.5}, "decimate_ratio")):
        with pytest.raises(PipelineError, match="与已预览的精确网格不一致") as caught:
            surface.run_surface(case_id, {"model_scope": "main_airway", **override})
        assert caught.value.to_dict()["detail"]["unsupported_parameters"] == [key]
        after = {path.relative_to(root).as_posix(): path.read_bytes()
                 for path in root.rglob("*") if path.is_file()}
        assert after == before


def test_local_inlet_exact_mesh_busy_rejects_preview_and_save_then_recovers(confirmed_case):
    case_id, root, _record, _mask = confirmed_case
    request = _local_inlet_request()
    before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with surface.local_geometry_guard():
        for action in (
            lambda: asyncio.run(api_preview_local_inlet_model(case_id, request)),
            lambda: asyncio.run(api_save_endplanes(case_id, request)),
        ):
            with pytest.raises(HTTPException) as caught:
                action()
            assert caught.value.detail["status"] == "blocked"
            assert "重试" in "".join(caught.value.detail["remediation"])
    after = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert after == before
    assert len(asyncio.run(api_preview_local_inlet_model(case_id, request)).body) > 84


def test_local_inlet_exact_mesh_capacity_blocks_before_clip(confirmed_case, monkeypatch):
    case_id, root, _record, _mask = confirmed_case
    previous = endplanes.save_endplanes(case_id, "main_airway", 20, 8, 3)
    (root / "surface" / "airway.stl").write_bytes(b"previous")
    monkeypatch.setattr(surface, "MAX_LOCAL_INLET_FACES", 1)

    def should_not_clip(*_args, **_kwargs):
        raise AssertionError("capacity gate must run before precise clipping")

    monkeypatch.setattr(surface, "clip_mesh_by_oblique_planes_local", should_not_clip)
    request = _local_inlet_request()
    assert asyncio.run(api_preview_oblique_endplanes(case_id, request))["can_preview_mesh"] is True
    for action in (
        lambda: asyncio.run(api_preview_local_inlet_model(case_id, request)),
        lambda: asyncio.run(api_save_endplanes(case_id, request)),
    ):
        with pytest.raises(HTTPException) as caught:
            action()
        assert caught.value.detail["status"] == "blocked"
        assert caught.value.detail["detail"]["source_faces"] > 1
        assert caught.value.detail["detail"]["local_inlet_face_limit"] == 1
    assert endplanes.load_plan(case_id, "main_airway") == previous
    assert (root / "surface" / "airway.stl").read_bytes() == b"previous"


def test_local_inlet_mask_capacity_blocks_before_marching_cubes(confirmed_case, monkeypatch):
    case_id, root, _record, _mask = confirmed_case
    called = []

    def reject_mask(_mask):
        called.append("mask_gate")
        raise PipelineError("blocked", "合成粗容量超限", ["收紧掩膜后重试"])

    def should_not_mesh(*_args, **_kwargs):
        raise AssertionError("marching cubes must not run after mask capacity rejection")

    monkeypatch.setattr(surface, "ensure_local_mask_capacity", reject_mask)
    monkeypatch.setattr(surface, "mask_to_stl", should_not_mesh)
    before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    for action in (
        lambda: asyncio.run(api_preview_local_inlet_model(case_id, _local_inlet_request())),
        lambda: asyncio.run(api_save_endplanes(case_id, _local_inlet_request())),
    ):
        with pytest.raises(HTTPException, match="合成粗容量超限"):
            action()
    assert called == ["mask_gate", "mask_gate"]
    after = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert after == before


def test_local_inlet_mesh_failure_does_not_replace_plan_or_geometry(confirmed_case, monkeypatch):
    case_id, root, _record, _mask = confirmed_case
    previous = endplanes.save_endplanes(case_id, "main_airway", 20, 8, 3)
    (root / "surface" / "airway.stl").write_bytes(b"previous")
    monkeypatch.setattr(oblique, "validate_local_inlet_pair", _local_inlet_validation)

    def fail_mesh(*_args):
        raise PipelineError("blocked", "局部裁切不水密")

    monkeypatch.setattr(local_preview, "build_local_inlet_mesh", fail_mesh)
    assert asyncio.run(api_preview_oblique_endplanes(case_id, _local_inlet_request()))["can_save"] is False
    for action in (
        lambda: asyncio.run(api_preview_local_inlet_model(case_id, _local_inlet_request())),
        lambda: asyncio.run(api_save_endplanes(case_id, _local_inlet_request())),
    ):
        with pytest.raises(HTTPException, match="局部裁切不水密"):
            action()
    assert endplanes.load_plan(case_id, "main_airway") == previous
    assert (root / "surface" / "airway.stl").read_bytes() == b"previous"


def test_local_inlet_v2_rejects_missing_seed_mixed_modes_and_single_save(confirmed_case):
    case_id, root, _record, _mask = confirmed_case
    invalid = (
        _local_inlet_request(anterior_seed_lps_mm=None),
        _local_inlet_request(anterior_index=20),
        _local_inlet_request(format="oblique_planes_v1"),
    )
    for request in invalid:
        with pytest.raises(HTTPException) as caught:
            asyncio.run(api_preview_oblique_endplanes(case_id, request))
        assert caught.value.status_code == 400
        with pytest.raises(HTTPException) as caught:
            asyncio.run(api_save_endplanes(case_id, request))
        assert caught.value.status_code == 400
    with pytest.raises(HTTPException) as caught:
        asyncio.run(api_save_single_endplane(case_id, "anterior", SingleEndplaneBody(
            scope="main_airway", mask_version=3, plane=_local_inlet_request().anterior_plane,
            format=endplanes.LOCAL_INLET_FORMAT, anterior_seed_lps_mm=[4, -23, 6],
        )))
    assert caught.value.status_code == 400
    assert not (root / "segment" / "endplanes.json").exists()


def test_local_inlet_v2_full_candidate_is_not_advertised_or_accepted(confirmed_case):
    case_id, root, _record, _mask = confirmed_case
    profile = asyncio.run(api_get_endplanes(case_id, "full_candidate"))
    assert endplanes.LOCAL_INLET_FORMAT not in profile["supported_formats"]
    request = _local_inlet_request(scope="full_candidate")
    for action in (
        lambda: asyncio.run(api_preview_oblique_endplanes(case_id, request)),
        lambda: asyncio.run(api_preview_local_inlet_model(case_id, request)),
        lambda: asyncio.run(api_save_endplanes(case_id, request)),
    ):
        with pytest.raises(HTTPException, match="暂仅支持主鼻腔"):
            action()
    assert not (root / "segment" / "endplanes.json").exists()


def test_local_inlet_v2_failed_preflight_preserves_existing_plan_and_geometry(confirmed_case, monkeypatch):
    case_id, root, _record, _mask = confirmed_case
    previous = endplanes.save_endplanes(case_id, "main_airway", 20, 8, 3)
    (root / "surface" / "airway.stl").write_bytes(b"previous")

    def fail_preflight(*_args, **_kwargs):
        raise PipelineError("blocked", "局部入口碰到未选分支")

    monkeypatch.setattr(oblique, "validate_local_inlet_pair", fail_preflight, raising=False)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(api_save_endplanes(case_id, _local_inlet_request()))
    assert caught.value.status_code == 400
    assert endplanes.load_plan(case_id, "main_airway") == previous
    assert (root / "surface" / "airway.stl").read_bytes() == b"previous"


def test_new_segmentation_removes_previous_endplane_choices(confirmed_case, monkeypatch):
    case_id, root, _record, mask = confirmed_case
    endplanes.save_endplanes(case_id, "main_airway", 20, 8, 3)

    def synthetic_segmentation(_volume, _params):
        return {
            "mask": mask.copy(),
            "candidate_mask": mask.copy(),
            "candidate_mask_available": True,
            "candidate_cleaning": {"status": "pending_manual"},
            "params": {"ap_axis": 1, "hu_low": -1024.0, "hu_high": -300.0},
            "metrics": {"volume_ml": 1.0, "ap_length_mm": 20.0},
            "candidate_metrics": {"components_total": 1},
            "review_flags": {"external_air_suspected": False, "mask_boundary_by_axis": [False] * 3},
            "candidate_review_flags": {"external_air_suspected": False, "mask_boundary_by_axis": [False] * 3},
        }

    monkeypatch.setattr(segmentation, "segment_airway", synthetic_segmentation)
    result = segmentation.run_segment(case_id)
    assert result["mask_version"] == 4
    assert not (root / "segment" / "endplanes.json").exists()
    assert endplanes.get_endplanes(case_id)["saved"] is None


def test_saved_endplanes_keep_versioned_corrections_after_mask_refresh(
    confirmed_case, tmp_path, monkeypatch,
):
    from pipeline import endplane_history

    monkeypatch.setenv("WINDCATCHER_LEARNING_DIR", str(tmp_path / "learning"))
    case_id, root, _record, _mask = confirmed_case
    first = endplanes.save_endplanes(case_id, "main_airway", 20, 8, 3)
    second = endplanes.save_endplanes(case_id, "main_airway", 19, 8, 3)
    endplanes.save_endplanes(case_id, "main_airway", 19, 8, 3)

    revisions = endplane_history.list_revisions(case_id, "main_airway")
    assert len(revisions) == 2
    assert revisions[0]["previous_plan"] is None
    assert revisions[0]["confirmed_plan"] == first
    assert revisions[1]["previous_plan"] == first
    assert revisions[1]["confirmed_plan"] == second
    assert revisions[1]["mask_version"] == 3
    assert revisions[1]["actor_id"] is None

    (root / "segment" / "endplanes.json").unlink()
    assert endplane_history.list_revisions(case_id, "main_airway") == revisions


def test_failed_endplane_save_never_becomes_confirmed_history(
    confirmed_case, tmp_path, monkeypatch,
):
    from pipeline import endplane_history

    monkeypatch.setenv("WINDCATCHER_LEARNING_DIR", str(tmp_path / "learning"))
    case_id, root, _record, _mask = confirmed_case
    first = endplanes.save_endplanes(case_id, "main_airway", 20, 8, 3)

    def fail_invalidation(*_args, **_kwargs):
        raise OSError("synthetic locked artifact")

    monkeypatch.setattr(endplanes, "invalidate_downstream", fail_invalidation)
    with pytest.raises(PipelineError, match="旧 STL/CFD"):
        endplanes.save_endplanes(case_id, "main_airway", 19, 8, 3)
    assert endplanes.load_plan(case_id, "main_airway") == first
    revisions = endplane_history.list_revisions(case_id, "main_airway")
    assert len(revisions) == 1
    assert revisions[0]["confirmed_plan"] == first


def test_endplane_history_recovers_a_saved_plan_after_commit_interruption(
    confirmed_case, tmp_path, monkeypatch,
):
    from pipeline import endplane_history

    monkeypatch.setenv("WINDCATCHER_LEARNING_DIR", str(tmp_path / "learning"))
    case_id, _root, _record, _mask = confirmed_case
    original_commit = endplane_history.commit_revision

    def interrupted_commit(_revision_id):
        monkeypatch.setattr(endplane_history, "commit_revision", original_commit)
        raise OSError("synthetic learning database outage")

    monkeypatch.setattr(endplane_history, "commit_revision", interrupted_commit)
    with pytest.raises(PipelineError, match="修正记录未完成"):
        endplanes.save_endplanes(case_id, "main_airway", 20, 8, 3)
    assert endplane_history.list_revisions(case_id, "main_airway") == []
    saved = endplanes.load_plan(case_id, "main_airway")

    assert endplanes.save_endplanes(case_id, "main_airway", 20, 8, 3) == saved
    revisions = endplane_history.list_revisions(case_id, "main_airway")
    assert len(revisions) == 1
    assert revisions[0]["confirmed_plan"] == saved


def test_direct_saves_in_separate_processes_serialize_the_entire_commit(
    confirmed_case, tmp_path, monkeypatch,
):
    """A second process must not abort a live pending revision or overwrite it."""
    from pipeline import endplane_history

    monkeypatch.setenv("WINDCATCHER_LEARNING_DIR", str(tmp_path / "learning"))
    case_id, _root, _record, _mask = confirmed_case
    entered_invalidation = threading.Event()
    release_first_save = threading.Event()
    original_invalidate = endplanes.invalidate_downstream

    def pause_first_save(*args, **kwargs):
        entered_invalidation.set()
        if not release_first_save.wait(timeout=15):
            raise TimeoutError("first save was not released")
        return original_invalidate(*args, **kwargs)

    monkeypatch.setattr(endplanes, "invalidate_downstream", pause_first_save)
    child_marker = tmp_path / "child-started"
    child_code = """
import sys
from pathlib import Path
from pipeline import cases, endplanes
cases.CASES_DIR = Path(sys.argv[1])
Path(sys.argv[3]).write_text("ready", encoding="utf-8")
endplanes.save_endplanes(sys.argv[2], "main_airway", 19, 8, 3)
"""
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(endplanes.save_endplanes, case_id, "main_airway", 20, 8, 3)
        assert entered_invalidation.wait(timeout=15)
        child = subprocess.Popen(
            [sys.executable, "-c", child_code, str(tmp_path), case_id, str(child_marker)],
            cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 15
            while not child_marker.exists() and time.monotonic() < deadline:
                assert child.poll() is None, child.communicate()[1]
                time.sleep(0.05)
            assert child_marker.exists(), "second process did not reach its save call"
            time.sleep(0.8)
            assert child.poll() is None, "second process saved while first commit was pending"
        finally:
            release_first_save.set()
            stdout, stderr = child.communicate(timeout=30)
        assert child.returncode == 0, (stdout, stderr)
        initial = first.result(timeout=15)

    revisions = endplane_history.list_revisions(case_id, "main_airway", include_pending=True)
    assert [revision["status"] for revision in revisions] == ["committed", "committed"]
    assert revisions[0]["confirmed_plan"] == initial
    assert revisions[1]["previous_plan"] == initial
    assert endplanes.load_plan(case_id, "main_airway") == revisions[1]["confirmed_plan"]
