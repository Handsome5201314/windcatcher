"""Read-only Windows public-workbench storage preflight (never prints .env values)."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
_ENTRY = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_SETTINGS = frozenset({
    "WINDCATCHER_MULTIUSER", "WINDCATCHER_IDENTITY_DIR",
    "WINDCATCHER_MONITOR_DIR", "WINDCATCHER_LEARNING_DIR",
    "WINDCATCHER_PREFERENCES_DIR", "WINDCATCHER_RECIPES_DIR",
    "WINDCATCHER_AUTOMATION_DIR", "WINDCATCHER_SHARE_DIR",
})


def _settings(env_file: Path) -> dict[str, str]:
    # Mirror the supervisor's simple dotenv parsing without echoing any line.
    values: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        match = _ENTRY.match(line)
        if match and match.group(1) in _SETTINGS:
            values[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    return values


def _effective_path(root: Path, raw: str | Path) -> Path:
    path = Path(raw)
    return (path if path.is_absolute() else root / path).resolve(strict=False)


def check_local_release(root: Path, env_file: Path | None = None) -> dict:
    """Check path metadata only, without opening any case or database file."""
    root = Path(root).resolve(strict=False)
    env_file = Path(env_file) if env_file is not None else root / ".env"
    issues: list[str] = []
    if not env_file.is_file():
        settings = {}
        issues.append("env_missing")
    else:
        try:
            settings = _settings(env_file)
        except (OSError, UnicodeError):
            settings = {}
            issues.append("env_unreadable")
    mode = settings.get("WINDCATCHER_MULTIUSER", "0") or "0"
    if mode != "1":
        issues.append("personal_mode_disabled" if mode == "0" else "personal_mode_invalid")
    if not settings.get("WINDCATCHER_IDENTITY_DIR"):
        issues.append("identity_dir_unset")
    identity = settings.get("WINDCATCHER_IDENTITY_DIR") or root / ".local-identity"
    preferences = settings.get("WINDCATCHER_PREFERENCES_DIR") or identity
    recipes = settings.get("WINDCATCHER_RECIPES_DIR") or preferences
    storage = {
        "cases": (root / "cases", "fixed"),
        "identity": (identity, "configured" if settings.get("WINDCATCHER_IDENTITY_DIR") else "default"),
        "monitor": (settings.get("WINDCATCHER_MONITOR_DIR") or root / ".local-monitor",
                    "configured" if settings.get("WINDCATCHER_MONITOR_DIR") else "default"),
        "learning": (settings.get("WINDCATCHER_LEARNING_DIR") or root / ".local-learning",
                     "configured" if settings.get("WINDCATCHER_LEARNING_DIR") else "default"),
        "preferences": (preferences, "configured" if settings.get("WINDCATCHER_PREFERENCES_DIR") else "identity_fallback"),
        "recipes": (recipes, "configured" if settings.get("WINDCATCHER_RECIPES_DIR") else "preferences_fallback"),
        "shares": (settings.get("WINDCATCHER_SHARE_DIR") or root / ".local-shares",
                   "configured" if settings.get("WINDCATCHER_SHARE_DIR") else "default"),
        "automation": (settings.get("WINDCATCHER_AUTOMATION_DIR") or root / ".local-automation",
                       "configured" if settings.get("WINDCATCHER_AUTOMATION_DIR") else "default"),
    }
    static_root = _effective_path(root, root / "static")
    checks = []
    for name, (raw_path, source) in storage.items():
        path = _effective_path(root, raw_path)
        if path == static_root or static_root in path.parents:
            status = "exposed_static_path"
        elif path.is_dir():
            status = "present"
        elif path.exists():
            status = "not_directory"
        else:
            status = "missing"
        if status != "present":
            issues.append(f"{name}_{status}")
        # Names, source kinds, and status only: no absolute path or .env value.
        checks.append({"name": name, "source": source, "status": status})
    return {"status": "ready_for_further_checks" if not issues else "not_ready",
            "deployment": "windows_local_public_reverse_tunnel",
            "personal_mode": mode == "1", "checks": checks, "issues": issues}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", type=Path, default=APP_ROOT,
                        help="workbench root; defaults to this script's parent project")
    parser.add_argument("--env-file", type=Path,
                        help="dotenv file; defaults to <app-root>/.env")
    args = parser.parse_args()
    report = check_local_release(args.app_root, args.env_file)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "ready_for_further_checks" else 2


if __name__ == "__main__":
    raise SystemExit(main())
