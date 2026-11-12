"""Case IDs reserve a fresh directory before writing any case content."""

import json

import pytest

from pipeline import cases, config
from pipeline.errors import PipelineError


OLD = "case-20261102-120000-aaaa"
NEW = "case-20261102-120000-bbbb"


def test_collision_never_overwrites_existing_case_or_files(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    monkeypatch.setattr(config, "CASES_DIR", tmp_path)
    monkeypatch.setattr(cases, "_new_case_id", lambda: OLD)
    assert cases.create_case("synthetic", "<test>", synthetic=True) == OLD
    original = (tmp_path / OLD / "case.json").read_bytes()
    (tmp_path / OLD / "import" / "sentinel.txt").write_text("keep", encoding="utf-8")

    ids = iter([OLD, NEW])
    monkeypatch.setattr(cases, "_new_case_id", lambda: next(ids))
    assert cases.create_case("synthetic", "<test>", synthetic=True) == NEW
    assert (tmp_path / OLD / "case.json").read_bytes() == original
    assert (tmp_path / OLD / "import" / "sentinel.txt").read_text(encoding="utf-8") == "keep"
    assert json.loads((tmp_path / NEW / "case.json").read_text(encoding="utf-8"))["case_id"] == NEW


def test_exhausted_collisions_fail_without_changing_existing_case(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    monkeypatch.setattr(config, "CASES_DIR", tmp_path)
    monkeypatch.setattr(cases, "_new_case_id", lambda: OLD)
    cases.create_case("synthetic", "<test>", synthetic=True)
    original = (tmp_path / OLD / "case.json").read_bytes()
    with pytest.raises(PipelineError, match="唯一病例编号"):
        cases.create_case("synthetic", "<test>", synthetic=True)
    assert (tmp_path / OLD / "case.json").read_bytes() == original


def test_pending_deletion_marker_reserves_case_id_after_files_are_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    monkeypatch.setattr(config, "CASES_DIR", tmp_path)
    monkeypatch.setattr(cases, "_new_case_id", lambda: OLD)
    assert cases.create_case("synthetic", "<test>", synthetic=True) == OLD
    cases.delete_case(OLD, keep_marker=True, owner_id="owner")
    assert not (tmp_path / OLD).exists()
    assert (tmp_path / f".windcatcher-deleting-{OLD}.json").is_file()

    ids = iter([OLD, NEW])
    monkeypatch.setattr(cases, "_new_case_id", lambda: next(ids))
    assert cases.create_case("synthetic", "<test>", synthetic=True) == NEW
    assert not (tmp_path / OLD).exists()
    assert (tmp_path / NEW / "case.json").is_file()
