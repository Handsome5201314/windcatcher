#!/usr/bin/env bash

# Continue the isolated GPU build only after the core libraries have finished.
# Never start a second src/Allwmake while one is still running.
set -uo pipefail
trap 'printf "%s\n" "$?" > /work/openfoam-apps-build.exit' EXIT

while [[ ! -f /work/openfoam-src-build-j16.exit ]]; do
    sleep 10
done
if [[ "$(cat /work/openfoam-src-build-j16.exit)" != 0 ]]; then
    echo "GPU OpenFOAM libraries failed; skipping solver build" >&2
    exit 2
fi

source /work/src/etc/bashrc WM_COMPILER=Nvidia-gpu WM_MPLIB=SYSTEMOPENMPI
if [[ ! -v WM_COMPILER || "$WM_COMPILER" != Nvidia-gpu ]]; then
    echo "NVIDIA compiler profile unavailable" >&2
    exit 3
fi
set -e

wmake -j4 /work/src/applications/solvers/incompressible/simpleFoam
export FOAM_USER_APPBIN=/work/tools/bin
mkdir -p "$FOAM_USER_APPBIN"
wmake -j4 /work/src/applications/test/std_execution

platform=/work/src/platforms/linux64Nvidia-gpuDPInt32Opt
test -x "$platform/bin/simpleFoam"
test -x "$FOAM_USER_APPBIN/Test-std_execution"
test -f "$platform/lib/libfieldFunctionObjects.so"
if ldd "$platform/bin/simpleFoam" | grep -q 'not found'; then
    echo "simpleFoam has unresolved shared libraries" >&2
    exit 4
fi
echo "Targeted GPU applications built and linked"
