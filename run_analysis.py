"""
run_analysis.py — Main pipeline orchestrator

Usage:
  python run_analysis.py                    # auto-detect session from time
  python run_analysis.py --session morning  # force morning session
  python run_analysis.py --session evening  # force evening session
  python run_analysis.py --tickers SPY QQQ  # override ticker list
  python run_analysis.py --no-claude        # skip API call, data only
  python run_analysis.py --no-browser       # don't auto-open report

Designed to be called by the scheduler or manually.
"""

import os
import sys
import json
import time
import logging
import argparse
import webbrowser
import concurrent.futures
from datetime import datetime
from typing import List, Optional, Dict

# ── Ensure project root is on path ───────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from utils import load_dotenv, validate_keys, is_trading_day, describe_next_run
from utils import safe_to_run_morning, safe_to_run_evening
from core import (
    fetch_options_chain, build_skew_profile,
    HistoryManager, HistoryRecord,
    ClaudeSkewClient, generate_report,
    SkewProfile
)

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────

def setup_logging(log_dir: str):
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"run_{datetime.now().strftime('%Y%m%d_%H%M')}.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ]
    )
    return logging.getLogger("run_analysis")


# ─────────────────────────────────────────────
#  SESSION DETECTION
# ─────────────────────────────────────────────

def detect_session() -> str:
    """Infer morning/evening from current time."""
    hour = datetime.now().hour
    return "morning" if hour < 13 else "evening"


# ─────────────────────────────────────────────
#  SINGLE TICKER PIPELINE
# ─────────────────────────────────────────────

def process_ticker(ticker: str, session: str,
                   history: HistoryManager,
                   logger) -> Optional[SkewProfile]:
    """
    Full data pipeline for one ticker.
    Returns a SkewProfile or None on failure.
    Safe to call from a thread pool.
    """
    logger.info(f"[{ticker}] Starting fetch...")
    t0 = time.time()

    # ── 1. Fetch options chain ────────────────────────────────
    chain = fetch_options_chain(
        ticker,
        source=config.DATA_SOURCE,
        tradier_key=config.TRADIER_API_KEY,
        polygon_key=config.POLYGON_API_KEY,
    )

    if chain is None:
        logger.error(f"[{ticker}] Chain fetch failed — skipping")
        return None

    if not chain.contracts:
        logger.warning(f"[{ticker}] Empty options chain")
        return None

    # ── 2. Get historical data from local DB ──────────────────
    rr_history    = history.get_rr_history(ticker, session="evening")
    price_history = history.get_price_history(ticker)

    # ── 3. Append today's spot to price history ───────────────
    # (for HV30 computation — spot is the best proxy if close isn't available yet)
    if chain.underlying.price > 0:
        price_history.append(chain.underlying.price)

    # ── 4. Build skew profile ─────────────────────────────────
    profile = build_skew_profile(
        chain=chain,
        r=config.RISK_FREE_RATE,
        q_override=None,
        session=session,
        skew_history=rr_history,
        price_history=price_history,
    )

    # ── 5. Compute IV rank from history ───────────────────────
    if profile.weekly and profile.weekly.atm_iv:
        profile.iv_rank = history.compute_iv_rank(ticker, profile.weekly.atm_iv)

    # ── 6. Persist today's reading ────────────────────────────
    record = HistoryRecord(
        date=datetime.now().strftime("%Y-%m-%d"),
        ticker=ticker,
        spot=chain.underlying.price,
        atm_iv_weekly=profile.weekly.atm_iv if profile.weekly else None,
        rr_25d_weekly=profile.weekly.risk_reversal if profile.weekly else None,
        atm_iv_monthly=profile.monthly.atm_iv if profile.monthly else None,
        rr_25d_monthly=profile.monthly.risk_reversal if profile.monthly else None,
        hv_30d=profile.hv_30d,
        session=session,
    )
    history.append(record)

    elapsed = time.time() - t0
    logger.info(f"[{ticker}] Profile built in {elapsed:.1f}s  "
                f"({len(chain.contracts)} contracts, "
                f"errors={profile.errors})")
    return profile


# ─────────────────────────────────────────────
#  MAIN PIPELINE
# ─────────────────────────────────────────────

def run(tickers: List[str], session: str,
        use_claude: bool = True,
        open_browser: bool = True,
        logger=None) -> str:
    """
    Full pipeline: fetch → compute → Claude analysis → HTML report.
    Returns path to generated HTML file.
    """
    if logger is None:
        logger = logging.getLogger("run_analysis")

    # Load .env file if present
    load_dotenv()

    # Trading day guard
    if not is_trading_day():
        logger.warning(f"Today ({datetime.now().date()}) is not a trading day — skipping run")
        logger.info(f"{describe_next_run()}")
        return ""

    logger.info("=" * 60)
    logger.info(f"SKEW ANALYZER — {session.upper()} SESSION — {datetime.now()}")
    logger.info(f"Tickers: {tickers}")
    logger.info(f"Data source: {config.DATA_SOURCE}")
    logger.info("=" * 60)

    # ── History manager ───────────────────────────────────────
    history = HistoryManager(
        path=config.SKEW_HISTORY_PATH,
        rolling_days=config.HISTORY_ROLLING_DAYS,
    )
    logger.info(f"History DB: {history.summary()}")

    # ── Fetch and compute all tickers ─────────────────────────
    # Use thread pool to parallelize data fetching
    # (I/O bound: safe to parallelize, each thread hits a different endpoint)
    profiles: List[SkewProfile] = []
    max_workers = min(len(tickers), 4)   # cap at 4 to be polite to data sources

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(process_ticker, t, session, history, logger): t
            for t in tickers
        }
        for future in concurrent.futures.as_completed(futures):
            ticker = futures[future]
            try:
                profile = future.result()
                if profile is not None:
                    profiles.append(profile)
            except Exception as e:
                logger.error(f"[{ticker}] Unhandled exception: {e}", exc_info=True)

    if not profiles:
        logger.error("No profiles computed — aborting report generation")
        return ""

    # Sort to match original ticker order
    ticker_order = {t: i for i, t in enumerate(tickers)}
    profiles.sort(key=lambda p: ticker_order.get(p.ticker, 99))

    # ── Claude analysis ───────────────────────────────────────
    analyses: Dict[str, dict] = {}

    if use_claude and config.ANTHROPIC_API_KEY:
        logger.info(f"Sending {len(profiles)} profiles to Claude API...")
        client = ClaudeSkewClient(
            api_key=config.ANTHROPIC_API_KEY,
            model=config.CLAUDE_MODEL,
            max_tokens=config.CLAUDE_MAX_TOKENS,
            use_cache=config.CLAUDE_USE_PROMPT_CACHE,
            batch_size=config.CLAUDE_BATCH_SIZE,
            batch_delay=config.CLAUDE_BATCH_DELAY_SECS,
        )
        payloads = [p.to_claude_payload() for p in profiles]
        analyses = client.analyze_all(payloads, session)

        usage = client.usage_report()
        logger.info(f"Claude usage: {usage}")

        # Save usage to JSON for cost tracking
        if config.SAVE_JSON_DATA:
            usage_path = os.path.join(config.OUTPUT_DIR, "usage_log.jsonl")
            os.makedirs(config.OUTPUT_DIR, exist_ok=True)
            with open(usage_path, "a") as f:
                entry = {"timestamp": datetime.now().isoformat(), "session": session, **usage}
                f.write(json.dumps(entry) + "\n")

    elif not config.ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set — skipping Claude analysis")
    else:
        logger.info("Claude analysis disabled (--no-claude)")

    # ── Save raw computed data ─────────────────────────────────
    if config.SAVE_JSON_DATA:
        data_path = os.path.join(
            config.OUTPUT_DIR,
            f"data_{session}_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
        )
        os.makedirs(config.OUTPUT_DIR, exist_ok=True)
        raw_data = {
            "session":   session,
            "timestamp": datetime.now().isoformat(),
            "profiles":  [p.to_claude_payload() for p in profiles],
            "analyses":  analyses,
        }
        with open(data_path, "w") as f:
            json.dump(raw_data, f, indent=2)
        logger.info(f"Raw data saved to {data_path}")

    # ── Generate HTML report ──────────────────────────────────
    report_path = generate_report(
        profiles=profiles,
        analyses=analyses,
        session=session,
        output_dir=config.OUTPUT_DIR,
        title=config.REPORT_TITLE,
    )
    logger.info(f"Report: {report_path}")

    if open_browser and report_path:
        try:
            webbrowser.open(f"file://{os.path.abspath(report_path)}")
            logger.info("Report opened in browser")
        except Exception as e:
            logger.warning(f"Could not open browser: {e}")

    logger.info("Run complete.")
    return report_path


# ─────────────────────────────────────────────
#  CLI ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Akuna-Style Skew Analyzer — local daily runner"
    )
    parser.add_argument(
        "--session", choices=["morning", "evening"], default=None,
        help="Force session. Defaults to auto-detect from current time."
    )
    parser.add_argument(
        "--tickers", nargs="+", default=None,
        help="Override ticker list (space-separated). Default: config.TICKERS"
    )
    parser.add_argument(
        "--no-claude", action="store_true",
        help="Skip Claude API call (compute data only, no narrative analysis)"
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="Don't auto-open the report in the browser"
    )
    args = parser.parse_args()

    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    logger  = setup_logging(log_dir)

    session  = args.session or detect_session()
    tickers  = args.tickers or config.TICKERS
    use_ai   = not args.no_claude
    browser  = not args.no_browser and config.OPEN_BROWSER_ON_FINISH

    run(
        tickers=tickers,
        session=session,
        use_claude=use_ai,
        open_browser=browser,
        logger=logger,
    )
