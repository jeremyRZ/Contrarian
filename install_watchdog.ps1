$ErrorActionPreference = "Stop"
$TaskName = "ContrarianWatchdog"
$PythonPath = "G:\Coding\envs\ContestTrade\python.exe"
$ScriptPath = Join-Path $PSScriptRoot "scripts\watchdog.py"
$Action = '"' + $PythonPath + '" "' + $ScriptPath + '" --once'
schtasks.exe /Create /TN $TaskName /TR $Action /SC MINUTE /MO 5 /F | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Failed to create $TaskName" }
schtasks.exe /Run /TN $TaskName | Out-Host
