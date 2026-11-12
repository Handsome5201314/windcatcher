"""Storage preflight must not disclose secrets or touch patient content."""

import json
import subprocess
import sys
from pathlib import Path

from scripts.check_local_release import check_local_release

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_local_release.py"


def test_ready_local_storage_does_not_print_credentials_or_paths(tmp_path):
    root = tmp_path / "workbench"
    root.mkdir()
    for name in ("cases", "static", ".local-monitor", ".local-learning",
                 ".local-shares", ".local-automation"):
        (root / name).mkdir()
    identity = tmp_path / "private-identity"
    identity.mkdir()
    (root / "cases" / "patient-bytes.dcm").write_bytes(b"CT_CONTENT_MUST_STAY_PRIVATE")
    secret = "SENSITIVE_PASSWORD_MUST_NOT_APPEAR"
    lines = ["WINDCATCHER_MULTIUSER=1", f"WINDCATCHER_IDENTITY_DIR='{identity}'",
             f"WINDCATCHER_PUBLIC_PASSWORD={secret}"]
    (root / ".env").write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    before = (root / ".env").read_bytes()
    run = subprocess.run([sys.executable, str(SCRIPT), "--app-root", str(root)],
                         capture_output=True, timeout=5, check=False)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["status"] == "ready_for_further_checks"
    assert {entry["name"] for entry in result["checks"]} == {
        "cases", "identity", "monitor", "learning", "preferences",
        "recipes", "shares", "automation"}
    assert secret.encode() not in run.stdout + run.stderr
    assert str(identity).encode() not in run.stdout + run.stderr
    assert b"CT_CONTENT_MUST_STAY_PRIVATE" not in run.stdout + run.stderr
    assert (root / ".env").read_bytes() == before
    assert (root / "cases" / "patient-bytes.dcm").read_bytes() == b"CT_CONTENT_MUST_STAY_PRIVATE"


def test_static_storage_override_is_rejected_even_when_directory_exists(tmp_path):
    root = tmp_path / "workbench"
    root.mkdir()
    for name in ("cases", "static", ".local-monitor", ".local-learning",
                 ".local-shares", ".local-automation"):
        (root / name).mkdir()
    (root / "static" / "identity").mkdir()
    (root / ".env").write_text(chr(10).join([
        "WINDCATCHER_MULTIUSER=1", "WINDCATCHER_IDENTITY_DIR=static/identity", ""]),
        encoding="utf-8")
    report = check_local_release(root)
    assert report["status"] == "not_ready"
    assert "identity_exposed_static_path" in report["issues"]
    assert "preferences_exposed_static_path" in report["issues"]
    assert "recipes_exposed_static_path" in report["issues"]


def test_missing_data_and_disabled_mode_are_reported_without_creating_directories(tmp_path):
    root = tmp_path / "workbench"
    root.mkdir()
    (root / ".env").write_text("WINDCATCHER_MULTIUSER=0" + chr(10), encoding="utf-8")
    report = check_local_release(root)
    assert report["status"] == "not_ready"
    assert "personal_mode_disabled" in report["issues"]
    assert "identity_dir_unset" in report["issues"]
    assert "cases_missing" in report["issues"]
    assert not (root / "cases").exists()
    assert not (root / ".local-identity").exists()
