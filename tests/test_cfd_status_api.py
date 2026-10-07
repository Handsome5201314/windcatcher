"""病例查询只读补充真实 CFD 求解记录，不能把旧结果当作当前运行。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import server
from pipeline import cases


@pytest.fixture
def cfd_case(tmp_path, monkeypatch):
    monkeypatch.setattr("pipeline.config.CASES_DIR", tmp_path)
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    case_id = cases.create_case("synthetic", "<合成测试>", synthetic=True)
    return case_id, cases.case_dir(case_id), None


def _query_run_data(cfd_case):
    case_id, _root, _client = cfd_case
    return server._require_case(case_id)["stages"]["cfd_run"]["data"]


@pytest.mark.parametrize("status", ["ok", "warn", "failed", "blocked"])
def test_terminal_status_returns_real_run_record(cfd_case, status):
    case_id, root, _client = cfd_case
    record = {
        "status": "ran" if status in ("ok", "warn") else "failed",
        "backend": "docker",
        "steps": [
            {"name": "simpleFoam", "returncode": 0, "log": "cfd/logs/simpleFoam.log"},
            {"name": "foamToVTK", "returncode": 1, "log": "cfd/logs/foamToVTK.log"},
        ],
        "residuals": {"status": "parsed", "converged": True},
        "postprocessing": {"mass_conservation_error_percent": 0.0007},
    }
    (root / "cfd" / "run_status.json").write_text(json.dumps(record), encoding="utf-8")
    cases.update_stage(
        case_id, "cfd_run", status,
        data={"run_status": {"backend": "old"}, "run_status_error": "旧错误"},
    )

    data = _query_run_data(cfd_case)

    assert data["run_status"] == record
    assert data["run_status"]["steps"][1]["returncode"] == 1
    assert "run_status_error" not in data


@pytest.mark.parametrize("status", ["running", "pending"])
def test_nonterminal_status_hides_old_record_and_error(cfd_case, status):
    case_id, root, _client = cfd_case
    (root / "cfd" / "run_status.json").write_text("invalid old record", encoding="utf-8")
    cases.update_stage(
        case_id, "cfd_run", status,
        data={"run_status": {"status": "ran"}, "run_status_error": "旧错误", "backend": "docker"},
    )

    data = _query_run_data(cfd_case)

    assert "run_status" not in data
    assert "run_status_error" not in data
    assert data["backend"] == "docker"


@pytest.mark.parametrize("status", ["ok", "warn"])
def test_completed_stage_without_record_reports_missing(cfd_case, status):
    case_id, _root, _client = cfd_case
    cases.update_stage(case_id, "cfd_run", status, data={"solved": True})

    data = _query_run_data(cfd_case)

    assert "run_status" not in data
    assert "缺失" in data["run_status_error"]
    assert "cfd/run_status.json" in data["run_status_error"]


@pytest.mark.parametrize("status", ["failed", "blocked"])
def test_failed_stage_without_record_preserves_stage_error(cfd_case, status):
    case_id, _root, client = cfd_case
    error = {"reason": "气道掩膜尚未人工确认"}
    cases.update_stage(case_id, "cfd_run", status, data={"error": error})

    stage = server._require_case(case_id)["stages"]["cfd_run"]

    assert stage["status"] == status
    assert stage["data"]["error"] == error
    assert "run_status" not in stage["data"]
    assert "run_status_error" not in stage["data"]


@pytest.mark.parametrize("status", ["ok", "warn", "failed", "blocked"])
@pytest.mark.parametrize(
    ("content", "reason"),
    [("{bad json", "JSON"), ("[]", "对象"), ("null", "对象")],
)
def test_malformed_record_reports_read_error(cfd_case, status, content, reason):
    case_id, root, _client = cfd_case
    (root / "cfd" / "run_status.json").write_text(content, encoding="utf-8")
    cases.update_stage(case_id, "cfd_run", status, data={"run_status": {"status": "ran"}})

    data = _query_run_data(cfd_case)

    assert "run_status" not in data
    assert reason in data["run_status_error"]


def test_record_read_failure_is_observable(cfd_case, monkeypatch):
    case_id, root, _client = cfd_case
    path = root / "cfd" / "run_status.json"
    path.write_text("{}", encoding="utf-8")
    cases.update_stage(case_id, "cfd_run", "warn")
    original_read = Path.read_text

    def denied_read(self, *args, **kwargs):
        if self == path:
            raise PermissionError("access denied")
        return original_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied_read)

    data = _query_run_data(cfd_case)

    assert "run_status" not in data
    assert "无法读取" in data["run_status_error"]


def test_record_with_invalid_encoding_is_observable(cfd_case):
    case_id, root, _client = cfd_case
    (root / "cfd" / "run_status.json").write_bytes(b"\xff")
    cases.update_stage(case_id, "cfd_run", "warn")

    data = _query_run_data(cfd_case)

    assert "run_status" not in data
    assert "编码" in data["run_status_error"]


def test_case_get_does_not_persist_derived_run_record(cfd_case):
    case_id, root, _client = cfd_case
    record_path = root / "cfd" / "run_status.json"
    record_path.write_text('{"status": "ran", "backend": "docker"}', encoding="utf-8")
    cases.update_stage(case_id, "cfd_run", "warn", data={"solved": True})
    case_path = root / cases.CASE_FILE
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (case_path, record_path)
    }

    assert _query_run_data(cfd_case)["run_status"]["backend"] == "docker"

    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (case_path, record_path)
    } == before
    assert "run_status" not in cases.get_case(case_id)["stages"]["cfd_run"]["data"]
