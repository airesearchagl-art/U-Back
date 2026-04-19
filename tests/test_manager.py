"""Tests for BackupManager and snapshot helpers."""

import json
import os
import time
from pathlib import Path

import pytest

from uback.manager import (
    BackupManager,
    _SNAPSHOT_FMT,
    _parse_snapshot_name,
    find_latest_snapshot,
    find_snapshots,
)


# ---------------------------------------------------------------------------
# _parse_snapshot_name
# ---------------------------------------------------------------------------

def test_parse_valid_snapshot_name():
    result = _parse_snapshot_name("2026-04-18-230000")
    assert result is not None
    dt, counter = result
    assert dt.year == 2026
    assert dt.month == 4
    assert dt.day == 18
    assert dt.hour == 23
    assert counter == 0


def test_parse_suffixed_snapshot_name():
    result = _parse_snapshot_name("2026-04-18-230000-3")
    assert result is not None
    dt, counter = result
    assert dt.hour == 23
    assert counter == 3


def test_parse_rejects_arbitrary_names():
    assert _parse_snapshot_name("latest") is None
    assert _parse_snapshot_name("2026-04-18") is None
    assert _parse_snapshot_name("backup_2026") is None
    assert _parse_snapshot_name("") is None


# ---------------------------------------------------------------------------
# find_snapshots / find_latest_snapshot
# ---------------------------------------------------------------------------

def test_find_snapshots_sorted(tmp_path):
    for name in ["2026-01-01-000000", "2026-03-15-120000", "2026-01-20-090000"]:
        (tmp_path / name).mkdir()

    snaps = find_snapshots(tmp_path)
    names = [p.name for _, p in snaps]
    assert names == ["2026-01-01-000000", "2026-01-20-090000", "2026-03-15-120000"]


def test_find_snapshots_ignores_non_matching(tmp_path):
    (tmp_path / "2026-04-01-000000").mkdir()
    (tmp_path / "latest").mkdir()
    (tmp_path / "backup_info.json").write_text("{}")

    snaps = find_snapshots(tmp_path)
    assert len(snaps) == 1
    assert snaps[0][1].name == "2026-04-01-000000"


def test_find_latest_snapshot_returns_newest(tmp_path):
    for name in ["2026-01-01-000000", "2026-06-30-235959", "2026-03-01-080000"]:
        (tmp_path / name).mkdir()

    latest = find_latest_snapshot(tmp_path)
    assert latest is not None
    assert latest.name == "2026-06-30-235959"


def test_find_latest_snapshot_empty_dir(tmp_path):
    assert find_latest_snapshot(tmp_path) is None


def test_find_latest_snapshot_nonexistent_dir(tmp_path):
    assert find_latest_snapshot(tmp_path / "missing") is None


# ---------------------------------------------------------------------------
# BackupManager — first run (no previous snapshot)
# ---------------------------------------------------------------------------

def make_source(base: Path, files: dict[str, bytes]) -> Path:
    src = base / "source"
    for rel, data in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return src


def test_first_run_creates_snapshot(tmp_path):
    src = make_source(tmp_path, {"a.txt": b"hello", "sub/b.txt": b"world"})
    nas = tmp_path / "nas"

    session = BackupManager(src, nas).run()

    assert session.snapshot_dir.is_dir()
    assert (session.snapshot_dir / "a.txt").read_bytes() == b"hello"
    assert (session.snapshot_dir / "sub" / "b.txt").read_bytes() == b"world"


def test_first_run_writes_backup_info(tmp_path):
    src = make_source(tmp_path, {"x.txt": b"data"})
    nas = tmp_path / "nas"

    session = BackupManager(src, nas).run()

    info_path = session.snapshot_dir / "backup_info.json"
    assert info_path.exists()
    info = json.loads(info_path.read_text())

    assert info["success"] is True
    assert info["previous_snapshot"] is None
    assert info["files_copied"] == 1
    assert info["files_linked"] == 0
    assert info["errors"] == 0
    assert info["total_files"] == 1
    assert info["total_bytes"] > 0
    assert "started_at" in info
    assert "finished_at" in info
    assert "elapsed_seconds" in info


# ---------------------------------------------------------------------------
# BackupManager — second run (links unchanged, copies changed)
# ---------------------------------------------------------------------------

def _same_inode(a: Path, b: Path) -> bool:
    return a.stat().st_ino == b.stat().st_ino


def test_second_run_links_unchanged(tmp_path):
    src = make_source(tmp_path, {"keep.txt": b"keep", "change.txt": b"v1"})
    nas = tmp_path / "nas"

    s1 = BackupManager(src, nas).run()

    (src / "change.txt").write_bytes(b"v2")

    s2 = BackupManager(src, nas).run()

    info = json.loads((s2.snapshot_dir / "backup_info.json").read_text())
    assert info["files_linked"] == 1
    assert info["files_copied"] == 1
    assert _same_inode(s1.snapshot_dir / "keep.txt", s2.snapshot_dir / "keep.txt")
    assert not _same_inode(s1.snapshot_dir / "change.txt", s2.snapshot_dir / "change.txt")


def test_second_run_previous_snapshot_field(tmp_path):
    src = make_source(tmp_path, {"f.txt": b"data"})
    nas = tmp_path / "nas"

    s1 = BackupManager(src, nas).run()
    time.sleep(0.01)  # ensure different timestamp
    s2 = BackupManager(src, nas).run()

    info = json.loads((s2.snapshot_dir / "backup_info.json").read_text())
    assert info["previous_snapshot"] == s1.snapshot_dir.name


# ---------------------------------------------------------------------------
# BackupManager — multiple runs accumulate snapshots
# ---------------------------------------------------------------------------

def test_multiple_runs_accumulate_snapshots(tmp_path):
    src = make_source(tmp_path, {"f.txt": b"v1"})
    nas = tmp_path / "nas"

    for i in range(3):
        time.sleep(0.01)
        (src / "f.txt").write_bytes(f"v{i}".encode())
        BackupManager(src, nas).run()

    snaps = find_snapshots(nas)
    assert len(snaps) == 3


# ---------------------------------------------------------------------------
# BackupManager — source dir does not exist
# ---------------------------------------------------------------------------

def test_missing_source_raises(tmp_path):
    with pytest.raises(ValueError, match="source is not a directory"):
        BackupManager(tmp_path / "no_such_dir", tmp_path / "nas").run()
