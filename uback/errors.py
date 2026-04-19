"""Exception hierarchy for U-Back."""

from __future__ import annotations


class BackupError(Exception):
    """Base class for all U-Back operational errors."""


class BackupAlreadyRunningError(BackupError):
    """Another backup process is already running (lock file held)."""


class NASUnavailableError(BackupError):
    """NAS share is offline, unreachable, or authentication failed."""
