"""Offline promotion gates never turn usage hints into quality evidence."""

import json
import sqlite3

import pytest

from pipeline.upgrade_gate import UpgradeError, UpgradeLedger, compare_reports, validate_report
from scripts import manage_upgrade_gate


def report(*, cohort="a", dataset="b", miscut=0, seconds=60,
           numeric=2, tokens=100, cost=2, mobile=2, total=2):
    def item(numerator, denominator):
        return {"value": numerator / denominator, "numerator": numerator,
                "denominator": denominator}

    return {
        "schema_version": 1, "data_kind": "curated_anonymous", "total": total,
        "cohort_sha256": cohort * 64, "dataset_sha256": dataset * 64,
        "endplane_report_sha256": "c" * 64,
        "evaluator_source_sha256": "f" * 64,
        "source_references_verified": False,
        "metrics": {
            "endplane_miscut_rate": item(miscut, total),
            "manual_seconds_mean": item(seconds * total, total),
            "numeric_gate_pass_rate": item(numeric, total),
            "tokens_per_eligible_case": item(tokens, total),
            "cost_usd_per_eligible_case": item(cost, total),
            "mobile_completion_rate": item(mobile, total),
        },
    }


def test_same_cohort_comparison_rejects_regressions_and_incomplete_inputs():
    baseline = report(miscut=1, seconds=60, numeric=1, tokens=100, cost=2, mobile=1)
    candidate = report(dataset="d", miscut=0, seconds=50, numeric=2,
                       tokens=80, cost=1, mobile=2)
    result = compare_reports(baseline, candidate)
    assert result["regressions"] == []
    assert len(result["improvements"]) == 6
    with pytest.raises(UpgradeError, match="different frozen cohorts"):
        compare_reports(baseline, report(cohort="e"))
    with pytest.raises(UpgradeError, match="curated"):
        validate_report(candidate | {"data_kind": "synthetic_only"})
    candidate["metrics"]["mobile_completion_rate"] = {
        "value": None, "numerator": 0, "denominator": 0}
    with pytest.raises(UpgradeError, match="denominator"):
        validate_report(candidate)


def test_gate_requires_improvement_and_human_approval_before_canary(tmp_path):
    ledger = UpgradeLedger(tmp_path / "upgrades.sqlite3")
    baseline = report(miscut=1)
    rejected = ledger.register("algo-rejected", "base-v1", "new-v2", baseline,
                               report(dataset="d", miscut=2))
    assert rejected["status"] == "rejected"
    with pytest.raises(UpgradeError, match="non-regressing"):
        ledger.approve("algo-rejected", "reviewer-1", "review:5", "auth:5", "source:5")
    evaluated = ledger.register("algo-good", "base-v1", "new-v2", baseline,
                                report(dataset="e", miscut=0))
    assert evaluated["status"] == "evaluated"
    with pytest.raises(UpgradeError, match="separate references"):
        ledger.approve("algo-good", "reviewer-1", "same:1", "same:1", "same:1")
    with pytest.raises(UpgradeError, match="approval"):
        ledger.start_canary("algo-good", 5)
    assert ledger.assignment("algo-good", "case-20261102-123456-abcd") == "base-v1"
    ledger.approve("algo-good", "reviewer-1", "review:6", "auth:6", "source:6")
    with pytest.raises(UpgradeError, match="between 1 and 10"):
        ledger.start_canary("algo-good", 25)
    state = ledger.start_canary("algo-good", 5)
    assert state["status"] == "canary" and state["canary_percent"] == 5
    case_id = "case-20261102-123456-abcd"
    assert ledger.assignment("algo-good", case_id) == ledger.assignment("algo-good", case_id)
    with sqlite3.connect(ledger.path) as db:
        assert case_id not in "".join(str(row) for row in db.execute("SELECT * FROM upgrade_events"))


def test_canary_observation_rolls_back_on_metric_or_security_regression(tmp_path):
    ledger = UpgradeLedger(tmp_path / "gate.sqlite3")
    baseline = report(miscut=1)
    candidate = report(dataset="d", miscut=0)
    ledger.register("algo-v2", "base-v1", "new-v2", baseline, candidate)
    ledger.approve("algo-v2", "reviewer-1", "review:6", "auth:6", "source:6")
    ledger.start_canary("algo-v2", 10)
    monitor_baseline = report(cohort="e", dataset="f", miscut=0, seconds=50)
    monitor_candidate = report(cohort="e", dataset="1", miscut=0, seconds=51)
    result = ledger.observe("algo-v2", monitor_baseline, monitor_candidate)
    assert result["status"] == "rolled_back"
    assert result["reason"] == "metric_regression"
    assert ledger.assignment("algo-v2", "case-20261102-123456-abcd") == "base-v1"
    assert ledger.status("algo-v2")["events"][-1]["event"] == "rolled_back_metric_regression"


@pytest.mark.parametrize("incident", [{"permission_errors": 1}, {"quality_errors": 1}])
def test_canary_security_incident_rolls_back_immediately(tmp_path, incident):
    ledger = UpgradeLedger(tmp_path / "gate.sqlite3")
    ledger.register("algo-v2", "base-v1", "new-v2", report(miscut=1),
                    report(dataset="d", miscut=0))
    ledger.approve("algo-v2", "reviewer-1", "review:6", "auth:6", "source:6")
    ledger.start_canary("algo-v2", 5)
    result = ledger.observe("algo-v2", report(cohort="e", dataset="f"),
                            report(cohort="e", dataset="1"), **incident)
    assert result["status"] == "rolled_back"
    assert result["reason"] in {"permission_error", "quality_error"}


def test_invalid_monitor_report_fails_closed_and_manual_rollback_is_durable(tmp_path):
    path = tmp_path / "gate.sqlite3"
    ledger = UpgradeLedger(path)
    ledger.register("algo-v2", "base-v1", "new-v2", report(miscut=1),
                    report(dataset="d", miscut=0))
    ledger.approve("algo-v2", "reviewer-1", "review:6", "auth:6", "source:6")
    ledger.start_canary("algo-v2", 5)
    assert ledger.observe("algo-v2", {}, {})["status"] == "rolled_back"
    assert UpgradeLedger(path).status("algo-v2")["canary_percent"] == 0
    with pytest.raises(UpgradeError, match="canary"):
        ledger.observe("algo-v2", {}, {})
    ledger.register("other-v2", "base-v1", "new-v2", report(miscut=1),
                    report(dataset="e", miscut=0))
    ledger.approve("other-v2", "reviewer-1", "review:7", "auth:7", "source:7")
    assert ledger.rollback("other-v2", "incident:8")["status"] == "rolled_back"
    with pytest.raises(UpgradeError, match="cannot be rolled back"):
        ledger.rollback("other-v2", "incident:9")


def test_replayed_frozen_report_rolls_back_and_one_canary_at_a_time(tmp_path):
    ledger = UpgradeLedger(tmp_path / "gate.sqlite3")
    baseline, candidate = report(miscut=1), report(dataset="d", miscut=0)
    for name in ("algo-v2", "algo-v3"):
        ledger.register(name, "base-v1", name, baseline, candidate)
        ledger.approve(name, "reviewer-1", "review:6", "auth:6", "source:6")
    ledger.start_canary("algo-v2", 5)
    with pytest.raises(UpgradeError, match="another candidate"):
        ledger.start_canary("algo-v3", 5)
    result = ledger.observe("algo-v2", baseline, candidate)
    assert result["status"] == "rolled_back"
    assert result["reason"] == "invalid_monitor_report"
    assert ledger.start_canary("algo-v3", 5)["status"] == "canary"
    assert ledger.incident("algo-v3", "permission_error", "incident:7")["status"] == "rolled_back"


def test_cli_register_and_invalid_monitor_file_rolls_back(tmp_path, capsys):
    database = tmp_path / "ledger.sqlite3"
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    baseline.write_text(json.dumps(report(miscut=1)), encoding="utf-8")
    candidate.write_text(json.dumps(report(dataset="d", miscut=0)), encoding="utf-8")
    common = ["--db", str(database)]
    assert manage_upgrade_gate.main(common + ["register", "algo-v2", "base-v1", "new-v2",
                                               str(baseline), str(candidate)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "evaluated"
    assert manage_upgrade_gate.main(common + ["approve", "algo-v2", "reviewer-1",
                                              "review:6", "auth:6", "source:6"]) == 0
    capsys.readouterr()
    assert manage_upgrade_gate.main(common + ["start-canary", "algo-v2", "5"]) == 0
    capsys.readouterr()
    assert manage_upgrade_gate.main(common + ["observe", "algo-v2", str(tmp_path / "missing"),
                                              str(candidate)]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "invalid_monitor_report"
    assert manage_upgrade_gate.main(common + ["assignment", "algo-v2",
                                              "case-20261102-123456-abcd"]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == "base-v1"


def test_cli_refuses_ledger_inside_repository(capsys):
    path = manage_upgrade_gate.ROOT / "static" / "upgrade-ledger.sqlite3"
    assert manage_upgrade_gate.main(["--db", str(path), "status", "algo-v2"]) == 2
    assert not path.exists()
    assert "outside the repository" in capsys.readouterr().err


def test_canary_repeated_monitor_snapshot_fails_closed(tmp_path):
    ledger = UpgradeLedger(tmp_path / "ledger.sqlite3")
    ledger.register("algo-v2", "base-v1", "new-v2", report(miscut=1),
                    report(dataset="d", miscut=0))
    ledger.approve("algo-v2", "reviewer-1", "review:6", "auth:6", "source:6")
    ledger.start_canary("algo-v2", 5)
    baseline = report(cohort="e", dataset="f", miscut=1)
    candidate = report(cohort="e", dataset="1", miscut=0)
    assert ledger.observe("algo-v2", baseline, candidate)["status"] == "canary"
    repeated = ledger.observe("algo-v2", baseline, candidate)
    assert repeated["status"] == "rolled_back"
    assert repeated["reason"] == "invalid_monitor_report"
