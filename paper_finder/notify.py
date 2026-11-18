"""Phase 5: Windows toast notification for new papers (best effort; other OSes just skip)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

# Windows PowerShell's registered app id, so the toast shows without registering our own app.
_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($env:PF_TOAST_XML)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:PF_TOAST_APP).Show($toast)
"""


def toast_xml(title: str, body: str, open_path: Path | None = None) -> str:
    launch = f' activationType="protocol" launch={quoteattr(open_path.resolve().as_uri())}' if open_path else ""
    return (f"<toast{launch}><visual><binding template=\"ToastGeneric\">"
            f"<text>{escape(title)}</text><text>{escape(body)}</text></binding></visual></toast>")


def notify(title: str, body: str, open_path: Path | None = None) -> bool:
    """Show a toast; clicking it opens `open_path`. Returns False when not shown."""
    if sys.platform != "win32":
        return False
    env = {**os.environ, "PF_TOAST_XML": toast_xml(title, body, open_path), "PF_TOAST_APP": _APP_ID}
    try:
        # WinRT types need Windows PowerShell 5.1 (powershell.exe), not PowerShell 7.
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _PS], env=env,
                           capture_output=True, text=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0
