$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path (Join-Path (Split-Path $PSScriptRoot -Parent) 'scripts') 'Start-PublicAccess.ps1'
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
foreach ($name in @('Read-DotEnv', 'Set-HarnessEnvironment')) {
    $function = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }.GetNewClosure(), $true)
    if (-not $function) { throw "Missing function: $name" }
    Invoke-Expression $function.Extent.Text
}

function Assert-Env([string]$Name, [string]$Expected) {
    $actual = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if ([string]$actual -cne [string]$Expected) { throw "Unexpected process value for $Name" }
}

function Assert-Rejected([hashtable]$Settings, [string]$Mode) {
    $rejected = $false
    try { Set-HarnessEnvironment $Settings $Mode } catch { $rejected = $true }
    if (-not $rejected) { throw 'Invalid Harness configuration was accepted.' }
    Assert-Env 'DEEPSEEK_API_KEY' $null
    Assert-Env 'WINDCATCHER_HARNESS_ADVICE' $null
    Assert-Env 'WINDCATCHER_HARNESS_ACTIONS' $null
    Assert-Env 'WINDCATCHER_HARNESS_BASE_URL' $null
}

$names = @('WINDCATCHER_HARNESS_ADVICE', 'WINDCATCHER_HARNESS_ACTIONS', 'WINDCATCHER_HARNESS_IMAGE',
    'WINDCATCHER_HARNESS_EGRESS_NETWORK_ID', 'WINDCATCHER_HARNESS_EGRESS_PROXY_IMAGE',
    'WINDCATCHER_HARNESS_MODEL', 'WINDCATCHER_HARNESS_DSH_BIN',
    'DEEPSEEK_API_KEY', 'DEEPSEEK_BASE_URL', 'WINDCATCHER_HARNESS_BASE_URL')
$previous = @{}
foreach ($name in $names) { $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
$temp = Join-Path ([IO.Path]::GetTempPath()) ('windcatcher-harness-env-' + [Guid]::NewGuid().ToString('N'))
try {
    [Environment]::SetEnvironmentVariable('WINDCATCHER_HARNESS_ADVICE', '1', 'Process')
    [Environment]::SetEnvironmentVariable('WINDCATCHER_HARNESS_ACTIONS', '1', 'Process')
    [Environment]::SetEnvironmentVariable('WINDCATCHER_HARNESS_DSH_BIN', 'other-program', 'Process')
    [Environment]::SetEnvironmentVariable('DEEPSEEK_API_KEY', 'other-program-key', 'Process')
    [Environment]::SetEnvironmentVariable('DEEPSEEK_BASE_URL', 'http://other-program', 'Process')
    [Environment]::SetEnvironmentVariable('WINDCATCHER_HARNESS_BASE_URL', 'http://other-program', 'Process')
    Set-HarnessEnvironment @{} '1'
    foreach ($name in @('WINDCATCHER_HARNESS_ADVICE', 'WINDCATCHER_HARNESS_ACTIONS', 'WINDCATCHER_HARNESS_DSH_BIN', 'DEEPSEEK_API_KEY', 'DEEPSEEK_BASE_URL', 'WINDCATCHER_HARNESS_BASE_URL')) {
        Assert-Env $name $null
    }
    Write-Output 'PASS: disabled pilot clears inherited Harness and DeepSeek settings.'

    $valid = @{
        WINDCATCHER_HARNESS_ADVICE = '1'
        WINDCATCHER_HARNESS_IMAGE = ('sha256:' + ('a' * 64))
        DEEPSEEK_API_KEY = 'synthetic-project-only-key'
    }
    Assert-Rejected $valid '0'
    [Environment]::SetEnvironmentVariable('DEEPSEEK_API_KEY', 'other-program-key', 'Process')
    $invalid = $valid.Clone(); $invalid.Remove('DEEPSEEK_API_KEY'); Assert-Rejected $invalid '1'
    $invalid = $valid.Clone(); $invalid.Remove('WINDCATCHER_HARNESS_IMAGE'); Assert-Rejected $invalid '1'
    $invalid = $valid.Clone(); $invalid['WINDCATCHER_HARNESS_ADVICE'] = 'yes'; Assert-Rejected $invalid '1'
    $invalid = $valid.Clone(); $invalid['WINDCATCHER_HARNESS_MODEL'] = 'bad model'; Assert-Rejected $invalid '1'
    $invalid = $valid.Clone(); $invalid['WINDCATCHER_HARNESS_BASE_URL'] = 'https://unapproved.example'; Assert-Rejected $invalid '1'
    $invalid = $valid.Clone(); $invalid['WINDCATCHER_HARNESS_EGRESS_NETWORK_ID'] = ('b' * 64); Assert-Rejected $invalid '1'
    $invalid = $valid.Clone(); $invalid['WINDCATCHER_HARNESS_ACTIONS'] = 'yes'; Assert-Rejected $invalid '1'
    $invalid = $valid.Clone(); $invalid['WINDCATCHER_HARNESS_ACTIONS'] = '1'; $invalid['WINDCATCHER_HARNESS_ADVICE'] = '0'; Assert-Rejected $invalid '1'
    Write-Output 'PASS: pilot rejects missing, invalid, and non-personal configuration without credential fallback.'

    [IO.File]::WriteAllLines($temp, @(
        'WINDCATCHER_HARNESS_ADVICE=1',
        'WINDCATCHER_HARNESS_ACTIONS=1',
        ('WINDCATCHER_HARNESS_IMAGE=sha256:' + ('a' * 64)),
        'DEEPSEEK_API_KEY=synthetic-project-only-key',
        'WINDCATCHER_HARNESS_MODEL=gpt-6.1-sol',
        'WINDCATCHER_HARNESS_BASE_URL=https://tongyimohe.aitoken.credit/v1',
        ('WINDCATCHER_HARNESS_EGRESS_NETWORK_ID=' + ('b' * 64)),
        ('WINDCATCHER_HARNESS_EGRESS_PROXY_IMAGE=sha256:' + ('c' * 64)),
        'WINDCATCHER_HARNESS_DSH_BIN=must-not-pass',
        'DEEPSEEK_BASE_URL=http://must-not-pass'
    ))
    [Environment]::SetEnvironmentVariable('WINDCATCHER_HARNESS_DSH_BIN', 'other-program', 'Process')
    [Environment]::SetEnvironmentVariable('DEEPSEEK_BASE_URL', 'http://other-program', 'Process')
    Set-HarnessEnvironment (Read-DotEnv $temp) '1'
    Assert-Env 'WINDCATCHER_HARNESS_ADVICE' '1'
    Assert-Env 'WINDCATCHER_HARNESS_ACTIONS' '1'
    Assert-Env 'WINDCATCHER_HARNESS_IMAGE' ('sha256:' + ('a' * 64))
    Assert-Env 'DEEPSEEK_API_KEY' 'synthetic-project-only-key'
    Assert-Env 'WINDCATCHER_HARNESS_MODEL' 'gpt-6.1-sol'
    Assert-Env 'WINDCATCHER_HARNESS_BASE_URL' 'https://tongyimohe.aitoken.credit/v1'
    Assert-Env 'WINDCATCHER_HARNESS_EGRESS_NETWORK_ID' ('b' * 64)
    Assert-Env 'WINDCATCHER_HARNESS_EGRESS_PROXY_IMAGE' ('sha256:' + ('c' * 64))
    Assert-Env 'WINDCATCHER_HARNESS_DSH_BIN' $null
    Assert-Env 'DEEPSEEK_BASE_URL' $null
    Write-Output 'PASS: enabled pilot imports only validated values from project .env.'
} finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }
    Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
}
