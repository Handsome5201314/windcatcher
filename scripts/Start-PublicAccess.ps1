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

function Set-HarnessEnvironment([hashtable]$Settings, [string]$MultiuserMode) {
    # Never inherit another local program's Harness settings or DeepSeek credential.
    # Only this project's .env can enable the pilot.
    foreach ($name in @([Environment]::GetEnvironmentVariables('Process').Keys)) {
        if ($name -match '^(WINDCATCHER_HARNESS_|DEEPSEEK_)') {
            [Environment]::SetEnvironmentVariable($name, $null, 'Process')
        }
    }

    $enabled = $Settings['WINDCATCHER_HARNESS_ADVICE']
    $actions = $Settings['WINDCATCHER_HARNESS_ACTIONS']
    if ($actions -and $actions -notin @('0', '1')) { throw 'WINDCATCHER_HARNESS_ACTIONS must be 0 or 1.' }
    if ($actions -eq '1' -and $enabled -ne '1') { throw 'Harness actions require the advice pilot.' }
    if (-not $enabled -or $enabled -eq '0') { return }
    if ($enabled -ne '1') { throw 'WINDCATCHER_HARNESS_ADVICE must be 0 or 1.' }
    if ($MultiuserMode -ne '1') { throw 'Harness advice requires personal accounts.' }

    $image = $Settings['WINDCATCHER_HARNESS_IMAGE']
    $key = $Settings['DEEPSEEK_API_KEY']
    $model = $Settings['WINDCATCHER_HARNESS_MODEL']
    $baseUrl = $Settings['WINDCATCHER_HARNESS_BASE_URL']
    $networkId = $Settings['WINDCATCHER_HARNESS_EGRESS_NETWORK_ID']
    $proxyImage = $Settings['WINDCATCHER_HARNESS_EGRESS_PROXY_IMAGE']
    if ($image -cnotmatch '^sha256:[0-9a-f]{64}$') { throw 'Harness requires a pinned worker image ID.' }
    if (-not $key -or $key.Length -gt 512 -or $key -match '[^\x21-\x7e]') {
        throw 'Harness requires a dedicated DEEPSEEK_API_KEY in the project .env.'
    }
    if ($model -cne 'gpt-6.1-sol') {
        throw 'Invalid WINDCATCHER_HARNESS_MODEL.'
    }
    if ($baseUrl -cne 'https://tongyimohe.aitoken.credit/v1') {
        throw 'Invalid WINDCATCHER_HARNESS_BASE_URL.'
    }
    if ($networkId -or $proxyImage) {
        if ($networkId -cnotmatch '^[0-9a-f]{64}$' -or $proxyImage -cnotmatch '^sha256:[0-9a-f]{64}$') {
            throw 'Harness egress network ID and pinned proxy image ID must be configured together.'
        }
    }

    [Environment]::SetEnvironmentVariable('WINDCATCHER_HARNESS_ADVICE', '1', 'Process')
    if ($actions -eq '1') { [Environment]::SetEnvironmentVariable('WINDCATCHER_HARNESS_ACTIONS', '1', 'Process') }
    [Environment]::SetEnvironmentVariable('WINDCATCHER_HARNESS_IMAGE', $image, 'Process')
    [Environment]::SetEnvironmentVariable('DEEPSEEK_API_KEY', $key, 'Process')
    if ($model) { [Environment]::SetEnvironmentVariable('WINDCATCHER_HARNESS_MODEL', $model, 'Process') }
    if ($baseUrl) { [Environment]::SetEnvironmentVariable('WINDCATCHER_HARNESS_BASE_URL', $baseUrl, 'Process') }
    if ($networkId) {
        [Environment]::SetEnvironmentVariable('WINDCATCHER_HARNESS_EGRESS_NETWORK_ID', $networkId, 'Process')
        [Environment]::SetEnvironmentVariable('WINDCATCHER_HARNESS_EGRESS_PROXY_IMAGE', $proxyImage, 'Process')
    }
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

function Test-PublicHttp([string]$Url, [string]$HostName, [string]$ServerIp) {
    # The local DNS proxy may return a 198.18/15 fake IP for this host.
    # Pin only the network destination; curl still verifies the HTTPS name.
    try {
        $resolve = $HostName + ':443:' + $ServerIp
        $result = & curl.exe --noproxy '*' --silent --show-error --max-time 12 --resolve $resolve --output NUL --write-out '%{http_code} %{ssl_verify_result}' $Url 2>&1
        $exitCode = $LASTEXITCODE
        $summary = ($result | Out-String).Trim()
        if ($exitCode -ne 0 -or $summary -notmatch '^(\d{3}) 0$') {
            return [pscustomobject]@{ Healthy = $false; StatusCode = $null; Error = "HTTPS probe failed (exit $exitCode)" }
        }
        $statusCode = [int]$matches[1]
        return [pscustomobject]@{ Healthy = ($statusCode -ge 200 -and $statusCode -lt 400); StatusCode = $statusCode; Error = $null }
    } catch {
        return [pscustomobject]@{ Healthy = $false; StatusCode = $null; Error = "HTTPS probe error: $($_.Exception.GetType().Name)" }
    }
}

function Stop-OwnedServer([int]$Port) {
    $listenerPid = Get-ListeningProcess $Port
    if (-not $listenerPid) { return }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$listenerPid"
    if ($process -and $process.CommandLine.Contains($python) -and $process.CommandLine -match 'uvicorn.*(server:app|cloud:create_app)') {
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
    foreach ($name in @('WINDCATCHER_PUBLIC_USERNAME', 'WINDCATCHER_PUBLIC_PASSWORD', 'WINDCATCHER_PUBLIC_SESSION_SECRET', 'WINDCATCHER_MCP_TOKEN')) {
        if (-not $public[$name]) { throw "Missing public access configuration: $name" }
        [Environment]::SetEnvironmentVariable($name, $public[$name], 'Process')
    }
    if ($public['WINDCATCHER_PUBLIC_PASSWORD'].Length -lt 16 -or $public['WINDCATCHER_PUBLIC_SESSION_SECRET'].Length -lt 32) {
        throw 'Public password must have at least 16 characters and session secret at least 32.'
    }
    if ($public['WINDCATCHER_MCP_TOKEN'] -notmatch '^[A-Za-z0-9_-]{43,128}$') {
        throw 'Local MCP token must be a 43-128 character base64url value.'
    }
    [Environment]::SetEnvironmentVariable('WINDCATCHER_MCP_TOKEN', $public['WINDCATCHER_MCP_TOKEN'], 'Process')
    [Environment]::SetEnvironmentVariable('WINDCATCHER_DEPLOYMENT', 'public', 'Process')
    $multiuserMode = $public['WINDCATCHER_MULTIUSER']
    if ($multiuserMode -and $multiuserMode -notin @('0', '1')) { throw 'WINDCATCHER_MULTIUSER must be 0 or 1.' }
    if ($multiuserMode -eq '1' -and -not $public['WINDCATCHER_IDENTITY_DIR']) {
        throw 'Personal accounts require an explicit WINDCATCHER_IDENTITY_DIR.'
    }
    foreach ($name in @('WINDCATCHER_MULTIUSER', 'WINDCATCHER_IDENTITY_DIR', 'WINDCATCHER_PREFERENCES_DIR', 'WINDCATCHER_RECIPES_DIR', 'WINDCATCHER_MONITOR_DIR', 'WINDCATCHER_LEARNING_DIR', 'WINDCATCHER_AUTOMATION_DIR', 'WINDCATCHER_SHARE_DIR', 'WINDCATCHER_AI_ALLOWED_HOSTS')) {
        [Environment]::SetEnvironmentVariable($name, $public[$name], 'Process')
    }
    Set-HarnessEnvironment $public $multiuserMode
    foreach ($name in @('WINDCATCHER_CLOUD_USERNAME', 'WINDCATCHER_CLOUD_PASSWORD', 'WINDCATCHER_CLOUD_SESSION_SECRET', 'WINDCATCHER_CLOUD_PUBLIC_ORIGIN')) {
        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
    $origin = $public['WINDCATCHER_PUBLIC_URL'].TrimEnd('/')
    if (-not $origin.StartsWith('https://')) { throw 'Public origin must be HTTPS.' }
    [Environment]::SetEnvironmentVariable('WINDCATCHER_PUBLIC_URL', $origin, 'Process')
    $key = $infra['ALIBABA_SSH_KEY']
    if (-not (Test-Path -LiteralPath $key)) { throw 'Configured SSH key does not exist.' }
    $localPort = [int]$public['WINDCATCHER_LOCAL_PORT']
    $remotePort = [int]$public['WINDCATCHER_TUNNEL_PORT']
    $target = "$($infra['ALIBABA_SSH_USER'])@$($infra['ALIBABA_SERVER_IP'])"
    $python = Join-Path $appRoot '.venv\Scripts\python.exe'
    $ssh = (Get-Command ssh.exe).Source
    if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) { throw 'curl.exe is required for verified public health checks.' }
    $publicHost = ([Uri]$origin).Host
    $publicHealthUrl = "$origin/static/patient-view.js"
    $serverIp = $infra['ALIBABA_SERVER_IP']
    $parsedIp = $null
    if (-not $serverIp -or -not [Net.IPAddress]::TryParse($serverIp, [ref]$parsedIp)) {
        throw 'Configured public server IP is invalid.'
    }
    if (Test-Path -LiteralPath $stopPath) { exit 0 }
    $forward = "127.0.0.1:${remotePort}:127.0.0.1:${localPort}"
    foreach ($oldTunnel in Get-CimInstance Win32_Process -Filter "Name='ssh.exe'") {
        if ($oldTunnel.CommandLine -and $oldTunnel.CommandLine.Contains($forward) -and $oldTunnel.CommandLine.Contains($target)) {
            Stop-Process -Id $oldTunnel.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Event "Closed orphaned project tunnel process $($oldTunnel.ProcessId)."
        }
    }
    Write-Event 'Supervisor started.'
    $lastPublicCheck = [DateTime]::MinValue
    $failureCount = 0
    $restartAfter = [DateTime]::MinValue
    $tunnelRetryAt = [DateTime]::MinValue
    $tunnelRetrySeconds = 5
    $localFailures = 0
    while (-not (Test-Path -LiteralPath $stopPath)) {
        $serverPid = Get-ListeningProcess $localPort
        $localResult = if ($serverPid) { Test-Http "http://127.0.0.1:$localPort/healthz" } else { [pscustomobject]@{ Healthy = $false; StatusCode = $null; Error = 'not listening' } }
        $localHealthy = [bool]$localResult.Healthy
        if ($localHealthy) { $localFailures = 0 } else { $localFailures++ }
        if (-not $localHealthy -and (-not $serverPid -or $localFailures -ge 3) -and (Get-Date) -ge $restartAfter) {
            Write-Event "Local health failed: HTTP $($localResult.StatusCode); $($localResult.Error)."
            if ($serverPid) { Stop-OwnedServer $localPort }
            Start-Process -FilePath $python -ArgumentList @('-m', 'uvicorn', 'cloud:create_app', '--factory', '--host', '127.0.0.1', '--port', $localPort, '--proxy-headers', '--forwarded-allow-ips', '127.0.0.1', '--no-access-log') -WorkingDirectory $appRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtime 'server.stdout.log') -RedirectStandardError (Join-Path $runtime 'server.stderr.log') | Out-Null
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
            $publicResult = Test-PublicHttp $publicHealthUrl $publicHost $serverIp
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
