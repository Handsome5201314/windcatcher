$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path (Split-Path $PSScriptRoot -Parent) 'scripts\Start-PublicAccess.ps1'
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
foreach ($name in @('Write-State', 'Write-Event')) {
    $function = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }.GetNewClosure(), $true)
    if (-not $function) { throw "Missing function: $name" }
    Invoke-Expression $function.Extent.Text
}

$runtime = Join-Path ([IO.Path]::GetTempPath()) ('windcatcher-state-test-' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($runtime) | Out-Null
$statePath = Join-Path $runtime 'state.json'
$lock = $null
try {
    Write-State @{ supervisor_pid = 100; server_pid = 101; tunnel_pid = 102; generation = 1 }
    $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    if ($state.generation -ne 1 -or $state.server_pid -isnot [int]) { throw 'Initial numeric state was not written.' }

    Write-State @{ supervisor_pid = 100; server_pid = $null; tunnel_pid = $null; generation = 2 }
    $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    if ($state.generation -ne 2 -or $null -ne $state.server_pid) { throw 'Replacement did not preserve null PIDs.' }
    if (([IO.File]::ReadAllBytes($statePath))[0] -ne 123) { throw 'State has a BOM or invalid JSON prefix.' }
    Write-Output 'PASS: first write and atomic replacement preserve numeric/null PIDs and UTF-8 JSON.'

    $lock = [IO.File]::Open($statePath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
    # This used to throw and terminate the supervisor, taking down its tunnel.
    Write-State @{ generation = 3 }
    $lock.Dispose()
    $lock = $null
    $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    if ($state.generation -ne 2) { throw 'A locked state was overwritten or corrupted.' }
    if ((Get-Content (Join-Path $runtime 'supervisor.log') -Raw) -notmatch 'State update skipped') { throw 'The file-lock failure was not observable.' }
    Write-Output 'PASS: exclusive file lock is bounded, logged, and does not terminate supervision.'

    Write-State @{ supervisor_pid = 100; generation = 4 }
    $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    if ($state.generation -ne 4) { throw 'State did not recover after the file lock was released.' }
    if (@(Get-ChildItem -LiteralPath $runtime -Filter '*.tmp').Count -or (Test-Path "$statePath.bak")) { throw 'Temporary state files were left behind.' }
    Write-Output 'PASS: state updates recover after lock release and clean up temporary files.'

    $logPath = Join-Path $runtime 'supervisor.log'
    $lock = [IO.File]::Open($logPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
    Write-Event 'Expected log-lock regression test.'
    $lock.Dispose()
    $lock = $null
    Write-Output 'PASS: a locked diagnostic log does not terminate supervision.'
} finally {
    if ($lock) { $lock.Dispose() }
    foreach ($file in [IO.Directory]::GetFiles($runtime)) { [IO.File]::Delete($file) }
    [IO.Directory]::Delete($runtime)
}
