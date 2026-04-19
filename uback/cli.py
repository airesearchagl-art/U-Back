"""
U-Back unified command-line interface.

Subcommands
-----------
run        Run one backup session immediately.
schedule   Register a recurring backup task with Windows Task Scheduler.
unschedule Remove a previously registered Task Scheduler task.

Examples
--------
  python -m uback.cli run --source C:\\Users\\you\\Documents --base-dir \\\\NAS\\Backup\\Docs
  python -m uback.cli run --source ... --base-dir ... --keep 10 --max-age 30
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
from pathlib import Path

from .errors import BackupAlreadyRunningError, BackupError, NASUnavailableError
from .manager import BackupManager, RetentionPolicy

log = logging.getLogger(__name__)

# Task Scheduler task name prefix
_TASK_PREFIX = "U-Back"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _task_name(base_dir: str) -> str:
    """
    Derive a stable, filesystem-safe Task Scheduler task name from base_dir.

    Uses the last two path components so that ``\\\\NAS\\Backup\\Docs`` becomes
    ``U-Back\\Backup-Docs``.
    """
    parts = [p for p in re.split(r"[\\/]+", base_dir.strip("\\/")) if p]
    label = "-".join(parts[-2:]) if len(parts) >= 2 else (parts[0] if parts else "default")
    # Strip characters that schtasks rejects
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
        return 2
    except NASUnavailableError as exc:
        log.error("NAS unavailable: %s", exc)
        return 3
    except BackupError as exc:
        log.error("Backup failed: %s", exc)
        return 1

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
        print(
            f"\nCleanup  : deleted={c.deleted}, freed=~{c.freed_bytes:,} bytes, "
            f"kept={c.kept}, errors={c.errors}"
        )

    print(f"Info     : {session.info_path}")
    return 0 if info.success else 1


# ---------------------------------------------------------------------------
# 'schedule' subcommand
# ---------------------------------------------------------------------------

def cmd_schedule(args: argparse.Namespace) -> int:
    """Register a recurring task with Windows Task Scheduler via schtasks."""
    _require_windows()

    interval = args.interval
    if interval < 1:
        print("ERROR: --interval must be at least 1 minute.", file=sys.stderr)
        return 1

    task_name = _task_name(args.base_dir)

    # Build the task action command.  Use sys.executable so the registered
    # task runs the same Python interpreter that is currently active.
    run_args = [
        sys.executable, "-m", "uback.cli", "run",
        "--source", args.source,
        "--base-dir", args.base_dir,
    ]
    if args.hash:
        run_args.append("--hash")
    if args.keep is not None:
        run_args += ["--keep", str(args.keep), "--max-age", str(args.max_age)]

    # schtasks wants the entire /TR value as a single quoted string.
    # We wrap it in double-quotes and escape inner quotes with \".
    tr_value = subprocess.list2cmdline(run_args)

    schtasks_cmd = [
        "schtasks", "/Create",
        "/TN", task_name,
        "/TR", tr_value,
        "/SC", "MINUTE",
        "/MO", str(interval),
        "/F",          # overwrite if the task already exists
        "/RL", "HIGHEST",  # run with elevated privileges (for network drives)
    ]

    log.info("Registering Task Scheduler task: %s", task_name)
    log.debug("schtasks command: %s", subprocess.list2cmdline(schtasks_cmd))

    try:
        result = subprocess.run(
            schtasks_cmd,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        print(
            "ERROR: schtasks.exe not found. "
            "Ensure you are running on Windows with Task Scheduler available.",
            file=sys.stderr,
        )
        return 1

    if result.returncode != 0:
        print(f"ERROR: schtasks failed (exit {result.returncode}):", file=sys.stderr)
        print(result.stderr or result.stdout, file=sys.stderr)
        return 1

    print(f"Task registered successfully.")
    print(f"  Name     : {task_name}")
    print(f"  Interval : every {interval} minute(s)")
    print(f"  Source   : {args.source}")
    print(f"  Base dir : {args.base_dir}")
    print(f"\nTo remove: python -m uback.cli unschedule --base-dir \"{args.base_dir}\"")
    return 0


# ---------------------------------------------------------------------------
# 'unschedule' subcommand
# ---------------------------------------------------------------------------

def cmd_unschedule(args: argparse.Namespace) -> int:
    """Remove a U-Back task from Windows Task Scheduler."""
    _require_windows()

    task_name = _task_name(args.base_dir)

    schtasks_cmd = ["schtasks", "/Delete", "/TN", task_name, "/F"]
    log.info("Removing Task Scheduler task: %s", task_name)

    try:
        result = subprocess.run(
            schtasks_cmd,
            capture_output=True,
            text=True,
            check=False,
        )
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
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- run ---
    p_run = sub.add_parser("run", help="Run one backup session")
    p_run.add_argument("--source", required=True, metavar="DIR",
                       help="Local directory to back up")
    p_run.add_argument("--base-dir", required=True, metavar="DIR",
                       help="NAS base directory for all snapshots")
    p_run.add_argument("--hash", action="store_true",
                       help="Use SHA-256 for change detection instead of mtime+size")
    p_run.add_argument("--keep", type=int, default=None, metavar="N",
                       help="Always keep the N most recent snapshots")
    p_run.add_argument("--max-age", type=float, default=30, metavar="DAYS",
                       help="Delete snapshots older than DAYS (requires --keep, default: 30)")

    # --- schedule ---
    p_sched = sub.add_parser(
        "schedule",
        help="Register a recurring backup with Windows Task Scheduler",
    )
    p_sched.add_argument("--source", required=True, metavar="DIR",
                         help="Local directory to back up")
    p_sched.add_argument("--base-dir", required=True, metavar="DIR",
                         help="NAS base directory for all snapshots")
    p_sched.add_argument("--interval", type=int, default=60, metavar="MINUTES",
                         help="Run interval in minutes (default: 60)")
    p_sched.add_argument("--hash", action="store_true",
                         help="Pass --hash to every scheduled run")
    p_sched.add_argument("--keep", type=int, default=None, metavar="N",
                         help="Pass --keep N to every scheduled run")
    p_sched.add_argument("--max-age", type=float, default=30, metavar="DAYS",
                         help="Pass --max-age DAYS to every scheduled run")

    # --- unschedule ---
    p_unsched = sub.add_parser(
        "unschedule",
        help="Remove a U-Back task from Windows Task Scheduler",
    )
    p_unsched.add_argument("--base-dir", required=True, metavar="DIR",
                           help="The base-dir used when the task was scheduled")

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
        "run": cmd_run,
        "schedule": cmd_schedule,
        "unschedule": cmd_unschedule,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
