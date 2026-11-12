import copy
import json

import pytest

from pipeline.endplane_learning import EndplaneLearningStore
from scripts import export_endplane_learning_dataset as exporter
from scripts import evaluate_endplane_learning as evaluator


def _plan():
    return {"format": "oblique_local_inlet_v2", "scope": "main_airway", "mask_version": 3,
            "anterior_plane": {"point_lps_mm": [0, 0, 0], "normal_lps": [0, -1, 0]},
            "posterior_plane": {"point_lps_mm": [0, 20, 0], "normal_lps": [0, 1, 0]},
            "anterior_seed_lps_mm": [0, 0, 0]}


def _data(tmp_path):
    store = EndplaneLearningStore(tmp_path)
    proposal = store.create_proposal("synthetic-eval-loop", "main_airway", 3, _plan(), "rules", "v1")
    store.record_feedback(proposal["proposal_id"], "accepted")
    frozen = exporter.export_dataset([proposal["proposal_id"]], source_cohort_sha256="a" * 64,
                                     store=store)
    candidates = {"schema_version": 1, "frozen_manifest_sha256": frozen["manifest_sha256"],
                  "candidates": [{"case_id": "synthetic-eval-loop", "scope": "main_airway",
                                  "mask_version": 3, "plan": _plan()}]}
    return frozen, candidates


TOLERANCES = {"max_plane_offset_mm": 1, "max_normal_deg": 5, "max_seed_mm": 1}


def test_frozen_reference_and_independent_candidate_evaluate_pass_and_fail(tmp_path):
    frozen, candidate = _data(tmp_path)
    report = evaluator.evaluate_learning(frozen, candidate, tolerances=TOLERANCES)
    assert (report["total"], report["passed"], report["failed"]) == (1, 1, 0)
    assert report["data_kind"] == "synthetic_only"
    assert report["auto_promotion"] is False
    candidate["candidates"][0]["plan"]["anterior_plane"]["normal_lps"] = [0, 1, 0]
    report = evaluator.evaluate_learning(frozen, candidate, tolerances=TOLERANCES)
    assert report["failed"] == 1
    assert report["cases"][0]["metrics"]["anterior_normal_deg"] == 180.0
    assert "anterior_normal_deg_exceeds_tolerance" in report["cases"][0]["failures"]
    assert '"plan"' not in json.dumps(report)


@pytest.mark.parametrize("mutation", [
    lambda c: c["candidates"].clear(),
    lambda c: c["candidates"].append(copy.deepcopy(c["candidates"][0])),
    lambda c: c["candidates"][0].update(mask_version=4),
    lambda c: c["candidates"][0]["plan"].update(mask_version=4),
    lambda c: c["candidates"][0]["plan"].pop("posterior_plane"),
    lambda c: c["candidates"][0]["plan"].update(format="oblique_planes_v1"),
    lambda c: c["candidates"][0]["plan"].update(patient_name="private"),
    lambda c: c["candidates"][0]["plan"]["anterior_plane"].update(path="private"),
    lambda c: c.update(frozen_manifest_sha256="0" * 64),
])
def test_candidate_must_cover_exact_frozen_cohort_and_strict_contract(tmp_path, mutation):
    frozen, candidate = _data(tmp_path)
    mutation(candidate)
    with pytest.raises(evaluator.EvaluationError):
        evaluator.evaluate_learning(frozen, candidate, tolerances=TOLERANCES)


@pytest.mark.parametrize("mutation", [
    lambda f: f["records"][0]["plan"]["anterior_seed_lps_mm"].__setitem__(0, 2),
    lambda f: f.update(cohort_sha256="0" * 64),
    lambda f: f.update(proposal_allowlist_sha256="0" * 64),
    lambda f: f["records"][0].update(feedback_status="rejected"),
    lambda f: f["records"][0].update(effective_plan_sha256="0" * 64),
    lambda f: f.update(source_references_verified=True),
])
def test_tampered_or_unreviewed_frozen_dataset_is_rejected(tmp_path, mutation):
    frozen, candidate = _data(tmp_path)
    mutation(frozen)
    # Recompute outer hash to prove inner hashes/status checks are independent.
    frozen["manifest_sha256"] = exporter._digest({k: v for k, v in frozen.items()
                                                if k != "manifest_sha256"})
    candidate["frozen_manifest_sha256"] = frozen["manifest_sha256"]
    with pytest.raises(evaluator.EvaluationError):
        evaluator.evaluate_learning(frozen, candidate, tolerances=TOLERANCES)


def test_cohort_hash_binds_tolerances_and_candidate_hash_binds_snapshot(tmp_path):
    frozen, candidate = _data(tmp_path)
    a = evaluator.evaluate_learning(frozen, candidate, tolerances=TOLERANCES)
    b = evaluator.evaluate_learning(frozen, candidate, tolerances=TOLERANCES | {"max_normal_deg": 10})
    assert a["evaluation_cohort_sha256"] != b["evaluation_cohort_sha256"]
    candidate["candidates"][0]["plan"]["anterior_seed_lps_mm"] = [0.5, 0, 0]
    c = evaluator.evaluate_learning(frozen, candidate, tolerances=TOLERANCES)
    assert a["candidate_snapshot_sha256"] != c["candidate_snapshot_sha256"]


def test_original_manifest_hash_and_real_review_hash_are_required(tmp_path):
    frozen, candidate = _data(tmp_path)
    frozen["records"][0]["plan"]["anterior_seed_lps_mm"][0] = 1
    with pytest.raises(evaluator.EvaluationError):
        evaluator.evaluate_learning(frozen, candidate, tolerances=TOLERANCES)

    frozen, candidate = _data(tmp_path / "other")
    frozen["records"][0]["case_id"] = "case-20261104-123456-abcd"
    candidate["candidates"][0]["case_id"] = "case-20261104-123456-abcd"
    frozen["cohort_sha256"] = exporter._digest([{
        "case_id": row["case_id"], "scope": row["scope"], "mask_version": row["mask_version"],
        "plan": row["plan"],
    } for row in frozen["records"]])
    frozen["manifest_sha256"] = exporter._digest({k: v for k, v in frozen.items()
                                                if k != "manifest_sha256"})
    candidate["frozen_manifest_sha256"] = frozen["manifest_sha256"]
    with pytest.raises(evaluator.EvaluationError):
        evaluator.evaluate_learning(frozen, candidate, tolerances=TOLERANCES)


def test_evaluation_never_opens_ledger_or_history(tmp_path, monkeypatch):
    frozen, candidate = _data(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("offline evaluation must not read live data")
    monkeypatch.setattr(exporter.endplane_learning, "EndplaneLearningStore", forbidden)
    monkeypatch.setattr(evaluator.evaluate_endplanes.endplane_history, "list_revisions", forbidden)
    report = evaluator.evaluate_learning(frozen, candidate, tolerances=TOLERANCES)
    assert report["passed"] == 1


def test_evaluator_source_hash_binds_exporter_canonicalization_source(tmp_path, monkeypatch):
    frozen, candidate = _data(tmp_path / "ledger")
    original = evaluator.evaluate_learning(frozen, candidate, tolerances=TOLERANCES)
    changed_exporter_source = tmp_path / "exporter-source.py"
    changed_exporter_source.write_text("# canonicalization changed\n", encoding="utf-8")
    monkeypatch.setattr(evaluator.exporter, "__file__", str(changed_exporter_source))
    changed = evaluator.evaluate_learning(frozen, candidate, tolerances=TOLERANCES)
    assert original["evaluator_source_sha256"] != changed["evaluator_source_sha256"]
    assert original["candidate_snapshot_sha256"] == changed["candidate_snapshot_sha256"]
    assert original["evaluation_cohort_sha256"] == changed["evaluation_cohort_sha256"]
    assert original["cases"] == changed["cases"]


def test_cli_reads_only_explicit_files_and_rejects_duplicate_json_keys(tmp_path, capsys):
    frozen, candidate = _data(tmp_path / "ledger")
    frozen_path, candidate_path = tmp_path / "frozen.json", tmp_path / "candidate.json"
    frozen_path.write_text(json.dumps(frozen), encoding="utf-8")
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    args = [str(frozen_path), str(candidate_path), "--max-plane-offset-mm", "1",
            "--max-normal-deg", "5", "--max-seed-mm", "1"]
    assert evaluator.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] == 1
    candidate_path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    assert evaluator.main(args) == 2
    assert "invalid_or_unreadable_learning_evaluation" in capsys.readouterr().err
