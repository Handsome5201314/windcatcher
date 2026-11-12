param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{64}$')]
    [string]$InternalNetworkId,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{64}$')]
    [string]$OutboundNetworkId,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^windcatcher-harness-proxy-[0-9a-f]{12}$')]
    [string]$ProxyContainerName
)

$ErrorActionPreference = 'Stop'

function Invoke-Docker([string[]]$Arguments) {
    $result = & docker @Arguments 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { throw "Docker $($Arguments[0]) failed" }
    return $result.Trim()
}

function Get-OptionalNetwork([string]$Id) {
    # An exact-ID listing distinguishes an already removed network from an inspect error.
    $listing = Invoke-Docker -Arguments @('network', 'ls', '--no-trunc', '--quiet',
        '--filter', "id=$Id")
    $ids = @($listing.Split([char]10) | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($ids.Count -eq 0) { return $null }
    if ($ids.Count -ne 1 -or $ids[0] -ne $Id) { throw 'Network ID lookup was not exact' }
    $network = @(Invoke-Docker -Arguments @('network', 'inspect', $Id) |
        ConvertFrom-Json)[0]
    return $network
}

function Get-OptionalContainer([string]$Name) {
    $listing = Invoke-Docker -Arguments @('container', 'ls', '--all', '--no-trunc',
        '--quiet', '--filter', "name=^/$Name$")
    $ids = @($listing.Split([char]10) | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($ids.Count -eq 0) { return $null }
    if ($ids.Count -ne 1 -or $ids[0] -notmatch '^[0-9a-f]{64}$') {
        throw 'Proxy container lookup was not exact'
    }
    $container = @(Invoke-Docker -Arguments @('container', 'inspect', $ids[0]) |
        ConvertFrom-Json)[0]
    return $container
}

if ($InternalNetworkId -eq $OutboundNetworkId) {
    throw 'Internal and outbound network IDs must differ'
}
$suffix = $ProxyContainerName.Substring('windcatcher-harness-proxy-'.Length)
$internalName = "windcatcher-harness-internal-$suffix"
$outboundName = "windcatcher-harness-outbound-$suffix"

$internal = Get-OptionalNetwork -Id $InternalNetworkId
$outbound = Get-OptionalNetwork -Id $OutboundNetworkId
$container = Get-OptionalContainer -Name $ProxyContainerName

if ($null -eq $internal -and $null -eq $outbound -and $null -eq $container) {
    Write-Output 'Proxy and both dedicated networks are already removed'
    return
}

# A retry may find only one or both networks. Every surviving resource must
# independently match the exact lease identity before any removal happens.
$instances = @()
if ($null -ne $internal) {
    if ($internal.Id -ne $InternalNetworkId -or $internal.Name -ne $internalName -or
        $internal.Driver -ne 'bridge' -or $internal.Internal -ne $true -or
        $internal.EnableIPv6 -ne $false -or
        $internal.Options.'com.docker.network.bridge.gateway_mode_ipv4' -ne 'isolated') {
        throw 'Internal network ownership validation failed'
    }
    $instances += $internal.Labels.'org.windcatcher.harness.instance'
}
if ($null -ne $outbound) {
    if ($outbound.Id -ne $OutboundNetworkId -or $outbound.Name -ne $outboundName -or
        $outbound.Driver -ne 'bridge' -or $outbound.Internal -ne $false -or
        $outbound.EnableIPv6 -ne $false) {
        throw 'Outbound network ownership validation failed'
    }
    $instances += $outbound.Labels.'org.windcatcher.harness.instance'
}
if ($null -ne $container) {
    if ($container.Id -notmatch '^[0-9a-f]{64}$' -or
        $container.Name -ne "/$ProxyContainerName" -or
        $container.Config.Labels.'org.windcatcher.component' -ne 'harness-egress-proxy' -or
        $container.Config.Labels.'windcatcher.role' -ne 'harness-egress-proxy') {
        throw 'Proxy container ownership validation failed'
    }
    $instances += $container.Config.Labels.'org.windcatcher.harness.instance'
}

$instance = $instances[0]
foreach ($observed in $instances) {
    if ($observed -isnot [string] -or $observed -notmatch '^[0-9a-f]{32}$' -or
        -not $observed.StartsWith($suffix) -or $observed -ne $instance) {
        throw 'Proxy resource lease labels do not match'
    }
}

if ($null -ne $container) {
    if ($null -eq $internal -or $null -eq $outbound -or
        $null -eq $container.NetworkSettings.Networks) {
        throw 'Proxy network attachments are incomplete'
    }
    $networks = $container.NetworkSettings.Networks.PSObject.Properties
    $names = @($networks.Name)
    if ($names.Count -ne 2 -or $internalName -notin $names -or
        $outboundName -notin $names -or
        $networks[$internalName].Value.NetworkID -ne $InternalNetworkId -or
        $networks[$outboundName].Value.NetworkID -ne $OutboundNetworkId) {
        throw 'Proxy has unexpected network attachments'
    }
}
foreach ($network in @($internal, $outbound)) {
    if ($null -eq $network) { continue }
    if ($null -eq $network.Containers) {
        throw "Network $($network.Name) membership is unavailable"
    }
    $members = @($network.Containers.PSObject.Properties | ForEach-Object { $_.Name })
    if ($members.Count -gt 1 -or
        ($members.Count -eq 1 -and
         ($null -eq $container -or $members[0] -ne $container.Id))) {
        throw "Network $($network.Name) has another attached container; refusing removal"
    }
}

# Remove by immutable IDs. If one network removal fails, still attempt the
# other; the original lease remains sufficient to retry any surviving network.
if ($null -ne $container) {
    Invoke-Docker -Arguments @('rm', '-f', $container.Id) | Out-Null
}
$failed = @()
foreach ($network in @($internal, $outbound)) {
    if ($null -eq $network) { continue }
    try {
        Invoke-Docker -Arguments @('network', 'rm', $network.Id) | Out-Null
    } catch {
        $failed += $network.Id
    }
}
if ($failed.Count -gt 0) {
    throw "Proxy stopped, but network removal is unconfirmed for: $($failed -join ', '); retry with the same lease"
}
Write-Output "Stopped and removed $ProxyContainerName and its dedicated networks"
