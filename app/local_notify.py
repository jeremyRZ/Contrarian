"""Dependency-free Windows desktop toast fallback for local alerts."""
from __future__ import annotations

import base64
import os
import subprocess


def _encoded_command(title: str, message: str) -> str:
    # XML escaping is done inside PowerShell so alert text is never executable.
    script = r'''
$title = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("TITLE64"))
$message = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("MESSAGE64"))
try {
  [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
  $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
  $nodes = $xml.GetElementsByTagName("text")
  $nodes.Item(0).AppendChild($xml.CreateTextNode($title)) > $null
  $nodes.Item(1).AppendChild($xml.CreateTextNode($message)) > $null
  $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Contrarian").Show($toast)
} catch {
  Add-Type -AssemblyName System.Windows.Forms
  Add-Type -AssemblyName System.Drawing
  $balloon = New-Object System.Windows.Forms.NotifyIcon
  $balloon.Icon = [System.Drawing.SystemIcons]::Information
  $balloon.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info
  $balloon.BalloonTipTitle = $title
  $balloon.BalloonTipText = $message
  $balloon.Visible = $true
  $balloon.ShowBalloonTip(8000)
  Start-Sleep -Seconds 3
  $balloon.Dispose()
}
'''.replace("TITLE64", base64.b64encode(title.encode("utf-8")).decode("ascii"))
    script = script.replace("MESSAGE64", base64.b64encode(message.encode("utf-8")).decode("ascii"))
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


def send(title: str, message: str, timeout: int = 8) -> tuple[bool, str]:
    if os.name != "nt":
        return False, "WINDOWS_ONLY"
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
             _encoded_command(str(title)[:120], str(message)[:500])],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        )
        if result.returncode == 0:
            return True, "WINDOWS_TOAST_OR_BALLOON"
        detail = (result.stderr or result.stdout or "").replace("\r", " ").replace("\n", " ")[:180]
        return False, f"TOAST_EXIT_{result.returncode}:{detail}"
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__
