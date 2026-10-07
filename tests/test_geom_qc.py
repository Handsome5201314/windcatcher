"""几何质检与 CFD 开口过滤使用同一实际网格和技术阈值。"""

from __future__ import annotations

import json

import numpy as np
import pytest

from pipeline import cases, cfd_prep, geom_qc
from pipeline.config import MIN_OPENING_AREA_MM2
from pipeline.errors import PipelineError
from pipeline.volume import Volume, save_volume


def _open_prism(tmp_path, width_mm: float):
    """两端开放的合成方管，前后端各一个真实边界环。"""
    half = width_mm / 2.0
    corners = [(-half, -half), (half, -half), (half, half), (-half, half)]
    vertices = np.asarray(
        [[x, y, z] for y in (0.0, 10.0) for x, z in corners], dtype=float
    )
    faces = []
    for i in range(4):
        j = (i + 1) % 4
        faces.extend(((i, j, 4 + j), (i, 4 + j, 4 + i)))
    np.save(tmp_path / "vertices.npy", vertices)
    np.save(tmp_path / "faces.npy", np.asarray(faces, dtype=np.int32))


def _capped_prism(tmp_path, *, missing_wall=False, duplicate_wall=False):
    _open_prism(tmp_path, width_mm=2.0)
    faces = np.load(tmp_path / "faces.npy")
    faces = np.vstack((faces, ((0, 1, 2), (0, 2, 3), (4, 6, 5), (4, 7, 6))))
    inlet, outlet = np.array([8, 9]), np.array([10, 11])
    if missing_wall:
        faces = faces[1:]
        inlet, outlet = inlet - 1, outlet - 1
    if duplicate_wall:
        faces = np.vstack((faces, faces[0]))
    np.save(tmp_path / "faces.npy", faces.astype(np.int32))
    np.save(tmp_path / "inlet_triangles.npy", inlet)
    np.save(tmp_path / "outlet_triangles.npy", outlet)


def test_capped_prism_passes_when_complete_shell_is_closed(tmp_path):
    _capped_prism(tmp_path)

    result = geom_qc.check_mesh(str(tmp_path))

    assert result["boundary_edges"] == 0
    assert result["non_manifold_edges"] == 0
    assert result["usable_openings_by_end"] == {"anterior": 1, "posterior": 1, "middle": 0}
    assert result["verdict"] == "pass"
    assert result["blocking"] is False


def test_caps_do_not_hide_extra_wall_hole(tmp_path):
    _capped_prism(tmp_path, missing_wall=True)

    result = geom_qc.check_mesh(str(tmp_path))

    assert result["usable_openings_by_end"] == {"anterior": 1, "posterior": 1, "middle": 0}
    assert result["boundary_edges"] == 3
    assert result["verdict"] == "fail"
    assert result["blocking"] is True
    assert any("封盖" in reason and "3" in reason and "未闭合" in reason for reason in result["reasons"])


def test_caps_do_not_downgrade_nonmanifold_edges_to_warning(tmp_path):
    _capped_prism(tmp_path, duplicate_wall=True)

    result = geom_qc.check_mesh(str(tmp_path))

    assert result["boundary_edges"] == 0
    assert result["non_manifold_edges"] == 3
    assert result["verdict"] == "fail"
    assert result["blocking"] is True
    assert any("封盖" in reason and "非流形" in reason for reason in result["reasons"])


def test_cfd_prep_blocks_capped_shell_with_extra_hole(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = cases.create_case("synthetic", "<合成测试>", synthetic=True)
    root = cases.case_dir(case_id)
    _capped_prism(root / "surface", missing_wall=True)
    result = geom_qc.check_mesh(str(root / "surface"))
    (root / "surface" / "geom_qc.json").write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(PipelineError, match="几何质检未通过") as caught:
        cfd_prep.prepare_case(case_id)

    assert caught.value.status == "blocked"
    assert not (root / "cfd" / "case_meta.json").exists()


def test_tiny_end_loops_fail_qc_before_cfd_prep(tmp_path):
    _open_prism(tmp_path, width_mm=0.5)

    result = geom_qc.check_mesh(str(tmp_path))

    assert result["openings_by_end"] == {"anterior": 1, "posterior": 1, "middle": 0}
    assert result["usable_openings_by_end"] == {"anterior": 0, "posterior": 0, "middle": 0}
    assert result["opening_area_threshold_mm2"] == MIN_OPENING_AREA_MM2
    assert result["verdict"] == "fail"
    assert result["blocking"] is True
    assert any("0.25 mm²" in reason and "1 mm²" in reason for reason in result["reasons"])


def test_end_loops_at_or_above_shared_threshold_can_pass_initial_qc(tmp_path):
    _open_prism(tmp_path, width_mm=1.0)

    result = geom_qc.check_mesh(str(tmp_path))

    assert result["usable_openings_by_end"] == {"anterior": 1, "posterior": 1, "middle": 0}
    assert result["verdict"] == "pass"


def test_run_geom_qc_compares_with_effective_surface_mask(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = cases.create_case("synthetic", "<合成测试>", synthetic=True)
    root = cases.case_dir(case_id)
    shape = (12, 20, 12)
    save_volume(
        case_id,
        Volume(
            array=np.zeros(shape, dtype=np.float32),
            spacing=(1.0, 1.0, 1.0),
            origin=(0.0, 0.0, 0.0),
            axis_dirs=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
            meta={"shape": list(shape)},
        ),
    )
    effective = np.zeros(shape, dtype=np.uint8)
    effective[3:7, 2:16, 3:7] = 1
    source = effective.copy()
    source[8:10, 8:10, 8:10] = 1  # 源候选多出一个与 STL 无关的区域
    np.save(root / "segment" / "candidate_mask.npy", source)
    np.save(root / "surface" / "effective_mask.npy", effective)
    (root / "surface" / "surface.json").write_text(
        json.dumps({"params": {"model_scope": "full_candidate"}}), encoding="utf-8"
    )
    _open_prism(root / "surface", width_mm=2.0)

    result = geom_qc.run_geom_qc(case_id)

    assert result["mask_source"] == "surface/effective_mask.npy"
    assert result["volume_check"]["mask_volume_ml"] == float(effective.sum()) / 1000.0
    saved = json.loads((root / "surface" / "geom_qc.json").read_text(encoding="utf-8"))
    assert saved["mask_source"] == "surface/effective_mask.npy"
