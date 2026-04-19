"""Tests for status collection and storage savings analysis."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from uback.status import (
    StorageSummary,
    collect_status,
    fmt_bytes,
    time_ago,
    _compute_storage,
    _read_snapshot_record,
)
from uback.manager import _SNAPSHOT_FMT


# ---------------------------------------------------------------------------
# fmt_bytes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n, expected", [
    (0,              "0.0 B"),
    (512,            "512.0 B"),
    (1024,           "1.0 KB"),
    (1024 * 1024,    "1.0 MB"),
    (int(8.3 * 1024 ** 2), "8.3 MB"),
    (int(1.5 * 1024 ** 3), "1.5 GB"),
])
def test_fmt_bytes(n, expected):
    assert fmt_bytes(n) == expected


# ---------------------------------------------------------------------------
# time_ago
# ---------------------------------------------------------------------------

def test_time_ago_seconds():
    now = datetime(2026, 4, 19, 12, 0, 30)
    dt  = datetime(2026, 4, 19, 12, 0, 10)
    assert time_ago(dt, now) == "20 seconds ago"


def test_time_ago_minutes():
    now = datetime(2026, 4, 19, 12, 30, 0)
    dt  = datetime(2026, 4, 19, 12, 0, 0)
    assert time_ago(dt, now) == "30 minutes ago"


def test_time_ago_hours():
    now = datetime(2026, 4, 19, 14, 0, 0)
    dt  = datetime(2026, 4, 19, 12, 0, 0)
    assert time_ago(dt, now) == "2 hours ago"


def test_time_ago_days():
    now = datetime(2026, 4, 19, 0, 0, 0)
    dt  = datetime(2026, 4, 16, 0, 0, 0)
    assert time_ago(dt, now) == "3 days ago"


def test_time_ago_singular():
    now = datetime(2026, 4, 19, 12, 1, 0)
    dt  = datetime(2026, 4, 19, 12, 0, 0)
    assert time_ago(dt, now) == "1 minute ago"


# ---------------------------------------------------------------------------
# _compute_storage — basic cases
# ---------------------------------------------------------------------------

def _make_file(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_compute_storage_single_snapshot(tmp_path):
    snap = tmp_path / "2026-01-01-000000"
    _make_file(snap / "a.txt", b"hello")
    _make_file(snap / "b.txt", b"world")

    virtual, actual, dedup_ok = _compute_storage([snap])
    assert virtual == 10
    assert actual == 10   # no hard links → all unique


def test_compute_storage_hard_links_save_space(tmp_path):
    snap1 = tmp_path / "snap1"
    snap2 = tmp_path / "snap2"
    src = snap1 / "shared.txt"
    _make_file(src, b"shared data")
    # Hard link the same inode into snap2
    (snap2).mkdir(parents=True, exist_ok=True)
    os.link(src, snap2 / "shared.txt")
    # One unique file in snap2
    _make_file(snap2 / "unique.txt", b"only in snap2")

    virtual, actual, dedup_ok = _compute_storage([snap1, snap2])
    # virtual = 11 + 11 + 13 = 35... wait:
    # snap1/shared.txt = 11 bytes, snap2/shared.txt = 11 bytes (same inode), snap2/unique.txt = 13
    assert virtual == 11 + 11 + 13   # 35 — every occurrence counted
    assert actual == 11 + 13          # 24 — shared inode counted once
    assert dedup_ok is True


def test_compute_storage_empty(tmp_path):
    virtual, actual, dedup_ok = _compute_storage([])
    assert virtual == 0
    assert actual == 0


# ---------------------------------------------------------------------------
# _read_snapshot_record — with backup_info.json
# ---------------------------------------------------------------------------

def test_read_record_with_info(tmp_path):
    snap = tmp_path / "2026-04-19-120000"
    snap.mkdir()
    info = {
        "total_files": 42,
        "total_bytes": 8388608,
        "success": True,
    }
    (snap / "backup_info.json").write_text(json.dumps(info))

    dt = datetime.strptime("2026-04-19-120000", _SNAPSHOT_FMT)
    rec = _read_snapshot_record(dt, snap)

    assert rec.files_total == 42
    assert rec.size_bytes == 8388608
    assert rec.success is True
    assert rec.has_info is True


def test_read_record_without_info(tmp_path):
    snap = tmp_path / "2026-04-19-120000"
    _make_file(snap / "f.txt", b"hello")

    dt = datetime.strptime("2026-04-19-120000", _SNAPSHOT_FMT)
    rec = _read_snapshot_record(dt, snap)

    assert rec.has_info is False
    assert rec.size_bytes == 5   # measured directly
    assert rec.files_total == 0


def test_read_record_corrupt_info(tmp_path):
    snap = tmp_path / "2026-04-19-120000"
    snap.mkdir()
    (snap / "backup_info.json").write_text("not json{{{")

    dt = datetime.strptime("2026-04-19-120000", _SNAPSHOT_FMT)
    rec = _read_snapshot_record(dt, snap)

    assert rec.has_info is False


# ---------------------------------------------------------------------------
# collect_status — end-to-end
# ---------------------------------------------------------------------------

def _create_snapshot(nas: Path, name: str, files: int, size: int, success: bool) -> None:
    d = nas / name
    d.mkdir(parents=True)
    info = {
        "total_files": files,
        "total_bytes": size,
        "success": success,
        "snapshot_name": name,
    }
    (d / "backup_info.json").write_text(json.dumps(info))


def test_collect_status_ordering(tmp_path):
    nas = tmp_path / "nas"
    _create_snapshot(nas, "2026-01-01-000000", 100, 1024, True)
    _create_snapshot(nas, "2026-03-15-120000", 110, 512,  True)
    _create_snapshot(nas, "2026-04-19-080000", 115, 256,  True)

    records, summary = collect_status(nas)

    assert len(records) == 3
    assert records[0].name == "2026-01-01-000000"
    assert records[-1].name == "2026-04-19-080000"
    assert summary.snapshot_count == 3


def test_collect_status_empty_dir(tmp_path):
    nas = tmp_path / "nas"
    nas.mkdir()
    records, summary = collect_status(nas)
    assert records == []
    assert summary.snapshot_count == 0
    assert summary.virtual_bytes == 0


def test_collect_status_savings_with_hard_links(tmp_path):
    nas = tmp_path / "nas"
    snap1 = nas / "2026-04-18-000000"
    snap2 = nas / "2026-04-19-000000"

    # snap1: two unique files
    _make_file(snap1 / "a.txt", b"a" * 1000)
    _make_file(snap1 / "b.txt", b"b" * 1000)
    # snap2: one hard link (a.txt shared), one new file
    snap2.mkdir(parents=True)
    os.link(snap1 / "a.txt", snap2 / "a.txt")
    _make_file(snap2 / "b.txt", b"b_new" * 200)

    # Add backup_info.json so snapshots are detected
    for snap in [snap1, snap2]:
        (snap / "backup_info.json").write_text(json.dumps({
            "total_files": 2, "total_bytes": 1000, "success": True,
        }))

    records, summary = collect_status(nas)

    # The hard-linked a.txt (1000 bytes) is counted twice in virtual but once in actual.
    # backup_info.json files are also counted, but those are unique per snapshot.
    # → savings = 1000 regardless of how many other files exist.
    assert summary.savings_bytes == 1000
    assert summary.virtual_bytes > summary.actual_bytes
    assert summary.savings_pct > 0
