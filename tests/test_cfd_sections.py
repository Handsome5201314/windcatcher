from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from pipeline import cfd_sections
from pipeline.errors import PipelineError


PLANE = {"point_lps_mm": [0., 0., 0.], "normal_lps": [0., 1., 0.],
         "u_lps": [1., 0., 0.], "v_lps": [0., 0., 1.], "position_fraction": .5}
SQUARE = np.asarray([[0., 0.], [1., 0.], [1., 1.], [0., 1.]])
TRIANGLES = np.asarray([[0, 1, 2], [0, 2, 3]])


def region(side, level, polygon):
    return {"id": f"{side}-{level}", "label": f"{side} {level}", "side": side,
            "level": level, "polygon_uv_mm": np.asarray(polygon).tolist()}


def rectangle(x0, x1, y0=0., y1=1.):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def integrate(values, annotations):
    return cfd_sections._integrate(SQUARE, np.asarray(values), TRIANGLES,
                                   cfd_sections._regions({"regions": annotations}), PLANE)


def test_nonuniform_linear_velocity_clips_across_triangles_without_centroid_assignment():
    result = integrate([1., 3., 3., 1.], [region("right", "lower", rectangle(0, .25)), region("left", "lower", rectangle(.25, 1))])
    assert result["totals"]["forward_lpm"] == pytest.approx(.12)
    assert result["regions"][0]["forward_lpm"] == pytest.approx(.01875)
    assert result["regions"][0]["area_mm2"] == pytest.approx(.25)
    assert result["regions"][1]["forward_lpm"] == pytest.approx(.10125)
    assert result["regions"][0]["percent_of_section"] == pytest.approx(15.625)
    assert result["regions"][0]["percent_of_side"] == pytest.approx(100)
    assert result["coverage_complete"] is True
    assert result["unassigned"]["area_mm2"] == pytest.approx(0)
    assert all(abs(v) < 1e-12 for v in result["partition_residual"].values())


def test_backflow_zero_contour_integrates_positive_and_negative_parts_exactly():
    result = integrate([-1., 1., 1., -1.], [region("right", "lower", rectangle(0, .5)), region("left", "lower", rectangle(.5, 1))])
    assert result["totals"]["forward_lpm"] == pytest.approx(.015)
    assert result["totals"]["reverse_lpm"] == pytest.approx(.015)
    assert result["totals"]["net_lpm"] == pytest.approx(0)
    assert result["regions"][0]["net_lpm"] == pytest.approx(-.015)
    assert result["regions"][1]["net_lpm"] == pytest.approx(.015)
    assert result["regions"][0]["percent_of_section"] == pytest.approx(0)
    assert result["regions"][0]["percent_of_side"] is None


def test_single_triangle_crossing_zero_line_has_known_positive_integral():
    triangle = np.asarray([[0., 0., -1.], [1., 0., 1.], [0., 1., -1.]])
    result = cfd_sections._integral(triangle)
    assert result["forward_lpm"] == pytest.approx(.0025)
    assert result["reverse_lpm"] == pytest.approx(.0125)
    assert result["net_lpm"] == pytest.approx(-.01)


def test_tangential_velocity_does_not_contribute_to_flux():
    # _integrate receives U dot n, not velocity magnitude.
    result = integrate([0., 0., 0., 0.], [region("left", "lower", SQUARE)])
    assert result["totals"]["forward_lpm"] == 0
    assert result["totals"]["reverse_lpm"] == 0
    assert result["regions"][0]["percent_of_section"] is None
    assert result["regions"][0]["percent_of_side"] is None


def test_unassigned_gap_is_retained_and_side_percent_is_not_renormalized():
    result = integrate([1.] * 4, [region("right", "lower", rectangle(0, .4)), region("left", "lower", rectangle(.6, 1))])
    assert result["unassigned"]["forward_lpm"] == pytest.approx(.012)
    assert result["unassigned"]["percent_of_section"] == pytest.approx(20)
    assert result["coverage_complete"] is False
    assert [item["percent_of_section"] for item in result["sides"]] == pytest.approx([40, 40])
    assert all(item["percent_of_side"] is None for item in result["regions"])
    assert result["region_meshes"][-1]["id"] == "unassigned"
    assert len(result["region_meshes"][-1]["positions"]) > 0


def test_unassigned_reverse_only_area_is_still_incomplete():
    result = integrate([-1., 1., 1., -1.], [region("left", "lower", rectangle(.5, 1))])
    assert result["unassigned"]["forward_lpm"] == pytest.approx(0)
    assert result["unassigned"]["reverse_lpm"] == pytest.approx(.015)
    assert result["coverage_complete"] is False
    assert result["regions"][0]["percent_of_side"] is None


@pytest.mark.parametrize("values", [[0.] * 4, [1e-12] * 4, [-1.] * 4])
def test_zero_near_zero_or_reverse_flow_has_no_forward_percentage(values):
    result = integrate(values, [region("left", "lower", SQUARE)])
    assert result["regions"][0]["percent_of_section"] is None
    assert result["sides"][0]["percent_of_section"] is None


def test_overlap_is_rejected_but_shared_edge_is_allowed():
    with pytest.raises(PipelineError, match="重叠"):
        cfd_sections._regions({"regions": [region("left", "lower", rectangle(0, .6)), region("right", "lower", rectangle(.5, 1))]})
    assert len(cfd_sections._regions({"regions": [region("left", "lower", rectangle(0, .5)), region("right", "lower", rectangle(.5, 1))]})) == 2


@pytest.mark.parametrize("polygon", [
    [[0, 0], [1, 1], [0, 1], [1, 0]],
    [[0, 0], [1, 0], [.5, .2], [1, 1], [0, 1]],
    [[0, 0], [1, 0], [1, 0], [0, 1]],
    [[0, 0], [1, float("nan")], [0, 1]],
])
def test_invalid_or_concave_polygons_are_rejected(polygon):
    with pytest.raises(PipelineError):
        cfd_sections._regions({"regions": [region("left", "lower", polygon)]})


def test_clockwise_polygon_is_normalized_and_outside_region_keeps_unassigned():
    result = integrate([1.] * 4, [region("left", "lower", rectangle(2, 3)[::-1])])
    assert result["regions"][0]["forward_lpm"] == 0
    assert result["unassigned"]["forward_lpm"] == pytest.approx(.06)


@pytest.fixture
def section_case(tmp_path, monkeypatch):
    vtk = pytest.importorskip("vtk")
    from vtk.util.numpy_support import numpy_to_vtk

    root = tmp_path / "synthetic-case"
    for folder in ("cfd/1000", "cfd/constant/polyMesh", "volume", "segment"):
        (root / folder).mkdir(parents=True)
    for name, dimensions in (("U", "0 1 -1 0 0 0 0"), ("p", "0 2 -2 0 0 0 0")):
        (root / "cfd" / "1000" / name).write_text(f"dimensions [{dimensions}];\ninternalField uniform {'(0 .2 0)' if name == 'U' else '0'};\n", encoding="ascii")
    for name in ("points", "faces", "owner", "neighbour", "boundary"):
        (root / "cfd" / "constant" / "polyMesh" / name).write_text(name, encoding="ascii")
    (root / "volume" / "meta.json").write_text(json.dumps({"shape": [5, 11, 5], "spacing_mm": [1., 1., 1.], "origin_mm": [0., 0., 0.],
                                                              "axis_dirs": [[0., 0., 1.], [0., 1., 0.], [1., 0., 0.]]}), encoding="utf-8")
    (root / "segment" / "segmentation.json").write_text(json.dumps({"mask_version": 1, "params": {"ap_axis": 1}}), encoding="utf-8")
    (root / "cfd" / "case_meta.json").write_text(json.dumps({"inlet_mode": "flowRate", "rho_kg_m3": 1.2}), encoding="utf-8")
    (root / "segment" / "mask.npy").write_bytes(b"immutable mask")

    image = vtk.vtkImageData()
    image.SetDimensions(5, 11, 5)
    image.SetSpacing(.001, .001, .001)
    append = vtk.vtkAppendFilter()
    append.AddInputData(image)
    append.Update()
    mesh = append.GetOutput()
    vectors = numpy_to_vtk(np.tile([3., .2, 4.], (mesh.GetNumberOfCells(), 1)), deep=True)
    vectors.SetName("U")
    mesh.GetCellData().AddArray(vectors)

    class Reader:
        def __init__(self):
            self.output = vtk.vtkMultiBlockDataSet()
            self.output.SetBlock(0, mesh)

        def GetTimeValues(self):
            times = vtk.vtkDoubleArray()
            times.InsertNextValue(1000.)
            return times

        def GetOutput(self):
            return self.output

        def __getattr__(self, name):
            if name.startswith(("Set", "Disable", "Update")):
                return lambda *args: None
            raise AttributeError(name)

    monkeypatch.setattr(vtk, "vtkOpenFOAMReader", Reader)
    return root, mesh


def complete_annotation(preview):
    uv = np.asarray(preview["section_mesh"]["uv_mm"]).reshape(-1, 2)
    lo, hi = uv.min(axis=0), uv.max(axis=0)
    middle = (lo[0] + hi[0]) / 2
    heights = np.linspace(lo[1], hi[1], 4)
    regions = []
    for side, limits in (("right", (lo[0], middle)), ("left", (middle, hi[0]))):
        for index, level in enumerate(("lower", "middle", "upper")):
            height = (heights[index], heights[index + 1])
            regions.append(region(side, level, rectangle(*limits, *height)))
    return {key: preview[key] for key in ("plane", "source_signature", "geometry_signature")} | {"confirmed": True, "regions": regions}


def test_vtk_cut_uses_ct_direction_and_full_vector_dot_product(section_case):
    root, _ = section_case
    preview = cfd_sections.preview_section(root, .5)
    assert preview["plane"]["normal_lps"] == pytest.approx([0, 1, 0])
    assert preview["plane"]["u_lps"] == pytest.approx([1, 0, 0])
    assert preview["plane"]["v_lps"] == pytest.approx([0, 0, 1])
    points = np.asarray(preview["section_mesh"]["positions"]).reshape(-1, 3)
    assert points[:, 1] == pytest.approx(np.full(len(points), .005))
    result = cfd_sections.compute_sections(root, complete_annotation(preview))
    assert result["totals"]["area_mm2"] == pytest.approx(16)
    assert result["totals"]["forward_lpm"] == pytest.approx(.192)
    assert result["totals"]["reverse_lpm"] == 0
    assert result["totals"]["net_lpm"] == pytest.approx(.192)
    assert [item["percent_of_section"] for item in result["sides"]] == pytest.approx([50, 50])
    for side in ("left", "right"):
        assert sum(r["percent_of_side"] for r in result["regions"] if r["side"] == side) == pytest.approx(100)
    assert result["validation"]["status"] == "pending"
    assert result["validation"]["boundary_reference"]["status"] == "unavailable"
    assert "按入口网格面积预设" in result["inlet_condition"]["description"]
    assert cfd_sections.get_sections(root)["status"] == "ready"
    assert (root / "segment" / "mask.npy").read_bytes() == b"immutable mask"


@pytest.mark.parametrize("relative", ["cfd/1000/U", "cfd/1000/p", "cfd/constant/polyMesh/points", "cfd/constant/polyMesh/faces", "cfd/case_meta.json"])
def test_saved_section_becomes_stale_when_final_source_changes(section_case, relative):
    root, _ = section_case
    cfd_sections.compute_sections(root, complete_annotation(cfd_sections.preview_section(root, .5)))
    with (root / relative).open("a", encoding="utf-8") as handle:
        handle.write(" ")
    result = cfd_sections.get_sections(root)
    assert result["status"] == "stale"
    assert "totals" not in result and "regions" not in result and "sides" not in result
    assert result["annotation"]["confirmed"] is True
    assert result["current_source_signature"] != result["annotation"]["source_signature"]


@pytest.mark.parametrize("relative,key,value", [("volume/meta.json", "origin_mm", [1., 0., 0.]), ("segment/segmentation.json", "mask_version", 2)])
def test_ct_geometry_and_mask_version_are_bound_to_analysis(section_case, relative, key, value):
    root, _ = section_case
    cfd_sections.compute_sections(root, complete_annotation(cfd_sections.preview_section(root, .5)))
    path = root / relative
    data = json.loads(path.read_text(encoding="utf-8"))
    data[key] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    result = cfd_sections.get_sections(root)
    assert result["status"] == "stale"
    assert "totals" not in result
    assert result["current_geometry_signature"] != result["annotation"]["geometry_signature"]


def test_missing_saved_result_returns_current_signatures_for_draft_validation(section_case):
    root, _ = section_case
    result = cfd_sections.get_sections(root)
    assert result["status"] == "missing"
    assert result["source_signature"] == result["current_source_signature"]
    assert result["geometry_signature"] == result["current_geometry_signature"]
    assert not (root / "cfd" / "case.foam").exists()


@pytest.mark.parametrize("key,vector", [("normal_lps", [1]), ("point_lps_mm", [[2, 5, 2]]), ("u_lps", [1, 0, 0, 0]), ("v_lps", [0, 0, float("nan")])])
def test_malformed_or_broadcastable_plane_vectors_are_rejected(section_case, key, vector):
    root, _ = section_case
    annotation = complete_annotation(cfd_sections.preview_section(root, .5))
    annotation["plane"][key] = vector
    with pytest.raises(PipelineError, match="截面坐标|截面平面"):
        cfd_sections.compute_sections(root, annotation)
    assert not (root / "cfd" / "sections" / "analysis.json").exists()


def test_unconfirmed_and_stale_annotation_are_not_published(section_case):
    root, _ = section_case
    annotation = complete_annotation(cfd_sections.preview_section(root, .5))
    annotation["confirmed"] = False
    with pytest.raises(PipelineError, match="人工确认"):
        cfd_sections.compute_sections(root, annotation)
    annotation["confirmed"] = True
    annotation["source_signature"] = "old"
    with pytest.raises(PipelineError, match="过期"):
        cfd_sections.compute_sections(root, annotation)


def test_source_change_during_calculation_preserves_previous_analysis(section_case, monkeypatch):
    root, _ = section_case
    annotation = complete_annotation(cfd_sections.preview_section(root, .5))
    cfd_sections.compute_sections(root, annotation)
    saved = root / "cfd" / "sections" / "analysis.json"
    previous = saved.read_bytes()
    actual_cut = cfd_sections._cut

    def change_source(mesh, plane):
        result = actual_cut(mesh, plane)
        with (root / "cfd" / "1000" / "U").open("a", encoding="ascii") as handle:
            handle.write("changed")
        return result

    monkeypatch.setattr(cfd_sections, "_cut", change_source)
    with pytest.raises(PipelineError, match="发生变化"):
        cfd_sections.compute_sections(root, annotation)
    assert saved.read_bytes() == previous


def test_atomic_publish_failure_preserves_previous_annotation(section_case, monkeypatch):
    root, _ = section_case
    annotation = complete_annotation(cfd_sections.preview_section(root, .5))
    cfd_sections.compute_sections(root, annotation)
    saved = root / "cfd" / "sections" / "analysis.json"
    previous = saved.read_bytes()
    original_replace = Path.replace

    def deny(self, destination):
        if Path(destination) == saved:
            raise PermissionError("commit denied")
        return original_replace(self, destination)

    monkeypatch.setattr(Path, "replace", deny)
    with pytest.raises(PermissionError, match="commit denied"):
        cfd_sections.compute_sections(root, annotation)
    assert saved.read_bytes() == previous
    assert list(saved.parent.glob("*.tmp")) == []


def test_tampered_annotation_never_returns_saved_numbers(section_case):
    root, _ = section_case
    cfd_sections.compute_sections(root, complete_annotation(cfd_sections.preview_section(root, .5)))
    saved = root / "cfd" / "sections" / "analysis.json"
    result = json.loads(saved.read_text(encoding="utf-8"))
    result["annotation"]["regions"][0]["polygon_uv_mm"][0][0] += .1
    saved.write_text(json.dumps(result), encoding="utf-8")
    assert cfd_sections.get_sections(root)["status"] == "error"
    assert "totals" not in cfd_sections.get_sections(root)


def write_six_region_foam(root: Path) -> dict:
    """Write a non-patient six-channel fixture readable by the real VTK reader.

    The six disconnected rectangular channels have different known velocities.
    This is test input, never a production fallback or patient annotation.
    Root may already have been initialized by cases.create_case.
    """
    root = Path(root)
    mesh = root / "cfd" / "constant" / "polyMesh"
    final = root / "cfd" / "1000"
    for folder in (mesh, final, root / "cfd" / "system", root / "volume", root / "segment"):
        folder.mkdir(parents=True, exist_ok=True)
    channels, points, velocities, faces_by_patch = [], [], [], {"inlet": [], "outlet": [], "wall": []}
    for side, xspan in (("right", (-.004, -.0002)), ("left", (.0002, .004))):
        for level, zspan in (("lower", (-.003, -.0012)), ("middle", (-.0009, .0009)), ("upper", (.0012, .003))):
            index = len(channels)
            x0, x1 = xspan
            z0, z1 = zspan
            velocity = .1 * (index + 1)
            area = (x1 - x0) * (z1 - z0)
            channels.append({"id": f"{side}-{level}", "side": side, "level": level, "speed": velocity,
                             "xspan_mm": [x0 * 1000, x1 * 1000], "zspan_mm": [z0 * 1000, z1 * 1000],
                             "forward_lpm": velocity * area * 60000})
            offset = len(points)
            points.extend([(x0, 0, z0), (x1, 0, z0), (x1, .012, z0), (x0, .012, z0),
                           (x0, 0, z1), (x1, 0, z1), (x1, .012, z1), (x0, .012, z1)])
            velocities.append((0, velocity, 0))
            for patch, local in (("inlet", (0, 1, 5, 4)), ("outlet", (3, 7, 6, 2)),
                                 ("wall", (0, 4, 7, 3)), ("wall", (1, 2, 6, 5)),
                                 ("wall", (0, 3, 2, 1)), ("wall", (4, 5, 6, 7))):
                faces_by_patch[patch].append((index, [offset + n for n in local]))

    def header(name, foam_class, extra=""):
        return f'FoamFile {{version 2.0; format ascii; class {foam_class}; object {name}; {extra}}}\n'

    def numbers(values):
        return "(" + " ".join(f"{number:.12g}" for number in values) + ")"

    ordered = [item for patch in ("inlet", "outlet", "wall") for item in faces_by_patch[patch]]
    (mesh / "points").write_text(header("points", "vectorField") + f"{len(points)}\n(\n" + "\n".join(numbers(p) for p in points) + "\n)\n", encoding="ascii")
    (mesh / "faces").write_text(header("faces", "faceList") + f"{len(ordered)}\n(\n" + "\n".join("4" + numbers(ids) for _, ids in ordered) + "\n)\n", encoding="ascii")
    (mesh / "owner").write_text(header("owner", "labelList", 'note "nPoints:48 nCells:6 nFaces:36 nInternalFaces:0";') + f"{len(ordered)}\n(\n" + "\n".join(str(owner) for owner, _ in ordered) + "\n)\n", encoding="ascii")
    (mesh / "neighbour").write_text(header("neighbour", "labelList") + "0()\n", encoding="ascii")
    (mesh / "boundary").write_text(header("boundary", "polyBoundaryMesh") + "3(\ninlet {type patch; nFaces 6; startFace 0;}\noutlet {type patch; nFaces 6; startFace 6;}\nwall {type wall; nFaces 24; startFace 12;}\n)\n", encoding="ascii")
    (final / "U").write_text(header("U", "volVectorField") + "dimensions [0 1 -1 0 0 0 0];\ninternalField nonuniform List<vector>\n6(\n" + "\n".join(numbers(v) for v in velocities) + "\n);\nboundaryField {inlet {type zeroGradient;} outlet {type zeroGradient;} wall {type fixedValue; value uniform (0 0 0);}}\n", encoding="ascii")
    (final / "p").write_text(header("p", "volScalarField") + "dimensions [0 2 -2 0 0 0 0];\ninternalField uniform 0;\nboundaryField {inlet {type zeroGradient;} outlet {type zeroGradient;} wall {type zeroGradient;}}\n", encoding="ascii")
    rates = [channel["forward_lpm"] / 60000 for channel in channels]
    inlet_phi = " ".join(f"{-value:.12g}" for value in rates)
    outlet_phi = " ".join(f"{value:.12g}" for value in rates)
    (final / "phi").write_text(header("phi", "surfaceScalarField") + "dimensions [0 3 -1 0 0 0 0];\ninternalField nonuniform List<scalar> 0();\nboundaryField {" + f"inlet {{type calculated; value nonuniform List<scalar> 6({inlet_phi});}} outlet {{type calculated; value nonuniform List<scalar> 6({outlet_phi});}} wall {{type calculated; value uniform 0;}}" + "}\n", encoding="ascii")
    (root / "cfd" / "system" / "controlDict").write_text(header("controlDict", "dictionary") + "application simpleFoam; startFrom latestTime; startTime 0; stopAt endTime; endTime 1000; deltaT 1; writeControl timeStep; writeInterval 1000;\n", encoding="ascii")
    (root / "cfd" / "case.foam").write_text("", encoding="ascii")
    (root / "cfd" / "case_meta.json").write_text(json.dumps({"inlet_mode": "flowRate", "rho_kg_m3": 1.2, "total_flow_lpm": sum(c["forward_lpm"] for c in channels), "solver": "simpleFoam", "turbulence_model": "laminar", "synthetic": True}), encoding="utf-8")
    (root / "volume" / "meta.json").write_text(json.dumps({"shape": [7, 13, 9], "spacing_mm": [1., 1., 1.], "origin_mm": [-4., 0., -3.], "axis_dirs": [[0., 0., 1.], [0., 1., 0.], [1., 0., 0.]], "source": "non-patient six-channel test geometry"}), encoding="utf-8")
    np.save(root / "volume" / "hu.npy", np.zeros((7, 13, 9), dtype=np.float32))
    np.save(root / "segment" / "mask.npy", np.ones((7, 13, 9), dtype=bool))
    (root / "segment" / "segmentation.json").write_text(json.dumps({"mask_version": 1, "params": {"ap_axis": 1, "hu_high": -300.}, "need_human_confirm": False, "review_flags": {"external_air_suspected": False}, "external_air_validation": {"mask_version": 1, "body_hu_threshold": -300., "outside_voxels": 0, "candidate_outside_voxels": 0}}), encoding="utf-8")
    return {"channels": channels, "total_forward_lpm": sum(c["forward_lpm"] for c in channels)}


def six_channel_annotation(preview: dict, fixture: dict) -> dict:
    # Enclose each actual channel, avoiding microscopic floating-point edge
    # slivers and keeping all six polygons apart in the intentional wall gaps.
    regions = []
    for channel in fixture["channels"]:
        x0, x1 = channel["xspan_mm"]
        z0, z1 = channel["zspan_mm"]
        regions.append(region(channel["side"], channel["level"], rectangle(x0 - .01, x1 + .01, z0 - .01, z1 + .01)))
    return {key: preview[key] for key in ("plane", "source_signature", "geometry_signature")} | {"confirmed": True, "regions": regions}


def test_real_openfoam_reader_integrates_six_known_channels(tmp_path):
    pytest.importorskip("vtk")
    root = tmp_path / "six-channel"
    fixture = write_six_region_foam(root)
    preview = cfd_sections.preview_section(root, .5)
    result = cfd_sections.compute_sections(root, six_channel_annotation(preview, fixture))
    assert result["coverage_complete"] is True
    assert result["totals"]["forward_lpm"] == pytest.approx(fixture["total_forward_lpm"], rel=1e-6)
    expected = {item["id"]: item["forward_lpm"] for item in fixture["channels"]}
    for record in result["regions"]:
        assert record["forward_lpm"] == pytest.approx(expected[record["id"]], rel=1e-6)
    reference = result["validation"]["boundary_reference"]
    assert reference["status"] == "compared"
    assert reference["integration_discrepancy_percent"] < 1e-4
    assert reference["acceptance_status"] == "pending"
