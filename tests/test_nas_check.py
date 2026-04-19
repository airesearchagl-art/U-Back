"""Tests for NAS reachability checking."""

from __future__ import annotations

import errno
from pathlib import Path
from unittest.mock import patch

import pytest

from uback.errors import NASUnavailableError
from uback.manager import _check_nas_accessible


def _os_error(err_no: int, msg: str = "test") -> OSError:
    exc = OSError(err_no, msg)
    exc.errno = err_no
    return exc


def test_accessible_dir_passes(tmp_path):
    # A local directory should pass without error
    _check_nas_accessible(tmp_path / "new_subdir")


def test_existing_dir_passes(tmp_path):
    _check_nas_accessible(tmp_path)


def test_offline_errno_raises_nas_unavailable(tmp_path):
    target = tmp_path / "nas"
    with patch("pathlib.Path.mkdir", side_effect=_os_error(errno.EHOSTUNREACH)):
        with pytest.raises(NASUnavailableError, match="offline or unreachable"):
            _check_nas_accessible(target)


def test_timeout_errno_raises_nas_unavailable(tmp_path):
    target = tmp_path / "nas"
    with patch("pathlib.Path.mkdir", side_effect=_os_error(errno.ETIMEDOUT)):
        with pytest.raises(NASUnavailableError, match="offline or unreachable"):
            _check_nas_accessible(target)


def test_auth_errno_raises_nas_unavailable(tmp_path):
    target = tmp_path / "nas"
    with patch("pathlib.Path.mkdir", side_effect=_os_error(errno.EACCES)):
        with pytest.raises(NASUnavailableError, match="Authentication error"):
            _check_nas_accessible(target)


def test_permission_denied_raises_nas_unavailable(tmp_path):
    target = tmp_path / "nas"
    with patch("pathlib.Path.mkdir", side_effect=_os_error(errno.EPERM)):
        with pytest.raises(NASUnavailableError, match="Authentication error"):
            _check_nas_accessible(target)


def test_windows_offline_winerror_raises(tmp_path):
    target = tmp_path / "nas"
    exc = OSError("network path not found")
    exc.errno = None
    exc.winerror = 53  # ERROR_BAD_NETPATH
    with patch("pathlib.Path.mkdir", side_effect=exc):
        with pytest.raises(NASUnavailableError, match="offline or unreachable"):
            _check_nas_accessible(target)


def test_windows_auth_winerror_raises(tmp_path):
    target = tmp_path / "nas"
    exc = OSError("access denied")
    exc.errno = None
    exc.winerror = 5  # ERROR_ACCESS_DENIED
    with patch("pathlib.Path.mkdir", side_effect=exc):
        with pytest.raises(NASUnavailableError, match="Authentication error"):
            _check_nas_accessible(target)


def test_unexpected_oserror_propagates(tmp_path):
    target = tmp_path / "nas"
    # An unrecognised errno (e.g. ENOSPC) should re-raise as-is, not wrapped
    with patch("pathlib.Path.mkdir", side_effect=_os_error(errno.ENOSPC)):
        with pytest.raises(OSError) as exc_info:
            _check_nas_accessible(target)
        assert not isinstance(exc_info.value, NASUnavailableError)
