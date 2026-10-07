from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from pipeline import cfd_results
from pipeline import cases
from pipeline.cases import case_dir, create_case
from pipeline.cfd_results import get_results
from pipeline.errors import PipelineError


@pytest.fixture
def result_case(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = create_case("synthetic", "<合成测试>", synthetic=True)
    root = case_dir(case_id)
    for timestep in ("1000", "1500"):
        field = root / "cfd" / timestep
        field.mkdir()
        for name, dimensions in (("U", "0 1 -1 0 0 0 0"), ("p", "0 2 -2 0 0 0 0")):
            (field / name).write_text(f"dimensions [{dimensions}];\n{timestep}", encoding="ascii")
    mesh = root / "cfd" / "constant" / "polyMesh"
    mesh.mkdir(parents=True)
    for name in ("points", "faces", "owner", "neighbour", "boundary"):
        (mesh / name).write_text(name, encoding="ascii")
    return case_id, root


@pytest.fixture
def vtk_pipe():
    vtk = pytest.importorskip("vtk")
    from vtk.util.numpy_support import numpy_to_vtk

    image = vtk.vtkImageData()
    image.SetDimensions(61, 5, 5)
    image.SetSpacing(0.001, 0.001, 0.001)
    converter = vtk.vtkAppendFilter()
    converter.AddInputData(image)
    converter.Update()
    mesh = converter.GetOutput()
    u = numpy_to_vtk(np.tile([0.1, 0.0, 0.0], (mesh.GetNumberOfCells(), 1)), deep=True)
    u.SetName("U")
    mesh.GetCellData().AddArray(u)
    centers = vtk.vtkCellCenters()
    centers.SetInputData(mesh)
    centers.Update()
    pressure = numpy_to_vtk(np.asarray([centers.GetOutput().GetPoint(i)[0] * 100 for i in range(mesh.GetNumberOfCells())]), deep=True)
    pressure.SetName("p")
    mesh.GetCellData().AddArray(pressure)
    inlet_source = vtk.vtkPlaneSource()
    inlet_source.SetOrigin(0.0, 0.0, 0.0)
    inlet_source.SetPoint1(0.0, 0.004, 0.0)
    inlet_source.SetPoint2(0.0, 0.0, 0.004)
    inlet_source.SetXResolution(4)
    inlet_source.SetYResolution(4)
    inlet_source.Update()
    inlet = inlet_source.GetOutput()
    inlet_u = numpy_to_vtk(np.tile([0.1, 0.0, 0.0], (inlet.GetNumberOfCells(), 1)), deep=True)
    inlet_u.SetName("U")
    inlet.GetCellData().AddArray(inlet_u)
    return mesh, inlet


@pytest.fixture
def tiny_reader(monkeypatch, vtk_pipe):
    mesh, _inlet = vtk_pipe
    vtk = cfd_results.vtk

    class Reader:
        def __init__(self):
            self.selected_time = None
            self.output = vtk.vtkMultiBlockDataSet()
            self.output.SetBlock(0, mesh)

        def GetTimeValues(self):
            values = vtk.vtkDoubleArray()
            values.InsertNextValue(1000)
            values.InsertNextValue(1500)
            return values

        def SetTimeValue(self, value):
            self.selected_time = value

        def GetOutput(self):
            return self.output

        def __getattr__(self, name):
            if name.startswith(("Set", "Disable", "Update")):
                return lambda *args: None
            raise AttributeError(name)

    reader = Reader()
    monkeypatch.setattr(vtk, "vtkOpenFOAMReader", lambda: reader)
    return reader


def test_get_results_reports_missing_without_creating_source_files(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = create_case("synthetic", "<合成测试>", synthetic=True)
    assert get_results(case_id) == {"status": "missing"}
    assert not (case_dir(case_id) / "cfd" / "case.foam").exists()


def test_get_results_marks_payload_stale_when_final_field_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = create_case("synthetic", "<合成测试>", synthetic=True)
    root = case_dir(case_id)
    field = root / "cfd" / "1000"
    field.mkdir(parents=True)
    (field / "U").write_text("old", encoding="ascii")
    (field / "p").write_text("old", encoding="ascii")
    (root / "cfd" / "results").mkdir()
    (root / "cfd" / "results" / "manifest.json").write_text(
        json.dumps({"status": "ok", "source_signature": "not-current"}), encoding="utf-8"
    )
    result = get_results(case_id)
    assert result["status"] == "stale"
    assert result["stale"] is True


def test_source_accepts_latest_non_1000_result_without_writing_foam(result_case):
    _case_id, root = result_case
    for name in ("U", "p"):
        (root / "cfd" / "1000" / name).unlink()
    (root / "cfd" / "1000").rmdir()
    source = cfd_results._source(root)
    assert source[2] == "1500"
    assert not (root / "cfd" / "case.foam").exists()


@pytest.mark.parametrize("relative", ["1500/U", "1500/p", "constant/polyMesh/points", "constant/polyMesh/faces", "constant/polyMesh/owner", "constant/polyMesh/neighbour", "constant/polyMesh/boundary"])
def test_source_signature_tracks_current_fields_and_mesh(result_case, relative):
    _case_id, root = result_case
    before = cfd_results._source(root)[1]
    with (root / "cfd" / relative).open("a", encoding="ascii") as handle:
        handle.write("changed")
    assert cfd_results._source(root)[1] != before


def test_source_signature_ignores_earlier_field_changes(result_case):
    _case_id, root = result_case
    before = cfd_results._source(root)[1]
    (root / "cfd" / "1000" / "U").write_text("older modified result", encoding="ascii")
    assert cfd_results._source(root)[1] == before


def test_latest_incomplete_field_does_not_fall_back_to_old_result(result_case):
    _case_id, root = result_case
    (root / "cfd" / "1500" / "U").unlink()
    with pytest.raises(PipelineError, match="U"):
        cfd_results._source(root)


def test_postprocess_keeps_task_and_publishes_matching_time(result_case, tiny_reader):
    case_id, root = result_case
    results = root / "cfd" / "results"
    results.mkdir()
    task = results / "task.json"
    task.write_text('{"status":"running","job_id":"keep-me"}', encoding="utf-8")
    before = task.read_bytes()
    manifest = cfd_results.run_postprocess(case_id)
    assert tiny_reader.selected_time == 1500
    assert task.read_bytes() == before
    assert isinstance(manifest["payload"], str)
    assert manifest["payload"].startswith("cfd/results/generations/")
    payload = json.loads((root / manifest["payload"]).read_text(encoding="utf-8"))
    assert payload["source"]["timestep"] == 1500
    assert payload["source"]["source_signature"] == manifest["source_signature"]
    assert payload["units"]["pressure"] == "m2/s2"
    assert get_results(case_id)["status"] == "ready"


def test_publish_failure_preserves_existing_ready_result_and_task(result_case, tiny_reader, monkeypatch):
    case_id, root = result_case
    results = root / "cfd" / "results"
    results.mkdir()
    manifest_path = results / "manifest.json"
    existing = {"status": "ready", "payload": "cfd/results/payload.json", "source_signature": cfd_results._source(root)[1]}
    manifest_path.write_text(json.dumps(existing), encoding="utf-8")
    (results / "payload.json").write_text('{"previous":true}', encoding="utf-8")
    (results / "task.json").write_text('{"job_id":"preserved"}', encoding="utf-8")
    before = {p.name: p.read_bytes() for p in results.iterdir()}
    original_replace = Path.replace

    def deny_commit(self, target):
        if Path(target) in (results, manifest_path):
            raise PermissionError("manifest commit denied")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", deny_commit)
    with pytest.raises((PipelineError, PermissionError), match="denied|发布"):
        cfd_results.run_postprocess(case_id)
    assert {name: (results / name).read_bytes() for name in before} == before
    assert get_results(case_id)["status"] == "ready"


def test_streamlines_cross_pipe_in_forward_direction_with_true_field_values(vtk_pipe):
    mesh, inlet = vtk_pipe
    lines, diagnostics = cfd_results._streamlines(mesh, inlet)
    assert diagnostics["valid_seeds"] > 0
    assert len(lines) >= 8
    assert diagnostics["direction"] == "forward"
    assert diagnostics["integration_step_unit"] == "m"
    for line in lines:
        positions = np.asarray(line["positions"]).reshape(-1, 3)
        assert positions[-1, 0] - positions[0, 0] > 0.055
        assert np.all(np.diff(positions[:, 0]) >= 0)
        assert np.allclose(line["speeds"], 0.1)
        assert line["termination"] == "out_of_domain"
        assert len(line["integration_time_s"]) == len(positions)
        assert np.all(np.diff(line["integration_time_s"]) >= 0)


def test_streamline_empty_output_without_u_is_observable(vtk_pipe, monkeypatch):
    mesh, inlet = vtk_pipe
    vtk = cfd_results.vtk

    class EmptyTracer:
        LENGTH_UNIT = vtk.vtkStreamTracer.LENGTH_UNIT

        def GetOutput(self):
            return vtk.vtkPolyData()

        def __getattr__(self, name):
            if name.startswith("Set") or name == "Update":
                return lambda *args: None
            raise AttributeError(name)

    monkeypatch.setattr(vtk, "vtkStreamTracer", EmptyTracer)
    lines, diagnostics = cfd_results._streamlines(mesh, inlet)
    assert lines == []
    assert diagnostics["status"] == "empty"
    assert diagnostics["reason"]


def test_slices_preserve_triangle_topology_and_point_fields(vtk_pipe):
    mesh, _inlet = vtk_pipe
    slices = cfd_results._slices(mesh)
    assert len(slices) == 3
    for cut in slices:
        points = np.asarray(cut["positions"]).reshape(-1, 3)
        indices = np.asarray(cut["indices"]).reshape(-1, 3)
        assert len(indices) > 0
        assert np.all(indices >= 0) and np.all(indices < len(points))
        triangles = points[indices]
        areas = np.linalg.norm(np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]), axis=1)
        assert np.all(areas > 0)
        axis = "xyz".index(cut["axis"])
        assert np.allclose(points[:, axis], points[0, axis])
        assert len(cut["pressure"]) == len(points)
        assert np.allclose(cut["speeds"], 0.1)


def test_postprocess_uses_p_dimensions_without_density_conversion(result_case, tiny_reader):
    case_id, root = result_case
    (root / "cfd" / "1500" / "p").write_text("dimensions [1 -1 -2 0 0 0 0];", encoding="ascii")
    manifest = cfd_results.run_postprocess(case_id)
    payload = json.loads((root / manifest["payload"]).read_text(encoding="utf-8"))
    assert payload["units"]["pressure"] == "Pa"
    assert payload["metadata"]["pressure_reference"] == "pressure"


def test_postprocess_rejects_source_changed_during_processing(result_case, tiny_reader):
    case_id, root = result_case

    def update_source(event):
        if event["stage"] == "slices":
            (root / "cfd" / "1500" / "U").write_text("solver wrote new field", encoding="ascii")

    with pytest.raises(PipelineError, match="已变化"):
        cfd_results.run_postprocess(case_id, progress_callback=update_source)
    assert not (root / "cfd" / "results" / "manifest.json").exists()


def test_streamline_seeds_are_deterministic_and_cover_inlet(vtk_pipe):
    mesh, inlet = vtk_pipe
    _lines, first = cfd_results._streamlines(mesh, inlet)
    _lines, second = cfd_results._streamlines(mesh, inlet)
    assert first["seed_positions_m"] == second["seed_positions_m"]
    assert first["eligible_face_fraction"] == 1
    positions = np.asarray(first["seed_positions_m"]).reshape(-1, 3)
    assert np.ptp(positions[:, 1]) > 0.0029
    assert np.ptp(positions[:, 2]) > 0.0029


@pytest.mark.parametrize("field", ["U", "p"])
def test_postprocess_does_not_publish_nonfinite_solver_fields(result_case, tiny_reader, field):
    case_id, root = result_case
    array = tiny_reader.GetOutput().GetBlock(0).GetCellData().GetArray(field)
    array.SetComponent(0, 0, float("nan"))
    with pytest.raises(PipelineError, match="NaN|Infinity"):
        cfd_results.run_postprocess(case_id)
    assert not (root / "cfd" / "results" / "manifest.json").exists()


def test_successful_new_publication_keeps_previous_payload_readable(result_case, tiny_reader):
    case_id, root = result_case
    first = cfd_results.run_postprocess(case_id)
    previous = (root / first["payload"]).read_bytes()
    second = cfd_results.run_postprocess(case_id)
    assert first["payload"] != second["payload"]
    assert (root / first["payload"]).read_bytes() == previous
    assert get_results(case_id)["payload"] == second["payload"]


@pytest.fixture
def curved_pressure_surface():
    vtk = pytest.importorskip("vtk")
    from vtk.util.numpy_support import numpy_to_vtk, vtk_to_numpy

    sphere = vtk.vtkSphereSource()
    sphere.SetRadius(0.03)
    sphere.SetThetaResolution(64)
    sphere.SetPhiResolution(64)
    sphere.Update()
    surface = sphere.GetOutput()
    centers = vtk.vtkCellCenters()
    centers.SetInputData(surface)
    centers.Update()
    positions = vtk_to_numpy(centers.GetOutput().GetPoints().GetData())
    values = 10 + positions[:, 0] * 100 + positions[:, 1] * 50 + positions[:, 2] * 20
    pressure = numpy_to_vtk(values, deep=True)
    pressure.SetName("p")
    surface.GetCellData().AddArray(pressure)
    return surface, values


def test_decimated_curved_surface_keeps_true_nonzero_pressure(curved_pressure_surface):
    surface, source_pressure = curved_pressure_surface
    result = cfd_results._sample_patch(surface, limit=600)
    pressure = np.asarray(result["pressure"])
    assert len(pressure) == len(result["positions"]) // 3
    assert len(pressure) < surface.GetNumberOfPoints()
    # 旧 probe 路径对此曲面返回 100% 的默认 0；源压力全部约为 6.6～13.4。
    assert np.all(pressure >= source_pressure.min() - 0.1)
    assert np.all(pressure <= source_pressure.max() + 0.1)
    assert np.ptp(pressure) > 6
    positions = np.asarray(result["positions"]).reshape(-1, 3)
    expected = 10 + positions[:, 0] * 100 + positions[:, 1] * 50 + positions[:, 2] * 20
    assert np.max(np.abs(pressure - expected)) < 0.12
    assert result["sampling"]["pressure_method"] == "edge_collapse_interpolation"
    assert result["sampling"]["pressure_valid_fraction"] == 1
    assert result["sampling"]["source_cells"] == surface.GetNumberOfCells()


def test_unsimplified_surface_keeps_original_interpolated_pressure(curved_pressure_surface):
    surface, _source_pressure = curved_pressure_surface
    result = cfd_results._sample_patch(surface, limit=surface.GetNumberOfCells() * 2)
    from vtk.util.numpy_support import vtk_to_numpy
    converter = cfd_results.vtk.vtkCellDataToPointData()
    converter.SetInputData(surface)
    converter.Update()
    expected = vtk_to_numpy(converter.GetOutput().GetPointData().GetArray("p"))
    assert np.allclose(result["pressure"], expected)
    assert result["sampling"]["pressure_method"] == "cell_to_point_interpolation"


def test_surface_without_pressure_fails_instead_of_silent_empty_geometry(curved_pressure_surface):
    surface, _source_pressure = curved_pressure_surface
    surface.GetCellData().RemoveArray("p")
    with pytest.raises(PipelineError, match="p|压力"):
        cfd_results._sample_patch(surface, limit=600)
