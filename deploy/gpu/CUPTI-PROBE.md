# CUPTI GPU kernel proof

This probe is for Windcatcher's experimental GPU OpenFOAM candidate only. It does not analyze patient data, and a positive kernel count is a necessary condition for device execution, not evidence of correct CFD results.

The implementation follows the injection entrypoint and activity-buffer lifecycle in NVIDIA HPC SDK 24.9's CUDA 12.6 samples/cupti_trace_injection. It enables only CUPTI_ACTIVITY_KIND_CONCURRENT_KERNEL. At process exit it flushes records and prints one parseable WINDCATCHER_GPU_DEVICE_KERNELS=N line only if N > 0, no records were dropped, and no CUPTI call failed. Otherwise it prints WINDCATCHER_GPU_PROBE_UNVERIFIED or WINDCATCHER_GPU_PROBE_ERROR, without a success marker. The caller must additionally require a zero solver exit code and the CFD numeric quality gates.

## Isolated test

Run from PowerShell with Docker Desktop and the RTX 3090 GPU available. The source is mounted read-only. No OpenFOAM build volume or patient data is mounted.

~~~powershell
$src = 'E:/AI新青年/鼻腔气流CFD比赛/开发/windcatcher/deploy/gpu'
$out = 'C:/Temp/windcatcher-cupti-poc-20261111'
New-Item -ItemType Directory -Force -Path $out | Out-Null
docker run --rm --gpus all -v "$($src):/src:ro" -v "$($out):/out" --entrypoint bash nvcr.io/nvidia/nvhpc:24.9-devel-cuda_multi-ubuntu22.04 /src/Test-CuptiKernelProbe.sh
~~~

Expected positive lines: stdpar_total=8000000 and WINDCATCHER_GPU_DEVICE_KERNELS=N where N is positive. Expected no-kernel line: WINDCATCHER_GPU_PROBE_UNVERIFIED=kernels:0,dropped:0, with no success marker. The script exits nonzero if either assertion fails.

## Candidate image packaging

The candidate staging script builds `libwindcatcher_gpu_kernel_count.so` in the same NVHPC 24.9/CUDA 12.6 base image and installs it at `/openfoam/libwindcatcher_gpu_kernel_count.so`. This is the path used by `pipeline/openfoam_runner.py`. Staging must verify that `ldd` resolves every dependency, including `libcupti.so.12`, before committing the isolated candidate. A slimmer runtime must copy its compatible CUPTI shared library and preserve the path used by the probe's rpath. Keep the normal CPU image unchanged.

For an isolated solver process, set `CUDA_INJECTION64_PATH=/openfoam/libwindcatcher_gpu_kernel_count.so`. Capture stdout/stderr, require process exit 0, then parse exactly one positive marker. Missing marker, zero, CUPTI error or dropped records means offload is unverified. Do not set the injection variable globally for unrelated workers.

The synthetic stdpar test proves the probe can observe a GPU kernel, but does not qualify the OpenFOAM candidate. The GPU label gate additionally requires Windcatcher's generated synthetic CFD case with its actual fvSolution.gpu, boundary layout and simpleFoam, followed by CPU/GPU numeric comparison. See docs/gpu-openfoam-build.md.
