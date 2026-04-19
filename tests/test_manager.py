"""Tests for BackupManager and snapshot helpers."""

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from uback.manager import (
    BackupManager,
    CleanupStats,
    RetentionPolicy,
    _SNAPSHOT_FMT,
    _freeable_bytes,
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


# ===========================================================================
# Retention / cleanup tests
# ===========================================================================

def _make_snapshots(nas: Path, names: list[str]) -> None:
    """Create dummy snapshot directories with a single file each."""
    for name in names:
        d = nas / name
        d.mkdir(parents=True)
        (d / "file.txt").write_bytes(b"data")


def _snap_names(nas: Path) -> list[str]:
    return sorted(p.name for _, p in find_snapshots(nas))


# ---------------------------------------------------------------------------
# _freeable_bytes
# ---------------------------------------------------------------------------

def test_freeable_bytes_sole_file(tmp_path):
    f = tmp_path / "f.txt"
    f.write_bytes(b"hello")
    assert _freeable_bytes(tmp_path) == 5


def test_freeable_bytes_excludes_hardlinked(tmp_path):
    src = tmp_path / "src.txt"
    src.write_bytes(b"shared")
    link = tmp_path / "link.txt"
    os.link(src, link)
    # Both nlink==2 → neither counted as freeable
    assert _freeable_bytes(tmp_path) == 0


# ---------------------------------------------------------------------------
# cleanup_old_snapshots — basic deletion
# ---------------------------------------------------------------------------

def test_cleanup_deletes_expired_snapshots(tmp_path):
    nas = tmp_path / "nas"
    # Old snapshots (35 days ago)
    old_names = ["2026-01-01-000000", "2026-01-02-000000"]
    # Recent snapshots (today-ish, won't be expired)
    recent_names = ["2026-04-19-000000", "2026-04-19-000001"]
    _make_snapshots(nas, old_names + recent_names)

    src = make_source(tmp_path, {"x.txt": b"x"})
    policy = RetentionPolicy(keep_count=2, max_age_days=30)
    mgr = BackupManager(src, nas)

    now = datetime(2026, 4, 19, 12, 0, 0)
    stats = mgr.cleanup_old_snapshots(policy, _now=now)

    assert stats.deleted == 2
    assert stats.kept == 0
    assert stats.errors == 0
    remaining = _snap_names(nas)
    assert "2026-01-01-000000" not in remaining
    assert "2026-01-02-000000" not in remaining
    assert "2026-04-19-000000" in remaining
    assert "2026-04-19-000001" in remaining


def test_cleanup_keeps_recent_outside_window(tmp_path):
    """Snapshots beyond keep_count but not yet expired should be kept."""
    nas = tmp_path / "nas"
    # 20 days old — beyond keep_count=2 but within max_age_days=30
    _make_snapshots(nas, [
        "2026-03-30-000000",  # 20 days old
        "2026-04-15-000000",  # 4 days old
        "2026-04-19-000000",  # today
    ])

    src = make_source(tmp_path, {"x.txt": b"x"})
    mgr = BackupManager(src, nas)
    policy = RetentionPolicy(keep_count=2, max_age_days=30)

    now = datetime(2026, 4, 19, 12, 0, 0)
    stats = mgr.cleanup_old_snapshots(policy, _now=now)

    assert stats.deleted == 0
    assert stats.kept == 1          # "2026-03-30-000000" is old but not expired
    assert len(_snap_names(nas)) == 3


# ---------------------------------------------------------------------------
# cleanup_old_snapshots — keep_count protects newest N
# ---------------------------------------------------------------------------

def test_cleanup_never_deletes_within_keep_count(tmp_path):
    nas = tmp_path / "nas"
    # All 5 are "old" (100 days ago), but keep_count=5 protects all of them
    _make_snapshots(nas, [
        "2026-01-01-000000",
        "2026-01-02-000000",
        "2026-01-03-000000",
        "2026-01-04-000000",
        "2026-01-05-000000",
    ])

    src = make_source(tmp_path, {"x.txt": b"x"})
    mgr = BackupManager(src, nas)
    policy = RetentionPolicy(keep_count=5, max_age_days=1)

    now = datetime(2026, 4, 19, 0, 0, 0)
    stats = mgr.cleanup_old_snapshots(policy, _now=now)

    assert stats.deleted == 0
    assert len(_snap_names(nas)) == 5


def test_cleanup_deletes_only_beyond_keep_count(tmp_path):
    nas = tmp_path / "nas"
    # 6 old snapshots, keep_count=4 → oldest 2 are candidates
    names = [f"2026-01-0{i}-000000" for i in range(1, 7)]
    _make_snapshots(nas, names)

    src = make_source(tmp_path, {"x.txt": b"x"})
    mgr = BackupManager(src, nas)
    policy = RetentionPolicy(keep_count=4, max_age_days=1)

    now = datetime(2026, 4, 19, 0, 0, 0)
    stats = mgr.cleanup_old_snapshots(policy, _now=now)

    assert stats.deleted == 2
    remaining = _snap_names(nas)
    assert len(remaining) == 4
    # Oldest two should be gone
    assert "2026-01-01-000000" not in remaining
    assert "2026-01-02-000000" not in remaining


# ---------------------------------------------------------------------------
# BackupManager.run() auto-cleanup integration
# ---------------------------------------------------------------------------

def test_run_triggers_cleanup_when_retention_set(tmp_path):
    src = make_source(tmp_path, {"f.txt": b"data"})
    nas = tmp_path / "nas"

    # Pre-populate 3 old snapshots (100 days old)
    _make_snapshots(nas, [
        "2026-01-01-000000",
        "2026-01-02-000000",
        "2026-01-03-000000",
    ])

    policy = RetentionPolicy(keep_count=2, max_age_days=30)
    session = BackupManager(src, nas, retention=policy).run()

    assert session.cleanup is not None
    # After run: 4 snapshots total (3 old + 1 new).
    # keep_count=2 protects newest 2 → 2 candidates → both >30 days → deleted.
    assert session.cleanup.deleted == 2
    snaps = find_snapshots(nas)
    assert len(snaps) == 2


def test_run_no_cleanup_when_retention_is_none(tmp_path):
    src = make_source(tmp_path, {"f.txt": b"data"})
    nas = tmp_path / "nas"

    session = BackupManager(src, nas, retention=None).run()

    assert session.cleanup is None


# ---------------------------------------------------------------------------
# freed_bytes tracking
# ---------------------------------------------------------------------------

def test_cleanup_reports_freed_bytes(tmp_path):
    nas = tmp_path / "nas"
    old = nas / "2026-01-01-000000"
    old.mkdir(parents=True)
    (old / "unique.txt").write_bytes(b"x" * 1000)  # nlink==1 → will be freed

    src = make_source(tmp_path, {"f.txt": b"y"})
    mgr = BackupManager(src, nas)
    policy = RetentionPolicy(keep_count=0, max_age_days=1)

    now = datetime(2026, 4, 19, 0, 0, 0)
    stats = mgr.cleanup_old_snapshots(policy, _now=now)

    assert stats.deleted == 1
    assert stats.freed_bytes == 1000
