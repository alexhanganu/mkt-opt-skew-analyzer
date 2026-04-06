"""
utils/market_calendar.py — US market trading calendar

Determines:
  - Is today a trading day?
  - Are markets currently open?
  - When is the next scheduled run?
  - DTE calculation excluding weekends (for more accurate theta)

No external dependencies — uses hardcoded US federal holiday schedule.
"""

from datetime import date, datetime, timedelta
from typing import Optional, Tuple
import logging

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  US MARKET HOLIDAYS (NYSE)
#  Update the YEAR_HOLIDAYS set at year-start
# ─────────────────────────────────────────────

# NYSE 2025 + 2026 holidays (observed dates)
NYSE_HOLIDAYS = {
    # 2025
    date(2025,  1,  1),   # New Year's Day
    date(2025,  1, 20),   # MLK Day
    date(2025,  2, 17),   # Presidents' Day
    date(2025,  4, 18),   # Good Friday
    date(2025,  5, 26),   # Memorial Day
    date(2025,  6, 19),   # Juneteenth
    date(2025,  7,  4),   # Independence Day
    date(2025,  9,  1),   # Labor Day
    date(2025, 11, 27),   # Thanksgiving
    date(2025, 12, 25),   # Christmas
    # 2026
    date(2026,  1,  1),   # New Year's Day
    date(2026,  1, 19),   # MLK Day
    date(2026,  2, 16),   # Presidents' Day
    date(2026,  4,  3),   # Good Friday
    date(2026,  5, 25),   # Memorial Day
    date(2026,  6, 19),   # Juneteenth
    date(2026,  7,  3),   # Independence Day (observed)
    date(2026,  9,  7),   # Labor Day
    date(2026, 11, 26),   # Thanksgiving
    date(2026, 12, 25),   # Christmas
}

MARKET_OPEN  = (9, 30)    # 9:30 AM ET
MARKET_CLOSE = (16, 0)    # 4:00 PM ET

# ─────────────────────────────────────────────
#  CORE FUNCTIONS
# ─────────────────────────────────────────────

def is_trading_day(d: Optional[date] = None) -> bool:
    """Return True if d is a NYSE trading day (Mon-Fri, not a holiday)."""
    if d is None:
        d = date.today()
    if d.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    return d not in NYSE_HOLIDAYS


def is_market_open(now: Optional[datetime] = None) -> bool:
    """Return True if US equity markets are currently open."""
    if now is None:
        now = datetime.now()
    if not is_trading_day(now.date()):
        return False
    open_dt  = now.replace(hour=MARKET_OPEN[0],  minute=MARKET_OPEN[1],  second=0)
    close_dt = now.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0)
    return open_dt <= now <= close_dt


def minutes_to_close(now: Optional[datetime] = None) -> Optional[int]:
    """Minutes until market close today. None if market is closed."""
    if now is None:
        now = datetime.now()
    if not is_market_open(now):
        return None
    close_dt = now.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0)
    return int((close_dt - now).total_seconds() / 60)


def next_trading_day(d: Optional[date] = None) -> date:
    """Return the next NYSE trading day after d."""
    if d is None:
        d = date.today()
    candidate = d + timedelta(days=1)
    while not is_trading_day(candidate):
        candidate += timedelta(days=1)
    return candidate


def trading_days_between(start: date, end: date) -> int:
    """Count trading days between start (exclusive) and end (inclusive)."""
    count = 0
    current = start + timedelta(days=1)
    while current <= end:
        if is_trading_day(current):
            count += 1
        current += timedelta(days=1)
    return count


def calendar_dte(expiry: date, today: Optional[date] = None) -> int:
    """Standard calendar DTE (what options chain uses)."""
    if today is None:
        today = date.today()
    return max((expiry - today).days, 0)


def trading_dte(expiry: date, today: Optional[date] = None) -> int:
    """DTE counting only trading days (more accurate for theta)."""
    if today is None:
        today = date.today()
    return trading_days_between(today, expiry)


def session_check(morning_time: str = "09:45",
                  evening_time: str = "15:30") -> Tuple[bool, str]:
    """
    Check if now is within ±10 minutes of a scheduled session.
    Returns (should_run, session_name).
    """
    now  = datetime.now()
    mh, mm = map(int, morning_time.split(":"))
    eh, em = map(int, evening_time.split(":"))

    morning_dt = now.replace(hour=mh, minute=mm, second=0)
    evening_dt = now.replace(hour=eh, minute=em, second=0)

    window = timedelta(minutes=10)

    if abs(now - morning_dt) <= window:
        return True, "morning"
    if abs(now - evening_dt) <= window:
        return True, "evening"
    return False, ""


def describe_next_run(morning_time: str = "09:45",
                      evening_time: str = "15:30") -> str:
    """Human-readable description of next scheduled run."""
    now = datetime.now()
    mh, mm = map(int, morning_time.split(":"))
    eh, em = map(int, evening_time.split(":"))

    today = now.date()
    candidates = []

    for d in [today, next_trading_day(today)]:
        if not is_trading_day(d):
            continue
        for h, m, label in [(mh, mm, "morning"), (eh, em, "evening")]:
            run_dt = datetime(d.year, d.month, d.day, h, m)
            if run_dt > now:
                candidates.append((run_dt, label))

    if not candidates:
        nxt = next_trading_day(today)
        return f"Next run: {nxt.strftime('%a %b %d')} at {morning_time} (morning)"

    candidates.sort()
    run_dt, label = candidates[0]
    delta = run_dt - now
    hours = int(delta.total_seconds() // 3600)
    mins  = int((delta.total_seconds() % 3600) // 60)
    return (
        f"Next run: {run_dt.strftime('%a %b %d at %H:%M')} "
        f"({label}) — in {hours}h {mins}m"
    )


# ─────────────────────────────────────────────
#  EARLY-OPEN / LATE-CLOSE GUARD
# ─────────────────────────────────────────────

def safe_to_run_morning(now: Optional[datetime] = None) -> Tuple[bool, str]:
    """
    Is it safe to run the morning analysis?
    We wait until 9:45 to let bid/ask spreads tighten after open.
    """
    if now is None:
        now = datetime.now()
    if not is_trading_day(now.date()):
        return False, "Not a trading day"
    safe_open = now.replace(hour=9, minute=40, second=0)
    mkt_close = now.replace(hour=16, minute=0, second=0)
    if now < safe_open:
        return False, f"Too early — wait until 09:40 for tighter spreads"
    if now > mkt_close:
        return False, "Market is closed"
    return True, "OK"


def safe_to_run_evening(now: Optional[datetime] = None) -> Tuple[bool, str]:
    """
    Is it safe to run the evening analysis?
    We want to run between 3:15pm and 3:55pm ET.
    """
    if now is None:
        now = datetime.now()
    if not is_trading_day(now.date()):
        return False, "Not a trading day"
    window_open  = now.replace(hour=15, minute=15, second=0)
    window_close = now.replace(hour=15, minute=55, second=0)
    if now < window_open:
        return False, "Too early for evening session"
    if now > window_close:
        return False, "Evening window passed (market closing)"
    return True, "OK"
