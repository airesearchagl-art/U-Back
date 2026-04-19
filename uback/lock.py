"""
Lock-file based mutual exclusion for backup runs.

The lock file (.uback.lock) lives in base_dir and stores the holder's PID
and start time.  On acquisition we check for stale locks (PID no longer
alive) and take them over with a warning rather than blocking forever.
"""

from __future__ import annotations

import json
import logging
import os
import platform
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator

from .errors import BackupAlreadyRunningError

log = logging.getLogger(__name__)

LOCK_FILENAME = ".uback.lock"


# ---------------------------------------------------------------------------
# PID liveness check
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    """Return True if a process with *pid* is currently running."""
    if platform.system() == "Windows":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    # POSIX: signal 0 probes existence without sending a signal
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists; we just lack permission to signal it


# ---------------------------------------------------------------------------
# Lock file I/O
# ---------------------------------------------------------------------------

def _write_lock(lock_path: Path) -> None:
    """Atomically create the lock file. Raises FileExistsError if it exists."""
    payload = json.dumps({
        "pid": os.getpid(),
        "started_at": datetime.now().isoformat(),
    }).encode()
    # O_EXCL guarantees atomic creation; raises FileExistsError on collision
    fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


def _read_lock_pid(lock_path: Path) -> int | None:
    """Return the PID stored in *lock_path*, or None on any parse failure."""
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        return int(data["pid"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Public context manager
# ---------------------------------------------------------------------------

@contextmanager
def backup_lock(lock_path: Path) -> Generator[None, None, None]:
    """
    Context manager that holds an exclusive lock for one backup session.

    Raises:
        BackupAlreadyRunningError: if the lock is held by a live process.
    """
    acquired = False
    try:
        try:
            _write_lock(lock_path)
            acquired = True
        except FileExistsError:
            pid = _read_lock_pid(lock_path)
            if pid is not None and _pid_alive(pid):
                raise BackupAlreadyRunningError(
                    f"Backup already running (PID {pid}). "
                    f"Lock file: {lock_path}"
                )
            # Stale lock — previous process died without cleanup
            log.warning(
                "Stale lock detected (PID %s no longer alive). "
                "Taking over: %s",
                pid, lock_path,
            )
            lock_path.unlink(missing_ok=True)
            _write_lock(lock_path)
            acquired = True

        yield

    finally:
        if acquired:
            lock_path.unlink(missing_ok=True)
            log.debug("Lock released: %s", lock_path)
