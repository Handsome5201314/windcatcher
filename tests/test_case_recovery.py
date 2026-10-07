"""服务意外中断后的病例状态恢复。"""

from __future__ import annotations

import asyncio

import pytest

from pipeline import cases
from pipeline.errors import PipelineError


def test_recover_interrupted_solver_allows_delete(tmp_path, monkeypatch):
    monkeypatch.setattr("pipeline.config.CASES_DIR", tmp_path)
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    interrupted = cases.create_case("synthetic", "<合成测试>", synthetic=True)
    untouched = cases.create_case("synthetic", "<合成测试>", synthetic=True)
    cases.update_stage(interrupted, "cfd_run", "running", message="正在执行 simpleFoam")
    cases.update_stage(untouched, "qc", "warn", message="待复核")
    original_untouched = cases.get_case(untouched)

    assert cases.recover_interrupted_cases() == 1
    recovered = cases.get_case(interrupted)
    assert recovered["stages"]["cfd_run"]["status"] == "failed"
    assert "中断" in recovered["stages"]["cfd_run"]["message"]
    assert cases.get_case(untouched) == original_untouched
    assert cases.recover_interrupted_cases() == 0

    cases.delete_case(interrupted)
    assert not cases.case_dir(interrupted).exists()
    assert cases.case_dir(untouched).exists()


def test_startup_recovers_running_import_job(tmp_path, monkeypatch):
    monkeypatch.setattr("pipeline.config.CASES_DIR", tmp_path)
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    interrupted = cases.create_case("synthetic", "<合成测试>", synthetic=True)
    cases.update_stage(interrupted, "import", "running", message="正在读取 DICOM")
    cases.update_job(interrupted, "running", "import", "正在读取 DICOM")
    preview_only = cases.create_case("synthetic", "<合成测试>", synthetic=True)
    cases.update_job(preview_only, "running", "preview", "正在生成切面预览")

    import server
    monkeypatch.setattr(server.app.state, "interrupted_cases_recovered", False, raising=False)

    async def start_once():
        async with server.app.router.lifespan_context(server.app):
            pass

    asyncio.run(start_once())
    recovered = cases.get_case(interrupted)
    assert recovered["job"]["status"] == "failed"
    assert recovered["job"]["stage"] == "import"
    assert "中断" in recovered["job"]["message"]
    assert recovered["stages"]["import"]["status"] == "failed"
    preview_recovered = cases.get_case(preview_only)
    assert preview_recovered["job"]["status"] == "failed"
    assert preview_recovered["job"]["stage"] == "preview"
    assert preview_recovered["stages"]["import"]["status"] == "ok"
    cases.delete_case(interrupted)
    cases.delete_case(preview_only)
    assert not cases.case_dir(interrupted).exists()
    assert not cases.case_dir(preview_only).exists()

    active_here = cases.create_case("synthetic", "<合成测试>", synthetic=True)
    cases.update_stage(active_here, "segment", "running", message="本进程正在分割")
    asyncio.run(start_once())
    assert cases.get_case(active_here)["stages"]["segment"]["status"] == "running"
    with pytest.raises(PipelineError) as exc_info:
        cases.delete_case(active_here)
    assert exc_info.value.status == "blocked"
