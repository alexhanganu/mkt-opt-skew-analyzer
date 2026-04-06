"""
scheduler_setup.py — Automated daily scheduling setup

Run this ONCE to configure your local machine to execute the skew analyzer
automatically at 9:45am (morning) and 3:30pm (evening).

Supports:
  - macOS / Linux : cron via crontab
  - Windows       : Windows Task Scheduler via schtasks

Usage:
  python scheduler_setup.py          # installs schedule
  python scheduler_setup.py --remove # removes schedule
  python scheduler_setup.py --test   # runs one cycle immediately
"""

import os
import sys
import platform
import subprocess
import argparse
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────
SCRIPT_DIR  = Path(__file__).parent.resolve()
RUNNER      = SCRIPT_DIR / "run_analysis.py"
PYTHON      = sys.executable                  # same python that runs this script
LOG_DIR     = SCRIPT_DIR / "logs"

# ── Schedule times ─────────────────────────────────────────────────
MORNING_H, MORNING_M = 9,  45
EVENING_H, EVENING_M = 15, 30


def _cron_line(hour: int, minute: int, session: str) -> str:
    """
    Build a cron expression.
    Runs Mon-Fri only (US market days; adjust if needed for other markets).
    """
    log_file = LOG_DIR / f"cron_{session}.log"
    # Redirect both stdout and stderr to log file
    return (
        f"{minute} {hour} * * 1-5 "
        f"cd {SCRIPT_DIR} && {PYTHON} {RUNNER} "
        f"--session {session} >> {log_file} 2>&1"
    )


# ─────────────────────────────────────────────
#  macOS / LINUX  (crontab)
# ─────────────────────────────────────────────

CRON_TAG = "# skew_analyzer"

def install_cron():
    """Add morning and evening cron jobs."""
    LOG_DIR.mkdir(exist_ok=True)

    # Read existing crontab
    try:
        existing = subprocess.check_output(["crontab", "-l"],
                                           stderr=subprocess.DEVNULL).decode()
    except subprocess.CalledProcessError:
        existing = ""

    # Remove any previous skew_analyzer entries
    lines = [l for l in existing.splitlines() if CRON_TAG not in l]

    # Add new entries
    lines.append(f"{_cron_line(MORNING_H, MORNING_M, 'morning')}  {CRON_TAG}")
    lines.append(f"{_cron_line(EVENING_H, EVENING_M, 'evening')}  {CRON_TAG}")

    new_crontab = "\n".join(lines) + "\n"
    proc = subprocess.run(["crontab", "-"], input=new_crontab.encode(),
                          capture_output=True)
    if proc.returncode == 0:
        print(f"✓ Cron jobs installed:")
        print(f"  Morning : {MORNING_H:02d}:{MORNING_M:02d} Mon-Fri → morning session")
        print(f"  Evening : {EVENING_H:02d}:{EVENING_M:02d} Mon-Fri → evening session")
    else:
        print(f"✗ Failed to install cron: {proc.stderr.decode()}")


def remove_cron():
    """Remove skew_analyzer cron jobs."""
    try:
        existing = subprocess.check_output(["crontab", "-l"],
                                           stderr=subprocess.DEVNULL).decode()
    except subprocess.CalledProcessError:
        print("No existing crontab.")
        return
    lines = [l for l in existing.splitlines() if CRON_TAG not in l]
    subprocess.run(["crontab", "-"], input=("\n".join(lines) + "\n").encode())
    print("✓ Skew analyzer cron jobs removed.")


def show_cron():
    """Print current cron entries for skew_analyzer."""
    try:
        existing = subprocess.check_output(["crontab", "-l"],
                                           stderr=subprocess.DEVNULL).decode()
        lines = [l for l in existing.splitlines() if CRON_TAG in l]
        if lines:
            print("Current skew_analyzer cron jobs:")
            for l in lines:
                print(f"  {l}")
        else:
            print("No skew_analyzer cron jobs found.")
    except subprocess.CalledProcessError:
        print("No crontab exists.")


# ─────────────────────────────────────────────
#  WINDOWS  (Task Scheduler)
# ─────────────────────────────────────────────

TASK_MORNING = "SkewAnalyzer_Morning"
TASK_EVENING = "SkewAnalyzer_Evening"


def _schtask_cmd(task_name: str, hour: int, minute: int, session: str) -> list:
    """Build schtasks.exe command for one scheduled task."""
    log_file = LOG_DIR / f"schtask_{session}.log"
    # /SC WEEKLY /D MON,TUE,WED,THU,FRI = weekdays only
    cmd = (
        f'cmd /c "cd /d {SCRIPT_DIR} && '
        f'{PYTHON} {RUNNER} --session {session} '
        f'>> {log_file} 2>&1"'
    )
    return [
        "schtasks", "/Create", "/F",
        "/TN", task_name,
        "/TR", cmd,
        "/SC", "WEEKLY",
        "/D", "MON,TUE,WED,THU,FRI",
        "/ST", f"{hour:02d}:{minute:02d}",
    ]


def install_windows():
    LOG_DIR.mkdir(exist_ok=True)
    for task, hour, minute, session in [
        (TASK_MORNING, MORNING_H, MORNING_M, "morning"),
        (TASK_EVENING, EVENING_H, EVENING_M, "evening"),
    ]:
        result = subprocess.run(_schtask_cmd(task, hour, minute, session),
                                capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✓ Task '{task}' created ({hour:02d}:{minute:02d} Mon-Fri)")
        else:
            print(f"✗ Failed to create task '{task}': {result.stderr.strip()}")


def remove_windows():
    for task in [TASK_MORNING, TASK_EVENING]:
        result = subprocess.run(["schtasks", "/Delete", "/F", "/TN", task],
                                capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✓ Task '{task}' deleted.")
        else:
            print(f"  Task '{task}' not found or could not be deleted.")


# ─────────────────────────────────────────────
#  PURE PYTHON FALLBACK SCHEDULER
#  (for environments where cron/schtasks isn't available)
# ─────────────────────────────────────────────

def run_python_scheduler():
    """
    Blocking Python scheduler using the 'schedule' library.
    Run this in a persistent terminal / screen / tmux session.
    Requires: pip install schedule
    """
    try:
        import schedule
        import time
        import importlib.util
    except ImportError:
        print("Run: pip install schedule")
        return

    # Dynamic import of run_analysis
    spec = importlib.util.spec_from_file_location("run_analysis", RUNNER)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    import config as cfg

    def morning_job():
        print(f"\n{'='*50}")
        print(f"SCHEDULED MORNING RUN — {__import__('datetime').datetime.now()}")
        mod.run(tickers=cfg.TICKERS, session="morning", open_browser=False)

    def evening_job():
        print(f"\n{'='*50}")
        print(f"SCHEDULED EVENING RUN — {__import__('datetime').datetime.now()}")
        mod.run(tickers=cfg.TICKERS, session="evening", open_browser=False)

    schedule.every().monday.at(f"{MORNING_H:02d}:{MORNING_M:02d}").do(morning_job)
    schedule.every().tuesday.at(f"{MORNING_H:02d}:{MORNING_M:02d}").do(morning_job)
    schedule.every().wednesday.at(f"{MORNING_H:02d}:{MORNING_M:02d}").do(morning_job)
    schedule.every().thursday.at(f"{MORNING_H:02d}:{MORNING_M:02d}").do(morning_job)
    schedule.every().friday.at(f"{MORNING_H:02d}:{MORNING_M:02d}").do(morning_job)

    schedule.every().monday.at(f"{EVENING_H:02d}:{EVENING_M:02d}").do(evening_job)
    schedule.every().tuesday.at(f"{EVENING_H:02d}:{EVENING_M:02d}").do(evening_job)
    schedule.every().wednesday.at(f"{EVENING_H:02d}:{EVENING_M:02d}").do(evening_job)
    schedule.every().thursday.at(f"{EVENING_H:02d}:{EVENING_M:02d}").do(evening_job)
    schedule.every().friday.at(f"{EVENING_H:02d}:{EVENING_M:02d}").do(evening_job)

    print(f"Python scheduler running. Jobs:")
    print(f"  Morning  : {MORNING_H:02d}:{MORNING_M:02d} Mon–Fri")
    print(f"  Evening  : {EVENING_H:02d}:{EVENING_M:02d} Mon–Fri")
    print(f"Press Ctrl+C to stop.\n")

    while True:
        schedule.run_pending()
        time.sleep(30)


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Skew Analyzer Scheduler Setup")
    parser.add_argument("--remove",  action="store_true", help="Remove scheduled jobs")
    parser.add_argument("--test",    action="store_true", help="Run one morning + evening cycle now")
    parser.add_argument("--python",  action="store_true", help="Use pure Python scheduler (blocking)")
    args = parser.parse_args()

    system = platform.system()

    if args.test:
        print("Running test cycle...")
        os.chdir(SCRIPT_DIR)
        subprocess.run([PYTHON, str(RUNNER), "--session", "morning", "--no-browser"])
        print("\n--- Morning done. Running evening... ---\n")
        subprocess.run([PYTHON, str(RUNNER), "--session", "evening", "--no-browser"])
        sys.exit(0)

    if args.python:
        run_python_scheduler()
        sys.exit(0)

    if system in ("Darwin", "Linux"):
        if args.remove:
            remove_cron()
        else:
            install_cron()
            show_cron()
    elif system == "Windows":
        if args.remove:
            remove_windows()
        else:
            install_windows()
    else:
        print(f"Unsupported platform: {system}")
        print("Use --python for the cross-platform Python scheduler.")
