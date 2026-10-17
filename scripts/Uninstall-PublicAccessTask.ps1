$ErrorActionPreference = 'Stop'
$taskName = 'Windcatcher Public Access'
& (Join-Path $PSScriptRoot 'Stop-PublicAccess.ps1')
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}
Write-Output "Removed scheduled task: $taskName"
