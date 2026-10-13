"""报告指标必须保留方向、单位、真实采样时间和缺项。"""

from __future__ import annotations

import copy
import json

import pytest

from pipeline import report


def sampled_gate(*, q=-0.00025, inlet=20.0, outlet=2.0, field_unit="m2/s2", rho=1.2):
    values = {"inlet_flow_m3s": q, "inlet_pressure_field": inlet, "outlet_pressure_field": outlet}
    return {
        "eligible": False, "reasons": ["网格敏感性尚未通过"], "fields": {"latest_time": "600"},
        "metrics": {"inlet_flow_m3s": q, "delta_p_pa": 999.0, "pressure_unit": "Pa"},
        "postprocessing": {
            **values,
            "sample_times": {name: 600.0 for name in values},
            "sources": {name: f"postProcessing/{name}/0/surfaceFieldValue.dat" for name in values},
            "pressure": {"status": "checked", "field_unit": field_unit, "rho_kg_m3": rho},
        },
    }


def baseline(gate, meta=None, payload=None, regions=None):
    return report._baseline_flow_report(meta or {"total_flow_lpm": 10.0}, {"status": "ran"}, gate, payload, "ready" if payload else "missing", regions)


@pytest.mark.parametrize("value", [None, "", "  ", True, False, "NaN", float("inf"), float("-inf")])
def test_missing_or_nonfinite_numbers_never_become_zero(value):
    assert report._number(value) is None
    assert report._display(value) == "—"


def test_resistance_uses_actual_inhalation_and_recorded_density_not_requested_flow_or_gate_snapshot():
    result = baseline(sampled_gate())
    assert result["requested_flow_lpm"] == 10.0
    assert result["flow_lpm"] == pytest.approx(15.0)
    assert result["flow_m3s"] == -0.00025
    assert result["pressure_drop"]["value"] == pytest.approx(21.6)
    assert result["pressure_drop"]["unit"] == "Pa"
    assert result["pressure_drop"]["field_unit"] == "m2/s2"
    assert result["resistance"]["value"] == pytest.approx(21.6 / 250.0)
    assert result["resistance"]["unit"] == "Pa·s/mL"
    assert result["resistance"]["sample_time"] == 600.0
    assert result["resistance"]["final_time"] == 600.0
    assert result["validation_status"] == "pending"


def test_pressure_dimensions_pa_need_no_density_conversion():
    result = baseline(sampled_gate(field_unit="Pa", rho=None))
    assert result["pressure_drop"]["value"] == 18.0
    assert result["resistance"]["value"] == pytest.approx(18.0 / 250.0)


@pytest.mark.parametrize("rho", [None, 0.0, -1.2, True, "", float("nan")])
def test_unknown_density_keeps_kinematic_pressure_and_suppresses_resistance(rho):
    result = baseline(sampled_gate(rho=rho))
    assert result["pressure_drop"]["value"] == 18.0
    assert result["pressure_drop"]["unit"] == "m2/s2"
    assert result["resistance"]["value"] is None
    assert "密度" in result["resistance"]["reason"]


@pytest.mark.parametrize("q,direction", [(0.00025, "outward"), (0.0, "near_zero"), (-1e-13, "near_zero"), (None, "unknown"), (True, "unknown")])
def test_invalid_inlet_direction_or_missing_flow_is_never_abs_or_requested_fallback(q, direction):
    result = baseline(sampled_gate(q=q))
    assert result["flow_m3s"] == report._number(q)
    assert result["flow_lpm"] is None
    assert result["flow_measurement"]["direction"] == direction
    assert result["resistance"]["value"] is None


@pytest.mark.parametrize("sample", ["inlet_flow_m3s", "inlet_pressure_field", "outlet_pressure_field"])
@pytest.mark.parametrize("fault", ["stale", "missing_time", "missing_source"])
def test_final_sampling_provenance_is_required_individually(sample, fault):
    gate = sampled_gate()
    if fault == "stale":
        gate["postprocessing"]["sample_times"][sample] = 599.0
    elif fault == "missing_time":
        del gate["postprocessing"]["sample_times"][sample]
    else:
        del gate["postprocessing"]["sources"][sample]
    result = baseline(gate)
    assert result["resistance"]["value"] is None
    measurement = result["flow_measurement"] if sample == "inlet_flow_m3s" else result["pressure_drop"]
    assert measurement["status"] == ("stale" if fault == "stale" else "missing")
    assert measurement["value"] is None


def test_missing_final_time_and_missing_pressure_sample_do_not_use_delta_snapshot():
    gate = sampled_gate()
    del gate["fields"]["latest_time"]
    assert baseline(gate)["flow_lpm"] is None
    gate = sampled_gate()
    gate["postprocessing"]["outlet_pressure_field"] = None
    result = baseline(gate)
    assert result["pressure_drop"]["value"] is None
    assert result["pressure_drop"]["status"] == "missing"
    assert result["resistance"]["value"] is None


def test_missing_actual_flow_does_not_use_gate_metric_snapshot_with_current_sample_labels():
    gate = sampled_gate()
    gate["postprocessing"]["inlet_flow_m3s"] = None
    assert gate["metrics"]["inlet_flow_m3s"] == -0.00025
    result = baseline(gate)
    assert result["flow_lpm"] is None
    assert result["flow_m3s"] is None
    assert result["flow_measurement"]["status"] == "missing"


def test_report_metrics_consume_real_openfoam_function_object_rows(tmp_path):
    from pipeline.openfoam_runner import _read_postprocessing

    (tmp_path / "case_meta.json").write_text(json.dumps({"rho_kg_m3": 1.2}), encoding="utf-8")
    for function, value in [("inletFlowRate", -0.00025), ("inletPressure", 20.0), ("outletPressure", 2.0)]:
        directory = tmp_path / "postProcessing" / function / "0"
        directory.mkdir(parents=True)
        (directory / "surfaceFieldValue.dat").write_text(f"# Time value\n599 999\n600 {value}\n", encoding="utf-8")
    fields = {"latest_time": "600", "fields": {"p": {"dimensions": [0, 2, -2, 0, 0, 0, 0]}}}
    post = _read_postprocessing(tmp_path, fields)
    result = baseline({"eligible": False, "fields": fields, "postprocessing": post})
    assert result["flow_lpm"] == 15.0
    assert result["pressure_drop"]["value"] == pytest.approx(21.6)
    assert result["resistance"]["value"] == pytest.approx(0.0864)
    assert result["pressure_drop"]["source"]["inlet_pressure_field"].replace("\\", "/") == "postProcessing/inletPressure/0/surfaceFieldValue.dat"


def test_boolean_density_in_case_meta_cannot_turn_kinematic_pressure_into_pa(tmp_path):
    from pipeline.openfoam_runner import _read_postprocessing

    (tmp_path / "case_meta.json").write_text(json.dumps({"rho_kg_m3": True}), encoding="utf-8")
    for function, value in [("inletFlowRate", -0.00025), ("inletPressure", 20.0), ("outletPressure", 2.0)]:
        directory = tmp_path / "postProcessing" / function / "0"
        directory.mkdir(parents=True)
        (directory / "surfaceFieldValue.dat").write_text(f"# Time value\n600 {value}\n", encoding="utf-8")
    fields = {"latest_time": "600", "fields": {"p": {"dimensions": [0, 2, -2, 0, 0, 0, 0]}}}
    post = _read_postprocessing(tmp_path, fields)
    assert post["pressure"]["rho_kg_m3"] is None
    assert post["pressure"]["pa_multiplier"] is None
    assert "inlet_pressure_pa" not in post
    result = baseline({"eligible": False, "fields": fields, "postprocessing": post})
    assert result["pressure_drop"]["value"] == 18.0
    assert result["pressure_drop"]["unit"] == "m2/s2"
    assert result["pressure_drop"]["rho_kg_m3"] is None
    assert result["resistance"]["value"] is None


def test_negative_pressure_drop_keeps_anomaly_and_suppresses_resistance():
    result = baseline(sampled_gate(inlet=1.0, outlet=2.0))
    assert result["pressure_drop"]["raw_delta_field"] == -1.0
    assert result["pressure_drop"]["value"] is None
    assert result["resistance"]["value"] is None
    assert "压降为负" in result["pressure_drop"]["reason"]


def test_unverified_pressure_dimensions_do_not_get_guessed_pa():
    result = baseline(sampled_gate(field_unit="unknown"))
    assert result["pressure_drop"]["status"] == "invalid"
    assert result["pressure_drop"]["value"] is None
    assert result["resistance"]["value"] is None


def test_ranges_only_are_not_fabricated_mean_or_p95():
    result = report._distribution({"ranges": {"velocity": [0.0, 5.0]}, "units": {"velocity": "m/s"}})
    assert result["velocity"]["status"] == "range_only"
    assert result["velocity"]["mean"] is None
    assert result["velocity"]["p95"] is None
    assert result["velocity"]["max"] == 5.0


def test_old_hand_filled_split_cannot_be_reported_as_production_result():
    result = baseline(sampled_gate(), meta={"total_flow_lpm": 15.0, "inlet_split": {"left_lpm": 8.0, "right_lpm": 7.0}})
    assert result["left_right_flow"]["status"] == "not_available"
    assert result["left_right_flow"]["left_lpm"] is None


def test_region_report_keeps_unassigned_and_coverage_and_removes_stale_values(tmp_path, monkeypatch):
    from pipeline import cfd_sections

    ready = {
        "status": "ready", "analysis_signature": "test-analysis", "coverage_complete": False,
        "totals": {"forward_lpm": 15.0},
        "unassigned": {"forward_lpm": 3.0, "percent_of_section": 20.0},
        "regions": [{"id": "left-upper", "forward_lpm": 5.0, "percent_of_side": None}],
        "sides": [{"id": "left", "forward_lpm": 7.0, "percent_of_section": 7.0 / 15 * 100}, {"id": "right", "forward_lpm": 5.0, "percent_of_section": 5.0 / 15 * 100}],
        "validation": {"status": "pending"},
    }
    monkeypatch.setattr(cfd_sections, "get_sections", lambda _root: ready)
    result = report._region_flow(tmp_path)
    assert result["unassigned"] == ready["unassigned"]
    assert result["coverage_complete"] is False
    split = report._flow_split(result, False)
    assert split["left_fraction"] + split["right_fraction"] == pytest.approx(0.8)
    assert split["status"] == "available_pending_validation"

    stale = copy.deepcopy(ready)
    stale.update(status="stale", reason="求解场已变化")
    monkeypatch.setattr(cfd_sections, "get_sections", lambda _root: stale)
    result = report._region_flow(tmp_path)
    assert result["status"] == "stale"
    assert result["regions"] == []
    assert "totals" not in result
    assert "unassigned" not in result
    assert report._flow_split(result, False)["left_fraction"] is None


def test_pressure_inlet_report_does_not_label_requested_flow(tmp_path, monkeypatch):
    from pipeline import cases
    monkeypatch.setattr("pipeline.config.CASES_DIR", tmp_path)
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = cases.create_case("synthetic", "<合成测试>", synthetic=True)
    root = cases.case_dir(case_id)
    (root / "cfd" / "case_meta.json").write_text(json.dumps({"inlet_mode": "pressure", "total_flow_lpm": 15.0, "inlet_pressure_pa": 12.0}), encoding="utf-8")
    (root / "cfd" / "run_status.json").write_text(json.dumps({"status": "missing", "message": "test"}), encoding="utf-8")
    monkeypatch.setattr(report, "check_result_gate", lambda _case_id: {"eligible": False, "checks": [], "reasons": []})
    monkeypatch.setattr(report, "_result_payload", lambda _case_id: (None, "missing"))
    monkeypatch.setattr(report, "_region_flow", lambda _root: {"status": "missing", "reason": "test"})
    report.build_report(case_id)
    markdown = (root / "report" / "report.md").read_text(encoding="utf-8")
    assert "压力入口 12.000 Pa（不预设流量）" in markdown
    assert "双侧合计设定流量 15.000 L/min" not in markdown
