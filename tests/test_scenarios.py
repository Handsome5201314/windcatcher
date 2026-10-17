"""鼻塞方案的版本化存储、人工审核和下载边界测试。"""

from __future__ import annotations

import json
import sqlite3
import zipfile

import pytest

from pipeline import activity_monitor, cases, config, plug_models, scenarios
from pipeline.errors import PipelineError


@pytest.fixture
def scenario_case(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CASES_DIR", tmp_path / "cases")
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path / "cases")
    case_id = cases.create_case("synthetic", "<合成测试>", synthetic=True)
    return case_id, cases.case_dir(case_id)


def _box_stl():
    vertices = (
        (0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0),
        (0, 0, 10), (10, 0, 10), (10, 10, 10), (0, 10, 10),
    )
    faces = (
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
        (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    )
    return plug_models.mesh_to_binary_stl(plug_models.Mesh(vertices, faces, "test"))


def test_scenario_preserves_original_and_placement_versions(scenario_case):
    case_id, case_root = scenario_case
    original = _box_stl()
    created = scenarios.create_scenario(case_id, original, "mm")
    scenario_id = created["scenario_id"]
    root = case_root / "scenarios" / scenario_id

    assert created["status"] == "uploaded"
    assert (root / "input" / "original.stl").read_bytes() == original
    assert created["model"]["original_sha256"]
    assert created["placement"]["confirmed"] is False

    updated = scenarios.update_placement(
        case_id,
        scenario_id,
        {"translation_mm": [1, 2, 3], "rotation_deg": [0, 0, 15], "scale": [1, 1, 1]},
    )
    assert updated["current_version"] == 2
    assert updated["status"] == "placement_pending_review"
    assert (root / "versions" / "v0001" / "placed" / "model.stl").is_file()
    assert (root / "versions" / "v0002" / "placed" / "model.stl").is_file()
    assert (root / "input" / "original.stl").read_bytes() == original
    events = activity_monitor.query_events(case_id=case_id)["events"]
    assert [event["action"] for event in events if event["target"] == "scenario"] == [
        "scenario_created", "scenario_placement_updated",
    ]


def test_review_requires_manual_placement_and_records_deidentified_history(scenario_case):
    case_id, _ = scenario_case
    created = scenarios.create_scenario(case_id, _box_stl(), "mm")
    with pytest.raises(PipelineError, match="摆位确认"):
        scenarios.record_review(case_id, created["scenario_id"], approved=True, reviewer_role="engineer")

    scenarios.update_placement(
        case_id,
        created["scenario_id"],
        {"translation_mm": [0, 0, 0], "rotation_deg": [0, 0, 0], "scale": [1, 1, 1]},
    )
    reviewed = scenarios.record_review(
        case_id,
        created["scenario_id"],
        approved=True,
        reviewer_role="engineer",
        notes="已核对摆位与气道方向",
    )
    assert reviewed["status"] == "reviewed"
    assert reviewed["review"]["approved"] is True
    assert reviewed["versions"][-1]["cfd_ready"] is False
    assert reviewed["base_status"] == "unavailable"
    assert "patient" not in json.dumps(reviewed, ensure_ascii=False).lower()


def test_stale_expected_version_is_rejected_and_history_snapshot_is_versioned(scenario_case):
    case_id, case_root = scenario_case
    created = scenarios.create_scenario(case_id, _box_stl(), "mm")
    scenario_id = created["scenario_id"]
    current = scenarios.update_placement(
        case_id, scenario_id,
        {"translation_mm": [1, 0, 0], "rotation_deg": [0, 0, 0], "scale": [1, 1, 1]},
        expected_version=1,
    )
    with pytest.raises(PipelineError, match="版本已变化"):
        scenarios.update_placement(
            case_id, scenario_id,
            {"translation_mm": [2, 0, 0], "rotation_deg": [0, 0, 0], "scale": [1, 1, 1]},
            expected_version=1,
        )
    old_snapshot = json.loads((case_root / "scenarios" / scenario_id / "versions" / "v0001" / "scenario.json").read_text())
    assert old_snapshot["current_version"] == 1
    assert current["current_version"] == 2


def test_audit_is_persistent_and_missing_zip_part_is_reported(scenario_case):
    case_id, case_root = scenario_case
    created = scenarios.create_scenario(case_id, _box_stl(), "mm")
    scenario_root = case_root / "scenarios" / created["scenario_id"]
    audit = (scenario_root / "audit.jsonl").read_text(encoding="utf-8")
    assert "scenario_created" in audit
    with sqlite3.connect(case_root / "research.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM scenario_versions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM scenario_events").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM scenario_artifacts WHERE kind='placed'").fetchone()[0] == 1
    (scenario_root / "versions" / "v0001" / "review.json").unlink()
    with pytest.raises(PipelineError, match="缺少必要产物"):
        scenarios.download_artifact(case_id, created["scenario_id"])


def test_case_base_change_marks_scenario_stale_and_blocks_review(scenario_case):
    case_id, case_root = scenario_case
    root = case_root
    (root / "segment").mkdir(exist_ok=True)
    (root / "surface").mkdir(exist_ok=True)
    (root / "segment" / "segmentation.json").write_text(json.dumps({"mask_version": 1}), encoding="utf-8")
    (root / "surface" / "airway.stl").write_bytes(_box_stl())
    (root / "surface" / "surface.json").write_text(json.dumps({"mask_version": 1}), encoding="utf-8")
    cases.update_stage(case_id, "surface", "ok", data={"mask_version": 1})
    created = scenarios.create_scenario(case_id, _box_stl(), "mm")
    scenario_id = created["scenario_id"]
    scenarios.update_placement(
        case_id, scenario_id,
        {"translation_mm": [0, 0, 0], "rotation_deg": [0, 0, 0], "scale": [1, 1, 1]},
        expected_version=1,
    )
    reviewed = scenarios.record_review(case_id, scenario_id, approved=True,
                                       reviewer_role="engineer", expected_version=2)
    assert reviewed["base_status"] == "current"
    assert reviewed["versions"][-1]["review_ready"] is True
    assert reviewed["versions"][-1]["cfd_ready"] is False
    (root / "segment" / "segmentation.json").write_text(json.dumps({"mask_version": 2}), encoding="utf-8")
    assert scenarios.get_scenario(case_id, scenario_id)["base_status"] == "stale"
    with pytest.raises(PipelineError, match="气道基准已变化"):
        scenarios.record_review(case_id, scenario_id, approved=True, reviewer_role="engineer", expected_version=2)


def test_download_bundle_contains_only_relative_research_assets(scenario_case):
    case_id, case_root = scenario_case
    created = scenarios.create_scenario(case_id, _box_stl(), "mm")
    path, media_type, filename = scenarios.download_artifact(case_id, created["scenario_id"])

    assert media_type == "application/zip"
    assert filename.endswith(".zip")
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
    assert {"input/original.stl", "normalized/model.stl", "placed/model.stl", "manifest.json"} <= names
    assert "scenario.json" in names
    assert all(not name.startswith(("C:", "\\")) for name in names)
    assert str(case_root) not in path.read_bytes().decode("latin1", errors="ignore")
