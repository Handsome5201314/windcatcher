#!/usr/bin/env bash
# OpenFOAM's environment script uses nonzero probe results internally; sourcing
# it under errexit aborts before Uvicorn can start (even when its final status is 0).
printf 'Windcatcher: loading OpenFOAM 11 environment\n'
source /opt/openfoam11/etc/bashrc
foam_status=$?
set -e
if [ "$foam_status" -ne 0 ]; then
    printf 'Windcatcher: OpenFOAM environment failed (%s)\n' "$foam_status" >&2
    exit "$foam_status"
fi
command -v simpleFoam blockMesh snappyHexMesh >/dev/null
export PATH="/opt/venv/bin:$PATH"
exec python -m uvicorn cloud:create_app --factory --host 0.0.0.0 --port 7860 --workers 1 --proxy-headers --forwarded-allow-ips='*'
