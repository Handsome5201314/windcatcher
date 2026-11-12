"""Frozen bilateral-airway regression: synthetic geometry, no patient data."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.evaluate_local_inlet_synthetic import FIXTURE, evaluate_fixture


def test_frozen_synthetic_bilateral_inlet_evaluation():
    result = evaluate_fixture()

    assert result["fixture_id"] == "synthetic-bilateral-local-inlet-v1"
    assert result["fixture_version"] == 1
    assert result["algorithm_version"] == "oblique_local_inlet_v2"
    assert len(result["algorithm_source_sha256"]) == 64
    assert len(result["input_sha256"]) == 64
    assert result["data_kind"] == "synthetic_only"
    assert result["passed_count"] == 10
    assert result["failed_count"] == 0, result["checks"]
    assert result["passed"] is True


def test_frozen_evaluation_reports_failed_check_and_reason(tmp_path):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["probes_ijk"]["unselected_anterior"] = [7, 5, 8]
    modified = tmp_path / "deliberately_wrong_fixture.json"
    modified.write_text(json.dumps(fixture), encoding="utf-8")

    result = evaluate_fixture(modified)

    assert result["passed"] is False
    failed = [check for check in result["checks"] if not check["passed"]]
    assert any(check["name"] == "unselected_anterior_retained" for check in failed)
    assert all(check["reason"] for check in failed)


def test_frozen_evaluation_cli_exits_nonzero_for_failed_check(tmp_path):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["probes_ijk"]["unselected_anterior"] = [7, 5, 8]
    modified = tmp_path / "deliberately_wrong_fixture.json"
    modified.write_text(json.dumps(fixture), encoding="utf-8")
    script = Path(__file__).resolve().parent.parent / "scripts" / "evaluate_local_inlet_synthetic.py"

    completed = subprocess.run(
        [sys.executable, str(script), "--fixture", str(modified)],
        capture_output=True, text=True, check=False,
    )

    assert completed.returncode == 1
    result = json.loads(completed.stdout)
    assert result["failed_count"] > 0
