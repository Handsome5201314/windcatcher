"""患者可读报告生成（真实数据与验证状态分开）。

报告只读取病例产物中的真实 CFD 数据。结果闸门决定能否形成工程设计结论，
不决定已有真实场数据是否可以作为“待复核观察值”展示。
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

from .config import DISCLAIMER
from .openfoam_runner import check_result_gate

SOURCE_LABEL = {
    "local_path": "本机本地目录",
    "browser_upload": "浏览器上传（服务主机处理）",
    "direct_link": "授权直链（本机下载）",
    "synthetic": "合成测试数据（非患者数据）",
}


def _load(case_id: str, rel: str) -> dict | None:
    from .cases import case_dir

    path = case_dir(case_id) / rel
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _load_path(path: Path) -> dict | None:
    """读取病例产物中的 JSON；报告只接受对象，避免把损坏文件当成结果。"""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _number(value) -> float | None:
    if value is None or isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _display(value, digits: int = 3) -> str:
    number = _number(value)
    if number is None:
        return "—"
    return f"{number:.{digits}f}"


def _flow_lpm(value_m3s: float | None) -> float | None:
    value = _number(value_m3s)
    # 正值表示穿出边界，不能通过 abs 把反向入口流量伪装为吸气。
    return _number(-value * 60000) if value is not None and value < -1e-12 else None


def _result_payload(case_id: str) -> tuple[dict | None, str]:
    """返回当前发布的浏览器流场及状态，不读取过期或未完成的 payload。"""
    from .cfd_results import get_results
    from .cases import case_dir

    try:
        manifest = get_results(case_id)
    except Exception as exc:  # 结果文件损坏时，报告仍应能生成并说明原因
        return None, f"流场结果不可读取：{exc}"
    if manifest.get("status") != "ready":
        return None, manifest.get("reason") or f"流场结果状态为 {manifest.get('status', 'missing')}"
    relative = manifest.get("payload")
    if not isinstance(relative, str) or not relative.startswith("cfd/results/"):
        return None, "流场结果清单缺少安全的 payload 路径"
    payload = _load_path(case_dir(case_id) / relative)
    if payload is None:
        return None, "流场 payload 缺失或 JSON 格式无效"
    return payload, "ready"


def _distribution(payload: dict | None) -> dict:
    """从已发布流场的真实采样点计算速度/压力分布摘要。"""
    if not payload:
        return {"status": "missing", "reason": "尚未发布可读取的流场 payload"}

    speeds: list[float] = []
    pressures: list[float] = []
    for line in payload.get("streamlines", []):
        if isinstance(line, dict):
            speeds.extend(value for value in (_number(item) for item in line.get("speeds", [])) if value is not None)
            pressures.extend(value for value in (_number(item) for item in line.get("pressure", [])) if value is not None)
    for cut in payload.get("slices", []):
        if isinstance(cut, dict):
            speeds.extend(value for value in (_number(item) for item in cut.get("speeds", [])) if value is not None)
            pressures.extend(value for value in (_number(item) for item in cut.get("pressure", [])) if value is not None)

    ranges = payload.get("ranges", {}) if isinstance(payload.get("ranges"), dict) else {}

    def summary(values: list[float], fallback, unit: str) -> dict:
        values = sorted(values)
        if not values:
            fallback_values = [value for value in (_number(item) for item in (fallback or [])) if value is not None]
            if fallback_values:
                return {
                    "status": "range_only", "unit": unit,
                    "min": min(fallback_values), "max": max(fallback_values),
                    "mean": None, "p95": None, "sample_count": 0,
                    "reason": "只有场值范围，没有可计算均值与 P95 的可视化采样",
                }
        if not values:
            return {"status": "missing", "unit": unit, "reason": "流场中没有可用的有限采样值"}
        index = min(len(values) - 1, int(round(0.95 * (len(values) - 1))))
        return {
            "status": "checked",
            "unit": unit,
            "min": values[0],
            "mean": sum(values) / len(values),
            "p95": values[index],
            "max": values[-1],
            "sample_count": len(values),
        }

    units = payload.get("units", {}) if isinstance(payload.get("units"), dict) else {}
    return {
        "status": "checked",
        "velocity": summary(speeds, ranges.get("velocity"), units.get("velocity", "m/s")),
        "pressure": summary(pressures, ranges.get("pressure"), units.get("pressure", "unknown")),
        "description": "流线与显示截面上的可视化采样摘要；不是全鼻腔体积加权统计",
        "source": "cfd/results/manifest.json 对应的已发布 payload",
    }


def _sample_provenance(gate: dict, names: tuple[str, ...]) -> dict:
    post = gate.get("postprocessing") or {}
    times = post.get("sample_times") or {}
    sources = post.get("sources") or {}
    final_time = _number((gate.get("fields") or {}).get("latest_time"))
    sampled = {name: _number(times.get(name)) for name in names}
    result = {
        "source": {name: sources.get(name) for name in names},
        "sample_times": sampled, "final_time": final_time,
        "sample_time": next(iter(sampled.values()), None),
    }
    if final_time is None or any(time is None or not sources.get(name) for name, time in sampled.items()):
        return {**result, "status": "missing", "reason": "缺少采样来源、采样时间或最终求解时间，不能确认指标来自最终场"}
    if any(time != final_time for time in sampled.values()):
        return {**result, "status": "stale", "reason": "指标采样时间与最终求解时间不一致，需要重新后处理"}
    return {**result, "status": "checked", "reason": None}


def _flow_measurement(gate: dict) -> dict:
    post = gate.get("postprocessing") or {}
    raw = _number(post.get("inlet_flow_m3s"))
    provenance = _sample_provenance(gate, ("inlet_flow_m3s",))
    measurement = {**provenance, "value": None, "unit": "L/min", "raw_boundary_flow_m3s": raw, "inhalation_flow_m3s": None}
    if raw is None:
        return {**measurement, "status": "missing", "direction": "unknown", "reason": "没有有限的实际入口流量采样"}
    if abs(raw) <= 1e-12:
        return {**measurement, "status": "invalid", "direction": "near_zero", "reason": "实际入口流量接近零，无法计算模拟鼻阻力"}
    if raw > 0:
        return {**measurement, "status": "invalid", "direction": "outward", "reason": "入口净流量方向为向外，不能作为吸气流量或吸气鼻阻力"}
    measurement["direction"] = "inward"
    if provenance["status"] != "checked":
        return measurement
    value = _flow_lpm(raw)
    if value is None:
        return {**measurement, "status": "invalid", "reason": "实际入口流量换算超出有限数值范围"}
    return {**measurement, "value": value, "inhalation_flow_m3s": -raw}


def _pressure_drop(gate: dict) -> dict:
    post = gate.get("postprocessing") or {}
    pressure = post.get("pressure") or {}
    field_unit = pressure.get("field_unit")
    density = _number(pressure.get("rho_kg_m3"))
    if density is not None and density <= 0:
        density = None
    unit = "Pa" if field_unit == "Pa" or (field_unit == "m2/s2" and density is not None) else field_unit
    provenance = _sample_provenance(gate, ("inlet_pressure_field", "outlet_pressure_field"))
    result = {**provenance, "value": None, "unit": unit, "field_unit": field_unit, "rho_kg_m3": density}
    inlet, outlet = _number(post.get("inlet_pressure_field")), _number(post.get("outlet_pressure_field"))
    if inlet is None or outlet is None:
        return {**result, "status": "missing", "reason": "入口或出口压力采样缺失，不能计算压降"}
    if pressure.get("status") != "checked" or field_unit not in {"Pa", "m2/s2"}:
        return {**result, "status": "invalid", "reason": "压力场维度或单位未确认，不能计算压降"}
    if provenance["status"] != "checked":
        return result
    delta = inlet - outlet
    if delta < 0:
        return {**result, "status": "invalid", "raw_delta_field": delta, "reason": "入口至出口压降为负，需要复核流向、边界条件和压力采样"}
    value = delta * density if field_unit == "m2/s2" and density is not None else delta
    if _number(value) is None:
        return {**result, "status": "invalid", "reason": "压降换算超出有限数值范围"}
    return {**result, "value": value, "reason": "旧算例未记录空气密度，压降只能以运动学压力表示" if unit == "m2/s2" else None}


def _resistance(flow: dict, pressure: dict) -> dict:
    result = {
        "status": "not_available", "value": None, "unit": "Pa·s/mL",
        "source": {"flow": flow.get("source"), "pressure": pressure.get("source")},
        "sample_time": pressure.get("sample_time"), "final_time": pressure.get("final_time"),
        "description": "当前模拟工况下入口至出口压降除以实际吸气流量；不是临床测量值",
    }
    if flow.get("status") != "checked":
        return {**result, "reason": flow.get("reason") or "实际吸气流量不可用"}
    if pressure.get("status") != "checked":
        return {**result, "reason": pressure.get("reason") or "入口至出口压降不可用"}
    if pressure.get("unit") != "Pa":
        return {**result, "reason": "压降不能换算为 Pa，缺少有效的空气密度记录，无法计算模拟鼻阻力"}
    q_ml_s = flow["inhalation_flow_m3s"] * 1e6
    value = _number(pressure["value"] / q_ml_s) if _number(q_ml_s) is not None else None
    if value is None:
        return {**result, "reason": "模拟鼻阻力换算超出有限数值范围"}
    return {**result, "status": "checked", "value": value, "reason": None}


def _region_flow(case_root: Path) -> dict:
    # 截面分析独立于分割；读取器会校验场、网格、CT 几何和结果签名。
    from .cfd_sections import get_sections

    sections = get_sections(case_root)
    if sections.get("status") != "ready":
        return {"status": sections.get("status", "missing"), "reason": sections.get("reason") or "尚未选择并计算分析截面", "regions": [], "sides": []}
    return {key: sections.get(key) for key in (
        "status", "analysis_type", "totals", "velocity", "regions", "sides", "plane", "validation", "analysis_signature",
        "source_signature", "geometry_signature", "inlet_condition", "unassigned", "coverage_complete",
    )}


def _flow_split(region_flow: dict, eligible: bool) -> dict:
    """只从经签名核对的截面积分读取左右分配，不接受手填 inlet_split。"""
    result = {"status": "not_available", "left_lpm": None, "right_lpm": None, "left_fraction": None, "right_fraction": None}
    if region_flow.get("status") != "ready":
        return {**result, "reason": region_flow.get("reason") or "尚未计算有效的截面流量"}
    if region_flow.get("analysis_type") == "whole_section":
        return {**result, "reason": "新整截面分析不再计算左右人工分区；如需左右比较，应在入口边界或独立物理量中核对"}
    sides = {side.get("id"): side for side in region_flow.get("sides") or [] if isinstance(side, dict)}
    left, right = sides.get("left", {}), sides.get("right", {})
    left_lpm, right_lpm = _number(left.get("forward_lpm")), _number(right.get("forward_lpm"))
    if left_lpm is None or right_lpm is None:
        return {**result, "reason": "分析截面没有完整的左右侧正向通量"}
    left_percent, right_percent = _number(left.get("percent_of_section")), _number(right.get("percent_of_section"))
    return {
        **result, "status": "checked" if eligible else "available_pending_validation",
        "left_lpm": left_lpm, "right_lpm": right_lpm,
        "left_fraction": left_percent / 100 if left_percent is not None else None,
        "right_fraction": right_percent / 100 if right_percent is not None else None,
        "total_lpm": _number((region_flow.get("totals") or {}).get("forward_lpm")),
        "source": "analysis_section_U_dot_n", "analysis_signature": region_flow.get("analysis_signature"),
        "description": "该分析截面的正向穿越通量；包含未标注区域时不重新归一化",
        "inlet_condition": region_flow.get("inlet_condition"),
    }


def _baseline_flow_report(cfd_meta: dict | None, run_status: dict | None, gate: dict, payload: dict | None, payload_status: str, region_flow: dict | None = None) -> dict:
    eligible = bool(gate.get("eligible"))
    flow = _flow_measurement(gate)
    pressure = _pressure_drop(gate)
    resistance = _resistance(flow, pressure)
    region_flow = region_flow or {"status": "missing", "reason": "尚未选择并计算分析截面"}
    distribution = _distribution(payload)
    has_numeric_data = flow.get("raw_boundary_flow_m3s") is not None or pressure.get("value") is not None
    has_data = has_numeric_data or payload is not None or region_flow.get("status") == "ready"
    has_run = bool(run_status and run_status.get("status") == "ran")
    data_status = "available" if has_data else "missing"
    validation_status = "passed" if eligible else ("pending" if has_run else "not_run")
    if not has_data:
        status = "missing"
    elif eligible:
        status = "ready" if payload is not None else "numeric_only"
    else:
        status = "pending_validation"
    return {
        "status": status,
        "data_status": data_status,
        "validation_status": validation_status,
        "validation_reasons": gate.get("reasons", []),
        "requested_flow_lpm": _number((cfd_meta or {}).get("total_flow_lpm")) if (cfd_meta or {}).get("inlet_mode", "flowRate") == "flowRate" else None,
        "flow_lpm": flow.get("value"),
        "flow_m3s": flow.get("raw_boundary_flow_m3s"),
        "flow_measurement": flow,
        "pressure_drop": pressure,
        "resistance": resistance,
        "velocity_distribution": distribution,
        "region_flow": region_flow,
        "left_right_flow": _flow_split(region_flow, eligible),
        "wall_shear_stress": {
            "status": "not_available", "value": None, "unit": "Pa",
            "reason": "当前算例未输出经单位与壁面位置核对的壁面剪切应力，不能从速度着色推算",
        },
        "narrow_regions": {
            "status": "not_available",
            "reason": "当前算例未输出沿程截面积曲线；速度 p95 只能提示高速区，不能替代狭窄解剖定位",
        },
        "source_status": payload_status,
    }


def _obstruction_comparison(case_id: str, baseline: dict) -> dict:
    """读取可选的 cfd/scenarios/<方案>/report.json，不存在时保持空结果。"""
    from .cases import case_dir

    directory = case_dir(case_id) / "cfd" / "scenarios"
    items = []
    if directory.is_dir():
        for path in sorted(directory.iterdir()):
            if not path.is_dir():
                continue
            item = _load_path(path / "report.json")
            if not item or not isinstance(item.get("flow_report"), dict):
                continue
            flow_report = item["flow_report"]
            items.append({
                "scenario_id": path.name,
                "label": item.get("label", path.name),
                "position": item.get("position"),
                "size_mm": _number(item.get("size_mm")),
                "occlusion_percent": _number(item.get("occlusion_percent")),
                "flow_report": flow_report,
            })
    if not items:
        return {
            "status": "not_available",
            "items": [],
            "scenario_directory": "cfd/scenarios/<方案>/report.json",
            "reason": "尚未生成鼻塞方案独立算例；每个位置、尺寸和阻塞率必须独立建模并重新求解",
        }
    base_dp = _number((baseline.get("pressure_drop") or {}).get("value"))
    base_flow = _number(baseline.get("flow_lpm"))
    for item in items:
        flow = item["flow_report"]
        dp = _number((flow.get("pressure_drop") or {}).get("value"))
        flow_lpm = _number(flow.get("flow_lpm"))
        same_pressure_unit = (flow.get("pressure_drop") or {}).get("unit") == (baseline.get("pressure_drop") or {}).get("unit")
        item["comparison"] = {
            "delta_pressure_percent": ((dp - base_dp) / base_dp * 100) if same_pressure_unit and dp is not None and base_dp not in (None, 0) else None,
            "delta_flow_percent": ((flow_lpm - base_flow) / base_flow * 100) if flow_lpm is not None and base_flow not in (None, 0) else None,
        }
    return {"status": "checked", "items": items, "scenario_directory": "cfd/scenarios/<方案>/report.json"}


def _design_reference(baseline: dict, comparison: dict) -> dict:
    if baseline.get("data_status") != "available":
        return {
            "status": "pending",
            "conclusions": ["基准流场数据尚未生成，暂不能形成鼻塞设计结论。"],
            "uncertainty": ["需要先完成基准 CFD 求解并发布可追溯的流场数据。"],
        }
    if baseline.get("validation_status") != "passed":
        return {
            "status": "pending_validation",
            "conclusions": ["已读取基准鼻腔流场观察值，但结果闸门尚未通过，暂不输出鼻塞设计推荐。"],
            "uncertainty": [
                "需要完成残差、质量守恒、最终场完整性、网格与封闭壳、网格敏感性和 CFD 工程师复核。",
                "当前数值只能用于查看真实场和定位下一步复核项。",
            ],
        }
    if comparison.get("status") != "checked":
        return {
            "status": "baseline_only",
            "conclusions": ["已具备基准鼻腔流场数据；尚未完成鼻塞前后对比，因此不输出改善或推荐结论。"],
            "uncertainty": [
                "左右与上中下比例描述所选分析截面上的正向穿越通量，不能解释为整个鼻腔体积的气流比例。",
                "当前算例未输出沿程截面积曲线，不能仅凭局部速度把区域判定为解剖狭窄。",
                "固定流量入口工况下，流量本身被指定，设计比较应优先观察压降和高速区。",
            ],
        }
    items = comparison.get("items") or []
    conclusions: list[str] = []
    # 把方案卡中的真实变化归纳到结论，避免报告只重复“已完成对比”。
    pressure_items = [
        item for item in items
        if _number((item.get("comparison") or {}).get("delta_pressure_percent")) is not None
    ]
    if pressure_items:
        best_pressure = min(
            pressure_items,
            key=lambda item: _number((item.get("comparison") or {}).get("delta_pressure_percent"))
        )
        change = _number((best_pressure.get("comparison") or {}).get("delta_pressure_percent"))
        label = best_pressure.get("label") or best_pressure.get("scenario_id") or "该方案"
        conclusions.append(f"压降相对基准最低的方案为“{label}”，变化 {change:+.1f}%；数值越低表示在相同入口流量工况下阻力增加较少。")
    else:
        conclusions.append("各鼻塞方案尚未提供可比较的压降变化，暂不判断阻力改善或恶化。")

    flow_items = [
        item for item in items
        if _number((item.get("comparison") or {}).get("delta_flow_percent")) is not None
    ]
    if flow_items:
        best_flow = max(
            flow_items,
            key=lambda item: _number((item.get("comparison") or {}).get("delta_flow_percent"))
        )
        change = _number((best_flow.get("comparison") or {}).get("delta_flow_percent"))
        label = best_flow.get("label") or best_flow.get("scenario_id") or "该方案"
        conclusions.append(f"流量相对基准变化最大的方案为“{label}”，变化 {change:+.1f}%；固定流量边界下该指标通常不会反映自然呼吸通气改善。")
    else:
        conclusions.append("各鼻塞方案尚未提供流量变化，暂不判断通气改善。")

    baseline_p95 = _number((((baseline.get("velocity_distribution") or {}).get("velocity") or {}).get("p95")))
    speed_items = []
    for item in items:
        p95 = _number((((item.get("flow_report") or {}).get("velocity_distribution") or {}).get("velocity") or {}).get("p95"))
        if p95 is not None:
            speed_items.append((item, p95))
    if speed_items:
        high_speed = max(speed_items, key=lambda pair: pair[1])
        item, p95 = high_speed
        label = item.get("label") or item.get("scenario_id") or "该方案"
        if baseline_p95 is not None:
            delta = (p95 - baseline_p95) / baseline_p95 * 100 if baseline_p95 else None
            suffix = f"，较基准 P95 {_number(delta):+.1f}%" if delta is not None else ""
        else:
            suffix = "，基准 P95 尚未记录"
        conclusions.append(f"局部高速区提示：方案“{label}”的可视化采样速度 P95 最高，为 {p95:.3g} m/s{suffix}；需要结合位置和截面积复核其解剖意义。")
    else:
        conclusions.append("各鼻塞方案尚未提供速度 P95，暂不判断局部高速区变化。")

    conclusions.append("鼻塞方案已按独立算例完成对比；只有在边界条件、网格与复核状态一致时，以上变化才可横向解释。")
    return {
        "status": "checked",
        "conclusions": conclusions,
        "uncertainty": [
            "CT 层厚、分割边界、端面位置和网格敏感性会直接影响比较结果。",
            "若使用固定流量入口，流量改善百分比不代表自然呼吸下的通气改善；需另行建立压力驱动工况。",
            "速度 P95 只能提示局部高速区，不能单独证明解剖狭窄或疗效；应结合沿程截面积和可视化复核。",
            "结果用于鼻塞设计研究参考，不构成诊断或治疗建议。",
        ],
    }


def build_report(case_id: str) -> dict:
    """生成 Markdown + JSON 报告，返回报告内容与路径。"""
    from .cases import case_dir, get_case, update_stage

    case = get_case(case_id)
    root = case_dir(case_id)
    qc = _load(case_id, "volume/qc.json")
    seg = _load(case_id, "segment/segmentation.json")
    surf = _load(case_id, "surface/surface.json")
    geom = _load(case_id, "surface/geom_qc.json")
    cfd_meta = _load(case_id, "cfd/case_meta.json")
    run_status = _load(case_id, "cfd/run_status.json")
    gate = check_result_gate(case_id)
    caps = _load(case_id, "capabilities.json") or {}
    result_payload, payload_status = _result_payload(case_id)
    region_flow = _region_flow(root)
    baseline_flow = _baseline_flow_report(cfd_meta, run_status, gate, result_payload, payload_status, region_flow)
    obstruction_comparison = _obstruction_comparison(case_id, baseline_flow)
    design_reference = _design_reference(baseline_flow, obstruction_comparison)

    lines: list[str] = []
    lines.append(f"# 鼻腔气流建模报告 · {case_id}")
    lines.append("")
    lines.append(f"- 生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}")
    lines.append(f"- 数据来源：{SOURCE_LABEL.get(case.get('source_type'), case.get('source_type'))}")
    lines.append(f"- 合成/公开测试数据：{'是（不可作为患者结果引用）' if case.get('synthetic') else '否'}")
    lines.append(f"- 管线：风见（Windcatcher）本地优先 · 仅在本机处理，未上传任何数据")
    lines.append("")

    lines.append("## 1. 输入与质控")
    if qc:
        series = qc.get("primary_series", {})
        lines.append(f"- 序列模态：{series.get('modality')}，文件数 {series.get('files')}")
        lines.append(f"- 层厚：{series.get('slice_thickness_mm')} mm；像素间距：{series.get('pixel_spacing_mm')} mm")
        lines.append(f"- 扫描范围（实测）：前后方向 {qc.get('anatomical_coverage', {}).get('measured', {}).get('in_plane_ap_extent_mm')} mm，"
                     f"上下方向 {qc.get('anatomical_coverage', {}).get('measured', {}).get('si_extent_mm')} mm")
        lines.append(f"- 质控结论：{qc.get('verdict')}（{'阻断' if qc.get('blocking') else '未阻断'}）")
        for reason in qc.get("reasons", []):
            lines.append(f"  - {reason}")
        coverage = qc.get("anatomical_coverage", {})
        lines.append(f"- 解剖覆盖：{coverage.get('status')} —— {coverage.get('reason', '')}")
        privacy = qc.get("privacy", {})
        lines.append(f"- 输入含识别性标签：{'是' if privacy.get('contains_phi') else '否'}（仅记录是否包含，不记录取值）")
    else:
        lines.append("- 尚未执行输入质控")
    lines.append("")

    lines.append("## 2. 气道分割")
    if seg:
        params = seg.get("params", {})
        metrics = seg.get("metrics", {})
        lines.append(f"- 阈值区间：{params.get('hu_low')} ~ {params.get('hu_high')} HU；连通性：{params.get('connectivity')}")
        lines.append(f"- 前后轴判定：{seg.get('ap_axis_source')}")
        lines.append(f"- 端面位置：前 {params.get('anterior_plane')} / 后 {params.get('posterior_plane')}"
                     f"（{'自动取连通域端点' if seg.get('planes_auto') else '人工指定'}）")
        lines.append(f"- 人工复核：{seg.get('human_review', {}).get('status')}"
                     + "；待确认：" + "、".join(seg.get("human_review", {}).get("items", [])))
        lines.append(f"- 气道体积：{metrics.get('volume_ml')} mL；前后长度：{metrics.get('ap_length_mm')} mm")
        lines.append(f"- 开口面积：鼻孔端 {metrics.get('anterior_opening_area_mm2')} mm²，"
                     f"鼻咽端 {metrics.get('posterior_opening_area_mm2')} mm²")
        lines.append(f"- 连通域：共 {seg.get('components_total')} 个，采用其中主气道"
                     f"（体积 {seg.get('chosen_component', {}).get('volume_mm3')} mm³）")
        cavity = seg.get("cavity_filling", {})
        if cavity.get("cavities_filled"):
            lines.append(f"- 已填充封闭微小空腔 {cavity.get('cavities_filled')} 体素（{cavity.get('cavity_volume_mm3')} mm³）")
    else:
        lines.append("- 尚未执行分割")
    lines.append("")

    lines.append("## 3. 几何与网格前检查")
    if geom:
        lines.append(f"- 面数 {geom.get('faces')}，连通分量 {geom.get('connected_components')}")
        by_end = geom.get("openings_by_end", {})
        lines.append(f"- 开口：鼻孔端 {by_end.get('anterior')} 个，鼻咽端 {by_end.get('posterior')} 个，"
                     f"中段 {by_end.get('middle')} 个")
        lines.append(f"- 退化面 {geom.get('degenerate_faces')}，非流形边 {geom.get('non_manifold_edges')}，"
                     f"重复顶点 {geom.get('duplicate_vertices')}")
        volume_check = geom.get("volume_check", {})
        if volume_check.get("status") == "checked":
            lines.append(f"- 体积对照：网格 {volume_check.get('mesh_volume_ml')} mL vs 掩膜 {volume_check.get('mask_volume_ml')} mL"
                         f"（偏差 {volume_check.get('deviation_percent')}%）")
        lines.append(f"- 质检结论：{geom.get('verdict')}")
        for reason in geom.get("reasons", []):
            lines.append(f"  - {reason}")
        si = geom.get("self_intersection", {})
        lines.append(f"- 自交检测：{si.get('status')} —— {si.get('reason', '')}")
    else:
        lines.append("- 尚未执行几何质检")
    lines.append("")

    lines.append("## 4. CFD 结果")
    if cfd_meta:
        lines.append(f"- 求解器：{cfd_meta.get('solver')}（OpenFOAM 分支：{cfd_meta.get('openfoam_flavour')}，"
                     f"湍流模型：{cfd_meta.get('turbulence_model')}）")
        mode = cfd_meta.get("inlet_mode")
        if mode == "pressure":
            condition = f"压力入口 {_display(cfd_meta.get('inlet_pressure_pa'))} Pa（不预设流量）"
        elif mode == "flowRate":
            condition = f"双侧合计设定流量 {_display(cfd_meta.get('total_flow_lpm'))} L/min"
        else:
            condition = "入口工况未记录，不能解释为设定流量"
        lines.append(f"- 工况：{condition}；入口面积 {_display(cfd_meta.get('inlet_area_mm2'))} mm²；平均入口速度 {_display(cfd_meta.get('mean_inlet_velocity_mps'))} m/s")
        lines.append(f"- 物性：ν = {cfd_meta.get('nu')} m²/s；背景网格单元 {cfd_meta.get('background_cell_mm')} mm")
        lines.append(f"- 算例状态：{cfd_meta.get('validation', {}).get('status')} —— {cfd_meta.get('validation', {}).get('note', '')}")
    else:
        lines.append("- 尚未生成算例")

    if run_status and run_status.get("status") == "missing":
        lines.append("")
        lines.append("### **未生成 CFD 结果**")
        lines.append("")
        lines.append(f"原因：{run_status.get('message')}")
        lines.append("")
        lines.append("下一步：")
        for index, item in enumerate(run_status.get("remediation", []), start=1):
            lines.append(f"{index}. {item}")
    elif run_status and run_status.get("status") == "failed":
        lines.append("")
        lines.append("### **求解失败**")
        lines.append(f"- 失败步骤：{run_status.get('failed_step')}")
        for item in run_status.get("remediation", []):
            lines.append(f"- {item}")
    elif run_status and run_status.get("status") == "ran":
        lines.append("")
        lines.append("### 求解已执行，待复核")
        lines.append(f"- 后端：{run_status.get('backend')}")
        lines.append(f"- 复核状态：{run_status.get('validation', {}).get('status')}")
        if gate.get("eligible"):
            lines.append("- 已通过的闸门项：残差收敛、质量守恒、场文件完整")
        else:
            lines.append("- 未进入指标区：以下闸门项尚未全部通过")
            for check in gate.get("checks", []):
                lines.append(f"  - {check['name']}：{check['status']}（{check['detail']}）")
    else:
        lines.append("")
        lines.append("### **未生成 CFD 结果**")
        lines.append("- 原因：尚未执行求解或尚未生成算例")
    lines.append("")

    lines.append("## 5. 基准鼻腔流场报告")
    if baseline_flow.get("data_status") == "available":
        if baseline_flow.get("validation_status") == "passed":
            status_text = "已生成真实流场并通过结果闸门" if baseline_flow.get("status") == "ready" else "已读取真实 CFD 指标，浏览器流场 payload 不可用"
        else:
            status_text = "已读取真实 CFD 结果，待数值与工程复核"
        lines.append(f"- 状态：{status_text}")
        lines.append(f"- 入口设定流量：{_display(baseline_flow.get('requested_flow_lpm'))} L/min（不代替实际流量采样）")
        flow = baseline_flow.get("flow_measurement", {})
        if flow.get("status") == "checked":
            lines.append(f"- 实际入口流量：{_display(flow.get('value'))} L/min（吸气；边界带符号流量 {_display(flow.get('raw_boundary_flow_m3s'), 7)} m³/s）")
        else:
            lines.append(f"- 实际入口流量：不可用；{flow.get('reason') or '采样尚未核对'}")
        pressure = baseline_flow.get("pressure_drop", {})
        if pressure.get("status") == "checked":
            lines.append(f"- 入口至出口压降：{_display(pressure.get('value'))} {pressure.get('unit') or '—'}")
            if pressure.get("reason"):
                lines.append(f"  - {pressure['reason']}")
        else:
            lines.append(f"- 入口至出口压降：不可用；{pressure.get('reason') or '采样尚未核对'}")
        resistance = baseline_flow.get("resistance", {})
        if resistance.get("status") == "checked":
            lines.append(f"- 模拟鼻阻力：{_display(resistance.get('value'), 5)} {resistance['unit']}（当前工况；非临床测量值）")
        else:
            lines.append(f"- 模拟鼻阻力：不可用；{resistance.get('reason')}")
        velocity = baseline_flow.get("velocity_distribution", {}).get("velocity", {})
        if velocity.get("status") == "checked":
            lines.append(f"- 速度分布（可视化采样摘要）：最小 {_display(velocity.get('min'))}、采样平均 {_display(velocity.get('mean'))}、P95 {_display(velocity.get('p95'))}、最大 {_display(velocity.get('max'))} {velocity.get('unit', 'm/s')}（{velocity.get('sample_count')} 个采样值；非全鼻腔体积加权统计）")
        else:
            lines.append(f"- 速度分布：{velocity.get('reason') or baseline_flow.get('velocity_distribution', {}).get('reason', '未生成')}")
        regions = baseline_flow.get("region_flow", {})
        if regions.get("analysis_type") != "whole_section":
            split = baseline_flow.get("left_right_flow", {})
            if split.get("status") in {"checked", "available_pending_validation"}:
                left_fraction = _number(split.get("left_fraction"))
                right_fraction = _number(split.get("right_fraction"))
                left_percent = left_fraction * 100 if left_fraction is not None else None
                right_percent = right_fraction * 100 if right_fraction is not None else None
                lines.append(f"- 截面左右侧正向通量：左 {_display(split.get('left_lpm'))} L/min（{_display(left_percent, 1)}%），右 {_display(split.get('right_lpm'))} L/min（{_display(right_percent, 1)}%）")
            elif regions.get("status") != "ready":
                lines.append(f"- 左右侧流量分配：{split.get('reason', '未生成')}")
        if regions.get("status") == "ready":
            if regions.get("analysis_type") == "whole_section":
                totals = regions.get("totals") or {}
                velocity = regions.get("velocity") or {}
                plane = regions.get("plane") or {}
                lines.append(f"- 整截面分析：位置 {_display(_number(plane.get('position_fraction')) * 100 if _number(plane.get('position_fraction')) is not None else None, 1)}%，坐标 {_display(plane.get('position_coordinate_mm'))} mm，面积 {_display(totals.get('area_mm2'))} mm²。")
                lines.append(f"- 整截面流量：正向 {_display(totals.get('forward_lpm'))}、反向 {_display(totals.get('reverse_lpm'))}、净流量 {_display(totals.get('net_lpm'))} L/min。")
                lines.append(f"- 整截面速度：面积加权平均 {_display(velocity.get('mean_mps'))}、P95 {_display(velocity.get('p95_mps'))}、最大 {_display(velocity.get('max_mps'))} m/s。")
            else:
                lines.append("- 历史左右及上／中／下人工分区结果（仅为旧版截面记录）：比例按所在侧的正向穿越通量计算；漏标时不补齐或重新归一化。")
                for region in regions.get("regions") or []:
                    percent = _number(region.get("percent_of_side"))
                    lines.append(f"  - {region.get('label', region.get('id', '区域'))}：正向 {_display(region.get('forward_lpm'))}、反向 {_display(region.get('reverse_lpm'))}、净通量 {_display(region.get('net_lpm'))} L/min；本侧占比 {_display(percent, 1)}%")
                unassigned = regions.get("unassigned") or {}
                lines.append(f"  - 未标注区域：正向 {_display(unassigned.get('forward_lpm'))}、反向 {_display(unassigned.get('reverse_lpm'))}、净通量 {_display(unassigned.get('net_lpm'))} L/min；占整个截面 {_display(unassigned.get('percent_of_section'), 1)}%")
                if not regions.get("coverage_complete"):
                    lines.append("  - 截面标注覆盖不完整，上／中／下的本侧占比暂不可用。")
            inlet_condition = regions.get("inlet_condition") or {}
            if inlet_condition.get("description"):
                lines.append(f"  - 入口工况：{inlet_condition['description']}")
        else:
            lines.append(f"- 上／中／下分区结果：{regions.get('reason') or '尚未计算'}")
        lines.append(f"- 壁面剪切应力：{baseline_flow['wall_shear_stress']['reason']}")
        lines.append(f"- 狭窄区域：{baseline_flow.get('narrow_regions', {}).get('reason', '未生成')}")
        if baseline_flow.get("validation_reasons"):
            lines.append("- 待复核项：" + "；".join(str(item) for item in baseline_flow["validation_reasons"]))
    elif baseline_flow.get("status") == "pending_validation":
        lines.append("- 状态：求解已执行，但没有可读取的真实 CFD 指标。")
        lines.append("- 下一步：检查求解产物和流场发布状态，再重新生成报告。")
    else:
        lines.append("- 状态：尚未生成可用基准流场。")
    lines.append("")

    lines.append("## 6. 鼻塞方案对比报告")
    if obstruction_comparison.get("status") == "checked":
        lines.append(f"- 已读取 {len(obstruction_comparison.get('items', []))} 个独立鼻塞方案算例。")
        lines.append("- 方案 | 位置 | 尺寸（mm） | 阻塞率（%） | 压降变化（%） | 流量变化（%）")
        lines.append("- | - | -: | -: | -: | -:")
        for item in obstruction_comparison.get("items", []):
            change = item.get("comparison", {})
            lines.append(f"- {item.get('label', item.get('scenario_id'))} | {item.get('position') or '—'} | {_display(item.get('size_mm'))} | {_display(item.get('occlusion_percent'), 1)} | {_display(change.get('delta_pressure_percent'), 1)} | {_display(change.get('delta_flow_percent'), 1)}")
    else:
        lines.append(f"- 状态：{obstruction_comparison.get('reason', '尚未生成鼻塞方案对比')}。")
        lines.append("- 每个鼻塞位置、尺寸和阻塞率都必须生成独立几何与 CFD 算例，不能把基准病例数值复制为方案结果。")
    lines.append("")

    lines.append("## 7. 设计参考结论")
    for conclusion in design_reference.get("conclusions", []):
        lines.append(f"- {conclusion}")
    if design_reference.get("uncertainty"):
        lines.append("- 不确定性：")
        for item in design_reference["uncertainty"]:
            lines.append(f"  - {item}")
    lines.append("")

    lines.append("## 8. 限制与边界")
    lines.append(f"- {DISCLAIMER}")
    lines.append("- 本报告中的数值全部来自本机产物；没有数值的条目表示尚未生成，不用参考样本或估计值填充")
    lines.append("- 分割阈值、网格方案都会影响结果（参见分割阈值与网格独立性研究），跨病例比较需统一协议")
    if caps:
        lines.append(f"- 本机能力快照：{caps.get('generated_at', '未记录')}（详见工作台环境页）")
    lines.append("")

    markdown = "\n".join(lines)
    report_dir = root / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.md").write_text(markdown, encoding="utf-8")
    (report_dir / "report.json").write_text(
        json.dumps(
            {
                "case_id": case_id,
                "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "synthetic": case.get("synthetic", False),
                "source_type": case.get("source_type"),
                "qc": qc,
                "segmentation": seg,
                "surface": surf,
                "geom_qc": geom,
                "cfd_case": cfd_meta,
                "cfd_run": run_status,
                "gate": gate,
                "baseline_flow": baseline_flow,
                "obstruction_comparison": obstruction_comparison,
                "design_reference": design_reference,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    update_stage(
        case_id,
        "report",
        "ok",
        message="报告已生成（CFD 部分按真实状态填写）",
        artifacts=["report/report.md", "report/report.json"],
        data={
            "cfd_eligible": bool(gate.get("eligible")),
            "baseline_flow_status": baseline_flow.get("status"),
            "obstruction_comparison_count": len(obstruction_comparison.get("items", [])),
        },
    )
    return {
        "case_id": case_id,
        "markdown_path": "report/report.md",
        "json_path": "report/report.json",
        "cfd_eligible": bool(gate.get("eligible")),
        "markdown": markdown,
    }
