param(
    [switch]$NoBrowser,
    [switch]$SkipOpenD
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$PythonPath = "G:\Coding\envs\ContestTrade\python.exe"
$DashboardUrl = "http://127.0.0.1:8000/strategy-center.html"
$HealthUrl = "http://127.0.0.1:8000/health"
$RuntimeDir = Join-Path $ProjectRoot ".runtime\launcher"

function Test-LocalPort {
    param([int]$Port, [int]$TimeoutMs = 400)
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $pending = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $pending.AsyncWaitHandle.WaitOne($TimeoutMs)) { return $false }
        $client.EndConnect($pending)
        return $true
    }
    catch { return $false }
    finally { $client.Dispose() }
}

function Show-LauncherMessage {
    param([string]$Message, [string]$Title = "Contrarian")
    try {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show($Message, $Title) | Out-Null
    }
    catch { Write-Host $Message }
}

if (-not (Test-Path -LiteralPath $PythonPath)) {
    Show-LauncherMessage "ContestTrade Python was not found:`n$PythonPath"
    exit 1
}

New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null

$openDReady = Test-LocalPort -Port 11111
if (-not $openDReady -and -not $SkipOpenD) {
    $openDShortcuts = @(
        (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Futu_OpenD\Futu OpenD.lnk"),
        (Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\Futu_OpenD\Futu OpenD.lnk")
    )
    $openDShortcut = $openDShortcuts | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    $openDTarget = $null
    if ($openDShortcut) {
        $shell = New-Object -ComObject WScript.Shell
        $openDTarget = $shell.CreateShortcut($openDShortcut).TargetPath
    }
    if (-not $openDTarget -or -not (Test-Path -LiteralPath $openDTarget)) {
        $openDTarget = Join-Path $env:APPDATA "Futu_OpenD\Futu_OpenD.exe"
    }
    if (Test-Path -LiteralPath $openDTarget) {
        try {
            Start-Process -FilePath $openDTarget
            for ($attempt = 0; $attempt -lt 20; $attempt++) {
                Start-Sleep -Milliseconds 500
                if (Test-LocalPort -Port 11111) { $openDReady = $true; break }
            }
        }
        catch {
            Write-Warning "Futu OpenD could not be started automatically: $($_.Exception.Message)"
        }
    }
}

$serverReady = $false
try {
    $response = Invoke-WebRequest -UseBasicParsing -Uri $HealthUrl -TimeoutSec 2
    $serverReady = ($response.StatusCode -eq 200)
}
catch { $serverReady = $false }

if (-not $serverReady) {
    if (Test-LocalPort -Port 8000) {
        Show-LauncherMessage "Port 8000 is already used by another application."
        exit 1
    }

    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $stdoutLog = Join-Path $RuntimeDir "server-$stamp.log"
    $stderrLog = Join-Path $RuntimeDir "server-$stamp.error.log"
    $server = Start-Process -FilePath $PythonPath `
        -ArgumentList @("-m", "uvicorn", "app.api:app", "--host", "127.0.0.1", "--port", "8000") `
        -WorkingDirectory $ProjectRoot -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -PassThru

    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 500
        if ($server.HasExited) { break }
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $HealthUrl -TimeoutSec 2
            if ($response.StatusCode -eq 200) { $serverReady = $true; break }
        }
        catch { }
    }

    if (-not $serverReady) {
        $detail = "Contrarian failed to start. Log:`n$stderrLog"
        if (Test-Path -LiteralPath $stderrLog) {
            $tail = (Get-Content -LiteralPath $stderrLog -Tail 8 -ErrorAction SilentlyContinue) -join "`n"
            if ($tail) { $detail += "`n`n$tail" }
        }
        Show-LauncherMessage $detail
        exit 1
    }
}

if (-not $NoBrowser) {
    Start-Process $DashboardUrl
    if (-not $openDReady) {
        Show-LauncherMessage "Contrarian is ready, but Futu OpenD port 11111 is not ready. Sign in to Futu OpenD, then refresh the page."
    }
}

Write-Output "CONTRARIAN_READY"
Write-Output "DASHBOARD=$DashboardUrl"
Write-Output "FUTU_OPEND=$openDReady"
