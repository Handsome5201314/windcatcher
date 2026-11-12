#!/usr/bin/env bash
# Diagnostic only: relink the original library with one patched object.
# This does not replace a complete rebuild or qualify a release image.
set -eo pipefail

log=/work/openfoam-src-build-j16.log
original=/work/src/build/linux64Nvidia-gpuDPInt32Opt/src/finiteVolume/fields/fvPatchFields/basic/zeroGradient/zeroGradientFvPatchFields.o
patched=/exp/src/build/linux64Nvidia-gpuDPInt32Opt/src/finiteVolume/fields/fvPatchFields/basic/zeroGradient/zeroGradientFvPatchFields.o
solver=/work/src/platforms/linux64Nvidia-gpuDPInt32Opt/bin/simpleFoam
library=/work/src/platforms/linux64Nvidia-gpuDPInt32Opt/lib/libfiniteVolume.so

test -r "$log"
test -r "$original"
test -r "$patched"
test -x "$solver"
test -r /case/0/p
test -r /case/system/fvSolution
test -s "$library"
mkdir -p /case/logs

# Exact two lines from the immutable original build log, with only the
# zeroGradient object and output path changed.
sed -n '1814,1815p' "$log" > /tmp/fvpatch-link.sh
grep -Fq "$original" /tmp/fvpatch-link.sh
grep -Fq -- "-o $library" /tmp/fvpatch-link.sh
sed -i "s#$original#$patched#;s#-o $library#-o /tmp/libfiniteVolume.so#" /tmp/fvpatch-link.sh
grep -Fq "$patched" /tmp/fvpatch-link.sh

source /work/src/etc/bashrc WM_COMPILER=Nvidia-gpu WM_MPLIB=SYSTEMOPENMPI || true
test "$WM_COMPILER" = Nvidia-gpu
set -u
cd /work/src/src/finiteVolume
bash /tmp/fvpatch-link.sh > /case/logs/fvpatch-fast-link.log 2>&1
test -s /tmp/libfiniteVolume.so
export LD_LIBRARY_PATH="/tmp:$LD_LIBRARY_PATH"
ldd "$solver" > /case/logs/fvpatch-fast-ldd.log
grep -Fq 'libfiniteVolume.so => /tmp/libfiniteVolume.so' /case/logs/fvpatch-fast-ldd.log
if grep -Fq 'not found' /case/logs/fvpatch-fast-ldd.log; then
    echo 'Unresolved library dependency' >&2
    exit 3
fi

ulimit -c 0
export CUDA_INJECTION64_PATH=/openfoam/libwindcatcher_gpu_kernel_count.so
set +e
timeout 180s "$solver" -case /case > /case/logs/fvpatch-fast-solver.log 2>&1
rc=$?
set -e
printf '%s\n' "$rc" > /case/logs/fvpatch-fast-solver.exit
tail -n 60 /case/logs/fvpatch-fast-solver.log
exit "$rc"
