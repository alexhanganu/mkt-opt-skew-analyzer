"""
utils/watchdog.py — Run health monitor and failure alerter

The watchdog serves two purposes:
  1. Verify the last scheduled run completed successfully
  2. Alert you (print / desktop notification / optional email) if a run
     was expected but never happened (e.g. laptop was asleep, cron failed)

Run this as a lightweight check from cron / Task Scheduler:
  # 5 minutes after each expected run, check it completed:
  50 9  * * 1-5  python /path/to/watchdog.py --session morning
  35 15 * * 1-5  python /path/to/watchdog.py --session evening
"""

import os
import sys
import json
import glob
import logging
import argparse
import platform
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).parent.parent / "output"
LOG_DIR    = Path(__file__).parent.parent / "logs"
USAGE_LOG  = OUTPUT_DIR / "usage_log.jsonl"


# ─────────────────────────────────────────────
#  HEALTH CHECKS
# ─────────────────────────────────────────────

def last_report_time(session: str) -> datetime | None:
    """Find the most recent report file for a given session."""
    pattern = str(OUTPUT_DIR / f"skew_report_{session}_*.html")
    files   = sorted(glob.glob(pattern))
    if not files:
        return None
    latest = files[-1]
    # Extract timestamp from filename: skew_report_morning_20260307_0945.html
    try:
        stem  = Path(latest).stem   # skew_report_morning_20260307_0945
        parts = stem.split("_")     # ['skew','report','morning','20260307','0945']
        date_str = parts[-2] + parts[-1]   # '202603070945'
        return datetime.strptime(date_str, "%Y%m%d%H%M")
    except Exception:
        return datetime.fromtimestamp(os.path.getmtime(latest))


def last_run_ok(session: str, max_age_hours: float = 26) -> tuple[bool, str]:
    """
    Check if the last run for this session completed within max_age_hours.
    Returns (ok, message).
    """
    last = last_report_time(session)
    if last is None:
        return False, f"No {session} report found in {OUTPUT_DIR}"

    age = datetime.now() - last
    if age > timedelta(hours=max_age_hours):
        return False, (
            f"Last {session} report is {age.seconds//3600}h "
            f"{(age.seconds%3600)//60}m old — may have missed a run"
        )
    return True, f"Last {session} run: {last.strftime('%Y-%m-%d %H:%M')} ({int(age.total_seconds()//60)}m ago)"


def last_error_in_log(session: str) -> str | None:
    """Scan the most recent log file for ERROR entries."""
    pattern = str(LOG_DIR / "run_*.log")
    files   = sorted(glob.glob(pattern))
    if not files:
        return None
    with open(files[-1], "r") as f:
        errors = [l.strip() for l in f if "[ERROR]" in l]
    return errors[-1] if errors else None


def usage_summary(last_n: int = 10) -> dict:
    """Read the last N entries from usage_log.jsonl."""
    if not USAGE_LOG.exists():
        return {}
    entries = []
    with open(USAGE_LOG, "r") as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    recent = entries[-last_n:]
    if not recent:
        return {}
    total_cost = sum(
        float(e.get("estimated_cost", "$0").lstrip("$"))
        for e in recent
    )
    return {
        "recent_runs":    len(recent),
        "total_cost":     f"${total_cost:.4f}",
        "last_run":       recent[-1].get("timestamp", "N/A"),
        "last_session":   recent[-1].get("session", "N/A"),
        "last_api_calls": recent[-1].get("api_calls", 0),
    }


# ─────────────────────────────────────────────
#  ALERTING
# ─────────────────────────────────────────────

def desktop_notify(title: str, message: str):
    """Send a desktop notification (cross-platform best-effort)."""
    system = platform.system()
    try:
        if system == "Darwin":
            script = f'display notification "{message}" with title "{title}"'
            subprocess.run(["osascript", "-e", script],
                           capture_output=True, timeout=5)
        elif system == "Linux":
            subprocess.run(["notify-send", title, message],
                           capture_output=True, timeout=5)
        elif system == "Windows":
            # Requires win10toast: pip install win10toast
            try:
                from win10toast import ToastNotifier
                ToastNotifier().show_toast(title, message, duration=10)
            except ImportError:
                pass   # Silently skip if not installed
    except Exception:
        pass   # Never crash on notification failure


def send_email_alert(subject: str, body: str,
                     to_address: str, smtp_config: dict):
    """
    Optional email alert via SMTP.
    smtp_config keys: host, port, user, password, use_tls
    Set in config.py under ALERT_EMAIL_* variables.
    """
    import smtplib
    from email.mime.text import MIMEText
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"]    = smtp_config.get("user", "skew_analyzer@local")
        msg["To"]      = to_address

        host = smtp_config.get("host", "smtp.gmail.com")
        port = smtp_config.get("port", 587)
        user = smtp_config.get("user", "")
        pw   = smtp_config.get("password", "")

        with smtplib.SMTP(host, port, timeout=10) as s:
            if smtp_config.get("use_tls", True):
                s.starttls()
            if user and pw:
                s.login(user, pw)
            s.sendmail(msg["From"], [to_address], msg.as_string())
        logger.info(f"Alert email sent to {to_address}")
    except Exception as e:
        logger.warning(f"Email alert failed: {e}")


# ─────────────────────────────────────────────
#  MAIN WATCHDOG CHECK
# ─────────────────────────────────────────────

def run_watchdog(session: str, alert: bool = True) -> int:
    """
    Run health check for a session. Returns exit code (0=ok, 1=issue).
    """
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    print(f"\n{'═'*55}")
    print(f"  SKEW ANALYZER WATCHDOG — {session.upper()} — {datetime.now():%Y-%m-%d %H:%M}")
    print(f"{'═'*55}")

    exit_code = 0

    # ── Check last report ────────────────────────────────────
    ok, msg = last_run_ok(session)
    status  = "✓" if ok else "✗"
    print(f"\n  {status}  Last run      : {msg}")
    if not ok:
        exit_code = 1
        if alert:
            desktop_notify(
                "Skew Analyzer Alert",
                f"{session.capitalize()} session may have failed: {msg}"
            )

    # ── Check for recent errors ──────────────────────────────
    last_err = last_error_in_log(session)
    if last_err:
        print(f"  ⚠  Last log error: {last_err[:120]}")
        exit_code = 1
    else:
        print(f"  ✓  Log errors     : None found")

    # ── Usage summary ────────────────────────────────────────
    usage = usage_summary()
    if usage:
        print(f"\n  Claude API (last {usage.get('recent_runs',0)} runs):")
        print(f"    Total cost   : {usage.get('total_cost','N/A')}")
        print(f"    Last run     : {usage.get('last_run','N/A')}")
        print(f"    Last session : {usage.get('last_session','N/A')}")
        print(f"    API calls    : {usage.get('last_api_calls','N/A')}")
    else:
        print(f"\n  ℹ  No usage log found yet (normal on first run)")

    # ── Report file list ─────────────────────────────────────
    reports = sorted(glob.glob(str(OUTPUT_DIR / "skew_report_*.html")))
    print(f"\n  Reports on disk : {len(reports)}")
    if reports:
        print(f"  Latest          : {Path(reports[-1]).name}")

    print(f"\n{'─'*55}")
    print(f"  Status: {'OK' if exit_code == 0 else 'ISSUES DETECTED'}")
    print(f"{'─'*55}\n")

    return exit_code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Skew Analyzer Watchdog")
    parser.add_argument("--session", choices=["morning", "evening"],
                        default="morning")
    parser.add_argument("--no-alert", action="store_true",
                        help="Suppress desktop notifications")
    args = parser.parse_args()
    sys.exit(run_watchdog(args.session, alert=not args.no_alert))
