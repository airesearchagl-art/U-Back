"""Tests for the notification helper."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, call, patch

from uback import notify


def test_send_calls_win11toast(monkeypatch):
    mock_notify = MagicMock()
    mock_module = MagicMock()
    mock_module.notify = mock_notify

    with patch.dict(sys.modules, {"win11toast": mock_module}):
        notify.send("Title", "Body text")

    mock_notify.assert_called_once_with("Title", body="Body text")


def test_send_graceful_when_not_installed():
    # Simulate win11toast not being installed
    with patch.dict(sys.modules, {"win11toast": None}):
        # Should not raise
        notify.send("Title", "Body")


def test_send_graceful_on_exception():
    mock_module = MagicMock()
    mock_module.notify.side_effect = RuntimeError("COM error")

    with patch.dict(sys.modules, {"win11toast": mock_module}):
        # Should not raise
        notify.send("Title", "Body")
