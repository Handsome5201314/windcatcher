"""报告三段式 CFD 数据摘要测试。"""

from __future__ import annotations

import json

import pytest
from pipeline import cases, report


@pytest.fixture
def report_case(tmp_path, monkeypatch):
    monkeypatch.setattr("pipeline.config.CASES_DIR", tmp_path)
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
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
        lambda _case_id: {
            "eligible": True,
            "metrics": {
                "inlet_flow_m3s": -0.00025,
                "delta_p_pa": 34.0,
                "pressure_unit": "Pa",
            },
            "checks": [],
        },
    )

    built = report.build_report(case_id)
    document = json.loads((root / "report" / "report.json").read_text(encoding="utf-8"))
    baseline = document["baseline_flow"]

    assert built["cfd_eligible"] is True
    assert baseline["status"] == "ready"
    assert baseline["flow_lpm"] == pytest.approx(15.0)
    assert baseline["pressure_drop"] == {"value": 34.0, "unit": "Pa"}
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
        lambda _case_id: {
            "eligible": False,
            "metrics": {
                "inlet_flow_m3s": -0.00025,
                "delta_p_m2s2": 34.0,
                "pressure_unit": "m2/s2",
            },
            "reasons": ["网格敏感性尚未通过"],
            "checks": [],
        },
    )

    report.build_report(case_id)
    document = json.loads((root / "report" / "report.json").read_text(encoding="utf-8"))
    baseline = document["baseline_flow"]

    assert baseline["status"] == "pending_validation"
    assert baseline["data_status"] == "available"
    assert baseline["validation_status"] == "pending"
    assert baseline["flow_lpm"] == pytest.approx(15.0)
    assert baseline["pressure_drop"] == {"value": 34.0, "unit": "m2/s2"}
    assert document["design_reference"]["status"] == "pending_validation"
    assert "实际入口流量：15.000 L/min" in (root / "report" / "report.md").read_text(encoding="utf-8")


def test_report_infers_legacy_pressure_unit_from_available_metric(report_case, monkeypatch):
    case_id, root = report_case
    cfd = root / "cfd"
    (cfd / "case_meta.json").write_text(json.dumps({"total_flow_lpm": 15.0}), encoding="utf-8")
    (cfd / "run_status.json").write_text(json.dumps({"status": "ran"}), encoding="utf-8")
    monkeypatch.setattr(
        report,
        "check_result_gate",
        lambda _case_id: {
            "eligible": False,
            "metrics": {"inlet_flow_m3s": -0.00025, "delta_p_m2s2": 34.0},
            "reasons": ["旧产物未记录压力单位"],
            "checks": [],
        },
    )

    report.build_report(case_id)
    document = json.loads((root / "report" / "report.json").read_text(encoding="utf-8"))

    assert document["baseline_flow"]["pressure_drop"] == {"value": 34.0, "unit": "m2/s2"}


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
    monkeypatch.setattr(
        report,
        "check_result_gate",
        lambda _case_id: {
            "eligible": True,
            "metrics": {"inlet_flow_m3s": -0.00025, "delta_p_pa": 34.0, "pressure_unit": "Pa"},
            "checks": [],
        },
    )

    report.build_report(case_id)
    markdown = (root / "report" / "report.md").read_text(encoding="utf-8")

    assert "左右侧流量分配：左 9.000 L/min（60.0%），右 6.000 L/min（40.0%）" in markdown


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
        lambda _case_id: {
            "eligible": True,
            "metrics": {"inlet_flow_m3s": -0.00025, "delta_p_pa": 34.0, "pressure_unit": "Pa"},
            "checks": [],
        },
    )

    report.build_report(case_id)
    document = json.loads((root / "report" / "report.json").read_text(encoding="utf-8"))
    assert document["obstruction_comparison"]["status"] == "checked"
    conclusions = "".join(document["design_reference"]["conclusions"])
    assert "下鼻甲 2 mm" in conclusions
    assert "压降相对基准最低" in conclusions
    assert "局部高速区提示" in conclusions
