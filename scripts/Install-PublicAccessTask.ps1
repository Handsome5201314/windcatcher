$ErrorActionPreference = 'Stop'
$appRoot = Split-Path $PSScriptRoot -Parent
$launcher = Join-Path $PSScriptRoot 'Run-PublicAccessHidden.vbs'
$wscript = Join-Path $env:SystemRoot 'System32\wscript.exe'
if (-not (Test-Path -LiteralPath $wscript)) { throw 'Windows Script Host is required for the hidden supervisor launcher.' }
$taskName = 'Windcatcher Public Access'
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $wscript -Argument "//B //NoLogo `"$launcher`"" -WorkingDirectory $appRoot
$loginTrigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$retryTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(30) -RepetitionInterval (New-TimeSpan -Minutes 1)
$retryTrigger.Repetition.StopAtDurationEnd = $false
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($loginTrigger, $retryTrigger) -Settings $settings -Principal $principal -Description 'Keeps Windcatcher local service and SSH public reverse tunnel healthy.' -Force | Out-Null
Remove-Item -LiteralPath (Join-Path $appRoot '.local-public\stop') -Force -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName $taskName
Write-Output "Installed and started scheduled task: $taskName"
Write-Output "It runs at logon; a one-minute trigger restores a stopped supervisor while logged in."
