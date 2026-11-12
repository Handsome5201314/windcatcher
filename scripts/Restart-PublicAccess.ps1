$ErrorActionPreference = 'Stop'
$appRoot = Split-Path $PSScriptRoot -Parent
$statePath = Join-Path $appRoot '.local-public\state.json'
if (-not (Test-Path -LiteralPath $statePath)) { throw 'Public supervisor state is missing; cannot identify the owned application.' }
$state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
$serverId = [int]$state.server_pid
if ($serverId -le 0) { throw 'Public application PID is missing; cannot restart safely.' }
$python = Join-Path $appRoot '.venv\Scripts\python.exe'
function Assert-OwnedServer([int]$ProcessId) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId"
    if ($process -and (-not $process.CommandLine -or -not $process.CommandLine.Contains($python) -or
            $process.CommandLine -notmatch 'uvicorn.*cloud:create_app')) {
        throw "Process $ProcessId is not the Windcatcher public application; refusing to stop it."
    }
    return $process
}
if (-not (Assert-OwnedServer $serverId)) { throw 'Public application exited before it could be identified.' }
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'Stop-PublicAccess.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Could not stop the public supervisor.' }
if (Assert-OwnedServer $serverId) { Stop-Process -Id $serverId -Force -ErrorAction Stop }
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'Start-PublicAccess.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Could not start the public supervisor.' }
Write-Output 'Public supervisor restarted; application will load the current .env.'
