"""Apply the password from the ignored project .env without putting it in argv."""

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
    if len(public["WINDCATCHER_PUBLIC_PASSWORD"]) < 16:
        raise ValueError("Use a public access password of at least 16 characters.")
    infra = read_env(Path(public["WINDCATCHER_INFRA_ENV"]))
    target = f"{infra['ALIBABA_SSH_USER']}@{infra['ALIBABA_SERVER_IP']}"
    options = ["-i", infra["ALIBABA_SSH_KEY"], "-o", "BatchMode=yes",
               "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15"]
    # Caddy requires a terminating LF. Binary stdin prevents Windows adding a CR.
    hashed = subprocess.run(
        ["ssh", *options, target, "caddy hash-password"],
        input=public["WINDCATCHER_PUBLIC_PASSWORD"].encode("utf-8") + b"\n",
        capture_output=True, check=True, timeout=30,
    ).stdout.decode().strip()
    if not re.fullmatch(r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}", hashed):
        raise RuntimeError("Caddy did not return a valid bcrypt password hash.")
    path = APP_ROOT / ".local-public" / "gateway.caddy"
    source = path.read_text(encoding="utf-8")
    username = public["WINDCATCHER_PUBLIC_USERNAME"]
    pattern = rf"(?m)^(\s*{re.escape(username)}\s+)\$2[aby]\$[^\s]+$"
    source, count = re.subn(pattern, lambda match: match[1] + hashed, source)
    if count != 1:
        raise RuntimeError("Expected exactly one configured public account.")
    path.write_text(source, encoding="utf-8")
    subprocess.run(["scp", *options, str(path), f"{target}:/tmp/windcatcher-password.caddy"],
                   check=True, timeout=30)
    script = """set -e
backup="/etc/caddy/windcatcher.caddy.before-password-$(date -u +%Y%m%dT%H%M%SZ)"
cp -p /etc/caddy/windcatcher.caddy "$backup"
install -o root -g caddy -m 640 /tmp/windcatcher-password.caddy /etc/caddy/windcatcher.caddy
if ! caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile; then
  cp -p "$backup" /etc/caddy/windcatcher.caddy
  exit 1
fi
if ! systemctl reload caddy; then
  cp -p "$backup" /etc/caddy/windcatcher.caddy
  systemctl reload caddy
  exit 1
fi
rm /tmp/windcatcher-password.caddy
systemctl is-active caddy
"""
    subprocess.run(["ssh", *options, target, "bash -s"],
                   input=script.encode("utf-8"), check=True, timeout=30)
    print("Public password applied; plaintext remains only in the local .env.")


if __name__ == "__main__":
    main()
