"""Validate and deploy the single-login Caddy gateway without exposing secrets."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if match:
            values[match[1]] = match[2].strip().strip("\"'")
    return values


def main() -> None:
    public = read_env(ROOT / ".env")
    infra = read_env(Path(public["WINDCATCHER_INFRA_ENV"]))
    host = urlsplit(public["WINDCATCHER_PUBLIC_URL"]).hostname
    source = ROOT / "deploy" / "public-gateway.caddy"
    config = source.read_text(encoding="utf-8")
    if not host or f"{host} {{" not in config or "basicauth" in config:
        raise RuntimeError("The public gateway template has an unexpected host or Basic Auth rule")
    target = f"{infra['ALIBABA_SSH_USER']}@{infra['ALIBABA_SERVER_IP']}"
    options = ["-i", infra["ALIBABA_SSH_KEY"], "-o", "BatchMode=yes",
               "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15"]
    candidate = f"/tmp/windcatcher-gateway-{os.getpid()}.caddy"
    subprocess.run(["scp", *options, str(source), f"{target}:{candidate}"],
                   capture_output=True, check=True, timeout=45)
    script = r"""set -euo pipefail
candidate="$1"
active=/etc/caddy/windcatcher.caddy
trap 'rm -f "$candidate"' EXIT
test -f "$candidate"
test -f "$active"
grep -Fq 'windcatcher.tongyimoheai.top {' "$active"
grep -Fq 'reverse_proxy 127.0.0.1:18765' "$active"
if cmp -s "$candidate" "$active"; then echo 'Gateway already current.'; exit 0; fi
backup="$active.before-single-login-$(date -u +%Y%m%dT%H%M%SZ)"
cp -p "$active" "$backup"
install -o root -g caddy -m 640 "$candidate" "$active"
if ! caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
    cp -p "$backup" "$active"
    echo 'Caddy validation failed; restored previous gateway.' >&2
    exit 1
fi
if ! systemctl reload caddy || ! systemctl is-active --quiet caddy; then
    cp -p "$backup" "$active"
    systemctl reload caddy || true
    echo 'Caddy reload failed; restored previous gateway.' >&2
    exit 1
fi
echo 'Single-login gateway active; previous config backed up.'
"""
    result = subprocess.run(["ssh", *options, target, "bash -s -- " + candidate],
                            input=script.encode(), capture_output=True, check=True, timeout=45)
    print(result.stdout.decode().strip())


if __name__ == "__main__":
    main()
