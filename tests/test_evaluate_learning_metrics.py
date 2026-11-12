"""Frozen upgrade metrics use explicit reviewed records, never feedback events."""

from __future__ import annotations

import hashlib
import json

import pytest

from scripts import evaluate_endplanes, evaluate_learning_metrics


def _plan():
    return {"scope": "main_airway", "mask_version": 2,
            "anterior_index": 20, "posterior_index": 5}


def _report(monkeypatch, *, revision_id=7):
    plan = _plan()
    monkeypatch.setattr(evaluate_endplanes.endplane_history, "list_revisions",
                        lambda _case_id, _scope: [{
                            "id": revision_id, "status": "committed", "confirmed_plan": plan,
                        }])
    manifest = {"schema_version": 1, "cases": [{
        "case_id": case_id, "scope": "main_airway", "revision_id": revision_id,
        "reference_plan": plan, "tolerances": {"max_index_delta": 0},
    } for case_id in ("synthetic-one", "synthetic-two")]}
    report = evaluate_endplanes.evaluate_manifest(manifest)
    raw = json.dumps(report, sort_keys=True).encode()
    return report, hashlib.sha256(raw).hexdigest(), raw


def _record(case_id, *, revision_id=7, **changes):
    row = {
        "case_id": case_id, "scope": "main_airway", "revision_id": revision_id,
        "miscut": case_id == "synthetic-two",
        "manual_seconds": 60 if case_id == "synthetic-one" else 120,
        "numeric_gate_ok": case_id == "synthetic-one",
        "cfd_eligible": case_id == "synthetic-one",
        "tokens": 100 if case_id == "synthetic-one" else 300,
        "cost_usd": 1 if case_id == "synthetic-one" else 3,
        "mobile_attempted": True,
        "mobile_completed": case_id == "synthetic-one",
        "quality_review_ref": "quality-review:7", "manual_time_ref": "time-record:7",
        "numeric_gate_ref": "gate-record:7", "usage_ref": "usage-record:7",
        "mobile_ref": "mobile-test:7", "reviewer_id": "operator-7",
        "reviewed_at_utc": "2026-11-02T12:30:00Z",
    }
    row.update(changes)
    return row


def _dataset(report_sha256, *rows):
    return {"schema_version": 1, "endplane_report_sha256": report_sha256,
            "cases": list(rows)}


def _summarize(report, report_sha256, dataset):
    raw = json.dumps(dataset, sort_keys=True).encode()
    return evaluate_learning_metrics.evaluate_metrics(
        report, dataset, report_sha256=report_sha256,
        dataset_sha256=hashlib.sha256(raw).hexdigest())


def test_frozen_quality_metrics_have_explicit_denominators_and_no_sources(monkeypatch):
    report, report_hash, _ = _report(monkeypatch)
    dataset = _dataset(report_hash, _record("synthetic-one"), _record("synthetic-two"))
    summary = _summarize(report, report_hash, dataset)

    assert summary["cohort_sha256"] == report["cohort_sha256"]
    assert summary["data_kind"] == "synthetic_only"
    assert summary["source_references_verified"] is False
    assert summary["metrics"] == {
        "endplane_miscut_rate": {"value": .5, "numerator": 1, "denominator": 2},
        "manual_seconds_mean": {"value": 90, "numerator": 180, "denominator": 2},
        "numeric_gate_pass_rate": {"value": .5, "numerator": 1, "denominator": 2},
        "tokens_per_eligible_case": {"value": 400, "numerator": 400, "denominator": 1},
        "cost_usd_per_eligible_case": {"value": 4, "numerator": 4, "denominator": 1},
        "mobile_completion_rate": {"value": .5, "numerator": 1, "denominator": 2},
    }
    assert "quality-review" not in json.dumps(summary)
    assert "feedback" not in json.dumps(summary)


def test_cohort_hash_excludes_candidate_revision_but_includes_reference(monkeypatch):
    first, _, _ = _report(monkeypatch, revision_id=7)
    second, _, _ = _report(monkeypatch, revision_id=8)
    assert first["cohort_sha256"] == second["cohort_sha256"]
    assert len(first["cohort_sha256"]) == 64
    changed = _plan() | {"anterior_index": 21}
    entries = [{
        "case_id": case_id, "scope": "main_airway", "revision_id": 7,
        "reference_plan": changed, "tolerances": {"max_index_delta": 0},
    } for case_id in ("synthetic-one", "synthetic-two")]
    different = evaluate_endplanes.evaluate_manifest({"schema_version": 1, "cases": entries})
    assert different["cohort_sha256"] != first["cohort_sha256"]


def test_quality_dataset_rejects_feedback_missing_case_and_unreviewed_source(monkeypatch):
    report, report_hash, _ = _report(monkeypatch)
    valid = _record("synthetic-one")
    other = _record("synthetic-two")
    for rows in (
        [valid],
        [valid, valid],
        [valid | {"patient_feedback": "looks wrong"}, other],
        [valid | {"quality_review_ref": ""}, other],
        [valid | {"reviewed_at_utc": "2026-1-2T12:30:00Z"}, other],
        [valid | {"mobile_completed": True, "mobile_attempted": False}, other],
        [valid | {"cfd_eligible": True, "numeric_gate_ok": False}, other],
        [valid | {"tokens": False}, other],
    ):
        with pytest.raises(evaluate_learning_metrics.ManifestError):
            _summarize(report, report_hash, _dataset(report_hash, *rows))


def test_quality_dataset_rejects_report_tampering_or_non_geometric_failure(monkeypatch):
    report, report_hash, _ = _report(monkeypatch)
    dataset = _dataset(report_hash, _record("synthetic-one"), _record("synthetic-two"))
    with pytest.raises(evaluate_learning_metrics.ManifestError):
        _summarize(report, "0" * 64, dataset)
    changed = report | {"cases": [report["cases"][0] | {
        "passed": False, "failures": ["committed_revision_not_found"]}, report["cases"][1]],
        "passed": 1, "failed": 1}
    with pytest.raises(evaluate_learning_metrics.ManifestError):
        _summarize(changed, report_hash, dataset)
    with pytest.raises(evaluate_learning_metrics.ManifestError):
        _summarize(report | {"data_kind": []}, report_hash, dataset)


def test_zero_eligible_and_mobile_denominators_remain_null(monkeypatch):
    report, report_hash, _ = _report(monkeypatch)
    rows = [_record(case_id, numeric_gate_ok=False, cfd_eligible=False,
                    mobile_attempted=False, mobile_completed=False)
            for case_id in ("synthetic-one", "synthetic-two")]
    summary = _summarize(report, report_hash, _dataset(report_hash, *rows))
    for name in ("tokens_per_eligible_case", "cost_usd_per_eligible_case", "mobile_completion_rate"):
        assert summary["metrics"][name]["value"] is None
        assert summary["metrics"][name]["denominator"] == 0


def test_cli_binds_raw_files_and_emits_only_aggregate(monkeypatch, tmp_path, capsys):
    report, report_hash, report_bytes = _report(monkeypatch)
    dataset = _dataset(report_hash, _record("synthetic-one"), _record("synthetic-two"))
    report_file, dataset_file = tmp_path / "report.json", tmp_path / "quality.json"
    report_file.write_bytes(report_bytes)
    dataset_file.write_text(json.dumps(dataset), encoding="utf-8")

    assert evaluate_learning_metrics.main([str(report_file), str(dataset_file)]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["total"] == 2
    assert "quality-review" not in output and "synthetic-one" not in output

    report_file.write_bytes(report_bytes + b"\n")
    assert evaluate_learning_metrics.main([str(report_file), str(dataset_file)]) == 2
    assert "invalid_or_unreadable_frozen_evaluation" in capsys.readouterr().err
