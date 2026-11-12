"""Keep test writes out of the user's local operational stores."""

import pytest


@pytest.fixture(autouse=True)
def isolated_local_storage(tmp_path, monkeypatch):
    # Retention tests explicitly supply their own synthetic case roots.
    monkeypatch.setattr('pipeline.segmentation_learning.retention_cases', lambda: [])
    monkeypatch.setattr('pipeline.segmentation_model_jobs.REPORT_ROOT', tmp_path / 'model-evaluations')
    monkeypatch.setenv("WINDCATCHER_MONITOR_DIR", str(tmp_path / "activity-monitor"))
    monkeypatch.setenv("WINDCATCHER_AUTOMATION_DIR", str(tmp_path / "automation"))
    monkeypatch.setenv("WINDCATCHER_LEARNING_DIR", str(tmp_path / "learning"))
