"""
Backup session manager.

Wraps BackupEngine to provide a fully automated snapshot workflow:
  1. Check NAS reachability (raises NASUnavailableError on failure)
  2. Acquire exclusive lock (raises BackupAlreadyRunningError if busy)
  3. Scan the NAS base directory for existing timestamped snapshots
  4. Pick the latest one as previous_backup (or None for first run)
  5. Create a new snapshot folder named after the current time
  6. Run BackupEngine
  7. Write backup_info.json into the new snapshot folder
  8. Apply retention policy to prune old snapshots (if configured)
  9. Release lock
"""

from __future__ import annotations

import errno as _errno_module
import json
import logging
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .backup_engine import BackupEngine, BackupStats
from .errors import NASUnavailableError
from .lock import LOCK_FILENAME, backup_lock

log = logging.getLogger(__name__)

# Primary snapshot format: YYYY-MM-DD-HHMMSS
# Collision suffix: YYYY-MM-DD-HHMMSS-N  (N = 1, 2, …)
_SNAPSHOT_FMT = "%Y-%m-%d-%H%M%S"
_SNAPSHOT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}-\d{6})(?:-(\d+))?$")
_INFO_FILENAME = "backup_info.json"

# OSError errno values that indicate the NAS share is unreachable
_OFFLINE_ERRNOS = frozenset({
    _errno_module.EHOSTUNREACH,   # No route to host
    _errno_module.ETIMEDOUT,      # Connection timed out
    _errno_module.ECONNREFUSED,   # Connection refused
    _errno_module.ENETUNREACH,    # Network unreachable
})
# OSError errno values that indicate authentication / permission failure
_AUTH_ERRNOS = frozenset({
    _errno_module.EACCES,         # Permission denied
    _errno_module.EPERM,          # Operation not permitted
})
# Windows-specific winerror codes for offline / auth (winerror attr on OSError)
_WIN_OFFLINE_CODES = frozenset({53, 64, 67, 1231})   # bad net path / unreachable
_WIN_AUTH_CODES = frozenset({5, 1326, 1327})          # access denied / logon failure


def _check_nas_accessible(base_dir: Path) -> None:
    """
    Verify that *base_dir* can be created/accessed on the NAS.

    Raises:
        NASUnavailableError: when the share is offline or authentication fails.
    """
    try:
        base_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        winerr = getattr(exc, "winerror", None)
        if winerr in _WIN_OFFLINE_CODES or exc.errno in _OFFLINE_ERRNOS:
            raise NASUnavailableError(
                f"NAS is offline or unreachable: {base_dir} — {exc}"
            ) from exc
        if winerr in _WIN_AUTH_CODES or exc.errno in _AUTH_ERRNOS:
            raise NASUnavailableError(
                f"Authentication error accessing NAS: {base_dir} — {exc}"
            ) from exc
        raise


# ---------------------------------------------------------------------------
# Snapshot name parsing
# ---------------------------------------------------------------------------

def _parse_snapshot_name(name: str) -> tuple[datetime, int] | None:
    """
    Parse a snapshot folder name.

    Returns (datetime, counter) on success where counter is 0 for the
    unsuffixed form and N for the '-N' collision-avoidance suffix.
    Returns None if the name does not match the expected pattern.
    """
    m = _SNAPSHOT_RE.match(name)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1), _SNAPSHOT_FMT)
        counter = int(m.group(2)) if m.group(2) else 0
        return (dt, counter)
    except ValueError:
        return None


def find_snapshots(base_dir: Path) -> list[tuple[datetime, Path]]:
    """
    Return all valid snapshot sub-directories under *base_dir*, sorted
    oldest-first.  Entries whose names do not match the timestamp pattern
    are silently ignored.
    """
    results: list[tuple[tuple[datetime, int], Path]] = []
    if not base_dir.is_dir():
        return []
    for entry in base_dir.iterdir():
        if not entry.is_dir():
            continue
        parsed = _parse_snapshot_name(entry.name)
        if parsed is not None:
            results.append((parsed, entry))
    results.sort(key=lambda t: t[0])
    return [(dt, path) for (dt, _counter), path in results]


def find_latest_snapshot(base_dir: Path) -> Path | None:
    """Return the path of the most recent snapshot, or None if none exist."""
    snaps = find_snapshots(base_dir)
    return snaps[-1][1] if snaps else None


def _new_snapshot_name(base_dir: Path) -> str:
    """
    Return a snapshot folder name that does not yet exist under *base_dir*.

    Uses YYYY-MM-DD-HHMMSS; appends -1, -2, … on collision (sub-second
    rapid runs or manual re-runs within the same second).
    """
    base = datetime.now().strftime(_SNAPSHOT_FMT)
    name = base
    counter = 0
    while (base_dir / name).exists():
        counter += 1
        name = f"{base}-{counter}"
    return name


# ---------------------------------------------------------------------------
# Retention policy
# ---------------------------------------------------------------------------

@dataclass
class RetentionPolicy:
    """
    Rules for pruning old snapshots.

    Attributes:
        keep_count:   Always preserve this many of the most recent snapshots,
                      regardless of age.
        max_age_days: Delete snapshots older than this many days, but only
                      when they fall outside the keep_count window.
    """
    keep_count: int = 10
    max_age_days: float = 30


@dataclass
class CleanupStats:
    deleted: int = 0
    freed_bytes: int = 0   # bytes from files that had nlink==1 (sole reference)
    kept: int = 0          # old snapshots intentionally retained (not yet expired)
    errors: int = 0


def _freeable_bytes(snapshot_dir: Path) -> int:
    """
    Estimate bytes that will be freed when *snapshot_dir* is deleted.

    Only files whose hard-link count is 1 are the sole reference to their
    data; those blocks will be released by the OS when the directory entry
    is removed.  Files with nlink > 1 are shared with other snapshots and
    their data survives deletion of this snapshot.
    """
    total = 0
    for p in snapshot_dir.rglob("*"):
        if p.is_file() and not p.is_symlink():
            try:
                st = p.stat()
                if st.st_nlink == 1:
                    total += st.st_size
            except OSError:
                pass
    return total


def _delete_snapshot(snapshot_dir: Path, stats: CleanupStats) -> None:
    """
    Remove a snapshot directory tree.

    shutil.rmtree is hard-link safe: it removes directory entries one by
    one, decrementing each inode's link count.  Files shared with other
    snapshots (nlink > 1) are unaffected because their data is only freed
    when the last reference is removed.
    """
    freed = _freeable_bytes(snapshot_dir)
    try:
        shutil.rmtree(snapshot_dir)
        stats.deleted += 1
        stats.freed_bytes += freed
        log.info("Deleted snapshot: %s (freed ~%d bytes)", snapshot_dir.name, freed)
    except OSError as exc:
        log.error("Failed to delete snapshot %s: %s", snapshot_dir.name, exc)
        stats.errors += 1


# ---------------------------------------------------------------------------
# Backup info record
# ---------------------------------------------------------------------------

@dataclass
class BackupInfo:
    snapshot_name: str
    source: str
    started_at: str           # ISO-8601 UTC
    finished_at: str          # ISO-8601 UTC
    elapsed_seconds: float
    previous_snapshot: str | None
    files_copied: int
    files_linked: int
    errors: int
    total_files: int
    total_bytes: int          # bytes of files with nlink == 1 (non-shared)
    success: bool

    def write(self, snapshot_dir: Path) -> Path:
        out = snapshot_dir / _INFO_FILENAME
        out.write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        log.debug("Wrote %s", out)
        return out


def _measure_snapshot_bytes(snapshot_dir: Path) -> int:
    """
    Sum the sizes of files that are not shared via hard links (nlink == 1).

    Files with nlink > 1 are hard-linked to a previous snapshot and consume
    no additional storage, so they are excluded from the reported size.
    """
    total = 0
    for p in snapshot_dir.rglob("*"):
        if p.is_file() and not p.is_symlink():
            try:
                st = p.stat()
                if st.st_nlink == 1:
                    total += st.st_size
            except OSError:
                pass
    return total


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class BackupSession:
    """Result of a completed backup run."""
    info: BackupInfo
    snapshot_dir: Path
    info_path: Path
    cleanup: CleanupStats | None = None


class BackupManager:
    """
    High-level manager that orchestrates incremental snapshot backups.

    Args:
        source:    Local directory to back up.
        base_dir:  NAS base directory that holds all timestamped snapshots
                   (e.g. ``//NAS/Backup/MyPC``).
        use_hash:  Passed through to BackupEngine (SHA-256 vs mtime+size).
        retention: Optional retention policy.  When set, old snapshots are
                   pruned automatically at the end of each run().
    """

    def __init__(
        self,
        source: Path | str,
        base_dir: Path | str,
        *,
        use_hash: bool = False,
        retention: RetentionPolicy | None = None,
    ) -> None:
        self.source = Path(source)
        self.base_dir = Path(base_dir)
        self.use_hash = use_hash
        self.retention = retention

    # ------------------------------------------------------------------
    def run(self) -> BackupSession:
        """
        Execute one backup session and return a BackupSession.

        Raises:
            NASUnavailableError: if the NAS share cannot be reached or
                authentication fails.
            BackupAlreadyRunningError: if another backup process is
                already holding the lock.
            ValueError: if *source* is not a directory.
        """
        # Step 1: verify NAS reachability before acquiring the lock so we
        # fail fast without leaving a stale lock on the share.
        _check_nas_accessible(self.base_dir)

        lock_path = self.base_dir / LOCK_FILENAME
        with backup_lock(lock_path):
            return self._run_locked()

    def _run_locked(self) -> BackupSession:
        """Internal: called after the lock is held."""
        previous = find_latest_snapshot(self.base_dir)
        snapshot_name = _new_snapshot_name(self.base_dir)
        snapshot_dir = self.base_dir / snapshot_name

        log.info("Starting backup: %s → %s", self.source, snapshot_dir)
        if previous:
            log.info("Previous snapshot: %s", previous.name)
        else:
            log.info("No previous snapshot — performing full copy")

        started_at = datetime.now(tz=timezone.utc)
        t0 = time.monotonic()

        engine = BackupEngine(use_hash=self.use_hash)
        stats: BackupStats = engine.run(
            source=self.source,
            destination=snapshot_dir,
            previous_backup=previous,
        )

        elapsed = time.monotonic() - t0
        finished_at = datetime.now(tz=timezone.utc)
        total_bytes = _measure_snapshot_bytes(snapshot_dir)

        info = BackupInfo(
            snapshot_name=snapshot_name,
            source=str(self.source),
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            elapsed_seconds=round(elapsed, 3),
            previous_snapshot=previous.name if previous else None,
            files_copied=stats.copied,
            files_linked=stats.linked,
            errors=stats.errors,
            total_files=stats.copied + stats.linked,
            total_bytes=total_bytes,
            success=stats.errors == 0,
        )

        info_path = info.write(snapshot_dir)
        log.info(
            "Snapshot complete in %.1fs — copied: %d, linked: %d, bytes: %d, errors: %d",
            elapsed, stats.copied, stats.linked, total_bytes, stats.errors,
        )

        cleanup_stats: CleanupStats | None = None
        if self.retention is not None:
            cleanup_stats = self.cleanup_old_snapshots(self.retention)

        return BackupSession(
            info=info,
            snapshot_dir=snapshot_dir,
            info_path=info_path,
            cleanup=cleanup_stats,
        )

    # ------------------------------------------------------------------
    def cleanup_old_snapshots(
        self,
        policy: RetentionPolicy | None = None,
        *,
        _now: datetime | None = None,
    ) -> CleanupStats:
        """
        Prune old snapshots according to *policy*.

        Snapshots are evaluated oldest-first.  The *keep_count* most recent
        snapshots are always preserved.  Among the remainder, any snapshot
        whose timestamp is older than *max_age_days* is deleted.

        Args:
            policy: Retention policy to apply.  Falls back to
                    ``self.retention`` when omitted; uses the default
                    RetentionPolicy() if neither is set.
            _now:   Reference time for age calculation.  Intended for
                    testing; defaults to datetime.now().
        """
        policy = policy or self.retention or RetentionPolicy()
        now = _now or datetime.now()
        stats = CleanupStats()

        snapshots = find_snapshots(self.base_dir)  # oldest-first
        n = len(snapshots)

        # The newest keep_count snapshots are unconditionally protected.
        protected_from = max(0, n - policy.keep_count)
        candidates = snapshots[:protected_from]   # older than the protected window

        if not candidates:
            log.debug(
                "Retention: %d snapshot(s) present, all within keep_count=%d — nothing to prune",
                n, policy.keep_count,
            )
            return stats

        cutoff = now - timedelta(days=policy.max_age_days)
        log.info(
            "Retention: %d candidate(s) older than keep window; cutoff date = %s",
            len(candidates), cutoff.strftime("%Y-%m-%d"),
        )

        for snap_dt, snap_path in candidates:
            if snap_dt < cutoff:
                _delete_snapshot(snap_path, stats)
            else:
                log.debug(
                    "Keeping %s (age %.1f days < max_age_days=%.1f)",
                    snap_path.name,
                    (now - snap_dt).total_seconds() / 86400,
                    policy.max_age_days,
                )
                stats.kept += 1

        log.info(
            "Cleanup done — deleted: %d, freed: ~%d bytes, kept: %d, errors: %d",
            stats.deleted, stats.freed_bytes, stats.kept, stats.errors,
        )
        return stats


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _main() -> None:
    import argparse
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )

    parser = argparse.ArgumentParser(
        description="U-Back — run one backup session",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  python -m uback.manager C:\\Users\\you\\Documents \\\\NAS\\Backup\\Documents\n"
            "  python -m uback.manager C:\\Users\\you\\Documents \\\\NAS\\Backup\\Documents"
            " --keep 10 --max-age 30\n"
        ),
    )
    parser.add_argument("source", help="Local directory to back up")
    parser.add_argument("base_dir", help="NAS base directory for all snapshots")
    parser.add_argument("--hash", action="store_true",
                        help="Use SHA-256 for change detection instead of mtime+size")
    parser.add_argument("--keep", type=int, default=None, metavar="N",
                        help="Always keep the N most recent snapshots (default: no pruning)")
    parser.add_argument("--max-age", type=float, default=30, metavar="DAYS",
                        help="Delete snapshots older than DAYS (default: 30, requires --keep)")
    args = parser.parse_args()

    retention = None
    if args.keep is not None:
        retention = RetentionPolicy(keep_count=args.keep, max_age_days=args.max_age)

    manager = BackupManager(
        source=args.source,
        base_dir=args.base_dir,
        use_hash=args.hash,
        retention=retention,
    )
    session = manager.run()
    info = session.info

    print(f"\nSnapshot : {info.snapshot_name}")
    print(f"Previous : {info.previous_snapshot or '(none — first backup)'}")
    print(f"Copied   : {info.files_copied}")
    print(f"Linked   : {info.files_linked}")
    print(f"Errors   : {info.errors}")
    print(f"Size     : {info.total_bytes:,} bytes")
    print(f"Elapsed  : {info.elapsed_seconds:.1f}s")
    print(f"Success  : {info.success}")

    if session.cleanup is not None:
        c = session.cleanup
        print(f"\nCleanup  : deleted={c.deleted}, freed=~{c.freed_bytes:,} bytes, "
              f"kept={c.kept}, errors={c.errors}")

    print(f"Info     : {session.info_path}")


if __name__ == "__main__":
    _main()
