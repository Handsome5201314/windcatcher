import hashlib
import json

import pytest

from pipeline.endplane_learning import EndplaneLearningStore, plan_hash
from scripts import export_endplane_learning_dataset as export


def _plan(index=20):
    return {
        "format": "oblique_planes_v1",
        "scope": "main_airway",
        "mask_version": 3,
        "anterior_plane": {"point_lps_mm": [index, 0, 0], "normal_lps": [0, -1, 0]},
        "posterior_plane": {"point_lps_mm": [0, 20, 0], "normal_lps": [0, 1, 0]},
    }


def _proposal(store, case_id, index=20):
    return store.create_proposal(
        case_id, "main_airway", 3, _plan(index), "baseline", "v1", actor_id="operator-1"
    )


def test_export_requires_explicit_nonempty_allowlist_and_cohort_digest(tmp_path):
    store = EndplaneLearningStore(tmp_path)
    proposal = _proposal(store, "synthetic-export-1")
    store.record_feedback(proposal["proposal_id"], "accepted", actor_id="operator-1")
    digest = "a" * 64

    with pytest.raises(export.ExportError):
        export.export_dataset([], source_cohort_sha256=digest, store=store, actor_id="operator-1")
    with pytest.raises(export.ExportError):
        export.export_dataset([proposal["proposal_id"]], source_cohort_sha256="bad",
                              store=store, actor_id="operator-1")
    with pytest.raises(export.ExportError):
        export.export_dataset([proposal["proposal_id"], proposal["proposal_id"]],
                              source_cohort_sha256=digest, store=store, actor_id="operator-1")


def test_export_accepts_only_terminal_review_and_keeps_proposed_plan(tmp_path):
    store = EndplaneLearningStore(tmp_path)
    accepted = _proposal(store, "synthetic-export-2", 20)
    pending = _proposal(store, "synthetic-export-3", 21)
    rejected = _proposal(store, "synthetic-export-4", 22)
    store.record_feedback(accepted["proposal_id"], "accepted", actor_id="operator-1")
    store.record_feedback(rejected["proposal_id"], "rejected", actor_id="operator-1",
                          feedback={"reason_code": "wrong_terminal"})
    digest = "b" * 64

    result = export.export_dataset([accepted["proposal_id"]], source_cohort_sha256=digest,
                                   store=store, actor_id="operator-1")
    assert result["schema_version"] == 1
    assert result["dataset_kind"] == "endplane_learning_reviewed"
    assert result["source_cohort_sha256"] == digest
    assert result["cohort_sha256"] != digest
    assert result["proposal_count"] == 1
    record = result["records"][0]
    assert record["feedback_status"] == "accepted"
    assert record["plan"] == accepted["plan"]
    assert record["proposal_plan_sha256"] == accepted["plan_hash"]
    assert record["effective_plan_sha256"] == plan_hash(accepted["plan"],
                                                          scope="main_airway", mask_version=3)
    assert "patient" not in json.dumps(result).lower()
    assert "path" not in json.dumps(result).lower()

    with pytest.raises(export.ExportError):
        export.export_dataset([pending["proposal_id"]], source_cohort_sha256=digest,
                              store=store, actor_id="operator-1")
    with pytest.raises(export.ExportError):
        export.export_dataset([rejected["proposal_id"]], source_cohort_sha256=digest,
                              store=store, actor_id="operator-1")


def test_adjusted_feedback_exports_human_plan_and_binds_hashes(tmp_path):
    store = EndplaneLearningStore(tmp_path)
    proposal = _proposal(store, "synthetic-export-5", 20)
    adjusted = _plan(25)
    feedback = store.record_feedback(
        proposal["proposal_id"], "adjusted", actor_id="operator-1",
        feedback={"reason_code": "operator_correction", "adjusted_plan": adjusted},
    )
    result = export.export_dataset([proposal["proposal_id"]], source_cohort_sha256="c" * 64,
                                   store=store, actor_id="operator-1")
    record = result["records"][0]
    assert record["feedback_status"] == "adjusted"
    assert record["plan"] == adjusted
    assert record["effective_plan_sha256"] == plan_hash(adjusted,
                                                         scope="main_airway", mask_version=3)
    assert len(record["feedback_sha256"]) == 64
    assert len(result["manifest_sha256"]) == 64


def test_export_reads_only_allowlisted_ids_and_never_scans_other_records(tmp_path, monkeypatch):
    store = EndplaneLearningStore(tmp_path)
    proposal = _proposal(store, "synthetic-export-6")
    store.record_feedback(proposal["proposal_id"], "accepted", actor_id="operator-1")

    def fail_list(*args, **kwargs):
        raise AssertionError("export must not scan all proposals")

    monkeypatch.setattr(store, "list_proposals", fail_list)
    result = export.export_dataset([proposal["proposal_id"]], source_cohort_sha256="d" * 64,
                                   store=store, actor_id="operator-1")
    assert result["proposal_count"] == 1


def test_cli_writes_manifest_to_explicit_output(tmp_path, capsys):
    store = EndplaneLearningStore(tmp_path / "learning")
    proposal = _proposal(store, "synthetic-export-7")
    store.record_feedback(proposal["proposal_id"], "accepted", actor_id="operator-1")
    output = tmp_path / "frozen.json"
    assert export.main([
        "--proposal-id", proposal["proposal_id"],
        "--source-cohort-sha256", "e" * 64,
        "--actor-id", "operator-1",
        "--learning-dir", str(tmp_path / "learning"),
        "--output", str(output),
    ]) == 0
    stdout = json.loads(capsys.readouterr().out)
    assert json.loads(output.read_text(encoding="utf-8")) == stdout


def test_export_cannot_read_another_actors_feedback(tmp_path):
    store = EndplaneLearningStore(tmp_path)
    proposal = _proposal(store, "synthetic-export-8")
    store.record_feedback(proposal["proposal_id"], "accepted", actor_id="operator-1")
    for actor_id in (None, "operator-2"):
        with pytest.raises(export.ExportError):
            export.export_dataset([proposal["proposal_id"]], source_cohort_sha256="f" * 64,
                                  store=store, actor_id=actor_id)


def test_export_revalidates_feedback_and_never_hashes_unknown_fields(tmp_path, monkeypatch):
    store = EndplaneLearningStore(tmp_path)
    proposal = _proposal(store, "synthetic-export-9")
    store.record_feedback(proposal["proposal_id"], "accepted", actor_id="operator-1")
    feedback = store.list_feedback(proposal["proposal_id"], actor_id="operator-1")[0]
    feedback["feedback"]["patient_name"] = "must-not-export"
    monkeypatch.setattr(store, "list_feedback", lambda *args, **kwargs: [feedback])
    with pytest.raises(export.ExportError):
        export.export_dataset([proposal["proposal_id"]], source_cohort_sha256="0" * 64,
                              store=store, actor_id="operator-1")


def test_cohort_hash_is_computed_from_reviewed_plan_not_source_digest(tmp_path):
    store = EndplaneLearningStore(tmp_path)
    proposal = _proposal(store, "synthetic-export-10")
    store.record_feedback(proposal["proposal_id"], "accepted", actor_id="operator-1")
    a = export.export_dataset([proposal["proposal_id"]], source_cohort_sha256="0" * 64,
                              store=store, actor_id="operator-1")
    b = export.export_dataset([proposal["proposal_id"]], source_cohort_sha256="1" * 64,
                              store=store, actor_id="operator-1")
    assert a["cohort_sha256"] == b["cohort_sha256"]
    assert a["manifest_sha256"] != b["manifest_sha256"]


def test_real_case_requires_explicit_review_refs_and_hashes_reviewer(tmp_path):
    store = EndplaneLearningStore(tmp_path)
    proposal = _proposal(store, "case-20261104-123456-abcd")
    store.record_feedback(proposal["proposal_id"], "accepted", actor_id="operator-1")
    with pytest.raises(export.ExportError):
        export.export_dataset([proposal["proposal_id"]], source_cohort_sha256="2" * 64,
                              store=store, actor_id="operator-1")
    review = {proposal["proposal_id"]: {
        "evaluation_authorization_ref": "approval:42",
        "reference_confirmation_ref": "review:9",
        "reviewer_id": "reviewer-2",
        "reviewed_at_utc": "2026-11-04T01:02:03Z",
    }}
    result = export.export_dataset([proposal["proposal_id"]], source_cohort_sha256="2" * 64,
                                   store=store, actor_id="operator-1", review_references=review)
    assert len(result["records"][0]["review_references_sha256"]) == 64
    encoded = json.dumps(result)
    assert "reviewer-2" not in encoded and "approval:42" not in encoded


def test_cli_rejects_existing_output_and_empty_ids_before_opening_store(tmp_path, monkeypatch, capsys):
    output = tmp_path / "frozen.json"
    output.write_text("existing", encoding="utf-8")
    def no_store(*args, **kwargs):
        raise AssertionError("must validate before creating ledger")
    monkeypatch.setattr(export.endplane_learning, "EndplaneLearningStore", no_store)
    args = ["--source-cohort-sha256", "3" * 64, "--output", str(output)]
    assert export.main(args) == 2
    assert export.main(args + ["--proposal-id", "proposal-" + "a" * 32]) == 2
    assert output.read_text(encoding="utf-8") == "existing"
