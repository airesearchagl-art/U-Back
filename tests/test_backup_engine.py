"""Tests for the incremental backup engine (Linux-compatible)."""

import os
import time
from pathlib import Path

import pytest

from uback.backup_engine import BackupEngine, _sha256, _signature


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_tree(base: Path, files: dict[str, bytes]) -> None:
    """Write a dict of relative_path → content into base."""
    for rel, content in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)


def same_inode(a: Path, b: Path) -> bool:
    return a.stat().st_ino == b.stat().st_ino


# ---------------------------------------------------------------------------
# First backup (no previous) — everything must be copied
# ---------------------------------------------------------------------------

def test_first_backup_copies_all(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    make_tree(src, {
        "a.txt": b"hello",
        "sub/b.txt": b"world",
    })

    stats = BackupEngine().run(src, dst)

    assert stats.copied == 2
    assert stats.linked == 0
    assert stats.errors == 0
    assert (dst / "a.txt").read_bytes() == b"hello"
    assert (dst / "sub" / "b.txt").read_bytes() == b"world"


# ---------------------------------------------------------------------------
# Second backup — unchanged files linked, changed file copied
# ---------------------------------------------------------------------------

def test_unchanged_files_are_hard_linked(tmp_path):
    src = tmp_path / "src"
    prev = tmp_path / "prev"
    dst = tmp_path / "dst"

    make_tree(src, {"a.txt": b"hello", "b.txt": b"world"})
    # Simulate previous backup by copying
    BackupEngine().run(src, prev)

    stats = BackupEngine().run(src, dst, previous_backup=prev)

    assert stats.errors == 0
    assert stats.copied + stats.linked == 2
    # At least one file should share inode with previous (hard linked)
    assert same_inode(prev / "a.txt", dst / "a.txt")
    assert same_inode(prev / "b.txt", dst / "b.txt")


def test_changed_file_is_copied_not_linked(tmp_path):
    src = tmp_path / "src"
    prev = tmp_path / "prev"
    dst = tmp_path / "dst"

    make_tree(src, {"keep.txt": b"same", "change.txt": b"original"})
    BackupEngine().run(src, prev)

    # Modify one file; bump mtime so signature differs
    (src / "change.txt").write_bytes(b"modified")

    stats = BackupEngine().run(src, dst, previous_backup=prev)

    assert stats.errors == 0
    # keep.txt → linked; change.txt → copied (different inode)
    assert same_inode(prev / "keep.txt", dst / "keep.txt")
    assert not same_inode(prev / "change.txt", dst / "change.txt")
    assert (dst / "change.txt").read_bytes() == b"modified"
    assert stats.linked == 1
    assert stats.copied == 1


# ---------------------------------------------------------------------------
# New file in source after previous backup
# ---------------------------------------------------------------------------

def test_new_file_is_copied(tmp_path):
    src = tmp_path / "src"
    prev = tmp_path / "prev"
    dst = tmp_path / "dst"

    make_tree(src, {"old.txt": b"old"})
    BackupEngine().run(src, prev)

    make_tree(src, {"new.txt": b"brand new"})

    stats = BackupEngine().run(src, dst, previous_backup=prev)

    assert (dst / "new.txt").read_bytes() == b"brand new"
    assert stats.copied >= 1
    assert stats.errors == 0


# ---------------------------------------------------------------------------
# SHA-256 mode
# ---------------------------------------------------------------------------

def test_hash_mode_detects_change(tmp_path):
    src = tmp_path / "src"
    prev = tmp_path / "prev"
    dst = tmp_path / "dst"

    make_tree(src, {"f.txt": b"data"})
    BackupEngine(use_hash=True).run(src, prev)

    # Touch the file to change mtime but keep content identical
    p = src / "f.txt"
    os.utime(p, (time.time() + 10, time.time() + 10))

    stats = BackupEngine(use_hash=True).run(src, dst, previous_backup=prev)

    # Content unchanged → should be linked despite mtime difference
    assert same_inode(prev / "f.txt", dst / "f.txt")
    assert stats.linked == 1


# ---------------------------------------------------------------------------
# Nested directories
# ---------------------------------------------------------------------------

def test_nested_structure_preserved(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    make_tree(src, {
        "a/b/c/deep.txt": b"deep",
        "x/y.txt": b"shallow",
    })

    BackupEngine().run(src, dst)

    assert (dst / "a" / "b" / "c" / "deep.txt").read_bytes() == b"deep"
    assert (dst / "x" / "y.txt").read_bytes() == b"shallow"


# ---------------------------------------------------------------------------
# Utility: _signature and _sha256
# ---------------------------------------------------------------------------

def test_signature_differs_after_modification(tmp_path):
    f = tmp_path / "f.txt"
    f.write_bytes(b"v1")
    sig1 = _signature(f)
    time.sleep(0.01)
    f.write_bytes(b"v2")
    sig2 = _signature(f)
    assert sig1 != sig2


def test_sha256_stable(tmp_path):
    f = tmp_path / "f.txt"
    g = tmp_path / "g.txt"
    f.write_bytes(b"hello world")
    g.write_bytes(b"different content")
    assert _sha256(f) == _sha256(f)
    assert _sha256(f) != _sha256(g)
