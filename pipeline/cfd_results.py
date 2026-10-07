"""Read the final OpenFOAM fields and publish a small browser payload."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Callable

import numpy as np

from .cases import case_dir
from .cfd_validation import inspect_latest_fields, pressure_metadata, strip_comments
from .errors import PipelineError

try:
    import vtk
    from vtk.util.numpy_support import vtk_to_numpy
except ImportError:
    vtk = None

MAX_POINTS = 120_000


def _source(case_root: Path) -> tuple[Path, str, str]:
    cfd = case_root / "cfd"
    foam = cfd / "case.foam"
    time_name = inspect_latest_fields(cfd).get("latest_time")
    if time_name is None:
        raise PipelineError("blocked", "没有非零 CFD 结果时间步", ["先完成 CFD 求解"])
    field = cfd / time_name
    mesh = field / "polyMesh"
    if not mesh.exists():
        mesh = cfd / "constant" / "polyMesh"
    files = []
    paths = [field / name for name in ("U", "p")]
    paths += [mesh / name for name in ("points", "faces", "owner", "neighbour", "boundary")]
    for path in paths:
        if not path.is_file():
            raise PipelineError("blocked", f"最终时间步 {time_name} 缺少 {path.name} 场或网格文件", ["检查 CFD 求解场与网格是否完整"])
        stat = path.stat()
        files.append((path.relative_to(cfd).as_posix(), stat.st_size, stat.st_mtime_ns))
    signature = hashlib.sha256(json.dumps(files, separators=(",", ":")).encode()).hexdigest()
    return foam, signature, time_name


def _pressure_unit(path: Path) -> str:
    with path.open("rb") as handle:
        header = strip_comments(handle.read(8192).decode("ascii", errors="replace"))
    match = re.search(r"\bdimensions\s*\[([^]]+)\]\s*;", header)
    dimensions = [int(value) for value in match.group(1).split()] if match else None
    metadata = pressure_metadata(dimensions, {})
    if metadata["status"] != "checked":
        raise PipelineError("failed", "p 场维度缺失或不支持，不能标注压力单位")
    return metadata["field_unit"]


def _flat(points) -> list[float]:
    return np.asarray(points, dtype=np.float32).reshape(-1).tolist()


def _emit(callback, event: str, stage: str, message: str, **extra):
    if callback:
        callback({"event": event, "stage": stage, "message": message, **extra})


def _patch_block(boundary, name: str):
    if boundary is None or not boundary.IsA("vtkMultiBlockDataSet"):
        return None
    for i in range(boundary.GetNumberOfBlocks()):
        meta, block = boundary.GetMetaData(i), boundary.GetBlock(i)
        if meta is not None and block is not None and meta.Get(vtk.vtkCompositeDataSet.NAME()) == name:
            return block
    return None


def _sample_patch(block, limit: int = 12_000) -> dict:
    if block is None or block.GetNumberOfCells() == 0:
        return {"positions": [], "indices": [], "pressure": [],
                "sampling": {"status": "empty", "reason": "边界没有可显示的网格面"}}
    # 先把面场插值到点，再三角化并减面。按面中心点抽样会破坏表面
    # 连通性；减面时直接沿边折叠映射真实点场，不能向原曲面 probe
    # 已离开曲面的顶点（无效采样会被 VTK 默认写成 0）。
    point_fields = vtk.vtkCellDataToPointData()
    point_fields.SetInputData(block)
    point_fields.PassCellDataOn()
    point_fields.Update()
    source = point_fields.GetOutput()
    source_pressure = source.GetPointData().GetArray("p")
    if source_pressure is None or source_pressure.GetNumberOfTuples() != source.GetNumberOfPoints() or source_pressure.GetNumberOfComponents() != 1:
        raise PipelineError("failed", "边界 p 场缺失或与网格点不匹配，不能生成压力表面")
    if not np.isfinite(vtk_to_numpy(source_pressure)).all():
        raise PipelineError("failed", "边界压力含 NaN 或 Infinity，不能生成压力表面")
    triangulate = vtk.vtkTriangleFilter()
    triangulate.SetInputData(source)
    triangulate.Update()
    surface = triangulate.GetOutput()
    pressure_method = "cell_to_point_interpolation"
    geometry_method = "triangulation"
    if surface.GetNumberOfCells() > limit:
        decimate = vtk.vtkQuadricDecimation()
        decimate.SetInputData(surface)
        decimate.SetTargetReduction(min(0.9, max(0.0, 1.0 - limit / surface.GetNumberOfCells())))
        decimate.AttributeErrorMetricOff()
        # VTK 在每次边折叠时插值输入点数据；保持压力来源且不让
        # 压力量纲的大小改变几何减面的误差权重。
        decimate.MapPointDataOn()
        decimate.Update()
        surface = decimate.GetOutput()
        pressure_method = "edge_collapse_interpolation"
        geometry_method = "quadric_decimation"
    if surface.GetPoints() is None or surface.GetNumberOfPoints() == 0:
        raise PipelineError("failed", "边界三角化或减面未生成有效表面")
    positions = vtk_to_numpy(surface.GetPoints().GetData()).astype(np.float32)
    pressure = surface.GetPointData().GetArray("p")
    if pressure is None or pressure.GetNumberOfTuples() != surface.GetNumberOfPoints() or pressure.GetNumberOfComponents() != 1:
        raise PipelineError("failed", "减面后丢失边界 p 场，结果未发布")
    pressure_values = vtk_to_numpy(pressure).astype(np.float32)
    valid_fraction = float(np.isfinite(pressure_values).sum() / len(pressure_values))
    if valid_fraction != 1:
        raise PipelineError("failed", "减面后部分压力映射无效，结果未发布", detail={"pressure_valid_fraction": valid_fraction})
    indices = []
    for cell_index in range(surface.GetNumberOfCells()):
        cell = surface.GetCell(cell_index)
        ids = cell.GetPointIds()
        if ids.GetNumberOfIds() == 3:
            indices.extend(ids.GetId(i) for i in range(3))
    return {"positions": _flat(positions), "indices": indices, "pressure": pressure_values.tolist(),
            "sampling": {"status": "checked", "geometry_method": geometry_method, "pressure_method": pressure_method,
                         "pressure_source": "boundary_p", "source_cells": block.GetNumberOfCells(),
                         "source_points": source.GetNumberOfPoints(), "output_points": surface.GetNumberOfPoints(),
                         "pressure_valid_fraction": valid_fraction}}


def _streamlines(mesh, inlet) -> tuple[list[dict], dict]:
    diagnostics = {"status": "empty", "valid_seeds": 0, "seeded_faces": 0, "inlet_faces": 0,
                   "direction": "forward", "integration_step_unit": "m"}
    if mesh is None or inlet is None or inlet.GetNumberOfCells() == 0:
        return [], {**diagnostics, "reason": "没有可用于种子的入口网格面"}
    converter = vtk.vtkCellDataToPointData()
    converter.SetInputData(mesh)
    converter.PassCellDataOn()
    converter.Update()
    point_mesh = converter.GetOutput()
    if point_mesh.GetPointData().GetArray("U") is None:
        return [], {**diagnostics, "reason": "内部网格没有可积分的 U 场"}
    centers = vtk.vtkCellCenters()
    centers.SetInputData(inlet)
    centers.Update()
    centers_data = centers.GetOutput()
    face_points = vtk_to_numpy(centers_data.GetPoints().GetData())
    velocity = inlet.GetCellData().GetArray("U")
    if velocity is None:
        probe = vtk.vtkProbeFilter()
        probe.SetInputData(centers_data)
        probe.SetSourceData(point_mesh)
        probe.Update()
        velocity = probe.GetOutput().GetPointData().GetArray("U")
    if velocity is None:
        return [], {**diagnostics, "reason": "入口没有可用的 U 场，不能确定向内种子方向"}
    vectors = vtk_to_numpy(velocity)
    locator = vtk.vtkStaticCellLocator()
    locator.SetDataSet(point_mesh)
    locator.BuildLocator()
    candidates, face_ids = [], []
    for face_id, (point, vector) in enumerate(zip(face_points, vectors)):
        speed = float(np.linalg.norm(vector))
        if not np.isfinite(speed) or speed <= 1e-12:
            continue
        direction = vector / speed
        # 使用入口面上的实际速度朝流体内部移动；仅接受落在真实网格中的种子。
        for offset in (0.00005, 0.00010, 0.00025, 0.00050):
            candidate = point + direction * offset
            if locator.FindCell(candidate) >= 0:
                candidates.append(candidate)
                face_ids.append(face_id)
                break
    diagnostics.update(inlet_faces=len(face_points), seeded_faces=len(candidates))
    if not candidates:
        return [], {**diagnostics, "reason": "入口种子沿速度偏移后均未进入内部流体网格"}
    candidates = np.asarray(candidates)
    # 最远点采样在整个入口上确定性铺开，避免球状随机种子落在体外。
    selected = [int(np.argmin(np.linalg.norm(candidates - candidates.mean(axis=0), axis=1)))]
    distance = np.full(len(candidates), np.inf)
    while len(selected) < min(64, len(candidates)):
        distance = np.minimum(distance, np.linalg.norm(candidates - candidates[selected[-1]], axis=1))
        distance[selected] = -1
        selected.append(int(np.argmax(distance)))
    seed_points = vtk.vtkPoints()
    for index in selected:
        seed_points.InsertNextPoint(*candidates[index])
    seeds = vtk.vtkPolyData()
    seeds.SetPoints(seed_points)
    diagnostics.update(valid_seeds=len(selected), seed_positions_m=_flat(candidates[selected]),
                       eligible_face_fraction=len(candidates) / len(face_points))
    tracer = vtk.vtkStreamTracer()
    tracer.SetInputData(point_mesh)
    tracer.SetSourceData(seeds)
    tracer.SetInputArrayToProcess(0, 0, 0, vtk.vtkDataObject.FIELD_ASSOCIATION_POINTS, "U")
    tracer.SetIntegratorTypeToRungeKutta45()
    tracer.SetIntegrationDirectionToForward()
    tracer.SetIntegrationStepUnit(vtk.vtkStreamTracer.LENGTH_UNIT)
    tracer.SetMaximumPropagation(0.20)
    tracer.SetInitialIntegrationStep(0.0005)
    tracer.SetMinimumIntegrationStep(0.00005)
    tracer.SetMaximumIntegrationStep(0.001)
    tracer.SetMaximumNumberOfSteps(20_000)
    tracer.Update()
    output = tracer.GetOutput()
    output_velocity = output.GetPointData().GetArray("U")
    if output.GetNumberOfCells() == 0 or output_velocity is None:
        return [], {**diagnostics, "reason": "积分没有产生含 U 场的有效流线"}
    vector = vtk_to_numpy(output_velocity)
    integration_time = output.GetPointData().GetArray("IntegrationTime")
    integration_time = vtk_to_numpy(integration_time) if integration_time is not None else None
    pressure = output.GetPointData().GetArray("p")
    pressure = vtk_to_numpy(pressure) if pressure is not None else None
    reasons = output.GetCellData().GetArray("ReasonForTermination")
    seed_ids = output.GetCellData().GetArray("SeedIds")
    termination_names = {1: "out_of_domain", 2: "not_initialized", 3: "unexpected_value",
                         4: "out_of_length", 5: "out_of_steps", 6: "stagnation"}
    result = []
    for i in range(min(output.GetNumberOfCells(), 64)):
        ids = output.GetCell(i).GetPointIds()
        indices = np.asarray([ids.GetId(j) for j in range(ids.GetNumberOfIds())], dtype=np.int64)
        if len(indices) < 2:
            continue
        positions = np.asarray([output.GetPoint(int(index)) for index in indices], dtype=np.float32)
        code = int(reasons.GetValue(i)) if reasons is not None else None
        seed_id = int(seed_ids.GetValue(i)) if seed_ids is not None else i
        line = {"positions": _flat(positions), "speeds": np.linalg.norm(vector[indices], axis=1).astype(np.float32).tolist(),
                "termination": termination_names.get(code, "unknown"), "termination_code": code,
                "seed_id": seed_id, "seed_face_id": face_ids[selected[seed_id]] if 0 <= seed_id < len(selected) else None,
                "length_m": float(np.linalg.norm(np.diff(positions, axis=0), axis=1).sum())}
        if integration_time is not None:
            line["integration_time_s"] = integration_time[indices].tolist()
        if pressure is not None:
            line["pressure"] = pressure[indices].astype(np.float32).tolist()
        result.append(line)
    return result, {**diagnostics, "status": "ready" if result else "empty", "lines": len(result),
                    "integrated_seed_fraction": len(result) / len(selected),
                    "termination_counts": {reason: sum(line["termination"] == reason for line in result) for reason in set(line["termination"] for line in result)}}


def _slices(mesh) -> list[dict]:
    """Create three orthogonal internal cuts from the interpolated final fields."""
    converter = vtk.vtkCellDataToPointData()
    converter.SetInputData(mesh)
    converter.PassCellDataOn()
    converter.Update()
    source = converter.GetOutput()
    bounds = source.GetBounds()
    result = []
    for axis, coordinate in zip("xyz", ((bounds[0] + bounds[1]) / 2, (bounds[2] + bounds[3]) / 2, (bounds[4] + bounds[5]) / 2)):
        plane = vtk.vtkPlane()
        origin = [0.0, 0.0, 0.0]
        origin["xyz".index(axis)] = coordinate
        normal = [0.0, 0.0, 0.0]
        normal["xyz".index(axis)] = 1.0
        plane.SetOrigin(*origin)
        plane.SetNormal(*normal)
        cutter = vtk.vtkCutter()
        cutter.SetCutFunction(plane)
        cutter.SetInputData(source)
        cutter.Update()
        triangulate = vtk.vtkTriangleFilter()
        triangulate.SetInputConnection(cutter.GetOutputPort())
        triangulate.Update()
        cut = triangulate.GetOutput()
        if cut.GetNumberOfPoints() == 0:
            continue
        positions = vtk_to_numpy(cut.GetPoints().GetData()).astype(np.float32)
        velocity = cut.GetPointData().GetArray("U")
        pressure = cut.GetPointData().GetArray("p")
        if velocity is None:
            continue
        speeds = np.linalg.norm(vtk_to_numpy(velocity), axis=1).astype(np.float32)
        values = vtk_to_numpy(pressure).astype(np.float32).tolist() if pressure is not None else []
        indices = []
        for cell_index in range(cut.GetNumberOfCells()):
            ids = cut.GetCell(cell_index).GetPointIds()
            if ids.GetNumberOfIds() == 3:
                indices.extend(ids.GetId(i) for i in range(3))
        result.append({"axis": axis, "positions": _flat(positions), "indices": indices,
                       "speeds": speeds.tolist(), "pressure": values})
    return result


def run_postprocess(case_id: str, progress_callback: Callable[[dict], None] | None = None, cancel_event=None) -> dict:
    root = case_dir(case_id)
    foam, signature, time_name = _source(root)
    pressure_unit = _pressure_unit(root / "cfd" / time_name / "p")
    if vtk is None:
        raise PipelineError("failed", "当前 Python 环境没有 VTK", ["在工作台虚拟环境安装 VTK"])

    def step(stage: str, message: str):
        if cancel_event is not None and cancel_event.is_set():
            raise PipelineError("failed", "后处理已取消")
        _emit(progress_callback, "progress", stage, message)

    step("read_fields", "读取最终时间步")
    if not foam.exists():
        foam.write_text("", encoding="ascii")
    reader = vtk.vtkOpenFOAMReader()
    reader.SetFileName(str(foam))
    reader.SetSkipZeroTime(True)
    reader.SetCreateCellToPoint(False)
    reader.UpdateInformation()
    times = reader.GetTimeValues()
    if times is None or times.GetNumberOfValues() == 0:
        raise PipelineError("failed", "OpenFOAM 没有可读时间步")
    timestep = float(time_name)
    if timestep not in [times.GetValue(i) for i in range(times.GetNumberOfValues())]:
        raise PipelineError("failed", f"VTK 不能读取选定的结果时间步 {time_name}")
    reader.DisableAllPatchArrays()
    reader.SetPatchArrayStatus("internalMesh", 1)
    reader.SetPatchArrayStatus("patch/wall", 1)
    reader.SetPatchArrayStatus("patch/inlet", 1)
    reader.SetPatchArrayStatus("patch/outlet", 1)
    reader.DisableAllCellArrays()
    reader.SetCellArrayStatus("U", 1)
    reader.SetCellArrayStatus("p", 1)
    reader.SetTimeValue(timestep)
    reader.Update()
    data = reader.GetOutput()
    mesh = data.GetBlock(0) if data.IsA("vtkMultiBlockDataSet") else data
    if mesh is None or not mesh.IsA("vtkDataSet"):
        raise PipelineError("failed", "VTK 没有返回 internalMesh")
    u, p = mesh.GetCellData().GetArray("U"), mesh.GetCellData().GetArray("p")
    points = mesh.GetPoints()
    if u is None or p is None or points is None:
        raise PipelineError("failed", "最终时间步缺少 U、p 或网格坐标")
    ua, pa = vtk_to_numpy(u), vtk_to_numpy(p).astype(np.float32)
    xyz = vtk_to_numpy(points.GetData()).astype(np.float32)
    count = mesh.GetNumberOfCells()
    if count == 0 or ua.shape != (count, 3) or pa.shape != (count,):
        raise PipelineError("failed", "VTK 返回的 U、p 数量或分量与内部网格不匹配")
    if not all(np.isfinite(array).all() for array in (ua, pa, xyz)):
        raise PipelineError("failed", "最终求解场或网格含 NaN 或 Infinity，结果未发布")
    boundary = data.GetBlock(1) if data.IsA("vtkMultiBlockDataSet") and data.GetNumberOfBlocks() > 1 else None
    step("wall", "提取 wall、inlet、outlet 边界")
    wall = _sample_patch(_patch_block(boundary, "wall"), limit=120_000)
    inlet = _patch_block(boundary, "inlet")
    outlet = _patch_block(boundary, "outlet")
    step("streamlines", "从入口种子积分流线")
    streamlines, streamline_metadata = _streamlines(mesh, inlet)
    step("slices", "生成三正交速度压力切片")
    slices = _slices(mesh)
    stride = max(1, int(np.ceil(len(xyz) / MAX_POINTS)))
    sampled = np.arange(0, len(xyz), stride)
    payload = {
        "units": {"coordinates": "m", "velocity": "m/s", "pressure": pressure_unit},
        "bounds_m": [float(v) for v in mesh.GetBounds()],
        "wall": wall,
        "patches": {"inlet": _sample_patch(inlet), "outlet": _sample_patch(outlet)},
        "streamlines": streamlines,
        "slices": slices,
        "ranges": {"velocity": [float(np.linalg.norm(ua, axis=1).min()), float(np.linalg.norm(ua, axis=1).max())], "pressure": [float(pa.min()), float(pa.max())]},
        "source": {"case_id": case_id, "timestep": timestep, "fields": ["U", "p"], "source_signature": signature},
        "metadata": {"solver": "simpleFoam", "pressure_reference": "kinematic" if pressure_unit == "m2/s2" else "pressure",
                     "streamlines": streamline_metadata, "sampling": {"point_stride": stride, "source_cells": int(len(pa)), "boundary_geometry": "sampled triangulated faces"}},
    }
    step("publish", "原子发布浏览器结果")
    if _source(root)[1] != signature:
        raise PipelineError("failed", "后处理期间求解场或网格已变化，结果未发布", ["等待上游任务结束后重新生成可视化"])
    output_dir = root / "cfd" / "results"
    generations = output_dir / "generations"
    generations.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".pending-", dir=generations))
    generation = generations / temporary.name.removeprefix(".pending-")
    manifest_temp = output_dir / f".{generation.name}.manifest.tmp"
    published = False
    try:
        (temporary / "payload.json").write_text(json.dumps(payload, separators=(",", ":"), allow_nan=False), encoding="utf-8")
        manifest = {"status": "ready", "payload": f"cfd/results/generations/{generation.name}/payload.json", "source_signature": signature, "source": payload["source"], "metadata": payload["metadata"], "counts": {"cells": int(len(pa)), "sampled_points": int(len(sampled)), "streamlines": len(streamlines), "slices": len(slices)}}
        temporary.replace(generation)
        manifest_temp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        manifest_temp.replace(output_dir / "manifest.json")
        published = True
    except (OSError, ValueError) as exc:
        raise PipelineError("failed", f"流场结果发布失败：{exc}", ["检查结果目录权限后重试后处理"]) from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        if not published and generation.exists():
            shutil.rmtree(generation, ignore_errors=True)
        if manifest_temp.exists():
            manifest_temp.unlink()
    return manifest


def get_results(case_id: str) -> dict:
    root = case_dir(case_id)
    path = root / "cfd" / "results" / "manifest.json"
    if not path.exists():
        return {"status": "missing"}
    result = json.loads(path.read_text(encoding="utf-8"))
    # 兼容早期已生成的清单；只有完整 payload 发布成功才算可查看。
    if result.get("status") == "ok":
        result["status"] = "ready"
    try:
        _, signature, _time_name = _source(root)
    except PipelineError as exc:
        return {**result, "status": "stale", "stale": True, "reason": exc.reason}
    result["stale"] = result.get("source_signature") != signature
    if result["stale"]:
        result["status"] = "stale"
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    args = parser.parse_args()

    def cli_event(event):
        print(json.dumps(event, ensure_ascii=False), flush=True)

    try:
        manifest = run_postprocess(args.case, progress_callback=cli_event)
        cli_event({"event": "complete", "stage": "publish", "message": "后处理完成", "manifest": manifest})
    except PipelineError as exc:
        cli_event({"event": "error", "stage": "publish", "message": exc.reason, "detail": exc.detail})
        raise SystemExit(1)
