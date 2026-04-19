"""
Backup status collection and storage savings analysis.

Reads backup_info.json from every snapshot folder and computes:
  - per-snapshot metadata (files, size, success flag)
  - virtual total size (as if every snapshot were a naive full copy)
  - actual disk usage  (inode-dedup'd: shared blocks counted once)
  - hard-link savings  (virtual − actual)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .manager import _INFO_FILENAME, find_snapshots

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SnapshotRecord:
    name: str
    dt: datetime
    files_total: int
    size_bytes: int   # total_bytes from backup_info (nlink==1 at backup time)
    success: bool
    has_info: bool    # False when backup_info.json is absent


@dataclass
class StorageSummary:
    snapshot_count: int
    virtual_bytes: int   # apparent total if every snapshot were a full copy
    actual_bytes: int    # unique-inode dedup'd true disk usage
    savings_bytes: int
    savings_pct: float   # 0.0 – 100.0
    inode_dedup_available: bool  # False on filesystems that report ino==0


# ---------------------------------------------------------------------------
# Formatting helpers (pure functions, easy to test)
# ---------------------------------------------------------------------------

def fmt_bytes(n: int) -> str:
    """Return a human-readable byte size string, e.g. '8.3 MB'."""
    v = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} TB"  # unreachable but satisfies type checker


def time_ago(dt: datetime, now: datetime | None = None) -> str:
    """Return a human-readable relative time, e.g. '3 hours ago'."""
    now = now or datetime.now()
    secs = max(0, int((now - dt).total_seconds()))
    if secs < 60:
        return f"{secs} second{'s' if secs != 1 else ''} ago"
    if secs < 3600:
        m = secs // 60
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if secs < 86400:
        h = secs // 3600
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = secs // 86400
    return f"{d} day{'s' if d != 1 else ''} ago"


# ---------------------------------------------------------------------------
# Snapshot metadata reader
# ---------------------------------------------------------------------------

def _read_snapshot_record(dt: datetime, snap_dir: Path) -> SnapshotRecord:
    info_path = snap_dir / _INFO_FILENAME
    if not info_path.exists():
        # Fallback: measure the directory directly
        size = sum(
            p.stat().st_size
            for p in snap_dir.rglob("*")
            if p.is_file() and not p.is_symlink()
        )
        return SnapshotRecord(
            name=snap_dir.name,
            dt=dt,
            files_total=0,
            size_bytes=size,
            success=True,
            has_info=False,
        )

    try:
        data = json.loads(info_path.read_text(encoding="utf-8"))
        return SnapshotRecord(
            name=snap_dir.name,
            dt=dt,
            files_total=data.get("total_files", 0),
            size_bytes=data.get("total_bytes", 0),
            success=data.get("success", True),
            has_info=True,
        )
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read %s: %s", info_path, exc)
        return SnapshotRecord(
            name=snap_dir.name, dt=dt,
            files_total=0, size_bytes=0,
            success=False, has_info=False,
        )


# ---------------------------------------------------------------------------
# Storage computation
# ---------------------------------------------------------------------------

def _compute_storage(
    snapshot_dirs: list[Path],
) -> tuple[int, int, bool]:
    """
    Walk all snapshot directories and compute virtual and actual storage.

    Returns:
        (virtual_bytes, actual_bytes, inode_dedup_available)

    virtual_bytes:
        Sum of every file's size across every snapshot, treating each
        occurrence as a unique copy — what a naive full-backup strategy
        would consume.

    actual_bytes:
        Same walk, but each (device, inode) pair is counted only once.
        Files shared via hard links contribute their bytes exactly once.

    inode_dedup_available:
        False when the filesystem reports ino=0 for all files (some SMB
        configurations), in which case actual_bytes == virtual_bytes and
        savings cannot be measured via inode tracking.
    """
    virtual = 0
    actual = 0
    seen: set[tuple[int, int]] = set()
    nonzero_inodes = 0
    total_files = 0

    for snap_dir in snapshot_dirs:
        for p in snap_dir.rglob("*"):
            if not (p.is_file() and not p.is_symlink()):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            total_files += 1
            virtual += st.st_size
            key = (st.st_dev, st.st_ino)
            if st.st_ino != 0:
                nonzero_inodes += 1
            if key not in seen:
                seen.add(key)
                actual += st.st_size

    # Consider inode dedup meaningful only when most files report non-zero inodes
    dedup_ok = total_files == 0 or (nonzero_inodes / total_files) >= 0.5
    return virtual, actual, dedup_ok


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def collect_status(
    base_dir: Path,
) -> tuple[list[SnapshotRecord], StorageSummary]:
    """
    Collect status for all snapshots under *base_dir*.

    Returns:
        (records, summary) where records are sorted oldest-first.
    """
    snaps = find_snapshots(base_dir)   # [(datetime, Path), ...] oldest-first

    records = [_read_snapshot_record(dt, path) for dt, path in snaps]

    snap_dirs = [path for _, path in snaps]
    virtual, actual, dedup_ok = _compute_storage(snap_dirs)
    savings = max(0, virtual - actual)
    pct = (savings / virtual * 100) if virtual > 0 else 0.0

    summary = StorageSummary(
        snapshot_count=len(snaps),
        virtual_bytes=virtual,
        actual_bytes=actual,
        savings_bytes=savings,
        savings_pct=round(pct, 1),
        inode_dedup_available=dedup_ok,
    )
    return records, summary
