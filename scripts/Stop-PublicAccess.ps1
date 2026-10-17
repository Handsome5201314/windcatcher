$ErrorActionPreference = 'Stop'
$runtime = Join-Path (Split-Path $PSScriptRoot -Parent) '.local-public'
$statePath = Join-Path $runtime 'state.json'
function Close-ProjectTunnels {
    $public = @{}
    foreach ($line in Get-Content -LiteralPath (Join-Path (Split-Path $PSScriptRoot -Parent) '.env')) {
        if ($line -match '^\s*(WINDCATCHER_INFRA_ENV|WINDCATCHER_LOCAL_PORT|WINDCATCHER_TUNNEL_PORT)\s*=\s*(.*)$') {
            $public[$matches[1]] = $matches[2].Trim().Trim([char]34).Trim([char]39)
        }
    }
    $infra = @{}
    foreach ($line in Get-Content -LiteralPath $public['WINDCATCHER_INFRA_ENV']) {
        if ($line -match '^\s*(ALIBABA_SSH_USER|ALIBABA_SERVER_IP)\s*=\s*(.*)$') {
            $infra[$matches[1]] = $matches[2].Trim().Trim([char]34).Trim([char]39)
        }
    }
    $forward = "127.0.0.1:$($public['WINDCATCHER_TUNNEL_PORT']):127.0.0.1:$($public['WINDCATCHER_LOCAL_PORT'])"
    $target = "$($infra['ALIBABA_SSH_USER'])@$($infra['ALIBABA_SERVER_IP'])"
    foreach ($process in Get-CimInstance Win32_Process -Filter "Name='ssh.exe'") {
        if ($process.CommandLine -and $process.CommandLine.Contains($forward) -and $process.CommandLine.Contains($target)) {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
        }
    }
}
$task = Get-ScheduledTask -TaskName 'Windcatcher Public Access' -ErrorAction SilentlyContinue
if ($task) {
    Disable-ScheduledTask -TaskName $task.TaskName | Out-Null
}
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
New-Item -ItemType File -Path (Join-Path $runtime 'stop') -Force | Out-Null
$runScript = Join-Path $PSScriptRoot 'Start-PublicAccess.ps1'
for ($attempt = 0; $attempt -lt 45; $attempt++) {
    $supervisors = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($runScript) -and $_.CommandLine -match '\s-Run(?:\s|$)' })
    if (-not $supervisors.Count) {
        Close-ProjectTunnels
        Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
        Write-Output 'Public tunnel stopped. Local application remains available.'
        exit 0
    }
    Start-Sleep -Seconds 1
}
throw 'Supervisor did not stop in 45 seconds. Task is disabled; check .local-public/supervisor.log.'
