"""鼻塞 STL 资产的几何预检与变换测试。"""

from __future__ import annotations

import struct

import pytest

from pipeline import plug_models
from pipeline.errors import PipelineError


def _box_mesh():
    vertices = (
        (0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0),
        (0, 0, 10), (10, 0, 10), (10, 10, 10), (0, 10, 10),
    )
    faces = (
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
        (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    )
    return plug_models.Mesh(vertices, faces, "test")


def test_binary_stl_qc_reports_closed_volume_and_unit_conversion():
    source = plug_models.mesh_to_binary_stl(_box_mesh())

    mesh = plug_models.parse_stl(source)
    qc = plug_models.inspect_mesh(mesh)
    _, centimeter_qc, normalized = plug_models.normalized_mesh(source, "cm")

    assert qc["verdict"] == "pass"
    assert qc["closed"] is True
    assert qc["can_use_for_cfd"] is True
    assert qc["volume_mm3"] == pytest.approx(1000.0)
    assert centimeter_qc["bbox_mm"]["size"] == [100.0, 100.0, 100.0]
    assert plug_models.parse_stl(normalized).encoding == "binary"


def test_open_mesh_is_kept_as_warning_but_blocked_for_cfd():
    mesh = _box_mesh()
    open_mesh = plug_models.Mesh(mesh.vertices, mesh.faces[:-2], "test")

    qc = plug_models.inspect_mesh(open_mesh)

    assert qc["verdict"] == "warn"
    assert qc["closed"] is False
    assert qc["can_use_for_cfd"] is False
    assert qc["boundary_edges"] > 0


def test_degenerate_mesh_and_bad_unit_are_rejected():
    degenerate = plug_models.Mesh(((0, 0, 0), (1, 0, 0), (2, 0, 0)), ((0, 1, 2),), "test")
    with pytest.raises(PipelineError, match="单位"):
        plug_models.normalized_mesh(b"solid empty\nendsolid empty\n", "yard")
    qc = plug_models.inspect_mesh(degenerate)
    assert qc["verdict"] == "failed"
    assert qc["degenerate_faces"] == 1
    with pytest.raises(PipelineError, match="三角面"):
        plug_models.parse_stl(b"solid empty\nendsolid empty\n")


def test_binary_parser_rejects_truncated_triangle_data():
    data = b"x" * 84 + struct.pack("<I", 1)
    with pytest.raises(PipelineError):
        plug_models.parse_stl(data)


def test_ascii_parser_requires_facet_structure_and_rejects_bool_placement_values():
    ascii_stl = b"""solid box
facet normal 0 0 1
 outer loop
  vertex 0 0 0
  vertex 1 0 0
  vertex 0 1 0
 endloop
endfacet
endsolid box
"""
    assert plug_models.parse_stl(ascii_stl).encoding == "ascii"
    with pytest.raises(PipelineError, match="facet"):
        plug_models.parse_stl(b"solid fake\n vertex 0 0 0\n vertex 1 0 0\n vertex 0 1 0\n endsolid fake\n")
    with pytest.raises(PipelineError, match="无效"):
        plug_models.validate_placement({"translation_mm": [True, 0, 0], "rotation_deg": [0, 0, 0], "scale": [1, 1, 1]})


def test_inspect_mesh_rejects_invalid_face_indices_and_huge_coordinates():
    invalid = plug_models.Mesh(((0, 0, 0),), ((0, 1, 0),), "test")
    with pytest.raises(PipelineError, match="面索引"):
        plug_models.inspect_mesh(invalid)
    huge = plug_models.Mesh(((0, 0, 0), (20_000_000, 0, 0), (0, 1, 0)), ((0, 1, 2),), "test")
    with pytest.raises(PipelineError, match="坐标"):
        plug_models.inspect_mesh(huge)
