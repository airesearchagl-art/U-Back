"""Tests for the CLI — run subcommand and task name helper."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from uback.cli import _task_name, main
from uback.errors import BackupAlreadyRunningError, NASUnavailableError


# ---------------------------------------------------------------------------
# _task_name
# ---------------------------------------------------------------------------

def test_task_name_two_components():
    assert _task_name("//NAS/Backup/Docs") == "U-Back\\Backup-Docs"


def test_task_name_single_component():
    assert _task_name("Backup") == "U-Back\\Backup"


def test_task_name_strips_invalid_chars():
    name = _task_name('//NAS/share/my:folder"')
    assert '"' not in name
    assert ":" not in name


# ---------------------------------------------------------------------------
# cli run — success
# ---------------------------------------------------------------------------

def test_cli_run_success(tmp_path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.txt").write_bytes(b"hello")
    nas = tmp_path / "nas"

    exit_code = main([
        "run",
        "--source", str(src),
        "--base-dir", str(nas),
    ])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Copied" in out
    assert "Snapshot" in out


# ---------------------------------------------------------------------------
# cli run — BackupAlreadyRunningError → exit code 2
# ---------------------------------------------------------------------------

def test_cli_run_already_running(tmp_path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    nas = tmp_path / "nas"

    with patch("uback.cli.BackupManager.run",
               side_effect=BackupAlreadyRunningError("busy")):
        exit_code = main(["run", "--source", str(src), "--base-dir", str(nas)])

    assert exit_code == 2


# ---------------------------------------------------------------------------
# cli run — NASUnavailableError → exit code 3
# ---------------------------------------------------------------------------

def test_cli_run_nas_unavailable(tmp_path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    nas = tmp_path / "nas"

    with patch("uback.cli.BackupManager.run",
               side_effect=NASUnavailableError("offline")):
        exit_code = main(["run", "--source", str(src), "--base-dir", str(nas)])

    assert exit_code == 3


# ---------------------------------------------------------------------------
# cli run — with retention flags
# ---------------------------------------------------------------------------

def test_cli_run_with_retention(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.txt").write_bytes(b"data")
    nas = tmp_path / "nas"

    exit_code = main([
        "run",
        "--source", str(src),
        "--base-dir", str(nas),
        "--keep", "5",
        "--max-age", "14",
    ])

    assert exit_code == 0


# ---------------------------------------------------------------------------
# cli schedule / unschedule — non-Windows guard
# ---------------------------------------------------------------------------

def test_cli_schedule_non_windows_exits(tmp_path):
    with patch("uback.cli.platform.system", return_value="Linux"):
        with pytest.raises(SystemExit) as exc_info:
            main([
                "schedule",
                "--source", str(tmp_path / "src"),
                "--base-dir", str(tmp_path / "nas"),
                "--interval", "60",
            ])
        assert exc_info.value.code == 1


def test_cli_unschedule_non_windows_exits(tmp_path):
    with patch("uback.cli.platform.system", return_value="Linux"):
        with pytest.raises(SystemExit) as exc_info:
            main(["unschedule", "--base-dir", str(tmp_path / "nas")])
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# cli schedule — Windows path (mock schtasks)
# ---------------------------------------------------------------------------

def test_cli_schedule_calls_schtasks(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    nas = tmp_path / "nas"

    mock_result = MagicMock()
    mock_result.returncode = 0

    with patch("uback.cli.platform.system", return_value="Windows"), \
         patch("uback.cli.subprocess.run", return_value=mock_result) as mock_run:
        exit_code = main([
            "schedule",
            "--source", str(src),
            "--base-dir", str(nas),
            "--interval", "30",
        ])

    assert exit_code == 0
    cmd = mock_run.call_args[0][0]
    assert "schtasks" in cmd[0]
    assert "/Create" in cmd
    assert "/SC" in cmd
    assert "MINUTE" in cmd
    assert "30" in cmd


def test_cli_schedule_schtasks_failure(tmp_path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    nas = tmp_path / "nas"

    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = "ERROR: Access is denied."
    mock_result.stdout = ""

    with patch("uback.cli.platform.system", return_value="Windows"), \
         patch("uback.cli.subprocess.run", return_value=mock_result):
        exit_code = main([
            "schedule",
            "--source", str(src),
            "--base-dir", str(nas),
            "--interval", "60",
        ])

    assert exit_code == 1


def test_cli_unschedule_calls_schtasks(tmp_path):
    nas = tmp_path / "nas"
    mock_result = MagicMock()
    mock_result.returncode = 0

    with patch("uback.cli.platform.system", return_value="Windows"), \
         patch("uback.cli.subprocess.run", return_value=mock_result) as mock_run:
        exit_code = main(["unschedule", "--base-dir", str(nas)])

    assert exit_code == 0
    cmd = mock_run.call_args[0][0]
    assert "/Delete" in cmd


# ---------------------------------------------------------------------------
# cli run — --notify triggers notification on success
# ---------------------------------------------------------------------------

def test_cli_run_notify_on_success(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.txt").write_bytes(b"data")
    nas = tmp_path / "nas"

    with patch("uback.cli.notify.send") as mock_send:
        exit_code = main([
            "run", "--source", str(src), "--base-dir", str(nas), "--notify",
        ])

    assert exit_code == 0
    mock_send.assert_called_once()
    title, body = mock_send.call_args[0]
    assert "✓" in title


def test_cli_run_notify_on_nas_error(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    nas = tmp_path / "nas"

    with patch("uback.cli.BackupManager.run",
               side_effect=NASUnavailableError("offline")), \
         patch("uback.cli.notify.send") as mock_send:
        exit_code = main([
            "run", "--source", str(src), "--base-dir", str(nas), "--notify",
        ])

    assert exit_code == 3
    mock_send.assert_called_once()
    title, _ = mock_send.call_args[0]
    assert "✗" in title


def test_cli_run_no_notify_without_flag(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.txt").write_bytes(b"data")
    nas = tmp_path / "nas"

    with patch("uback.cli.notify.send") as mock_send:
        main(["run", "--source", str(src), "--base-dir", str(nas)])

    mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# cli status — basic output
# ---------------------------------------------------------------------------

def test_cli_status_empty_dir(tmp_path, capsys):
    nas = tmp_path / "nas"
    nas.mkdir()

    exit_code = main(["status", "--base-dir", str(nas)])

    assert exit_code == 0
    assert "No snapshots found" in capsys.readouterr().out


def test_cli_status_missing_dir(tmp_path, capsys):
    exit_code = main(["status", "--base-dir", str(tmp_path / "missing")])
    assert exit_code == 1


def test_cli_status_shows_snapshots(tmp_path, capsys):
    import json
    nas = tmp_path / "nas"
    for name, files, size in [
        ("2026-01-01-000000", 100, 1024 * 1024),
        ("2026-04-19-120000", 110, 512 * 1024),
    ]:
        d = nas / name
        d.mkdir(parents=True)
        (d / "backup_info.json").write_text(json.dumps({
            "total_files": files, "total_bytes": size, "success": True,
        }))

    exit_code = main(["status", "--base-dir", str(nas)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "2026-01-01-000000" in out
    assert "2026-04-19-120000" in out
    assert "Latest backup" in out
    assert "Storage summary" in out


def test_cli_status_shows_savings_message(tmp_path, capsys):
    import json, os
    nas = tmp_path / "nas"
    snap1 = nas / "2026-01-01-000000"
    snap2 = nas / "2026-04-19-000000"

    (snap1).mkdir(parents=True)
    (snap1 / "big.txt").write_bytes(b"X" * 10_000)
    (snap1 / "backup_info.json").write_text(json.dumps(
        {"total_files": 1, "total_bytes": 10_000, "success": True}
    ))

    snap2.mkdir(parents=True)
    os.link(snap1 / "big.txt", snap2 / "big.txt")   # hard link = shared
    (snap2 / "backup_info.json").write_text(json.dumps(
        {"total_files": 1, "total_bytes": 0, "success": True}
    ))

    exit_code = main(["status", "--base-dir", str(nas)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "saved" in out.lower()


def test_cli_schedule_passes_notify_flag(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    nas = tmp_path / "nas"

    mock_result = MagicMock()
    mock_result.returncode = 0

    with patch("uback.cli.platform.system", return_value="Windows"), \
         patch("uback.cli.subprocess.run", return_value=mock_result) as mock_run:
        main([
            "schedule",
            "--source", str(src), "--base-dir", str(nas),
            "--interval", "60", "--notify",
        ])

    tr_value = mock_run.call_args[0][0][
        mock_run.call_args[0][0].index("/TR") + 1
    ]
    assert "--notify" in tr_value
