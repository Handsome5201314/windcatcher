param([switch]$Run)

$ErrorActionPreference = 'Stop'
$appRoot = Split-Path $PSScriptRoot -Parent
$runtime = Join-Path $appRoot '.local-public'
$statePath = Join-Path $runtime 'state.json'
$stopPath = Join-Path $runtime 'stop'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null

if (-not $Run) {
    $taskName = 'Windcatcher Public Access'
    Remove-Item -LiteralPath $stopPath -Force -ErrorAction SilentlyContinue
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) {
        Enable-ScheduledTask -TaskName $taskName | Out-Null
        Start-ScheduledTask -TaskName $taskName
        Write-Output "Started scheduled task: $taskName"
        exit 0
    }
    & (Join-Path $PSScriptRoot 'Install-PublicAccessTask.ps1')
    exit 0
}

function Read-DotEnv([string]$Path) {
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $values[$matches[1]] = $matches[2].Trim().Trim([char]34).Trim([char]39)
        }
    }
    return $values
}

function Write-Event([string]$Message) {
    try {
        Add-Content -LiteralPath (Join-Path $runtime 'supervisor.log') -Value "$(Get-Date -Format o) $Message"
    } catch {
        [Console]::Error.WriteLine("Supervisor log unavailable: $Message")
    }
}

function Write-State([hashtable]$State) {
    $tempPath = "$statePath.$PID.tmp"
    $backupPath = "$statePath.bak"
    $json = $State | ConvertTo-Json -Compress
    $lastError = $null
    for ($attempt = 0; $attempt -lt 5; $attempt++) {
        try {
            [IO.File]::WriteAllText($tempPath, $json, [Text.UTF8Encoding]::new($false))
            if (Test-Path -LiteralPath $statePath) {
                Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
                [IO.File]::Replace($tempPath, $statePath, $backupPath, $true)
                Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
            } else {
                Move-Item -LiteralPath $tempPath -Destination $statePath -Force
            }
            return
        } catch {
            $lastError = $_.Exception.Message
            Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
            if ($attempt -lt 4) { Start-Sleep -Milliseconds 100 }
        }
    }
    Write-Event "State update skipped after repeated file-lock conflicts: $lastError"
}

function Get-ListeningProcess([int]$Port) {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($listener) { return [int]$listener.OwningProcess }
    return $null
}

function Test-Http([string]$Url, [hashtable]$Headers = @{}) {
    try {
        $response = Invoke-WebRequest -Uri $Url -Headers $Headers -UseBasicParsing -TimeoutSec 12 -ErrorAction Stop
        return [pscustomobject]@{ Healthy = ([int]$response.StatusCode -ge 200 -and [int]$response.StatusCode -lt 400); StatusCode = [int]$response.StatusCode; Error = $null }
    } catch {
        $statusCode = $null
        if ($_.Exception.Response) {
            $statusCode = [int]$_.Exception.Response.StatusCode
        }
        return [pscustomobject]@{ Healthy = $false; StatusCode = $statusCode; Error = $_.Exception.Message }
    }
}

function Stop-OwnedServer([int]$Port) {
    $listenerPid = Get-ListeningProcess $Port
    if (-not $listenerPid) { return }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$listenerPid"
    if ($process -and $process.CommandLine.Contains($python) -and $process.CommandLine -match 'uvicorn.*server:app') {
        Stop-Process -Id $listenerPid -Force -ErrorAction SilentlyContinue
        Write-Event "Stopped unhealthy local application process $listenerPid."
    } else {
        throw "Port $Port is owned by another application; refusing to stop it."
    }
}

$mutex = [Threading.Mutex]::new($false, 'Local\WindcatcherPublicAccess')
try {
    if (-not $mutex.WaitOne(0)) { $mutex.Dispose(); exit 0 }
} catch [Threading.AbandonedMutexException] {
    # An abandoned mutex is acquired by this process after the former owner died.
    Write-Event 'Recovered the single-instance guard after an unexpected exit.'
}
$tunnel = $null
try {
    $public = Read-DotEnv (Join-Path $appRoot '.env')
    $infra = Read-DotEnv $public['WINDCATCHER_INFRA_ENV']
    $key = $infra['ALIBABA_SSH_KEY']
    if (-not (Test-Path -LiteralPath $key)) { throw 'Configured SSH key does not exist.' }
    $localPort = [int]$public['WINDCATCHER_LOCAL_PORT']
    $remotePort = [int]$public['WINDCATCHER_TUNNEL_PORT']
    $target = "$($infra['ALIBABA_SSH_USER'])@$($infra['ALIBABA_SERVER_IP'])"
    $python = Join-Path $appRoot '.venv\Scripts\python.exe'
    $ssh = (Get-Command ssh.exe).Source
    if (Test-Path -LiteralPath $stopPath) { exit 0 }
    $forward = "127.0.0.1:${remotePort}:127.0.0.1:${localPort}"
    foreach ($oldTunnel in Get-CimInstance Win32_Process -Filter "Name='ssh.exe'") {
        if ($oldTunnel.CommandLine -and $oldTunnel.CommandLine.Contains($forward) -and $oldTunnel.CommandLine.Contains($target)) {
            Stop-Process -Id $oldTunnel.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Event "Closed orphaned project tunnel process $($oldTunnel.ProcessId)."
        }
    }
    Write-Event 'Supervisor started.'
    $publicHeaders = @{}
    $publicCredential = "$($public['WINDCATCHER_PUBLIC_USERNAME']):$($public['WINDCATCHER_PUBLIC_PASSWORD'])"
    $publicHeaders['Authorization'] = "Basic $([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($publicCredential)))"
    $lastPublicCheck = [DateTime]::MinValue
    $failureCount = 0
    $restartAfter = [DateTime]::MinValue
    $tunnelRetryAt = [DateTime]::MinValue
    $tunnelRetrySeconds = 5
    $localFailures = 0
    while (-not (Test-Path -LiteralPath $stopPath)) {
        $serverPid = Get-ListeningProcess $localPort
        $localResult = if ($serverPid) { Test-Http "http://127.0.0.1:$localPort/" } else { [pscustomobject]@{ Healthy = $false; StatusCode = $null; Error = 'not listening' } }
        $localHealthy = [bool]$localResult.Healthy
        if ($localHealthy) { $localFailures = 0 } else { $localFailures++ }
        if (-not $localHealthy -and (-not $serverPid -or $localFailures -ge 3) -and (Get-Date) -ge $restartAfter) {
            Write-Event "Local health failed: HTTP $($localResult.StatusCode); $($localResult.Error)."
            if ($serverPid) { Stop-OwnedServer $localPort }
            Start-Process -FilePath $python -ArgumentList @('-m', 'uvicorn', 'server:app', '--host', '127.0.0.1', '--port', $localPort, '--proxy-headers', '--forwarded-allow-ips', '127.0.0.1') -WorkingDirectory $appRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtime 'server.stdout.log') -RedirectStandardError (Join-Path $runtime 'server.stderr.log') | Out-Null
            Write-Event 'Started local application.'
            $restartAfter = (Get-Date).AddSeconds(10)
            $localFailures = 0
        }
        if ($tunnel -and $tunnel.HasExited) {
            Write-Event "Tunnel exited with code $($tunnel.ExitCode); reconnecting."
            $tunnel = $null
            $tunnelRetryAt = (Get-Date).AddSeconds($tunnelRetrySeconds)
            $tunnelRetrySeconds = [Math]::Min(60, $tunnelRetrySeconds * 2)
        }
        if (-not $tunnel -and (Get-Date) -ge $tunnelRetryAt) {
            $arguments = @('-N', '-T', '-i', ('"' + $key + '"'), '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=15', '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3', '-R', $forward, $target)
            try {
                $tunnel = Start-Process -FilePath $ssh -ArgumentList $arguments -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtime 'tunnel.stdout.log') -RedirectStandardError (Join-Path $runtime 'tunnel.stderr.log') -PassThru
                Write-Event "Started SSH tunnel process $($tunnel.Id)."
            } catch {
                Write-Event "Tunnel start failed: $($_.Exception.Message)"
                $tunnelRetryAt = (Get-Date).AddSeconds($tunnelRetrySeconds)
                $tunnelRetrySeconds = [Math]::Min(60, $tunnelRetrySeconds * 2)
            }
        }
        if ((Get-Date) -ge $lastPublicCheck) {
            $publicResult = Test-Http $public['WINDCATCHER_PUBLIC_URL'] $publicHeaders
            $lastPublicCheck = (Get-Date).AddSeconds(30)
            if ($publicResult.Healthy) {
                $failureCount = 0
                $tunnelRetrySeconds = 5
            } elseif ($publicResult.StatusCode -in @(401, 403)) {
                $failureCount = 0
                Write-Event "Public health check rejected with HTTP $($publicResult.StatusCode); tunnel unchanged."
            } elseif ($publicResult.StatusCode -and $publicResult.StatusCode -notin @(502, 503, 504)) {
                $failureCount = 0
                Write-Event "Public health check returned HTTP $($publicResult.StatusCode); tunnel unchanged."
            } else {
                $failureCount++
                Write-Event "Public health check failed ($failureCount): HTTP $($publicResult.StatusCode); $($publicResult.Error)."
                if ($failureCount -ge 2 -and $tunnel -and -not $tunnel.HasExited) {
                    Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue
                    Write-Event 'Restarted tunnel after repeated public health failures.'
                    $tunnel = $null
                    $tunnelRetryAt = Get-Date
                    $failureCount = 0
                }
            }
        }
        $serverPid = Get-ListeningProcess $localPort
        $tunnelPid = if ($tunnel -and -not $tunnel.HasExited) { [int]$tunnel.Id } else { $null }
        Write-State @{
            supervisor_pid = $PID
            tunnel_pid = $tunnelPid
            server_pid = if ($serverPid) { [int]$serverPid } else { $null }
            public_url = $public['WINDCATCHER_PUBLIC_URL']
            local_healthy = [bool]$localHealthy
            local_failures = $localFailures
            tunnel_alive = [bool]$tunnelPid
            public_healthy = [bool]$publicResult.Healthy
            public_status = $publicResult.StatusCode
            public_check_due = $lastPublicCheck.ToUniversalTime().ToString('o')
            public_failures = $failureCount
            updated_at = (Get-Date).ToUniversalTime().ToString('o')
        }
        Start-Sleep -Seconds 5
    }
    Write-Event 'Stop requested; closing public tunnel. Local application remains available.'
} catch {
    Write-Event "Supervisor failed: $($_.Exception.Message)"
    throw
} finally {
    if ($tunnel -and -not $tunnel.HasExited) { Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue }
    Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
