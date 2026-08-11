from __future__ import annotations

from feedian.notifications import notify_windows


def test_windows_notification_is_best_effort_when_powershell_is_missing(monkeypatch) -> None:
    def missing(*_args, **_kwargs):
        raise OSError("not installed")

    monkeypatch.setattr("feedian.notifications.subprocess.run", missing)

    notify_windows("Feedian", "done")
