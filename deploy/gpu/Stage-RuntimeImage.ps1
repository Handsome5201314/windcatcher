param(
    [string]$Volume = "windcatcher-openfoam-gpu-poc-20261111",
    [string]$Image = "windcatcher/openfoam-gpu:2606-cc86-candidate"
)

$ErrorActionPreference = "Stop"
$baseImage = "nvcr.io/nvidia/nvhpc:24.9-devel-cuda_multi-ubuntu22.04"
$stage = "windcatcher-gpu-stage-$([guid]::NewGuid().ToString("N").Substring(0, 12))"
$wrapper = Join-Path $PSScriptRoot "bash"

function Invoke-Docker([string[]]$DockerArgs) {
    & docker @DockerArgs
    if ($LASTEXITCODE -ne 0) {
        throw "docker $($DockerArgs[0]) failed (exit $LASTEXITCODE)"
    }
}

if (-not (Test-Path -LiteralPath $wrapper)) { throw "Missing GPU wrapper: $wrapper" }
Invoke-Docker -DockerArgs @("volume", "inspect", $Volume) | Out-Null
Invoke-Docker -DockerArgs @("image", "inspect", $baseImage) | Out-Null

try {
    Invoke-Docker -DockerArgs @(
        "run", "-d", "--name", $stage,
        "--mount", "type=volume,source=$Volume,target=/source,readonly",
        "--entrypoint", "sleep", $baseImage, "infinity"
    )
    $copyCommand = (
        "set -e; test -x /source/src/platforms/linux64Nvidia-gpuDPInt32Opt/bin/simpleFoam; " +
        "test -f /source/src/platforms/linux64Nvidia-gpuDPInt32Opt/lib/libfieldFunctionObjects.so; " +
        "mkdir -p /work/src/platforms/linux64Nvidia-gpuDPInt32Opt /work/ThirdParty-common /openfoam; " +
        "cp -a /source/src/etc /source/src/bin /source/src/META-INFO /work/src/; " +
        "cp -a /source/src/platforms/linux64Nvidia-gpuDPInt32Opt/bin " +
        "/source/src/platforms/linux64Nvidia-gpuDPInt32Opt/lib " +
        "/work/src/platforms/linux64Nvidia-gpuDPInt32Opt/; " +
        "cp -a /source/ThirdParty-common/etc /source/ThirdParty-common/platforms /work/ThirdParty-common/; " +
        "test -r /work/src/etc/bashrc; " +
        "test -x /work/src/platforms/linux64Nvidia-gpuDPInt32Opt/bin/simpleFoam"
    )
    Invoke-Docker -DockerArgs @("exec", $stage, "bash", "-lc", $copyCommand)
    Invoke-Docker -DockerArgs @("exec", $stage, "mkdir", "-p", "/src")
    Invoke-Docker -DockerArgs @("cp", (Join-Path $PSScriptRoot "cupti-kernel-count.cpp"), "${stage}:/src/cupti-kernel-count.cpp")
    Invoke-Docker -DockerArgs @("cp", (Join-Path $PSScriptRoot "Build-CuptiKernelProbe.sh"), "${stage}:/src/Build-CuptiKernelProbe.sh")
    $probeCommand = (
        "set -e; bash /src/Build-CuptiKernelProbe.sh /openfoam; " +
        "test -s /openfoam/libwindcatcher_gpu_kernel_count.so; " +
        "ldd /openfoam/libwindcatcher_gpu_kernel_count.so | tee /openfoam/cupti-probe-ldd.txt; " +
        "! grep -q 'not found' /openfoam/cupti-probe-ldd.txt; " +
        "grep -q 'libcupti.so.12' /openfoam/cupti-probe-ldd.txt"
    )
    Invoke-Docker -DockerArgs @("exec", $stage, "bash", "-lc", $probeCommand)
    Invoke-Docker -DockerArgs @("cp", $wrapper, "${stage}:/openfoam/bash")
    Invoke-Docker -DockerArgs @("exec", $stage, "chmod", "+x", "/openfoam/bash")
    Invoke-Docker -DockerArgs @("commit", $stage, $Image)
    $imageCheck = (
        "test -r /work/src/etc/bashrc; " +
        "test -x /work/src/platforms/linux64Nvidia-gpuDPInt32Opt/bin/simpleFoam; " +
        "test -s /openfoam/libwindcatcher_gpu_kernel_count.so"
    )
    Invoke-Docker -DockerArgs @("run", "--rm", "--entrypoint", "bash", $Image, "-lc", $imageCheck)
} finally {
    & docker rm -f $stage | Out-Null
}

# This candidate deliberately has no verified-offload or CFD-smoke labels.
# Keep it unavailable to the workbench until a synthetic GPU solve is audited.
Invoke-Docker -DockerArgs @(
    "run", "--rm", "--entrypoint", "/openfoam/bash",
    $Image, "-c",
    'set -e; command -v simpleFoam; test "$WM_COMPILER" = Nvidia-gpu; ! ldd "$(command -v simpleFoam)" | grep -q "not found"; test -s /openfoam/libwindcatcher_gpu_kernel_count.so; ldd /openfoam/libwindcatcher_gpu_kernel_count.so | tee /tmp/cupti-probe-ldd.txt; ! grep -q "not found" /tmp/cupti-probe-ldd.txt; grep -q "libcupti.so.12" /tmp/cupti-probe-ldd.txt'
)
Write-Output "Unverified GPU candidate staged: $Image"
