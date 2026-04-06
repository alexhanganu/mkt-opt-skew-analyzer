"""
config.py — Central configuration for the Skew Analyzer
Edit this file to customize tickers, schedules, API keys, and thresholds.
"""

import os
from dataclasses import dataclass, field
from typing import List

# ─────────────────────────────────────────────
#  API KEYS  (set via environment variables)
# ─────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TRADIER_API_KEY   = os.environ.get("TRADIER_API_KEY", "")   # optional but recommended
POLYGON_API_KEY   = os.environ.get("POLYGON_API_KEY", "")   # optional alternative

# ─────────────────────────────────────────────
#  DATA SOURCE PRIORITY
#  "tradier"  → best real-time options (paid, ~$10/mo free tier)
#  "polygon"  → good alternative (paid)
#  "yfinance" → free, delayed, good enough for development
# ─────────────────────────────────────────────
DATA_SOURCE = "yfinance"   # change to "tradier" or "polygon" in production

# ─────────────────────────────────────────────
#  TARGET ETFs  (10 default, fully customizable)
# ─────────────────────────────────────────────
TICKERS: List[str] = [
    "SPY",   # S&P 500
    "QQQ",   # Nasdaq-100
    "IWM",   # Russell 2000
    "GLD",   # Gold
    "TLT",   # 20Y Treasury
    "XLE",   # Energy
    "XLK",   # Technology
    "ARKK",  # Innovation (high vol)
    "EEM",   # Emerging Markets
    "HYG",   # High Yield Bonds
]

# ─────────────────────────────────────────────
#  SCHEDULE  (24h format, local machine time)
# ─────────────────────────────────────────────
MORNING_RUN_TIME  = "09:45"   # 15 min after open — tighter bid/ask spreads
EVENING_RUN_TIME  = "15:30"   # 30 min before close — positioning window

# ─────────────────────────────────────────────
#  OPTIONS ANALYSIS PARAMETERS
# ─────────────────────────────────────────────
TARGET_DELTAS        = [0.10, 0.15, 0.20, 0.25, 0.50]  # deltas to map
TARGET_EXPIRATIONS   = {                                  # DTE buckets
    "daily":   (0,  3),
    "weekly":  (4,  10),
    "monthly": (20, 50),
}
MIN_OPEN_INTEREST    = 50       # filter out illiquid strikes
MIN_VOLUME           = 5        # filter out no-volume strikes
RISK_FREE_RATE       = 0.0525   # 10Y UST approximate — update periodically
IV_SOLVER_TOLERANCE  = 1e-6
IV_MAX_ITERATIONS    = 500

# ─────────────────────────────────────────────
#  SKEW THRESHOLDS  (for regime classification)
# ─────────────────────────────────────────────
SKEW_REGIMES = {
    "flat":     (0.00, 0.03),   # RR between 0 and -3%  → complacency
    "normal":   (0.03, 0.05),   # RR between -3% and -5% → baseline
    "elevated": (0.05, 0.07),   # RR between -5% and -7% → cautious
    "steep":    (0.07, 0.09),   # RR between -7% and -9% → fearful
    "extreme":  (0.09, 1.00),   # RR > -9%              → crash pricing
}

# ─────────────────────────────────────────────
#  HISTORICAL SKEW DATABASE (local CSV rolling)
# ─────────────────────────────────────────────
SKEW_HISTORY_PATH    = os.path.join(os.path.dirname(__file__), "cache", "skew_history.csv")
HISTORY_ROLLING_DAYS = 252       # ~1 trading year for percentile calculations
MIN_HISTORY_DAYS     = 20        # minimum before percentiles are computed

# ─────────────────────────────────────────────
#  CLAUDE API  (cost-minimization settings)
# ─────────────────────────────────────────────
CLAUDE_MODEL               = "claude-sonnet-4-20250514"
CLAUDE_MAX_TOKENS          = 2000       # per ETF narrative block
CLAUDE_MAX_INPUT_TOKENS    = 1200       # target per ETF payload (keep lean)
CLAUDE_USE_PROMPT_CACHE    = True       # cache system prompt — saves ~90% on repeated tokens
CLAUDE_BATCH_SIZE          = 3          # ETFs per API call batch
CLAUDE_BATCH_DELAY_SECS    = 1.5        # delay between batches (rate limit safety)

# ─────────────────────────────────────────────
#  OUTPUT SETTINGS
# ─────────────────────────────────────────────
OUTPUT_DIR              = os.path.join(os.path.dirname(__file__), "output")
OPEN_BROWSER_ON_FINISH  = True          # auto-open HTML report after run
SAVE_JSON_DATA          = True          # save raw computed data alongside HTML
REPORT_TITLE            = "Akuna-Style Skew Analysis"
