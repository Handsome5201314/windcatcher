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


def test_running_status_exposes_only_current_live_record(cfd_case):
    case_id, root, _client = cfd_case
    cases.update_stage(case_id, "cfd_run", "running", data={"run_id": "current-run", "backend": "docker"})
    record = {"status": "running", "run_id": "current-run", "current_step": "simpleFoam", "solver": {"iteration": 7}}
    (root / "cfd" / "run_status.json").write_text(json.dumps(record), encoding="utf-8")

    data = _query_run_data(cfd_case)

    assert data["run_status"] == record
    assert data["run_status"]["solver"]["iteration"] == 7


def test_legacy_timeout_record_is_explained_without_rewriting_file(cfd_case):
    case_id, root, _client = cfd_case
    record = {
        "status": "failed",
        "backend": "docker",
        "failed_step": "simpleFoam",
        "timeout_seconds": 7200,
        "steps": [{"name": "simpleFoam", "returncode": 124}],
    }
    path = root / "cfd" / "run_status.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    cases.update_stage(case_id, "cfd_run", "failed", data={"solved": False})

    data = _query_run_data(cfd_case)

    status = data["run_status"]
    assert status["timed_out"] is True
    assert "超时" in status["message"]
    assert status["returncode"] == 124
    assert path.read_text(encoding="utf-8") == json.dumps(record)


def test_mpi_slots_failure_uses_real_log_without_rewriting_artifacts(cfd_case):
    case_id, root, _client = cfd_case
    record = {
        "status": "failed", "backend": "docker", "failed_step": "simpleFoam",
        "returncode": 1, "message": "simpleFoam 退出码 1",
        "steps": [{"name": "simpleFoam", "returncode": 1}],
    }
    record_path = root / "cfd" / "run_status.json"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    log_path = root / "cfd" / "logs" / "simpleFoam.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "There are not enough slots available in the system to satisfy the 16\n"
        "slots that were requested by the application:\n  simpleFoam\n",
        encoding="utf-8",
    )
    cases.update_stage(case_id, "cfd_run", "failed", data={"solved": False})
    paths = (root / cases.CASE_FILE, record_path, log_path)
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}

    status = _query_run_data(cfd_case)["run_status"]

    assert "MPI" in status["message"] and "槽位" in status["message"]
    assert status["message"] != record["message"]
    assert status["remediation"]
    assert status["returncode"] == 1
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths} == before


@pytest.mark.parametrize("record_changes", [
    {"message": "已确认的其他失败原因", "remediation": ["保留既有处理建议"]},
    {"timed_out": True, "returncode": 124, "message": "已确认超时"},
])
def test_slots_log_does_not_override_explicit_failure_or_timeout(cfd_case, record_changes):
    case_id, root, _client = cfd_case
    record = {"status": "failed", "failed_step": "simpleFoam", "returncode": 1, **record_changes}
    (root / "cfd" / "run_status.json").write_text(json.dumps(record), encoding="utf-8")
    log = root / "cfd" / "logs" / "simpleFoam.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("There are not enough slots available", encoding="utf-8")
    cases.update_stage(case_id, "cfd_run", "failed")

    status = _query_run_data(cfd_case)["run_status"]

    assert status["message"] == record["message"]
    if record.get("timed_out"):
        assert status["timed_out"] is True and status["returncode"] == 124
    else:
        assert status["remediation"] == record["remediation"]


def test_generic_failure_without_mpi_log_remains_unchanged(cfd_case):
    case_id, root, _client = cfd_case
    record = {"status": "failed", "failed_step": "simpleFoam", "returncode": 1, "message": "simpleFoam 退出码 1"}
    (root / "cfd" / "run_status.json").write_text(json.dumps(record), encoding="utf-8")
    cases.update_stage(case_id, "cfd_run", "failed")

    assert _query_run_data(cfd_case)["run_status"] == record


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
