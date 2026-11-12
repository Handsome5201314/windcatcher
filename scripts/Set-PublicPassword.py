"""Reload the local public account after changing ignored .env values.

The public Caddy gateway no longer has a Basic Auth password to rotate.
"""
from pathlib import Path
import re
import subprocess

APP_ROOT = Path(__file__).resolve().parent.parent


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if match:
            values[match[1]] = match[2].strip().strip("\"'")
    return values


def main() -> None:
    public = read_env(APP_ROOT / ".env")
    if not public.get("WINDCATCHER_PUBLIC_USERNAME"):
        raise ValueError("WINDCATCHER_PUBLIC_USERNAME is required.")
    if len(public.get("WINDCATCHER_PUBLIC_PASSWORD", "")) < 16:
        raise ValueError("Use a public access password of at least 16 characters.")
    if len(public.get("WINDCATCHER_PUBLIC_SESSION_SECRET", "")) < 32:
        raise ValueError("Use a public session secret of at least 32 characters.")
    subprocess.run([
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        str(APP_ROOT / "scripts" / "Restart-PublicAccess.ps1"),
    ], check=True, timeout=120)
    print("Public application restarted with the current .env; Caddy credentials were not changed.")


if __name__ == "__main__":
    main()