"""Read-only checks of OpenFOAM dictionaries, convergence and final fields."""

from __future__ import annotations

import functools
import json
import math
import mmap
import re
import warnings
from pathlib import Path

import numpy as np

NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
RESIDUAL_PATTERN = re.compile(
    rf"Solving for (\w+),\s+Initial residual = ({NUMBER}),\s+Final residual = ({NUMBER})"
)
KINEMATIC_PRESSURE_DIMENSIONS = [0, 2, -2, 0, 0, 0, 0]
PRESSURE_DIMENSIONS = [1, -1, -2, 0, 0, 0, 0]


def strip_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)


def dictionary_block(text: str, name: str) -> str | None:
    text = strip_comments(text)
    match = re.search(rf"\b{re.escape(name)}\s*\{{", text)
    if not match:
        return None
    depth = 1
    start = match.end()
    for index in range(start, len(text)):
        depth += (text[index] == "{") - (text[index] == "}")
        if depth == 0:
            return text[start:index]
    return None


def residual_controls(fv_solution_text: str | None) -> dict:
    block = dictionary_block(fv_solution_text or "", "residualControl")
    if block is None:
        return {}
    # SIMPLE accepts absolute tolerances. Includes and unsupported entries remain
    # observable rather than supplying guessed defaults.
    controls = {}
    for name, value in re.findall(rf'("[^"]+"|\w+)\s+({NUMBER})\s*;', block):
        tolerance = float(value)
        if math.isfinite(tolerance) and tolerance > 0:
            controls[name.strip('"')] = tolerance
    return controls


def parse_residuals(
    log_text: str, fv_solution_text: str | None = None, control_dict_text: str | None = None
) -> dict:
    time_pattern = re.compile(rf"^\s*Time\s*=\s*({NUMBER})(?:s)?\s*$", re.M)
    steps = []
    time_matches = list(time_pattern.finditer(log_text))
    for index, match in enumerate(time_matches):
        end = time_matches[index + 1].start() if index + 1 < len(time_matches) else len(log_text)
        initial, final = {}, {}
        for field, initial_value, final_value in RESIDUAL_PATTERN.findall(log_text[match.end():end]):
            # Non-orthogonal pressure corrections are inner solves. SIMPLE tests
            # the first initial residual of each field in the outer iteration.
            initial.setdefault(field, float(initial_value))
            final[field] = float(final_value)
        if initial:
            steps.append({"time": float(match.group(1)), "initial": initial, "final": final})
    if not steps:
        return {
            "status": "missing", "converged": False, "iterations": 0,
            "reason": "日志没有包含 Time 的完整外层残差记录", "stop_reason": "unknown",
        }
    controls = residual_controls(fv_solution_text)
    last = steps[-1]
    comparisons = []
    for name, tolerance in controls.items():
        try:
            pattern = re.compile(name)
        except re.error:
            comparisons.append({"field": name, "tolerance": tolerance, "passed": False, "reason": "无法解析字段表达式"})
            continue
        fields = [field for field in last["initial"] if pattern.fullmatch("U" if field in ("Ux", "Uy", "Uz") else field)]
        if name == "U" and not all(field in fields for field in ("Ux", "Uy", "Uz")):
            fields = []
        comparisons.append({
            "field": name, "tolerance": tolerance,
            "initial_residual": {field: last["initial"][field] for field in fields},
            "passed": bool(fields) and all(last["initial"][field] < tolerance for field in fields),
        })
    criteria_satisfied = bool(comparisons) and all(item["passed"] for item in comparisons)
    solver_declared = bool(re.search(r"SIMPLE\s+(?:solution\s+)?converged", log_text, flags=re.I))
    end_match = re.search(rf"\bendTime\s+({NUMBER})\s*;", strip_comments(control_dict_text or ""))
    end_time = float(end_match.group(1)) if end_match else None
    reached_end = end_time is not None and last["time"] >= end_time
    converged = solver_declared and criteria_satisfied
    return {
        "status": "parsed", "iterations": len(steps), "last_time": last["time"],
        "initial_residual": last["initial"], "final_residual": last["final"],
        "history": [{"time": item["time"], "initial": item["initial"]} for item in steps],
        "residual_control": controls, "criteria": comparisons, "criteria_satisfied": criteria_satisfied,
        "solver_declared_converged": solver_declared, "reached_end_time": reached_end,
        "end_time": end_time, "converged": converged,
        "stop_reason": "residual_control" if converged else "end_time_limit" if reached_end else "unverified_stop",
        "reason": "SIMPLE 声明收敛且满足配置的初始残差阈值" if converged else (
            "已达到 endTime，尚未确认稳态收敛" if reached_end else "未获得匹配 residualControl 的稳态收敛证据"
        ),
    }


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} 必须是 JSON 对象")
    return data


def pressure_metadata(dimensions: list[int] | None, case_meta: dict) -> dict:
    density = case_meta.get("rho_kg_m3")
    if density is not None and not isinstance(density, bool) and not (isinstance(density, str) and not density.strip()):
        try:
            density = float(density)
        except (TypeError, ValueError, OverflowError):
            density = None
    else:
        density = None
    if density is not None and (not math.isfinite(density) or density <= 0):
        density = None
    if dimensions == KINEMATIC_PRESSURE_DIMENSIONS:
        return {
            "status": "checked", "field_unit": "m2/s2", "dimensions": dimensions,
            "rho_kg_m3": density, "display_unit": "Pa" if density is not None else "m2/s2",
            "pa_multiplier": density,
            "reason": "p 为运动学压力；仅按算例已记录密度换算" if density is not None else "旧算例未记录空气密度，不能换算为 Pa",
        }
    if dimensions == PRESSURE_DIMENSIONS:
        return {"status": "checked", "field_unit": "Pa", "display_unit": "Pa", "dimensions": dimensions, "rho_kg_m3": density, "pa_multiplier": 1.0}
    return {"status": "invalid", "dimensions": dimensions, "field_unit": None, "display_unit": None, "pa_multiplier": None, "reason": "p 维度缺失或不支持"}


def _header(path: Path) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        return handle.read(8192).decode("ascii", errors="replace")


def _mesh_counts(cfd_dir: Path, mesh_dir: Path) -> dict:
    header = _header(mesh_dir / "owner")
    cell = re.search(r"\bnCells:(\d+)", header)
    internal = re.search(r"\bnInternalFaces:(\d+)", header)
    if not cell or not internal:
        log_path = cfd_dir / "logs" / "checkMesh.log"
        text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        cell = re.search(r"\bcells:\s*(\d+)", text)
        internal = re.search(r"\binternal faces:\s*(\d+)", text)
    if not cell or not internal:
        return {"status": "missing", "reason": "网格未包含可核对的单元与内部面数量"}
    boundary_path = mesh_dir / "boundary"
    if not boundary_path.exists():
        return {"status": "missing", "reason": "网格 boundary 文件缺失"}
    text = strip_comments(boundary_path.read_text(encoding="utf-8"))
    patches = {
        name: int(count) for name, count in re.findall(r"(\w+)\s*\{[^{}]*?nFaces\s+(\d+)\s*;[^{}]*\}", text, flags=re.S)
    }
    if not patches:
        return {"status": "invalid", "reason": "网格边界 patch 数量无法解析"}
    return {"status": "checked", "cells": int(cell.group(1)), "internal_faces": int(internal.group(1)), "patch_faces": patches}


def _numeric_values(data: bytes) -> np.ndarray:
    text = data.decode("ascii").replace("(", " ").replace(")", " ")
    if re.sub(rf"{NUMBER}|[+-]?(?:nan|inf(?:inity)?)|\s+", "", text, flags=re.I):
        raise ValueError("字段列表包含无效数字或未闭合语法")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        values = np.fromstring(text, sep=" ")
    if not np.isfinite(values).all():
        raise ValueError("字段含 NaN 或 Infinity")
    return values


def _direct_patch(data: mmap.mmap, boundary_start: int, entry_start: int) -> str | None:
    # Closed Function1 dictionaries may precede a patch's value. Resolve its
    # still-open scope rather than using the last opening brace in the file.
    scopes = []
    for match in re.compile(rb"[{}]").finditer(data, boundary_start, entry_start):
        if match.group() == b"{":
            scopes.append(match.start())
        elif scopes:
            scopes.pop()
        else:
            raise ValueError("字段值位于 boundaryField 之外")
    if not scopes:
        raise ValueError("边界字段值未包含在 patch 字典内")
    if len(scopes) > 1:
        return None  # Nested boundary-condition settings are not field values.
    brace = scopes[0]
    patch_match = re.search(rb"(\w+)\s*$", data[max(boundary_start, brace - 150):brace])
    return patch_match.group(1).decode("ascii") if patch_match else "unknown"


def _inspect_ascii_field(path: Path, name: str, mesh: dict) -> dict:
    result = {"status": "invalid", "path": path.name, "complete": False, "finite": False}
    if not path.exists() or path.stat().st_size == 0:
        return {**result, "status": "missing", "reason": f"{name} 场缺失或为空"}
    header = _header(path)
    format_match = re.search(r"\bformat\s+(\w+)\s*;", strip_comments(header))
    if not format_match or format_match.group(1) != "ascii":
        return {**result, "status": "unsupported", "reason": "场校验仅支持 ASCII；需要使用 OpenFOAM/VTK 校验二进制场"}
    dimensions_match = re.search(r"\bdimensions\s*\[([^]]+)\]\s*;", header)
    dimensions = [int(v) for v in dimensions_match.group(1).split()] if dimensions_match else None
    result["dimensions"] = dimensions
    expected_dimensions = {"U": [0, 1, -1, 0, 0, 0, 0], "phi": [0, 3, -1, 0, 0, 0, 0]}
    if name == "p":
        dimensions_ok = dimensions in (KINEMATIC_PRESSURE_DIMENSIONS, PRESSURE_DIMENSIONS)
    else:
        dimensions_ok = dimensions == expected_dimensions[name]
    if not dimensions_ok:
        return {**result, "reason": f"{name} 场维度不匹配"}
    object_match = re.search(r"\bobject\s+(\w+)\s*;", header)
    expected_class = "volVectorField" if name == "U" else "surfaceScalarField" if name == "phi" else "volScalarField"
    class_match = re.search(r"\bclass\s+(\w+)\s*;", header)
    if not object_match or object_match.group(1) != name or not class_match or class_match.group(1) != expected_class:
        return {**result, "reason": f"{name} 场对象名或类型不匹配"}
    expected_count = mesh["internal_faces"] if name == "phi" else mesh["cells"]
    internal_seen = False
    list_summaries = []
    patch_fluxes = {}
    try:
        with path.open("rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
            boundary_match = re.search(rb"\bboundaryField\s*\{", data)
            if not boundary_match:
                raise ValueError("场缺少 boundaryField")
            boundary_start = boundary_match.end()
            balance = 0
            for start in range(0, len(data), 1024 * 1024):
                chunk = data[start:start + 1024 * 1024]
                balance += chunk.count(b"{") - chunk.count(b"}")
            if balance != 0:
                raise ValueError("字段字典未闭合")
            list_pattern = re.compile(rb"(internalField|value)\s+nonuniform\s+List<(scalar|vector)>\s+(\d+)\s*\(")
            for match in list_pattern.finditer(data):
                is_internal = match.group(1) == b"internalField"
                patch = None if is_internal else _direct_patch(data, boundary_start, match.start())
                if not is_internal and patch is None:
                    continue
                arity = 3 if match.group(2) == b"vector" else 1
                declared = int(match.group(3))
                # mmap supports regex without loading a full 200 MB vector field.
                end_match = re.compile(rb"\)\s*;").search(data, match.end())
                if not end_match:
                    raise ValueError("非均匀场列表未闭合")
                observed = 0
                scalar_sum = 0.0
                cursor = match.end()
                while cursor < end_match.start():
                    end = min(cursor + 1024 * 1024, end_match.start())
                    if end < end_match.start():
                        newline = data.rfind(b"\n", cursor, end)
                        if newline < cursor:
                            newline = data.rfind(b" ", cursor, end)
                        if newline < cursor:
                            raise ValueError("字段数字行过长或无分隔符")
                        end = newline + 1
                    values = _numeric_values(data[cursor:end])
                    observed += values.size
                    scalar_sum += float(values.sum())
                    cursor = end
                if observed != declared * arity:
                    raise ValueError(f"非均匀场声明 {declared} 项，实际数字数量 {observed}，每项应有 {arity} 个分量")
                if is_internal:
                    if internal_seen or declared != expected_count or arity != (3 if name == "U" else 1):
                        raise ValueError(f"{name} 内部场数量/分量与网格不匹配，应为 {expected_count} 项")
                    internal_seen = True
                    result["count"] = declared
                else:
                    if patch not in mesh["patch_faces"] or declared != mesh["patch_faces"][patch]:
                        raise ValueError(f"{patch} 边界场数量与网格不匹配")
                    if name == "phi":
                        patch_fluxes[patch] = scalar_sum
                list_summaries.append({"internal": is_internal, "count": declared})
            for match in re.finditer(rb"(internalField|value)\s+uniform\s+([^;]+);", data):
                is_internal = match.group(1) == b"internalField"
                patch = None if is_internal else _direct_patch(data, boundary_start, match.start())
                if not is_internal and patch is None:
                    continue
                values = _numeric_values(match.group(2))
                if values.size != (3 if name == "U" else 1):
                    raise ValueError("均匀场分量数量不匹配")
                if is_internal:
                    if internal_seen:
                        raise ValueError("内部场重复定义")
                    internal_seen = True
                    result["count"] = expected_count
                elif name == "phi":
                    if patch not in mesh["patch_faces"]:
                        raise ValueError(f"未知 phi 边界 {patch}")
                    patch_fluxes[patch] = float(values[0]) * mesh["patch_faces"][patch]
            if not internal_seen:
                raise ValueError("无法解析 internalField")
            boundary_text = data[boundary_start:].decode("ascii")
            for patch in mesh["patch_faces"]:
                if not re.search(rf"\b{re.escape(patch)}\s*\{{", boundary_text):
                    raise ValueError(f"边界场缺少 {patch} patch")
    except (ValueError, OSError, UnicodeError, DeprecationWarning) as exc:
        return {**result, "reason": str(exc)}
    return {**result, "status": "checked", "complete": True, "finite": True, "lists": list_summaries, "patch_fluxes_m3s": patch_fluxes if name == "phi" else None}


def _signature(path: Path) -> tuple:
    if not path.exists():
        return (str(path), None, None)
    stat = path.stat()
    return (str(path), stat.st_size, stat.st_mtime_ns)


@functools.lru_cache(maxsize=8)
def _inspect_cached(root: str, time_name: str, signatures: tuple) -> dict:
    cfd_dir = Path(root)
    time_dir = cfd_dir / time_name
    mesh_dir = time_dir / "polyMesh" if (time_dir / "polyMesh").exists() else cfd_dir / "constant" / "polyMesh"
    mesh = _mesh_counts(cfd_dir, mesh_dir)
    if mesh.get("status") != "checked":
        return {"status": mesh["status"], "complete": False, "latest_time": time_name, "mesh": mesh, "reason": mesh["reason"]}
    fields = {name: _inspect_ascii_field(time_dir / name, name, mesh) for name in ("U", "p", "phi")}
    complete = all(field["complete"] for field in fields.values())
    return {"status": "checked" if complete else "invalid", "complete": complete, "latest_time": time_name, "mesh": mesh, "fields": fields}


def inspect_latest_fields(cfd_dir: Path) -> dict:
    times = []
    for path in cfd_dir.iterdir() if cfd_dir.exists() else []:
        if not path.is_dir():
            continue
        try:
            value = float(path.name)
        except ValueError:
            continue
        if math.isfinite(value) and value > 0:
            times.append((value, path.name))
    if not times:
        return {"status": "missing", "complete": False, "latest_time": None, "reason": "没有非零结果时间目录"}
    time_name = max(times)[1]
    mesh_dir = cfd_dir / time_name / "polyMesh"
    if not mesh_dir.exists():
        mesh_dir = cfd_dir / "constant" / "polyMesh"
    paths = [cfd_dir / time_name / name for name in ("U", "p", "phi")]
    paths += [mesh_dir / name for name in ("owner", "neighbour", "boundary")]
    paths.append(cfd_dir / "logs" / "checkMesh.log")
    return _inspect_cached(str(cfd_dir.resolve()), time_name, tuple(_signature(path) for path in paths))


def parse_mesh_checks(cfd_dir: Path) -> dict:
    path = cfd_dir / "logs" / "checkMesh.log"
    if not path.exists():
        return {"status": "missing", "clean": False, "reason": "缺少 checkMesh 日志"}
    text = path.read_text(encoding="utf-8", errors="replace")
    conflicts = re.findall(r"Writing\s+(\d+)\s+conflicting points.*?nonManifoldPoints", text)
    nonmanifold_points = sum(int(value) for value in conflicts)
    failed = re.search(r"Failed\s+(\d+)\s+mesh checks", text, flags=re.I)
    mesh_ok = bool(re.search(r"\bMesh OK\.", text)) and not failed
    meta = read_json(cfd_dir / "case_meta.json")
    cleaning = meta.get("stl_cleaning", {})
    closure_known = "open_edges" in cleaning and "nonmanifold_edges" in cleaning
    open_edges = cleaning.get("open_edges")
    nonmanifold_edges = cleaning.get("nonmanifold_edges")
    clean = mesh_ok and nonmanifold_points == 0 and closure_known and open_edges == 0 and nonmanifold_edges == 0
    return {
        "status": "checked", "mesh_ok": mesh_ok, "clean": clean,
        "failed_checks": int(failed.group(1)) if failed else 0,
        "nonmanifold_points": nonmanifold_points, "stl_open_edges": open_edges,
        "stl_nonmanifold_edges": nonmanifold_edges,
        "reason": "网格基础检查与封闭壳检查通过" if clean else "存在网格拓扑/封闭壳警告或检查证据缺失，需要定位复核",
    }
