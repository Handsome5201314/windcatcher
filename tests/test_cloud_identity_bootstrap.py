"""Fail-closed startup when personal identity initialization cannot finish."""

import pytest

import cloud
import server
from pipeline import access_identity, cases, config


def test_failed_bootstrap_does_not_leave_enabled_marker(tmp_path, monkeypatch):
    case_root = tmp_path / "cases"
    case_root.mkdir()
    for module in (server, cases, config):
        monkeypatch.setattr(module, "CASES_DIR", case_root)
    monkeypatch.setenv("WINDCATCHER_IDENTITY_DIR", str(tmp_path / "identity"))
    monkeypatch.setenv("WINDCATCHER_MULTIUSER", "1")
    monkeypatch.setenv("WINDCATCHER_DEPLOYMENT", "cloud")
    monkeypatch.setenv("WINDCATCHER_CLOUD_USERNAME", "bootstrap-test")
    monkeypatch.setenv("WINDCATCHER_CLOUD_PASSWORD", "test-admin-password-very-long")
    monkeypatch.setenv("WINDCATCHER_CLOUD_SESSION_SECRET", "test-session-secret-long-enough-32-chars")

    def fail_bootstrap(*_args, **_kwargs):
        raise OSError("injected bootstrap failure")

    monkeypatch.setattr(access_identity.IdentityStore, "bootstrap_admin", fail_bootstrap)
    with pytest.raises(OSError, match="injected bootstrap failure"):
        cloud.create_app()
    assert not (case_root / ".multiuser-identity-enabled").exists()
    assert (tmp_path / "identity" / "identity.sqlite3").is_file()

    monkeypatch.setenv("WINDCATCHER_MULTIUSER", "0")
    with pytest.raises(RuntimeError, match="拒绝回退"):
        cloud.create_app()
