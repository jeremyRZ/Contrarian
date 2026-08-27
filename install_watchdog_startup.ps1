$ErrorActionPreference = "Stop"
$PythonwPath = "G:\Coding\envs\ContestTrade\pythonw.exe"
$ScriptPath = Join-Path $PSScriptRoot "scripts\watchdog.py"
$Command = '"' + $PythonwPath + '" "' + $ScriptPath + '" --loop'
reg.exe add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" `
    /v "ContrarianWatchdog" /t REG_SZ /d $Command /f | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Failed to register startup watchdog" }
Start-Process -FilePath $PythonwPath -ArgumentList @($ScriptPath, "--loop") `
    -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
