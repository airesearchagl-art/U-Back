"""
Windows toast notification helper.

Wraps win11toast with graceful degradation: if the library is not installed
or notification fails (e.g. non-Windows environment), the error is logged at
DEBUG level and execution continues normally.

Install:  pip install win11toast
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def send(title: str, body: str) -> None:
    """
    Send a Windows 11 toast notification.

    Silently skips when win11toast is not installed or unavailable.
    Never raises — notifications are best-effort.
    """
    try:
        from win11toast import notify  # type: ignore[import]
        notify(title, body=body)
    except ImportError:
        log.debug("win11toast not installed — skipping notification")
    except Exception as exc:  # noqa: BLE001
        log.debug("Notification failed (non-fatal): %s", exc)
