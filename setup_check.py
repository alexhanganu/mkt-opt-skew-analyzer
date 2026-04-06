"""
setup_check.py — First-run environment validator and smoke test

Run this BEFORE anything else to confirm your environment is ready:
  python setup_check.py

Checks:
  1. Python version (3.10+ required for union types)
  2. All required packages installed
  3. API keys configured
  4. IV engine working (quick math smoke test)
  5. yfinance reachable (fetches SPY quote — no options, just price)
  6. Output directory writable
  7. Scheduler availability (cron or schtasks)
"""

import sys
import os
import platform
import importlib
import subprocess
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LINE  = "─" * 58
DLINE = "═" * 58

ok_count   = 0
fail_count = 0
warn_count = 0


def ok(msg):
    global ok_count
    ok_count += 1
    print(f"  ✓  {msg}")


def fail(msg, hint=""):
    global fail_count
    fail_count += 1
    print(f"  ✗  {msg}")
    if hint:
        print(f"       → {hint}")


def warn(msg, hint=""):
    global warn_count
    warn_count += 1
    print(f"  ⚠  {msg}")
    if hint:
        print(f"       → {hint}")


def section(title):
    print(f"\n{LINE}")
    print(f"  {title}")
    print(LINE)


# ─────────────────────────────────────────────
print(f"\n{DLINE}")
print(f"  SKEW ANALYZER — SETUP CHECK")
print(f"  {datetime.now():%Y-%m-%d %H:%M}")
print(DLINE)

# ── 1. Python version ─────────────────────────────────────────────
section("1. Python Version")
major, minor = sys.version_info[:2]
if major == 3 and minor >= 10:
    ok(f"Python {major}.{minor} ({sys.executable})")
elif major == 3 and minor >= 8:
    warn(f"Python {major}.{minor} — works but 3.10+ recommended",
         "Upgrade: https://python.org/downloads")
else:
    fail(f"Python {major}.{minor} — Python 3.10+ required",
         "Upgrade: https://python.org/downloads")

# ── 2. Required packages ──────────────────────────────────────────
section("2. Required Packages")
PACKAGES = {
    "numpy":      "numpy",
    "scipy":      "scipy",
    "yfinance":   "yfinance",
    "anthropic":  "anthropic",
    "requests":   "requests",
}
OPTIONAL = {
    "schedule":   "schedule (optional — needed for Python scheduler mode)",
}

for import_name, display in PACKAGES.items():
    try:
        mod = importlib.import_module(import_name)
        ver = getattr(mod, "__version__", "?")
        ok(f"{display} v{ver}")
    except ImportError:
        fail(f"{display} NOT INSTALLED",
             f"pip install {import_name}")

for import_name, display in OPTIONAL.items():
    try:
        mod = importlib.import_module(import_name)
        ver = getattr(mod, "__version__", "?")
        ok(f"{display} v{ver}")
    except ImportError:
        warn(f"{display} not installed",
             f"pip install {import_name}  (only needed for --python scheduler)")

# ── 3. API Keys ───────────────────────────────────────────────────
section("3. API Key Configuration")
from utils.env_loader import load_dotenv
load_dotenv()

anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
tradier_key   = os.environ.get("TRADIER_API_KEY", "")
polygon_key   = os.environ.get("POLYGON_API_KEY", "")

if anthropic_key and anthropic_key.startswith("sk-ant-"):
    masked = anthropic_key[:12] + "..." + anthropic_key[-4:]
    ok(f"ANTHROPIC_API_KEY set ({masked})")
elif anthropic_key:
    warn("ANTHROPIC_API_KEY set but looks malformed",
         "Should start with 'sk-ant-' — check console.anthropic.com")
else:
    warn("ANTHROPIC_API_KEY not set — Claude analysis will be skipped",
         "Set in .env file or: export ANTHROPIC_API_KEY='sk-ant-...'")

if tradier_key:
    ok(f"TRADIER_API_KEY set ({tradier_key[:6]}...)")
else:
    warn("TRADIER_API_KEY not set — will use yfinance (15min delayed data)",
         "Optional: tradier.com → Account → API Access → $10/mo for real-time")

if polygon_key:
    ok(f"POLYGON_API_KEY set ({polygon_key[:6]}...)")
else:
    warn("POLYGON_API_KEY not set (optional alternative to Tradier)")

# ── 4. IV Engine smoke test ───────────────────────────────────────
section("4. IV Engine (Black-Scholes Math)")
try:
    from core.iv_engine import bs_price, compute_iv

    S, K, T, r, q, sigma = 600.0, 580.0, 7/365, 0.0525, 0.013, 0.32
    price  = bs_price(S, K, T, r, q, sigma, "put")
    iv_rec = compute_iv(price, S, K, T, r, q, "put")

    if iv_rec and abs(iv_rec - sigma) < 0.0001:
        ok(f"Round-trip IV: {sigma*100:.1f}% → price ${price:.4f} → recovered {iv_rec*100:.4f}%")
    else:
        fail(f"IV round-trip failed: expected {sigma:.4f}, got {iv_rec}")
except Exception as e:
    fail(f"IV engine error: {e}")

# ── 5. History manager ────────────────────────────────────────────
section("5. History Manager (Local Database)")
try:
    import tempfile
    from core.history_manager import HistoryManager, HistoryRecord
    from datetime import date

    tmp  = tempfile.mktemp(suffix=".csv")
    hm   = HistoryManager(tmp)
    hm.append(HistoryRecord(
        date=date.today().strftime("%Y-%m-%d"), ticker="TEST",
        spot=100.0, atm_iv_weekly=0.25, rr_25d_weekly=-0.05,
        atm_iv_monthly=0.23, rr_25d_monthly=-0.045,
        hv_30d=0.20, session="evening",
    ))
    count = hm.record_count("TEST")
    os.unlink(tmp)
    if count == 1:
        ok(f"History manager: write/read OK (stored at {os.path.dirname(tmp)})")
    else:
        fail("History manager: unexpected record count")
except Exception as e:
    fail(f"History manager error: {e}")

# ── 6. Output directory ───────────────────────────────────────────
section("6. Output Directory")
import config
try:
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    test_file = os.path.join(config.OUTPUT_DIR, ".write_test")
    with open(test_file, "w") as f:
        f.write("test")
    os.unlink(test_file)
    ok(f"Output directory writable: {config.OUTPUT_DIR}")
except Exception as e:
    fail(f"Cannot write to output directory: {e}")

# Also check cache and logs
for d, label in [(config.SKEW_HISTORY_PATH, "cache"), ("logs", "logs")]:
    parent = os.path.dirname(d) if "." in os.path.basename(d) else d
    try:
        os.makedirs(parent, exist_ok=True)
        ok(f"{label} directory ready: {parent}")
    except Exception as e:
        fail(f"Cannot create {label} directory: {e}")

# ── 7. Network / yfinance reachability ───────────────────────────
section("7. Network Reachability (yfinance SPY quote)")
try:
    import yfinance as yf
    tk    = yf.Ticker("SPY")
    price = tk.fast_info.last_price
    if price and price > 0:
        ok(f"yfinance reachable: SPY @ ${price:.2f}")
    else:
        warn("yfinance returned zero price — may be outside market hours",
             "This is normal on weekends or after hours")
except Exception as e:
    warn(f"yfinance check failed: {e}",
         "Check internet connection. yfinance sometimes has outages.")

# ── 8. Scheduler availability ─────────────────────────────────────
section("8. Scheduler")
system = platform.system()
if system in ("Darwin", "Linux"):
    try:
        subprocess.run(["crontab", "-l"], capture_output=True, timeout=5)
        ok(f"cron available ({system}) — use: python scheduler_setup.py")
    except Exception:
        warn("crontab not available",
             "Use: python scheduler_setup.py --python  (pure Python fallback)")
elif system == "Windows":
    try:
        r = subprocess.run(["schtasks", "/?"], capture_output=True, timeout=5)
        ok("Windows Task Scheduler available — use: python scheduler_setup.py")
    except Exception:
        warn("schtasks not found",
             "Use: python scheduler_setup.py --python  (pure Python fallback)")
else:
    warn(f"Unknown platform: {system}",
         "Use: python scheduler_setup.py --python")

# ── Summary ───────────────────────────────────────────────────────
print(f"\n{DLINE}")
print(f"  RESULT SUMMARY")
print(DLINE)
print(f"  ✓  Passed   : {ok_count}")
print(f"  ⚠  Warnings : {warn_count}")
print(f"  ✗  Failed   : {fail_count}")
print()

if fail_count == 0 and warn_count == 0:
    print("  🟢  ALL CHECKS PASSED — ready to run!")
    print(f"      python run_analysis.py --session morning --no-claude")
elif fail_count == 0:
    print("  🟡  WARNINGS PRESENT — functional but check items above")
    print(f"      python run_analysis.py --session morning --no-claude")
else:
    print("  🔴  FAILURES DETECTED — fix items above before running")
    print("      Re-run this script after fixing to confirm.")

print()
print(f"  Next step: python run_analysis.py --session morning --no-claude")
print(f"             (omit --no-claude once ANTHROPIC_API_KEY is set)")
print(DLINE + "\n")

sys.exit(0 if fail_count == 0 else 1)
