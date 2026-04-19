"""Tests for the lock file mechanism."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from uback.errors import BackupAlreadyRunningError
from uback.lock import LOCK_FILENAME, _pid_alive, _write_lock, backup_lock


# ---------------------------------------------------------------------------
# _pid_alive
# ---------------------------------------------------------------------------

def test_pid_alive_current_process():
    assert _pid_alive(os.getpid()) is True


def test_pid_alive_nonexistent():
    # PID 0 is the idle process on Windows; use a very large invalid PID
    assert _pid_alive(999_999_999) is False


# ---------------------------------------------------------------------------
# backup_lock — normal acquisition and release
# ---------------------------------------------------------------------------

def test_lock_created_and_released(tmp_path):
    lock_path = tmp_path / LOCK_FILENAME

    with backup_lock(lock_path):
        assert lock_path.exists()
        data = json.loads(lock_path.read_text())
        assert data["pid"] == os.getpid()

    assert not lock_path.exists()


def test_lock_released_on_exception(tmp_path):
    lock_path = tmp_path / LOCK_FILENAME

    with pytest.raises(RuntimeError):
        with backup_lock(lock_path):
            raise RuntimeError("boom")

    assert not lock_path.exists()


# ---------------------------------------------------------------------------
# backup_lock — concurrent attempt raises BackupAlreadyRunningError
# ---------------------------------------------------------------------------

def test_lock_raises_when_held_by_live_process(tmp_path):
    lock_path = tmp_path / LOCK_FILENAME

    with backup_lock(lock_path):
        # Simulate a second process trying to acquire the same lock.
        # The inner call sees the file exists AND the PID (our PID) is alive.
        with pytest.raises(BackupAlreadyRunningError):
            with backup_lock(lock_path):
                pass  # should not be reached


# ---------------------------------------------------------------------------
# backup_lock — stale lock takeover
# ---------------------------------------------------------------------------

def test_stale_lock_is_taken_over(tmp_path):
    lock_path = tmp_path / LOCK_FILENAME

    # Write a lock file with a dead PID
    payload = json.dumps({"pid": 999_999_999, "started_at": "2000-01-01T00:00:00"})
    lock_path.write_text(payload)

    # Should succeed: stale lock is silently removed and re-acquired
    with backup_lock(lock_path):
        data = json.loads(lock_path.read_text())
        assert data["pid"] == os.getpid()

    assert not lock_path.exists()


def test_corrupt_lock_treated_as_stale(tmp_path):
    lock_path = tmp_path / LOCK_FILENAME
    lock_path.write_text("not valid json{{")

    with backup_lock(lock_path):
        assert lock_path.exists()

    assert not lock_path.exists()


# ---------------------------------------------------------------------------
# BackupManager integration — lock prevents double run
# ---------------------------------------------------------------------------

def test_manager_raises_when_locked(tmp_path):
    from uback.manager import BackupManager

    src = tmp_path / "src"
    src.mkdir()
    (src / "f.txt").write_bytes(b"hi")
    nas = tmp_path / "nas"
    nas.mkdir()

    lock_path = nas / LOCK_FILENAME
    # Pre-create a lock file held by the current (live) process
    payload = json.dumps({"pid": os.getpid(), "started_at": "2026-01-01T00:00:00"})
    lock_path.write_text(payload)

    with pytest.raises(BackupAlreadyRunningError):
        BackupManager(src, nas).run()

    # Lock file should still be there (we didn't remove it)
    assert lock_path.exists()
