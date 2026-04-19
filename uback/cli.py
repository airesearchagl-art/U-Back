"""
U-Back unified command-line interface.

Subcommands
-----------
run        Run one backup session immediately.
status     Show snapshot history and storage savings.
schedule   Register a recurring backup task with Windows Task Scheduler.
unschedule Remove a previously registered Task Scheduler task.

Examples
--------
  python -m uback.cli run --source C:\\Users\\you\\Documents --base-dir \\\\NAS\\Backup\\Docs
  python -m uback.cli run --source ... --base-dir ... --keep 10 --max-age 30 --notify
  python -m uback.cli status --base-dir \\\\NAS\\Backup\\Docs
  python -m uback.cli schedule --source ... --base-dir ... --interval 60
  python -m uback.cli unschedule --base-dir \\\\NAS\\Backup\\Docs
"""

from __future__ import annotations

import argparse
import logging
import platform
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import notify
from .errors import BackupAlreadyRunningError, BackupError, NASUnavailableError
from .manager import BackupManager, RetentionPolicy
from .status import collect_status, fmt_bytes, time_ago

log = logging.getLogger(__name__)

_TASK_PREFIX = "U-Back"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _task_name(base_dir: str) -> str:
    """
    Derive a stable Task Scheduler task name from base_dir.

    ``\\\\NAS\\Backup\\Docs`` → ``U-Back\\Backup-Docs``
    """
    parts = [p for p in re.split(r"[\\/]+", base_dir.strip("\\/")) if p]
    label = "-".join(parts[-2:]) if len(parts) >= 2 else (parts[0] if parts else "default")
    label = re.sub(r'[<>:"/|?*]', "_", label)
    return f"{_TASK_PREFIX}\\{label}"


def _require_windows() -> None:
    if platform.system() != "Windows":
        print(
            "ERROR: Task Scheduler integration is only available on Windows.\n"
            "       On Linux/macOS, use cron or systemd timers instead.",
            file=sys.stderr,
        )
        sys.exit(1)


def _notify(args: argparse.Namespace, title: str, body: str) -> None:
    if getattr(args, "notify", False):
        notify.send(title, body)


# ---------------------------------------------------------------------------
# 'run' subcommand
# ---------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> int:
    retention = None
    if args.keep is not None:
        retention = RetentionPolicy(keep_count=args.keep, max_age_days=args.max_age)

    manager = BackupManager(
        source=args.source,
        base_dir=args.base_dir,
        use_hash=args.hash,
        retention=retention,
    )

    try:
        session = manager.run()
    except BackupAlreadyRunningError as exc:
        log.warning("Skipping: %s", exc)
        _notify(args, "U-Back — Skipped",
                "Another backup process is already running.")
        return 2
    except NASUnavailableError as exc:
        log.error("NAS unavailable: %s", exc)
        _notify(args, "U-Back ✗",
                f"NAS is unavailable.\n{exc}")
        return 3
    except BackupError as exc:
        log.error("Backup failed: %s", exc)
        _notify(args, "U-Back ✗",
                f"Backup failed.\n{str(exc)[:200]}")
        return 1

    info = session.info
    _notify(
        args,
        "U-Back ✓" if info.success else "U-Back ✗",
        (
            f"Snapshot: {info.snapshot_name}\n"
            f"Copied: {info.files_copied:,}  Linked: {info.files_linked:,}\n"
            f"Size: {fmt_bytes(info.total_bytes)}  ({info.elapsed_seconds:.1f}s)"
        ) if info.success else (
            f"Snapshot: {info.snapshot_name}\n"
            f"Errors: {info.errors}"
        ),
    )

    print(f"\nSnapshot : {info.snapshot_name}")
    print(f"Previous : {info.previous_snapshot or '(none — first backup)'}")
    print(f"Copied   : {info.files_copied:,}")
    print(f"Linked   : {info.files_linked:,}")
    print(f"Errors   : {info.errors}")
    print(f"Size     : {fmt_bytes(info.total_bytes)}")
    print(f"Elapsed  : {info.elapsed_seconds:.1f}s")
    print(f"Success  : {info.success}")

    if session.cleanup is not None:
        c = session.cleanup
        print(
            f"\nCleanup  : deleted={c.deleted}, "
            f"freed=~{fmt_bytes(c.freed_bytes)}, "
            f"kept={c.kept}, errors={c.errors}"
        )

    print(f"Info     : {session.info_path}")
    return 0 if info.success else 1


# ---------------------------------------------------------------------------
# 'status' subcommand
# ---------------------------------------------------------------------------

_SEP  = "─" * 62
_DSEP = "═" * 62

def cmd_status(args: argparse.Namespace) -> int:
    base_dir = Path(args.base_dir)

    if not base_dir.exists():
        print(f"ERROR: base-dir does not exist: {base_dir}", file=sys.stderr)
        return 1

    records, summary = collect_status(base_dir)

    now = datetime.now()

    # ── Header ────────────────────────────────────────────────────────────
    print(f"\n{_DSEP}")
    print(f"  U-Back Status  —  {base_dir}")
    print(_DSEP)

    if not records:
        print("\n  No snapshots found.\n")
        return 0

    # ── Snapshot table ────────────────────────────────────────────────────
    print(f"\n  Snapshots (oldest → newest):\n")
    print(f"  {'#':>3}  {'Snapshot':<22}  {'Files':>7}  {'New data':>10}  Status")
    print(f"  {_SEP}")

    for i, rec in enumerate(records, 1):
        status_icon = "✓" if rec.success else "✗"
        info_flag   = "" if rec.has_info else " ?"
        print(
            f"  {i:>3}  {rec.name:<22}  "
            f"{rec.files_total:>7,}  "
            f"{fmt_bytes(rec.size_bytes):>10}  "
            f"{status_icon}{info_flag}"
        )

    # ── Latest backup summary ─────────────────────────────────────────────
    latest = records[-1]
    print(f"\n  Latest backup : {latest.name}  ({time_ago(latest.dt, now)})")
    print(f"  Total snapshots: {summary.snapshot_count}")

    # ── Storage savings ───────────────────────────────────────────────────
    print(f"\n  {_SEP}")
    print(f"  Storage summary:")
    print(f"  {_SEP}")
    print(f"  {'Virtual full size':.<38} {fmt_bytes(summary.virtual_bytes):>10}")
    print(f"  {'  (as if every snapshot were a full copy)'}")
    print(f"  {'Actual NAS usage':.<38} {fmt_bytes(summary.actual_bytes):>10}")
    print(f"  {'  (hard links share identical blocks)'}")
    print(f"  {_SEP}")

    if not summary.inode_dedup_available:
        print(
            "  NOTE: inode numbers unavailable on this filesystem.\n"
            "        Savings cannot be accurately measured."
        )
    elif summary.savings_bytes > 0:
        saved_str   = fmt_bytes(summary.savings_bytes)
        virtual_str = fmt_bytes(summary.virtual_bytes)
        pct         = summary.savings_pct
        print(
            f"  Hard link savings : {saved_str} saved  ({pct:.1f}%)\n"
            f"\n"
            f"  Thanks to hard links, U-Back saved {saved_str} on your NAS\n"
            f"  — that's {pct:.1f}% of the {virtual_str} a naive full-copy\n"
            f"    strategy would have used!"
        )
    else:
        print("  No savings measured (single snapshot or all files unique).")

    print(f"  {_SEP}\n")
    return 0


# ---------------------------------------------------------------------------
# 'schedule' subcommand
# ---------------------------------------------------------------------------

def cmd_schedule(args: argparse.Namespace) -> int:
    _require_windows()

    if args.interval < 1:
        print("ERROR: --interval must be at least 1 minute.", file=sys.stderr)
        return 1

    task_name = _task_name(args.base_dir)

    run_args = [
        sys.executable, "-m", "uback.cli", "run",
        "--source", args.source,
        "--base-dir", args.base_dir,
    ]
    if args.hash:
        run_args.append("--hash")
    if args.notify:
        run_args.append("--notify")
    if args.keep is not None:
        run_args += ["--keep", str(args.keep), "--max-age", str(args.max_age)]

    tr_value = subprocess.list2cmdline(run_args)
    schtasks_cmd = [
        "schtasks", "/Create",
        "/TN", task_name,
        "/TR", tr_value,
        "/SC", "MINUTE",
        "/MO", str(args.interval),
        "/F",
        "/RL", "HIGHEST",
    ]

    log.info("Registering Task Scheduler task: %s", task_name)
    try:
        result = subprocess.run(schtasks_cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        print("ERROR: schtasks.exe not found.", file=sys.stderr)
        return 1

    if result.returncode != 0:
        print(f"ERROR: schtasks failed (exit {result.returncode}):", file=sys.stderr)
        print(result.stderr or result.stdout, file=sys.stderr)
        return 1

    print(f"Task registered successfully.")
    print(f"  Name     : {task_name}")
    print(f"  Interval : every {args.interval} minute(s)")
    print(f"  Source   : {args.source}")
    print(f"  Base dir : {args.base_dir}")
    print(f"\nTo remove: python -m uback.cli unschedule --base-dir \"{args.base_dir}\"")
    return 0


# ---------------------------------------------------------------------------
# 'unschedule' subcommand
# ---------------------------------------------------------------------------

def cmd_unschedule(args: argparse.Namespace) -> int:
    _require_windows()

    task_name = _task_name(args.base_dir)
    schtasks_cmd = ["schtasks", "/Delete", "/TN", task_name, "/F"]
    log.info("Removing Task Scheduler task: %s", task_name)

    try:
        result = subprocess.run(schtasks_cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        print("ERROR: schtasks.exe not found.", file=sys.stderr)
        return 1

    if result.returncode != 0:
        print(f"ERROR: schtasks failed (exit {result.returncode}):", file=sys.stderr)
        print(result.stderr or result.stdout, file=sys.stderr)
        return 1

    print(f"Task '{task_name}' removed from Task Scheduler.")
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m uback.cli",
        description="U-Back — Windows Time Machine backup tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    # --- run ---
    p_run = sub.add_parser("run", help="Run one backup session")
    p_run.add_argument("--source", required=True, metavar="DIR")
    p_run.add_argument("--base-dir", required=True, metavar="DIR")
    p_run.add_argument("--hash", action="store_true",
                       help="Use SHA-256 for change detection instead of mtime+size")
    p_run.add_argument("--keep", type=int, default=None, metavar="N")
    p_run.add_argument("--max-age", type=float, default=30, metavar="DAYS")
    p_run.add_argument("--notify", action="store_true",
                       help="Send a Windows toast notification on completion "
                            "(requires win11toast)")

    # --- status ---
    p_status = sub.add_parser("status", help="Show snapshot history and storage savings")
    p_status.add_argument("--base-dir", required=True, metavar="DIR",
                          help="NAS base directory to inspect")

    # --- schedule ---
    p_sched = sub.add_parser("schedule",
                              help="Register a recurring backup with Windows Task Scheduler")
    p_sched.add_argument("--source", required=True, metavar="DIR")
    p_sched.add_argument("--base-dir", required=True, metavar="DIR")
    p_sched.add_argument("--interval", type=int, default=60, metavar="MINUTES")
    p_sched.add_argument("--hash", action="store_true")
    p_sched.add_argument("--notify", action="store_true",
                         help="Pass --notify to every scheduled run")
    p_sched.add_argument("--keep", type=int, default=None, metavar="N")
    p_sched.add_argument("--max-age", type=float, default=30, metavar="DAYS")

    # --- unschedule ---
    p_unsched = sub.add_parser("unschedule",
                                help="Remove a U-Back task from Windows Task Scheduler")
    p_unsched.add_argument("--base-dir", required=True, metavar="DIR")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )

    dispatch = {
        "run":        cmd_run,
        "status":     cmd_status,
        "schedule":   cmd_schedule,
        "unschedule": cmd_unschedule,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
