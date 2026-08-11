from __future__ import annotations

import html
import subprocess


def notify_windows(title: str, message: str) -> None:
    """Best-effort Windows toast; a missing notification API must not fail a sync."""
    escaped_title = html.escape(title, quote=True)
    escaped_message = html.escape(message, quote=True)
    xml = f"<toast><visual><binding template='ToastGeneric'><text>{escaped_title}</text><text>{escaped_message}</text></binding></visual></toast>"
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null; "
        "$doc = New-Object Windows.Data.Xml.Dom.XmlDocument; "
        f"$doc.LoadXml('{xml}'); "
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($doc); "
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Feedian').Show($toast)"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass
