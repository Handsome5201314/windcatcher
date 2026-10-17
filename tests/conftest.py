"""Keep test operations out of the user's local activity history."""

import pytest


@pytest.fixture(autouse=True)
def isolated_activity_history(tmp_path, monkeypatch):
    monkeypatch.setenv("WINDCATCHER_MONITOR_DIR", str(tmp_path / "activity-monitor"))
    monkeypatch.setenv("WINDCATCHER_AUTOMATION_DIR", str(tmp_path / "automation"))
