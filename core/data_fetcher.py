"""
core/data_fetcher.py — Options data retrieval layer

Supports three backends in priority order:
  1. Tradier API   → real-time, paid (~$10/mo), recommended for production
  2. Polygon.io    → good alternative, paid
  3. yfinance      → free, delayed ~15min, good for dev/testing

All backends return the same normalized OptionsChain dataclass.
"""

import math
import time
import logging
from datetime import datetime, date, timedelta
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
import requests

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  NORMALIZED DATA STRUCTURES
# ─────────────────────────────────────────────

@dataclass
class OptionContract:
    ticker:        str
    expiry:        date
    dte:           int
    strike:        float
    option_type:   str          # 'call' or 'put'
    bid:           float
    ask:           float
    last:          float
    volume:        int
    open_interest: int
    mid_price:     float = 0.0  # computed: (bid + ask) / 2

    def __post_init__(self):
        if self.bid > 0 and self.ask > 0:
            self.mid_price = (self.bid + self.ask) / 2.0
        elif self.last > 0:
            self.mid_price = self.last
        else:
            self.mid_price = 0.0


@dataclass
class UnderlyingData:
    ticker:       str
    price:        float
    prev_close:   float
    change_pct:   float
    volume:       int
    div_yield:    float   # continuous dividend yield
    timestamp:    datetime = field(default_factory=datetime.now)


@dataclass
class OptionsChain:
    underlying:  UnderlyingData
    contracts:   List[OptionContract]
    fetched_at:  datetime = field(default_factory=datetime.now)
    source:      str = "unknown"

    def filter_by_dte(self, min_dte: int, max_dte: int) -> List[OptionContract]:
        return [c for c in self.contracts if min_dte <= c.dte <= max_dte]

    def filter_by_type(self, option_type: str) -> List[OptionContract]:
        return [c for c in self.contracts if c.option_type == option_type]

    def get_atm_contracts(self, option_type: str = "call",
                          dte_range: Tuple[int, int] = (20, 50)) -> List[OptionContract]:
        S = self.underlying.price
        chain = self.filter_by_dte(*dte_range)
        chain = [c for c in chain if c.option_type == option_type]
        if not chain:
            return []
        # Return contracts closest to ATM
        chain.sort(key=lambda c: abs(c.strike - S))
        return chain[:3]


# ─────────────────────────────────────────────
#  YFINANCE BACKEND  (free, development)
# ─────────────────────────────────────────────

def _fetch_yfinance(ticker: str, min_oi: int = 10, min_vol: int = 0) -> Optional[OptionsChain]:
    """
    Fetch options chain via yfinance.
    Note: yfinance returns last-trade prices (not always bid/ask midpoint).
    Adequate for development; use Tradier/Polygon for production.
    """
    try:
        import yfinance as yf
        tk = yf.Ticker(ticker)

        # ── Underlying price ──
        info  = tk.fast_info
        price = float(info.last_price or info.previous_close or 0)
        if price <= 0:
            hist  = tk.history(period="2d")
            price = float(hist["Close"].iloc[-1]) if not hist.empty else 0
        if price <= 0:
            logger.warning(f"{ticker}: could not retrieve underlying price")
            return None

        prev_close  = float(info.previous_close or price)
        change_pct  = (price - prev_close) / prev_close if prev_close else 0
        volume      = int(info.three_month_average_volume or 0)

        # Dividend yield (continuous approximation)
        div_yield = 0.0
        try:
            raw_div = tk.info.get("dividendYield") or 0
            div_yield = float(raw_div) if raw_div else 0.0
        except Exception:
            div_yield = 0.0

        underlying = UnderlyingData(
            ticker=ticker, price=price, prev_close=prev_close,
            change_pct=change_pct, volume=volume, div_yield=div_yield
        )

        # ── Options chain ──
        expirations = tk.options
        if not expirations:
            logger.warning(f"{ticker}: no option expirations found")
            return None

        today    = date.today()
        contracts = []

        # Fetch expirations covering 0–90 DTE
        for exp_str in expirations:
            try:
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            dte = (exp_date - today).days
            if dte < 0 or dte > 90:
                continue

            try:
                chain_data = tk.option_chain(exp_str)
            except Exception as e:
                logger.debug(f"{ticker} {exp_str}: chain fetch error: {e}")
                continue

            for opt_type, df in [("call", chain_data.calls), ("put", chain_data.puts)]:
                if df is None or df.empty:
                    continue
                for _, row in df.iterrows():
                    try:
                        oi     = int(row.get("openInterest", 0) or 0)
                        vol    = int(row.get("volume", 0) or 0)
                        strike = float(row["strike"])
                        bid    = float(row.get("bid", 0) or 0)
                        ask    = float(row.get("ask", 0) or 0)
                        last   = float(row.get("lastPrice", 0) or 0)

                        if oi < min_oi:
                            continue
                        # Filter extreme OTM (> 40% away from spot) — usually illiquid
                        if abs(strike - price) / price > 0.40:
                            continue

                        contracts.append(OptionContract(
                            ticker=ticker, expiry=exp_date, dte=dte,
                            strike=strike, option_type=opt_type,
                            bid=bid, ask=ask, last=last,
                            volume=vol, open_interest=oi
                        ))
                    except Exception:
                        continue

            time.sleep(0.15)   # polite rate limiting for yfinance

        logger.info(f"{ticker}: fetched {len(contracts)} contracts via yfinance")
        return OptionsChain(underlying=underlying, contracts=contracts, source="yfinance")

    except ImportError:
        logger.error("yfinance not installed. Run: pip install yfinance")
        return None
    except Exception as e:
        logger.error(f"{ticker} yfinance fetch failed: {e}")
        return None


# ─────────────────────────────────────────────
#  TRADIER BACKEND  (real-time, recommended)
# ─────────────────────────────────────────────

def _fetch_tradier(ticker: str, api_key: str,
                   min_oi: int = 50, min_vol: int = 5) -> Optional[OptionsChain]:
    """
    Fetch via Tradier brokerage API.
    Free tier: 1 year delayed + sandbox. Developer tier ($10/mo): real-time.
    Docs: https://documentation.tradier.com/brokerage-api/markets/get-options-chains
    """
    BASE = "https://api.tradier.com/v1/markets"
    HEADERS = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json"
    }

    try:
        # ── Underlying quote ──
        r = requests.get(f"{BASE}/quotes", params={"symbols": ticker}, headers=HEADERS, timeout=10)
        r.raise_for_status()
        q = r.json()["quotes"]["quote"]
        price      = float(q.get("last") or q.get("close") or 0)
        prev_close = float(q.get("prevclose") or price)
        change_pct = (price - prev_close) / prev_close if prev_close else 0
        volume     = int(q.get("volume") or 0)
        div_yield  = 0.0  # Tradier doesn't expose div yield in quotes endpoint

        underlying = UnderlyingData(
            ticker=ticker, price=price, prev_close=prev_close,
            change_pct=change_pct, volume=volume, div_yield=div_yield
        )

        # ── Option expirations ──
        r = requests.get(f"{BASE}/options/expirations",
                         params={"symbol": ticker, "includeAllRoots": "true"},
                         headers=HEADERS, timeout=10)
        r.raise_for_status()
        expirations = r.json().get("expirations", {}).get("date", [])
        if isinstance(expirations, str):
            expirations = [expirations]

        today     = date.today()
        contracts = []

        for exp_str in expirations:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte = (exp_date - today).days
            if dte < 0 or dte > 90:
                continue

            r = requests.get(
                f"{BASE}/options/chains",
                params={"symbol": ticker, "expiration": exp_str, "greeks": "false"},
                headers=HEADERS, timeout=15
            )
            if r.status_code != 200:
                continue

            options = r.json().get("options", {}).get("option", [])
            if not options:
                continue

            for opt in options:
                try:
                    oi     = int(opt.get("open_interest") or 0)
                    vol    = int(opt.get("volume") or 0)
                    strike = float(opt["strike"])
                    bid    = float(opt.get("bid") or 0)
                    ask    = float(opt.get("ask") or 0)
                    last   = float(opt.get("last") or 0)
                    otype  = opt.get("option_type", "").lower()   # 'call' or 'put'

                    if oi < min_oi or vol < min_vol:
                        continue
                    if abs(strike - price) / price > 0.40:
                        continue

                    contracts.append(OptionContract(
                        ticker=ticker, expiry=exp_date, dte=dte,
                        strike=strike, option_type=otype,
                        bid=bid, ask=ask, last=last,
                        volume=vol, open_interest=oi
                    ))
                except Exception:
                    continue

            time.sleep(0.2)

        logger.info(f"{ticker}: fetched {len(contracts)} contracts via Tradier")
        return OptionsChain(underlying=underlying, contracts=contracts, source="tradier")

    except Exception as e:
        logger.error(f"{ticker} Tradier fetch failed: {e}")
        return None


# ─────────────────────────────────────────────
#  POLYGON BACKEND  (alternative paid source)
# ─────────────────────────────────────────────

def _fetch_polygon(ticker: str, api_key: str, min_oi: int = 50) -> Optional[OptionsChain]:
    """
    Fetch via Polygon.io options API.
    Requires Starter plan or above for options data.
    Docs: https://polygon.io/docs/options/get_v3_snapshot_options__underlyingAsset
    """
    BASE = "https://api.polygon.io"

    try:
        # ── Underlying price ──
        r = requests.get(
            f"{BASE}/v2/last/trade/{ticker}",
            params={"apiKey": api_key}, timeout=10
        )
        r.raise_for_status()
        price = float(r.json()["results"]["p"])

        underlying = UnderlyingData(
            ticker=ticker, price=price, prev_close=price,
            change_pct=0.0, volume=0, div_yield=0.0
        )

        # ── Options snapshot ──
        today      = date.today()
        contracts  = []
        cursor     = None
        max_pages  = 20

        for _ in range(max_pages):
            params = {
                "apiKey":       api_key,
                "limit":        250,
                "contract_type": "put",   # fetch puts first, then calls
                "expiration_date.gte": today.strftime("%Y-%m-%d"),
                "expiration_date.lte": (today + timedelta(days=90)).strftime("%Y-%m-%d"),
            }
            if cursor:
                params["cursor"] = cursor

            for otype in ["put", "call"]:
                params["contract_type"] = otype
                r = requests.get(
                    f"{BASE}/v3/snapshot/options/{ticker}",
                    params=params, timeout=15
                )
                if r.status_code != 200:
                    continue

                data    = r.json()
                results = data.get("results", [])

                for res in results:
                    try:
                        details = res.get("details", {})
                        day     = res.get("day", {})
                        greeks  = res.get("greeks", {})

                        exp_str  = details.get("expiration_date", "")
                        exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                        dte      = (exp_date - today).days
                        if dte < 0:
                            continue

                        strike = float(details.get("strike_price", 0))
                        oi     = int(res.get("open_interest", 0) or 0)
                        vol    = int(day.get("volume", 0) or 0)
                        bid    = float(res.get("last_quote", {}).get("bid", 0) or 0)
                        ask    = float(res.get("last_quote", {}).get("ask", 0) or 0)
                        last   = float(day.get("close", 0) or 0)

                        if oi < min_oi:
                            continue
                        if abs(strike - price) / price > 0.40:
                            continue

                        contracts.append(OptionContract(
                            ticker=ticker, expiry=exp_date, dte=dte,
                            strike=strike, option_type=otype,
                            bid=bid, ask=ask, last=last,
                            volume=vol, open_interest=oi
                        ))
                    except Exception:
                        continue

                cursor = data.get("next_url", "").split("cursor=")[-1] if "cursor=" in data.get("next_url", "") else None

            if not cursor:
                break

        logger.info(f"{ticker}: fetched {len(contracts)} contracts via Polygon")
        return OptionsChain(underlying=underlying, contracts=contracts, source="polygon")

    except Exception as e:
        logger.error(f"{ticker} Polygon fetch failed: {e}")
        return None


# ─────────────────────────────────────────────
#  UNIFIED FETCH INTERFACE
# ─────────────────────────────────────────────

def fetch_options_chain(ticker: str, source: str = "yfinance",
                        tradier_key: str = "", polygon_key: str = "") -> Optional[OptionsChain]:
    """
    Unified entry point. Falls back to yfinance if preferred source fails.
    """
    chain = None

    if source == "tradier" and tradier_key:
        chain = _fetch_tradier(ticker, tradier_key)
    elif source == "polygon" and polygon_key:
        chain = _fetch_polygon(ticker, polygon_key)

    if chain is None:
        if source != "yfinance":
            logger.warning(f"{ticker}: {source} failed, falling back to yfinance")
        chain = _fetch_yfinance(ticker)

    return chain
