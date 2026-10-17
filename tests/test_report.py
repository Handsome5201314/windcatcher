"""报告三段式 CFD 数据摘要测试。"""

from __future__ import annotations

import json

import pytest
from pipeline import cases, report


def _gate(metrics, *, eligible=False, reasons=None):
    """真实采样契约：最终场时间、带符号 phi 与入口/出口压力来源。"""
    unit = "Pa" if metrics.get("delta_p_pa") is not None else "m2/s2"
    delta = metrics.get("delta_p_pa") if unit == "Pa" else metrics.get("delta_p_m2s2")
    values = {"inlet_flow_m3s": metrics.get("inlet_flow_m3s"), "inlet_pressure_field": delta, "outlet_pressure_field": 0.0}
    return {
        "eligible": eligible, "metrics": metrics, "checks": [], "reasons": reasons or [],
        "fields": {"latest_time": "800"},
        "postprocessing": {
            **values, "sample_times": {name: 800.0 for name in values},
            "sources": {name: f"postProcessing/{name}/0/surfaceFieldValue.dat" for name in values},
            "pressure": {"status": "checked", "field_unit": unit, "display_unit": unit, "rho_kg_m3": None},
        },
    }


@pytest.fixture
def report_case(tmp_path, monkeypatch):
    monkeypatch.setattr("pipeline.config.CASES_DIR", tmp_path)
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    monkeypatch.setattr(report, "_region_flow", lambda _root: {"status": "missing", "reason": "尚未确认分析截面和区域标注"})
    case_id = cases.create_case("synthetic", "<合成测试>", synthetic=True)
    return case_id, cases.case_dir(case_id)


def test_report_declares_missing_obstruction_runs_without_fabricating_values(report_case, monkeypatch):
    case_id, root = report_case
    monkeypatch.setattr(
        report,
        "check_result_gate",
        lambda _case_id: {"eligible": False, "checks": [], "reasons": ["未求解"]},
    )

    built = report.build_report(case_id)
    document = json.loads((root / "report" / "report.json").read_text(encoding="utf-8"))

    assert "## 5. 基准鼻腔流场报告" in built["markdown"]
    assert "## 6. 鼻塞方案对比报告" in built["markdown"]
    assert "## 7. 设计参考结论" in built["markdown"]
    assert document["baseline_flow"]["status"] == "missing"
    assert document["obstruction_comparison"]["status"] == "not_available"
    assert document["design_reference"]["status"] == "pending"
    assert document["baseline_flow"]["pressure_drop"]["value"] is None


def test_report_uses_only_gate_metrics_for_real_baseline(report_case, monkeypatch):
    case_id, root = report_case
    cfd = root / "cfd"
    (cfd / "case_meta.json").write_text(
        json.dumps({"total_flow_lpm": 15.0, "mean_inlet_velocity_mps": 1.0}), encoding="utf-8"
    )
    (cfd / "run_status.json").write_text(json.dumps({"status": "ran"}), encoding="utf-8")
    (cfd / "results" / "generations" / "test").mkdir(parents=True)
    (cfd / "results" / "generations" / "test" / "payload.json").write_text(
        json.dumps(
            {
                "units": {"velocity": "m/s", "pressure": "Pa"},
                "ranges": {"velocity": [0.2, 2.0], "pressure": [1.0, 9.0]},
                "streamlines": [{"speeds": [0.2, 1.0, 2.0], "pressure": [1.0, 3.0, 9.0]}],
                "slices": [],
            }
        ),
        encoding="utf-8",
    )
    (cfd / "results" / "manifest.json").write_text(
        json.dumps(
            {
                "status": "ready",
                "payload": "cfd/results/generations/test/payload.json",
                "source_signature": "not-current",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(report, "_result_payload", lambda _case_id: (json.loads((cfd / "results" / "generations" / "test" / "payload.json").read_text()), "ready"))
    monkeypatch.setattr(
        report,
        "check_result_gate",
        lambda _case_id: _gate({
                "inlet_flow_m3s": -0.00025,
                "delta_p_pa": 34.0,
                "pressure_unit": "Pa",
            }, eligible=True),
    )

    built = report.build_report(case_id)
    document = json.loads((root / "report" / "report.json").read_text(encoding="utf-8"))
    baseline = document["baseline_flow"]

    assert built["cfd_eligible"] is True
    assert baseline["status"] == "ready"
    assert baseline["flow_lpm"] == pytest.approx(15.0)
    assert baseline["pressure_drop"]["value"] == 34.0
    assert baseline["pressure_drop"]["unit"] == "Pa"
    assert baseline["resistance"]["value"] == pytest.approx(0.136)
    assert baseline["velocity_distribution"]["velocity"]["p95"] == pytest.approx(2.0)
    assert "## 6. 鼻塞方案对比报告" in built["markdown"]


def test_report_keeps_real_observations_when_validation_is_pending(report_case, monkeypatch):
    case_id, root = report_case
    cfd = root / "cfd"
    (cfd / "case_meta.json").write_text(
        json.dumps({"total_flow_lpm": 15.0}), encoding="utf-8"
    )
    (cfd / "run_status.json").write_text(json.dumps({"status": "ran"}), encoding="utf-8")
    monkeypatch.setattr(
        report,
        "check_result_gate",
        lambda _case_id: _gate({
                "inlet_flow_m3s": -0.00025,
                "delta_p_m2s2": 34.0,
                "pressure_unit": "m2/s2",
            }, reasons=["网格敏感性尚未通过"]),
    )

    report.build_report(case_id)
    document = json.loads((root / "report" / "report.json").read_text(encoding="utf-8"))
    baseline = document["baseline_flow"]

    assert baseline["status"] == "pending_validation"
    assert baseline["data_status"] == "available"
    assert baseline["validation_status"] == "pending"
    assert baseline["flow_lpm"] == pytest.approx(15.0)
    assert baseline["pressure_drop"]["value"] == 34.0
    assert baseline["pressure_drop"]["unit"] == "m2/s2"
    assert baseline["resistance"]["value"] is None
    assert document["design_reference"]["status"] == "pending_validation"
    assert "实际入口流量：15.000 L/min" in (root / "report" / "report.md").read_text(encoding="utf-8")


def test_report_uses_verified_field_unit_for_legacy_kinematic_pressure(report_case, monkeypatch):
    case_id, root = report_case
    cfd = root / "cfd"
    (cfd / "case_meta.json").write_text(json.dumps({"total_flow_lpm": 15.0}), encoding="utf-8")
    (cfd / "run_status.json").write_text(json.dumps({"status": "ran"}), encoding="utf-8")
    monkeypatch.setattr(
        report,
        "check_result_gate",
        lambda _case_id: _gate({"inlet_flow_m3s": -0.00025, "delta_p_m2s2": 34.0}, reasons=["旧产物未记录空气密度"]),
    )

    report.build_report(case_id)
    document = json.loads((root / "report" / "report.json").read_text(encoding="utf-8"))

    assert document["baseline_flow"]["pressure_drop"]["value"] == 34.0
    assert document["baseline_flow"]["pressure_drop"]["unit"] == "m2/s2"


def test_report_renders_left_right_flow_fractions_as_percentages(report_case, monkeypatch):
    case_id, root = report_case
    cfd = root / "cfd"
    (cfd / "case_meta.json").write_text(
        json.dumps(
            {
                "total_flow_lpm": 15.0,
                "inlet_split": {"left_flow_m3s": -0.00015, "right_flow_m3s": -0.0001},
            }
        ),
        encoding="utf-8",
    )
    (cfd / "run_status.json").write_text(json.dumps({"status": "ran"}), encoding="utf-8")
    monkeypatch.setattr(report, "_region_flow", lambda _root: {
        "status": "ready", "analysis_signature": "synthetic-section", "totals": {"forward_lpm": 15.0}, "regions": [],
        "sides": [{"id": "left", "forward_lpm": 9.0, "percent_of_section": 60.0}, {"id": "right", "forward_lpm": 6.0, "percent_of_section": 40.0}],
    })
    monkeypatch.setattr(
        report,
        "check_result_gate",
        lambda _case_id: _gate({"inlet_flow_m3s": -0.00025, "delta_p_pa": 34.0, "pressure_unit": "Pa"}, eligible=True),
    )

    report.build_report(case_id)
    markdown = (root / "report" / "report.md").read_text(encoding="utf-8")

    assert "截面左右侧正向通量：左 9.000 L/min（60.0%），右 6.000 L/min（40.0%）" in markdown


def test_report_renders_whole_section_metrics_without_manual_regions(report_case, monkeypatch):
    case_id, root = report_case
    cfd = root / "cfd"
    (cfd / "case_meta.json").write_text(json.dumps({"total_flow_lpm": 15.0}), encoding="utf-8")
    (cfd / "run_status.json").write_text(json.dumps({"status": "ran"}), encoding="utf-8")
    monkeypatch.setattr(report, "_region_flow", lambda _root: {
        "status": "ready", "analysis_type": "whole_section", "analysis_signature": "whole-analysis",
        "totals": {"area_mm2": 42.0, "forward_lpm": 12.0, "reverse_lpm": .5, "net_lpm": 11.5},
        "velocity": {"mean_mps": 1.2, "p95_mps": 2.1, "max_mps": 3.0},
        "plane": {"position_fraction": .62, "position_coordinate_mm": 18.0},
        "validation": {"status": "pending", "reasons": []},
    })
    monkeypatch.setattr(report, "check_result_gate", lambda _case_id: _gate({"inlet_flow_m3s": -0.00025, "delta_p_pa": 34.0, "pressure_unit": "Pa"}, eligible=True))
    report.build_report(case_id)
    markdown = (root / "report" / "report.md").read_text(encoding="utf-8")
    assert "整截面分析" in markdown
    assert "面积 42.000 mm²" in markdown
    assert "上／中／下分区结果" not in markdown


def test_report_derives_design_conclusions_from_independent_scenario(report_case, monkeypatch):
    case_id, root = report_case
    cfd = root / "cfd"
    (cfd / "case_meta.json").write_text(json.dumps({"total_flow_lpm": 15.0}), encoding="utf-8")
    (cfd / "run_status.json").write_text(json.dumps({"status": "ran"}), encoding="utf-8")
    scenario = cfd / "scenarios" / "inferior-turbinate-2mm"
    scenario.mkdir(parents=True)
    (scenario / "report.json").write_text(
        json.dumps(
            {
                "label": "下鼻甲 2 mm",
                "position": "左侧下鼻甲",
                "size_mm": 2.0,
                "occlusion_percent": 20.0,
                "flow_report": {
                    "flow_lpm": 14.0,
                    "pressure_drop": {"value": 40.0, "unit": "Pa"},
                    "velocity_distribution": {"velocity": {"p95": 3.2, "unit": "m/s"}},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        report,
        "check_result_gate",
        lambda _case_id: _gate({"inlet_flow_m3s": -0.00025, "delta_p_pa": 34.0, "pressure_unit": "Pa"}, eligible=True),
    )

    report.build_report(case_id)
    document = json.loads((root / "report" / "report.json").read_text(encoding="utf-8"))
    assert document["obstruction_comparison"]["status"] == "checked"
    conclusions = "".join(document["design_reference"]["conclusions"])
    assert "下鼻甲 2 mm" in conclusions
    assert "压降相对基准最低" in conclusions
    assert "局部高速区提示" in conclusions
