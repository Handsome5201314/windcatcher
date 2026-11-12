param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^sha256:[0-9a-f]{64}$')]
    [string]$ImageId,

    [ValidateSet('api.deepseek.com', 'tongyimohe.aitoken.credit')]
    [string]$TargetHost = 'api.deepseek.com',

    [switch]$Persistent
)

# Explicit opt-in only. No credentials, cases, mounts, or host-published ports.
$ErrorActionPreference = 'Stop'
$restartPolicy = if ($Persistent) { 'unless-stopped' } else { 'no' }

function Invoke-Docker([string[]]$Arguments) {
    $result = & docker @Arguments 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { throw "Docker $($Arguments[0]) failed" }
    return $result.Trim()
}

$image = @(Invoke-Docker -Arguments @('image', 'inspect', $ImageId) | ConvertFrom-Json)[0]
if ($image.Id -ne $ImageId -or
    $image.Config.Labels.'org.windcatcher.component' -ne 'harness-egress-proxy' -or
    $image.Config.Labels.'windcatcher.role' -ne 'harness-egress-proxy') {
    throw 'Image is not the dedicated immutable harness egress proxy image'
}

$instance = [guid]::NewGuid().ToString('N')
$suffix = $instance.Substring(0, 12)
$internalName = "windcatcher-harness-internal-$suffix"
$egressName = "windcatcher-harness-outbound-$suffix"
$containerName = "windcatcher-harness-proxy-$suffix"
$label = "org.windcatcher.harness.instance=$instance"
$createdInternal = $false
$createdEgress = $false
$createdContainer = $false

try {
    Invoke-Docker -Arguments @('network', 'create', '--driver', 'bridge', '--internal',
        '--ipv6=false', '--opt', 'com.docker.network.bridge.gateway_mode_ipv4=isolated',
        '--label', $label, $internalName) | Out-Null
    $createdInternal = $true
    $internal = @(Invoke-Docker -Arguments @('network', 'inspect', $internalName) |
        ConvertFrom-Json)[0]
    if (-not $internal.Internal -or $internal.EnableIPv6 -or
        $internal.Options.'com.docker.network.bridge.gateway_mode_ipv4' -ne 'isolated' -or
        $internal.Id -notmatch '^[0-9a-f]{64}$') {
        throw 'Isolated internal network validation failed'
    }
    $subnets = @($internal.IPAM.Config | Where-Object {
        $_.Subnet -match '^\d+\.\d+\.\d+\.\d+/\d+$'
    })
    if ($subnets.Count -ne 1) { throw 'Expected exactly one internal IPv4 subnet' }

    Invoke-Docker -Arguments @('network', 'create', '--driver', 'bridge', '--ipv6=false',
        '--label', $label, $egressName) | Out-Null
    $createdEgress = $true
    $egress = @(Invoke-Docker -Arguments @('network', 'inspect', $egressName) |
        ConvertFrom-Json)[0]
    if ($egress.Internal -or $egress.EnableIPv6 -or
        $egress.Id -notmatch '^[0-9a-f]{64}$') {
        throw 'Outbound network validation failed'
    }

    Invoke-Docker -Arguments @('create', '--name', $containerName,
        '--network', $internalName, '--network-alias', 'windcatcher-harness-proxy',
        '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
        '--pids-limit', '32', '--memory', '128m', '--memory-swap', '128m',
        '--cpus', '0.5', '--user', '10001:10001', '--restart', $restartPolicy,
        '--env', "WINDCATCHER_HARNESS_INTERNAL_SUBNET=$($subnets[0].Subnet)",
        '--env', "WINDCATCHER_HARNESS_TARGET_HOST=$TargetHost",
        '--label', $label, '--label', 'windcatcher.role=harness-egress-proxy',
        $ImageId) | Out-Null
    $createdContainer = $true
    Invoke-Docker -Arguments @('network', 'connect', '--gw-priority', '1',
        $egressName, $containerName) | Out-Null
    Invoke-Docker -Arguments @('start', $containerName) | Out-Null

    $ready = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        Start-Sleep -Milliseconds 250
        $container = @(Invoke-Docker -Arguments @('inspect', $containerName) |
            ConvertFrom-Json)[0]
        $logs = Invoke-Docker -Arguments @('logs', $containerName)
        if ($container.State.Running -and $logs -match '"event":"proxy_listening"') {
            $ready = $true
            break
        }
        if (-not $container.State.Running) { break }
    }
    if (-not $ready) { throw 'Proxy did not become ready' }
    $ports = Invoke-Docker -Arguments @('port', $containerName)
    if ($ports) { throw 'Proxy unexpectedly published a host port' }
    $connected = @($container.NetworkSettings.Networks.PSObject.Properties.Name)
    if ($connected.Count -ne 2 -or $internalName -notin $connected -or
        $egressName -notin $connected) {
        throw 'Proxy network attachment validation failed'
    }
    if ("WINDCATCHER_HARNESS_TARGET_HOST=$TargetHost" -notin @($container.Config.Env)) {
        throw 'Proxy target validation failed'
    }

    [pscustomobject]@{
        internal_network_id = $internal.Id
        internal_network_name = $internalName
        proxy_alias = 'windcatcher-harness-proxy'
        proxy_port = 3128
        target_host = $TargetHost
        proxy_container_name = $containerName
        outbound_network_id = $egress.Id
        image_id = $ImageId
    } | ConvertTo-Json -Compress
}
catch {
    $originalError = $_
    $residual = @()
    if ($createdContainer) {
        & docker rm -f $containerName 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { $residual += "container:$containerName (removal unconfirmed)" }
    }
    if ($createdEgress) {
        & docker network rm $egressName 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { $residual += "network:$egressName (removal unconfirmed)" }
    }
    if ($createdInternal) {
        & docker network rm $internalName 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { $residual += "network:$internalName (removal unconfirmed)" }
    }
    if ($residual.Count) {
        throw "Proxy startup failed: $($originalError.Exception.Message); residual resources: $($residual -join ', ')"
    }
    throw
}
