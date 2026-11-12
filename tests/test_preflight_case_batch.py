"""Regression checks for the anonymous, read-only batch preflight."""

from __future__ import annotations

from scripts import preflight_case_batch as subject
from pipeline.errors import PipelineError


CASES = (
    "case-20261102-122120-0aca",
    "case-20261102-122104-6357",
    "case-20261102-122049-52c3",
)


def inspected(*, segment="ok", surface="ok", cfd_prep="ok", cfd_run="pending", ready=True):
    return {"stages": {"import": "ok", "qc": "ok", "anonymize": "ok",
                       "segment": segment, "surface": surface,
                       "cfd_prep": cfd_prep, "cfd_run": cfd_run},
            "baseline": {"ready": ready, "reason": "人工复核未完成"} if not ready else {"ready": True}}


def test_readiness_only_offers_solver_after_all_gates():
    report = subject.scan_cases(CASES, inspect_fn=lambda _id: inspected())
    assert report["summary"] == {"ready": 3, "paused": 0, "invalid_id": 0,
                                  "duplicate_id": 0, "inspection_error": 0}
    assert all(row["preflight_eligible_actions"] == ["submit_cfd_batch"] for row in report["items"])
    assert all(not row["requires_human_review"] for row in report["items"])


def test_unconfirmed_mask_and_unprepared_surface_do_not_offer_solver():
    data = {CASES[0]: inspected(segment="warn", surface="pending", cfd_prep="pending", ready=False),
            CASES[1]: inspected(surface="pending", cfd_prep="pending", ready=False)}
    report = subject.scan_cases(CASES[:2], inspect_fn=data.__getitem__)
    assert [row["next_step"] for row in report["items"]] == ["review_mask", "review_endplanes_and_surface"]
    assert report["summary"]["ready"] == 0
    assert all(row["preflight_eligible_actions"] == [] for row in report["items"])


def test_existing_or_failed_solver_requires_review_not_duplicate_run():
    data = {CASES[0]: inspected(cfd_run="warn"), CASES[1]: inspected(cfd_run="failed")}
    report = subject.scan_cases(CASES[:2], inspect_fn=data.__getitem__)
    assert [row["next_step"] for row in report["items"]] == ["review_existing_numeric_gate", "review_failed_cfd"]
    assert all(row["preflight_eligible_actions"] == [] for row in report["items"])


def test_authoritative_preflight_accepts_existing_warn_stage():
    report = subject.scan_cases((CASES[0],), inspect_fn=lambda _id: inspected(
        surface="warn", cfd_prep="warn", ready=True))
    assert report["items"][0]["next_step"] == "submit_cfd_batch"


def test_old_global_planes_require_review_before_preparing_cfd():
    report = subject.scan_cases(
        (CASES[0],),
        inspect_fn=lambda _id: inspected(cfd_prep="pending", ready=False),
        plan_fn=lambda _id, _scope: {"format": "oblique_planes_v1"},
    )
    row = report["items"][0]
    assert row["status"] == "paused"
    assert row["next_step"] == "review_legacy_global_endplanes"
    assert row["requires_human_review"] is True
    assert row["preflight_eligible_actions"] == []


def test_missing_and_invalid_cases_are_visible_without_stopping_batch():
    def inspect(case_id):
        if case_id == CASES[0]:
            raise PipelineError("failed", "病例不存在", ["检查匿名病例编号"])
        return inspected(segment="warn", ready=False)

    report = subject.scan_cases((CASES[0], "bad-id", CASES[1], CASES[1]), inspect_fn=inspect)
    assert [row["status"] for row in report["items"]] == [
        "inspection_error", "invalid_id", "paused", "duplicate_id",
    ]
    assert report["items"][0]["reason"] == "病例不存在"
    assert report["items"][2]["next_step"] == "review_mask"


def test_malformed_inspection_does_not_hide_following_case():
    report = subject.scan_cases(CASES[:2], inspect_fn=lambda case_id: (
        {} if case_id == CASES[0] else inspected(segment="warn", ready=False)))
    assert [row["status"] for row in report["items"]] == ["inspection_error", "paused"]


def test_date_selection_is_sorted_and_anonymous():
    rows = [{"case_id": CASES[1]}, {"case_id": CASES[0]},
            {"case_id": "case-20261011-013210-0080"}, {"case_id": "patient-name"}]
    assert subject.select_case_ids([], "2026-11-02", list_fn=lambda: rows) == sorted(CASES[:2])
