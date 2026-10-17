Option Explicit
Dim shell, fs, scriptPath, powershellPath, command, exitCode
Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
scriptPath = fs.BuildPath(fs.GetParentFolderName(WScript.ScriptFullName), "Start-PublicAccess.ps1")
powershellPath = shell.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\WindowsPowerShell\v1.0\powershell.exe"
command = """" & powershellPath & """ -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & scriptPath & """ -Run"
' Wait for the supervisor and propagate its exit code to Task Scheduler.
exitCode = shell.Run(command, 0, True)
WScript.Quit exitCode
