"""
Backup session manager.

Wraps BackupEngine to provide a fully automated snapshot workflow:
  1. Scan the NAS base directory for existing timestamped snapshots
  2. Pick the latest one as previous_backup (or None for first run)
  3. Create a new snapshot folder named after the current time
  4. Run BackupEngine
  5. Write backup_info.json into the new snapshot folder
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .backup_engine import BackupEngine, BackupStats

log = logging.getLogger(__name__)

# Primary snapshot format: YYYY-MM-DD-HHMMSS
# Collision suffix: YYYY-MM-DD-HHMMSS-N  (N = 1, 2, …)
_SNAPSHOT_FMT = "%Y-%m-%d-%H%M%S"
_SNAPSHOT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}-\d{6})(?:-(\d+))?$")
_INFO_FILENAME = "backup_info.json"


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
    # Return without the internal counter in the public tuple
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


class BackupManager:
    """
    High-level manager that orchestrates incremental snapshot backups.

    Args:
        source:    Local directory to back up.
        base_dir:  NAS base directory that holds all timestamped snapshots
                   (e.g. ``//NAS/Backup/MyPC``).
        use_hash:  Passed through to BackupEngine (SHA-256 vs mtime+size).
    """

    def __init__(
        self,
        source: Path | str,
        base_dir: Path | str,
        *,
        use_hash: bool = False,
    ) -> None:
        self.source = Path(source)
        self.base_dir = Path(base_dir)
        self.use_hash = use_hash

    def run(self) -> BackupSession:
        """Execute one backup session and return a BackupSession."""
        self.base_dir.mkdir(parents=True, exist_ok=True)

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

        return BackupSession(info=info, snapshot_dir=snapshot_dir, info_path=info_path)


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
        ),
    )
    parser.add_argument("source", help="Local directory to back up")
    parser.add_argument("base_dir", help="NAS base directory for all snapshots")
    parser.add_argument(
        "--hash", action="store_true",
        help="Use SHA-256 for change detection instead of mtime+size",
    )
    args = parser.parse_args()

    manager = BackupManager(
        source=args.source,
        base_dir=args.base_dir,
        use_hash=args.hash,
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
    print(f"Info     : {session.info_path}")


if __name__ == "__main__":
    _main()
