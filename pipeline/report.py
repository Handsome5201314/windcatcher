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
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _display(value, digits: int = 3) -> str:
    number = _number(value)
    if number is None:
        return "—"
    return f"{number:.{digits}f}"


def _flow_lpm(value_m3s: float | None) -> float | None:
    value = _number(value_m3s)
    return abs(value) * 60000 if value is not None else None


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
            values = sorted(fallback_values)
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
        "source": "cfd/results/manifest.json 对应的已发布 payload",
    }


def _flow_split(cfd_meta: dict | None, gate: dict, eligible: bool) -> dict:
    """提取左右鼻孔流量；旧算例只有 aggregate inlet 时明确标记不可用。"""
    metrics = gate.get("metrics", {}) if isinstance(gate.get("metrics"), dict) else {}
    left = metrics.get("left_inlet_flow_m3s")
    right = metrics.get("right_inlet_flow_m3s")
    split = (cfd_meta or {}).get("inlet_split") if isinstance(cfd_meta, dict) else None
    if isinstance(split, dict):
        left = split.get("left_flow_m3s", split.get("left_lpm", left))
        right = split.get("right_flow_m3s", split.get("right_lpm", right))
        if _number(split.get("left_lpm")) is not None:
            left = _number(split.get("left_lpm")) / 60000
        if _number(split.get("right_lpm")) is not None:
            right = _number(split.get("right_lpm")) / 60000
    left_lpm, right_lpm = _flow_lpm(left), _flow_lpm(right)
    if left_lpm is None or right_lpm is None:
        return {
            "status": "not_available",
            "left_lpm": None,
            "right_lpm": None,
            "reason": "当前算例只输出 inlet 汇总流量，未建立左右鼻孔独立边界或采样",
        }
    total = left_lpm + right_lpm
    return {
        "status": "checked" if eligible else "available_pending_validation",
        "left_lpm": left_lpm,
        "right_lpm": right_lpm,
        "total_lpm": total,
        "left_fraction": left_lpm / total if total else None,
        "right_fraction": right_lpm / total if total else None,
    }


def _baseline_flow_report(cfd_meta: dict | None, run_status: dict | None, gate: dict, payload: dict | None, payload_status: str) -> dict:
    eligible = bool(gate.get("eligible"))
    metrics = gate.get("metrics", {}) if isinstance(gate.get("metrics"), dict) else {}
    flow_m3s = metrics.get("inlet_flow_m3s")
    # 旧算例可能没有记录 pressure_unit；优先使用明确单位，否则根据已有字段推断，
    # 避免把运动学压力误标为 Pa 或静默显示为空。
    pressure_unit = metrics.get("pressure_unit")
    if pressure_unit not in {"Pa", "m2/s2"}:
        if _number(metrics.get("delta_p_pa")) is not None:
            pressure_unit = "Pa"
        elif _number(metrics.get("delta_p_m2s2")) is not None:
            pressure_unit = "m2/s2"
        else:
            pressure_unit = "Pa"
    pressure_value = metrics.get("delta_p_pa") if pressure_unit == "Pa" else metrics.get("delta_p_m2s2")
    distribution = _distribution(payload)
    has_numeric_data = _number(flow_m3s) is not None or _number(pressure_value) is not None
    has_data = has_numeric_data or payload is not None
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
        "requested_flow_lpm": _number((cfd_meta or {}).get("total_flow_lpm")),
        "flow_lpm": _flow_lpm(flow_m3s),
        "flow_m3s": _number(flow_m3s),
        "pressure_drop": {"value": _number(pressure_value), "unit": pressure_unit},
        "velocity_distribution": distribution,
        "left_right_flow": _flow_split(cfd_meta, gate, eligible),
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
        item["comparison"] = {
            "delta_pressure_percent": ((dp - base_dp) / base_dp * 100) if dp is not None and base_dp not in (None, 0) else None,
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
                "当前算例未输出左右鼻孔独立流量。",
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
        conclusions.append(f"局部高速区提示：方案“{label}”的速度 P95 最高，为 {p95:.3g} m/s{suffix}；需要结合位置和截面积复核其解剖意义。")
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
    baseline_flow = _baseline_flow_report(cfd_meta, run_status, gate, result_payload, payload_status)
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
        lines.append(f"- 工况：双侧合计 {_display(cfd_meta.get('total_flow_lpm'))} L/min；"
                     f"入口面积 {_display(cfd_meta.get('inlet_area_mm2'))} mm²；平均入口速度 {_display(cfd_meta.get('mean_inlet_velocity_mps'))} m/s")
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
            metrics = gate.get("metrics", {})
            lines.append("- 已通过的闸门项：残差收敛、质量守恒、场文件完整")
            if metrics.get("delta_p_pa") is not None:
                lines.append(f"- 入口与出口压差 ΔP：{metrics.get('delta_p_pa')} Pa")
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
        lines.append(f"- 实际入口流量：{_display(baseline_flow.get('flow_lpm'))} L/min（{_display(baseline_flow.get('flow_m3s'), 7)} m³/s）")
        pressure = baseline_flow.get("pressure_drop", {})
        lines.append(f"- 入口至出口压降：{_display(pressure.get('value'))} {pressure.get('unit') or '—'}")
        velocity = baseline_flow.get("velocity_distribution", {}).get("velocity", {})
        if velocity.get("status") == "checked":
            lines.append(f"- 速度分布（真实采样）：最小 {_display(velocity.get('min'))}、平均 {_display(velocity.get('mean'))}、P95 {_display(velocity.get('p95'))}、最大 {_display(velocity.get('max'))} {velocity.get('unit', 'm/s')}（{velocity.get('sample_count')} 个采样值）")
        else:
            lines.append(f"- 速度分布：{baseline_flow.get('velocity_distribution', {}).get('reason', '未生成')}")
        split = baseline_flow.get("left_right_flow", {})
        if split.get("status") == "checked":
            left_fraction = _number(split.get("left_fraction"))
            right_fraction = _number(split.get("right_fraction"))
            left_percent = left_fraction * 100 if left_fraction is not None else None
            right_percent = right_fraction * 100 if right_fraction is not None else None
            lines.append(f"- 左右侧流量分配：左 {_display(split.get('left_lpm'))} L/min（{_display(left_percent, 1)}%），右 {_display(split.get('right_lpm'))} L/min（{_display(right_percent, 1)}%）")
        else:
            lines.append(f"- 左右侧流量分配：{split.get('reason', '未生成')}")
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
