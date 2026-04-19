"""
Core incremental backup engine with hard link support.

For each file in source:
  - If unchanged vs previous backup → create hard link in destination
  - If new or changed             → copy file to destination
"""

from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import platform
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# File identity helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FileSignature:
    mtime_ns: int
    size: int


def _signature(path: Path) -> FileSignature:
    st = path.stat()
    return FileSignature(mtime_ns=st.st_mtime_ns, size=st.st_size)


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while data := f.read(chunk):
            h.update(data)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Hard link creation (NTFS / SMB aware)
# ---------------------------------------------------------------------------

def _create_hardlink_windows(src: Path, dst: Path) -> bool:
    """Use Win32 CreateHardLinkW for NTFS and SMB shares."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ok = kernel32.CreateHardLinkW(str(dst), str(src), None)
    if not ok:
        err = ctypes.get_last_error()
        log.debug("CreateHardLinkW failed (error %d): %s → %s", err, src, dst)
    return bool(ok)


def _create_hardlink(src: Path, dst: Path) -> bool:
    """Create a hard link dst → src. Returns True on success."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        if platform.system() == "Windows":
            return _create_hardlink_windows(src, dst)
        os.link(src, dst)
        return True
    except (OSError, NotImplementedError) as exc:
        log.debug("Hard link failed (%s): %s → %s", exc, src, dst)
        return False


# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------

@dataclass
class BackupStats:
    copied: int = 0
    linked: int = 0
    errors: int = 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class BackupEngine:
    """
    Incremental backup with hard links.

    Args:
        use_hash: When True, compare SHA-256 for change detection (slower but
                  accurate across FAT→NTFS copies that reset mtime).
                  When False, compare mtime_ns + size (fast, good for local NTFS).
    """

    def __init__(self, use_hash: bool = False) -> None:
        self.use_hash = use_hash

    def run(
        self,
        source: Path | str,
        destination: Path | str,
        previous_backup: Path | str | None = None,
    ) -> BackupStats:
        source = Path(source)
        destination = Path(destination)
        previous_backup = Path(previous_backup) if previous_backup else None

        if not source.is_dir():
            raise ValueError(f"source is not a directory: {source}")
        destination.mkdir(parents=True, exist_ok=True)

        stats = BackupStats()
        self._walk(source, destination, previous_backup, stats)
        log.info(
            "Backup done — copied: %d, linked: %d, errors: %d",
            stats.copied, stats.linked, stats.errors,
        )
        return stats

    def _walk(
        self,
        src_dir: Path,
        dst_dir: Path,
        prev_dir: Path | None,
        stats: BackupStats,
    ) -> None:
        try:
            entries = list(src_dir.iterdir())
        except PermissionError as exc:
            log.warning("Cannot read directory (%s): %s", exc, src_dir)
            stats.errors += 1
            return

        for entry in entries:
            dst_entry = dst_dir / entry.name
            prev_entry = (prev_dir / entry.name) if prev_dir else None

            if entry.is_symlink():
                log.debug("Skipping symlink: %s", entry)
                continue

            if entry.is_dir():
                dst_entry.mkdir(parents=True, exist_ok=True)
                prev_sub = prev_entry if (prev_entry and prev_entry.is_dir()) else None
                self._walk(entry, dst_entry, prev_sub, stats)

            elif entry.is_file():
                self._handle_file(entry, dst_entry, prev_entry, stats)

    def _handle_file(
        self,
        src_file: Path,
        dst_file: Path,
        prev_file: Path | None,
        stats: BackupStats,
    ) -> None:
        if prev_file and prev_file.is_file() and self._is_unchanged(src_file, prev_file):
            if _create_hardlink(prev_file, dst_file):
                log.debug("Linked:  %s", src_file)
                stats.linked += 1
                return
            # Hard link unavailable (e.g. cross-volume SMB share); fall back to copy
            log.debug("Hard link unavailable, copying: %s", src_file)

        self._copy_file(src_file, dst_file, stats)

    def _is_unchanged(self, src: Path, prev: Path) -> bool:
        if self.use_hash:
            return _sha256(src) == _sha256(prev)
        return _signature(src) == _signature(prev)

    @staticmethod
    def _copy_file(src: Path, dst: Path, stats: BackupStats) -> None:
        try:
            shutil.copy2(src, dst)
            log.debug("Copied:  %s", src)
            stats.copied += 1
        except (OSError, shutil.Error) as exc:
            log.error("Failed to copy %s → %s: %s", src, dst, exc)
            stats.errors += 1


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _main() -> None:
    import argparse
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stdout,
    )

    parser = argparse.ArgumentParser(description="U-Back incremental backup prototype")
    parser.add_argument("source", help="Backup source directory")
    parser.add_argument("destination", help="New backup snapshot directory")
    parser.add_argument("--previous", "-p", default=None,
                        help="Previous snapshot directory (enables hard linking)")
    parser.add_argument("--hash", action="store_true",
                        help="Use SHA-256 for change detection instead of mtime+size")
    args = parser.parse_args()

    engine = BackupEngine(use_hash=args.hash)
    stats = engine.run(
        source=args.source,
        destination=args.destination,
        previous_backup=args.previous,
    )
    print(f"\nFiles copied : {stats.copied}")
    print(f"Files linked : {stats.linked}")
    print(f"Errors       : {stats.errors}")


if __name__ == "__main__":
    _main()
