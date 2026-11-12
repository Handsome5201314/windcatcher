"""Offline comparison of explicitly curated endplane revisions; no CT data."""

from __future__ import annotations

import json
import hashlib

import pytest

from pipeline import endplane_history
from scripts import evaluate_endplanes


def _revision(plan, revision_id=7):
    return {"id": revision_id, "status": "committed", "confirmed_plan": plan}


def _entry(reference, tolerances, *, case_id="synthetic-eval", revision_id=7):
    return {
        "case_id": case_id,
        "scope": "main_airway",
        "revision_id": revision_id,
        "reference_plan": reference,
        "tolerances": tolerances,
    }


def _manifest(*entries):
    return {"schema_version": 1, "cases": list(entries)}


def test_index_comparison_uses_manifest_thresholds_and_committed_revision(monkeypatch):
    reference = {"scope": "main_airway", "mask_version": 3,
                 "anterior_index": 20, "posterior_index": 8}
    candidate = {**reference, "anterior_index": 21}
    calls = []
    monkeypatch.setattr(evaluate_endplanes.endplane_history, "list_revisions",
                        lambda case_id, scope: calls.append((case_id, scope)) or [_revision(candidate)])

    result = evaluate_endplanes.evaluate_manifest(_manifest(
        _entry(reference, {"max_index_delta": 0}),
    ))

    assert calls == [("synthetic-eval", "main_airway")]
    assert result["total"] == 1 and result["passed"] == 0 and result["failed"] == 1
    case = result["cases"][0]
    assert case["metrics"] == {"anterior_index_delta": 1, "posterior_index_delta": 0}
    assert case["failures"] == ["anterior_index_delta_exceeds_tolerance"]
    assert "reference_plan" not in json.dumps(result)


def test_oblique_comparison_reports_plane_offset_and_signed_normal_error(monkeypatch):
    reference = {
        "format": "oblique_planes_v1", "scope": "main_airway", "mask_version": 3,
        "anterior_plane": {"point_lps_mm": [1, 2, 3], "normal_lps": [0, -1, 0]},
        "posterior_plane": {"point_lps_mm": [1, 20, 3], "normal_lps": [0, 1, 0]},
    }
    candidate = {
        **reference,
        "anterior_plane": {"point_lps_mm": [4, 6, 3], "normal_lps": [1, 0, 0]},
    }
    monkeypatch.setattr(evaluate_endplanes.endplane_history, "list_revisions",
                        lambda _case_id, _scope: [_revision(candidate)])

    result = evaluate_endplanes.evaluate_manifest(_manifest(
        _entry(reference, {"max_plane_offset_mm": 4, "max_normal_deg": 90}),
    ))

    assert result["passed"] == 1
    assert result["cases"][0]["metrics"] == {
        "anterior_plane_offset_mm": 4.0, "anterior_normal_deg": 90.0,
        "posterior_plane_offset_mm": 0.0, "posterior_normal_deg": 0.0,
    }
    assert result["cases"][0]["failures"] == []


def test_local_inlet_keeps_orientation_and_seed_error_without_rounding_pass(monkeypatch):
    reference = {
        "format": "oblique_local_inlet_v2",
        "anterior_plane": {"point_lps_mm": [0, 0, 0], "normal_lps": [0, -1, 0]},
        "posterior_plane": {"point_lps_mm": [0, 20, 0], "normal_lps": [0, 1, 0]},
        "anterior_seed_lps_mm": [0, 0, 0],
    }
    candidate = {
        **reference,
        "anterior_plane": {"point_lps_mm": [1.0000004, 0, 0], "normal_lps": [0, 1, 0]},
        "anterior_seed_lps_mm": [0, 0, 1.0000004],
    }
    monkeypatch.setattr(evaluate_endplanes.endplane_history, "list_revisions",
                        lambda _case_id, _scope: [_revision(candidate)])

    result = evaluate_endplanes.evaluate_manifest(_manifest(
        _entry(reference, {"max_plane_offset_mm": 1, "max_normal_deg": 5, "max_seed_mm": 1}),
    ))

    assert result["failed"] == 1
    case = result["cases"][0]
    assert case["metrics"]["anterior_plane_offset_mm"] == 0.0
    assert case["metrics"]["anterior_normal_deg"] == 180.0
    assert case["metrics"]["anterior_seed_mm"] == 1.0
    assert case["failures"] == [
        "anterior_normal_deg_exceeds_tolerance",
        "anterior_seed_mm_exceeds_tolerance",
    ]


def test_tangential_point_shift_describes_the_same_plane(monkeypatch):
    reference = {
        "format": "oblique_planes_v1",
        "anterior_plane": {"point_lps_mm": [0, 0, 0], "normal_lps": [0, -1, 0]},
        "posterior_plane": {"point_lps_mm": [0, 20, 0], "normal_lps": [0, 1, 0]},
    }
    candidate = {
        **reference,
        "anterior_plane": {"point_lps_mm": [25, 0, 0], "normal_lps": [0, -1, 0]},
    }
    monkeypatch.setattr(evaluate_endplanes.endplane_history, "list_revisions",
                        lambda _case_id, _scope: [_revision(candidate)])

    result = evaluate_endplanes.evaluate_manifest(_manifest(
        _entry(reference, {"max_plane_offset_mm": 0, "max_normal_deg": 0}),
    ))

    assert result["passed"] == 1
    assert result["cases"][0]["metrics"]["anterior_plane_offset_mm"] == 0.0
    assert result["cases"][0]["failures"] == []


def test_normal_point_shift_exceeding_tolerance_fails_before_rounding(monkeypatch):
    reference = {
        "format": "oblique_planes_v1",
        "anterior_plane": {"point_lps_mm": [0, 0, 0], "normal_lps": [0, -1, 0]},
        "posterior_plane": {"point_lps_mm": [0, 20, 0], "normal_lps": [0, 1, 0]},
    }
    candidate = {
        **reference,
        "anterior_plane": {"point_lps_mm": [0, 1.0000004, 0], "normal_lps": [0, -1, 0]},
    }
    monkeypatch.setattr(evaluate_endplanes.endplane_history, "list_revisions",
                        lambda _case_id, _scope: [_revision(candidate)])

    result = evaluate_endplanes.evaluate_manifest(_manifest(
        _entry(reference, {"max_plane_offset_mm": 1, "max_normal_deg": 0}),
    ))

    assert result["failed"] == 1
    assert result["cases"][0]["metrics"]["anterior_plane_offset_mm"] == 1.0
    assert result["cases"][0]["failures"] == ["anterior_plane_offset_mm_exceeds_tolerance"]


def test_missing_revision_is_visible_and_never_uses_a_different_revision(monkeypatch):
    reference = {"anterior_index": 20, "posterior_index": 8}
    monkeypatch.setattr(evaluate_endplanes.endplane_history, "list_revisions",
                        lambda _case_id, _scope: [_revision(reference, revision_id=8)])

    result = evaluate_endplanes.evaluate_manifest(_manifest(
        _entry(reference, {"max_index_delta": 0}),
    ))

    assert result["failed"] == 1
    assert result["cases"][0]["failures"] == ["committed_revision_not_found"]
    assert result["cases"][0]["metrics"] == {}


def test_real_ledger_revision_is_read_only_and_explicitly_selected(tmp_path, monkeypatch):
    monkeypatch.setenv(endplane_history.DIRECTORY_ENV, str(tmp_path / "learning"))
    plan = {"scope": "main_airway", "mask_version": 3,
            "anterior_index": 20, "posterior_index": 8}
    revision_id = endplane_history.begin_revision("synthetic-eval", "main_airway", 3, None, plan)
    endplane_history.commit_revision(revision_id)

    result = evaluate_endplanes.evaluate_manifest(_manifest(
        _entry(plan, {"max_index_delta": 0}, revision_id=revision_id),
    ))

    assert result["passed"] == 1 and result["failed"] == 0
    assert result["cases"][0]["metrics"] == {
        "anterior_index_delta": 0, "posterior_index_delta": 0,
    }


@pytest.mark.parametrize("manifest", [
    {},
    {"schema_version": 2, "cases": []},
    {"schema_version": True, "cases": []},
    _manifest(),
    _manifest(_entry({}, {"max_index_delta": -1})),
    _manifest(_entry({}, {"max_index_delta": 0}, revision_id=True)),
    _manifest(_entry({}, {"max_index_delta": 0}) | {"scope": []}),
    _manifest(_entry({"format": {}, "anterior_index": 20, "posterior_index": 8},
                     {"max_index_delta": 0})),
    _manifest(_entry({"anterior_index": 20, "posterior_index": 8,
                      "patient_name": "must-not-appear"}, {"max_index_delta": 0})),
    _manifest(_entry({}, {"max_index_delta": 0}), _entry({}, {"max_index_delta": 0})),
    _manifest(_entry({"anterior_index": 20, "posterior_index": 8},
                     {"max_index_delta": 0})) | {"free_text": "not permitted"},
])
def test_invalid_or_duplicate_manifest_fails_closed(manifest):
    with pytest.raises(evaluate_endplanes.ManifestError):
        evaluate_endplanes.evaluate_manifest(manifest)


def test_cli_writes_only_summary_to_stdout(tmp_path, monkeypatch, capsys):
    reference = {"anterior_index": 20, "posterior_index": 8}
    monkeypatch.setattr(evaluate_endplanes.endplane_history, "list_revisions",
                        lambda _case_id, _scope: [_revision(reference)])
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest(_entry(reference, {"max_index_delta": 0}))), encoding="utf-8")

    assert evaluate_endplanes.main([str(path)]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["passed"] == 1
    assert "reference_plan" not in output


def _real_manifest():
    plan = {"scope": "main_airway", "mask_version": 3,
            "anterior_index": 20, "posterior_index": 8}
    return _manifest(_entry(plan, {"max_index_delta": 0}, case_id="case-20261102-123456-abcd")), plan


def _provenance(manifest_bytes, plan):
    return {
        "schema_version": 1,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "records": [{
            "case_id": "case-20261102-123456-abcd",
            "scope": "main_airway", "revision_id": 7,
            "revision_plan_sha256": endplane_history.plan_hash(plan),
            "evaluation_authorization_ref": "research-approval:42",
            "reference_confirmation_ref": "endplane-review:19",
            "reviewer_id": "user-123", "reviewed_at_utc": "2026-11-02T12:34:56Z",
        }],
    }


def test_real_case_requires_complete_bound_provenance_before_reading_ledger(monkeypatch):
    manifest, plan = _real_manifest()
    raw = json.dumps(manifest).encode()
    calls = []
    monkeypatch.setattr(evaluate_endplanes.endplane_history, "list_revisions",
                        lambda *_args: calls.append(True) or [])

    with pytest.raises(evaluate_endplanes.ManifestError):
        evaluate_endplanes.evaluate_manifest(manifest)
    assert calls == []

    provenance = _provenance(raw, plan)
    for changed in (
        provenance | {"manifest_sha256": "0" * 64},
        provenance | {"records": []},
        provenance | {"records": [provenance["records"][0] | {"reviewer_id": ""}]},
        provenance | {"records": [provenance["records"][0] | {"case_id": "synthetic-other"}]},
        provenance | {"records": [provenance["records"][0] | {"case_id": []}]},
        provenance | {"records": [provenance["records"][0] | {
            "reviewed_at_utc": "2026-1-2T3:04:05Z"}]},
    ):
        with pytest.raises(evaluate_endplanes.ManifestError):
            evaluate_endplanes.evaluate_manifest(
                manifest, provenance=changed,
                manifest_sha256=hashlib.sha256(raw).hexdigest(),
            )
    assert calls == []


def test_real_case_reports_frozen_revision_hash_mismatch(monkeypatch):
    manifest, plan = _real_manifest()
    raw = json.dumps(manifest).encode()
    provenance = _provenance(raw, plan)
    changed_plan = plan | {"anterior_index": 21}
    monkeypatch.setattr(evaluate_endplanes.endplane_history, "list_revisions",
                        lambda *_args: [_revision(changed_plan) | {
                            "plan_hash": endplane_history.plan_hash(changed_plan),
                        }])

    result = evaluate_endplanes.evaluate_manifest(
        manifest, provenance=provenance,
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
    )
    assert result["data_kind"] == "curated_anonymous"
    assert result["source_references_verified"] is False
    assert result["cases"][0]["failures"] == ["revision_plan_sha256_mismatch"]


def test_real_case_cli_accepts_exact_frozen_files_and_rejects_tampering(tmp_path, monkeypatch, capsys):
    manifest, plan = _real_manifest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    sidecar_path = tmp_path / "provenance.json"
    sidecar_path.write_text(json.dumps(_provenance(manifest_path.read_bytes(), plan)), encoding="utf-8")
    monkeypatch.setattr(evaluate_endplanes.endplane_history, "list_revisions",
                        lambda *_args: [_revision(plan) | {"plan_hash": endplane_history.plan_hash(plan)}])

    assert evaluate_endplanes.main([str(manifest_path), "--provenance", str(sidecar_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] == 1 and report["provenance_sha256"]
    assert len(report["evaluator_source_sha256"]) == 64
    assert "research-approval:42" not in json.dumps(report)

    manifest_path.write_bytes(manifest_path.read_bytes() + b"\n")
    assert evaluate_endplanes.main([str(manifest_path), "--provenance", str(sidecar_path)]) == 2
    assert "invalid_or_unreadable_manifest" in capsys.readouterr().err


def test_cli_rejects_duplicate_json_keys_before_any_ledger_read(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(evaluate_endplanes.endplane_history, "list_revisions",
                        lambda *_args: calls.append(True) or [])
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema_version":1,"schema_version":1,"cases":[]}', encoding="utf-8")
    assert evaluate_endplanes.main([str(path)]) == 2
    assert calls == []
    assert "invalid_or_unreadable_manifest" in capsys.readouterr().err
