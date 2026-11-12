#!/usr/bin/env bash
set -euo pipefail

# Run only inside the disposable NVHPC 24.9 CUDA 12.6 test container.
test -r /src/stdpar-smoke.cpp
test -r /src/Build-CuptiKernelProbe.sh

out=/out
bash /src/Build-CuptiKernelProbe.sh "$out" > "$out/cupti-build.log"

nvc++ -std=c++17 -stdpar=gpu -gpu=cc86,mem:managed -O2 \
    /src/stdpar-smoke.cpp -o "$out/stdpar-smoke"

CUDA_INJECTION64_PATH="$out/libwindcatcher_gpu_kernel_count.so" \
    "$out/stdpar-smoke" > "$out/stdpar-positive.log" 2>&1
cat "$out/stdpar-positive.log"
grep -qx 'stdpar_total=8000000' "$out/stdpar-positive.log"
count=$(sed -n 's/^WINDCATCHER_GPU_DEVICE_KERNELS=//p' \
    "$out/stdpar-positive.log")
if [[ ! "$count" =~ ^[1-9][0-9]*$ ]]; then
    echo "Expected exactly one positive device-kernel count marker" >&2
    exit 4
fi

cat > "$out/no-kernel.cu" <<'CPP'
#include <cuda_runtime.h>
int main() {
    int count = 0;
    return cudaGetDeviceCount(&count) == cudaSuccess && count > 0 ? 0 : 1;
}
CPP
nvcc "$out/no-kernel.cu" -o "$out/no-kernel"
CUDA_INJECTION64_PATH="$out/libwindcatcher_gpu_kernel_count.so" \
    "$out/no-kernel" > "$out/no-kernel-negative.log" 2>&1
cat "$out/no-kernel-negative.log"
if grep -q '^WINDCATCHER_GPU_DEVICE_KERNELS=' \
    "$out/no-kernel-negative.log"; then
    echo "False positive: marker appeared without a CUDA kernel" >&2
    exit 5
fi
grep -qx 'WINDCATCHER_GPU_PROBE_UNVERIFIED=kernels:0,dropped:0' \
    "$out/no-kernel-negative.log"

echo 'CUPTI injection positive and negative smoke checks passed'
