#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 1 ]]; then
    echo "Usage: Build-CuptiKernelProbe.sh OUTPUT_DIRECTORY" >&2
    exit 2
fi

cuda=/opt/nvidia/hpc_sdk/Linux_x86_64/24.9/cuda/12.6
source_file=/src/cupti-kernel-count.cpp
output_directory="$1"

test -r "$source_file"
test -r "$cuda/extras/CUPTI/include/cupti.h"
mkdir -p "$output_directory"

g++ -std=c++17 -O2 -fPIC -shared -Wall -Wextra -Werror \
    -I"$cuda/include" \
    -I"$cuda/extras/CUPTI/include" \
    "$source_file" \
    -L"$cuda/extras/CUPTI/lib64" -lcupti \
    -Wl,-rpath,"$cuda/extras/CUPTI/lib64" \
    -o "$output_directory/libwindcatcher_gpu_kernel_count.so"

test -s "$output_directory/libwindcatcher_gpu_kernel_count.so"
ldd "$output_directory/libwindcatcher_gpu_kernel_count.so" |
    tee "$output_directory/cupti-probe-ldd.txt"
if grep -q 'not found' "$output_directory/cupti-probe-ldd.txt"; then
    echo "CUPTI probe has unresolved shared libraries" >&2
    exit 3
fi
