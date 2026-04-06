"""
core/skew_engine.py — Full skew surface computation

Takes a raw OptionsChain and produces a structured SkewProfile
containing all metrics needed for strategy recommendations and
the Claude API payload (kept compact to minimize token cost).
"""

import math
import logging
import numpy as np
from datetime import date
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .iv_engine import (
    compute_iv, bs_delta, bs_theta, bs_gamma,
    compute_risk_reversal, compute_butterfly,
    classify_skew_regime, compute_skew_percentile
)
from .data_fetcher import OptionsChain, OptionContract

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  OUTPUT STRUCTURES
# ─────────────────────────────────────────────

@dataclass
class ExpirySkew:
    """Skew metrics for a single expiration bucket."""
    label:          str             # "daily", "weekly", "monthly"
    dte:            int             # representative DTE
    expiry_date:    Optional[date]
    atm_iv:         Optional[float]
    iv_put_10d:     Optional[float]
    iv_put_15d:     Optional[float]
    iv_put_25d:     Optional[float]
    iv_call_10d:    Optional[float]
    iv_call_15d:    Optional[float]
    iv_call_25d:    Optional[float]
    risk_reversal:  Optional[float]   # call25 - put25  (negative in equity mkts)
    butterfly_25d:  Optional[float]   # wing curvature
    atm_theta:      Optional[float]   # per day
    atm_gamma:      Optional[float]
    expected_move:  Optional[float]   # 1σ move in dollars

    def to_compact_dict(self) -> dict:
        """Compact JSON for Claude API payload — keeps tokens minimal."""
        def fmt(v, pct=False, dp=1):
            if v is None:
                return "N/A"
            if pct:
                return f"{v*100:.{dp}f}%"
            return f"{v:.{dp}f}"

        return {
            "label":         self.label,
            "dte":           self.dte,
            "atm_iv":        fmt(self.atm_iv, pct=True),
            "put_10d_iv":    fmt(self.iv_put_10d, pct=True),
            "put_25d_iv":    fmt(self.iv_put_25d, pct=True),
            "call_25d_iv":   fmt(self.iv_call_25d, pct=True),
            "call_10d_iv":   fmt(self.iv_call_10d, pct=True),
            "risk_reversal": fmt(self.risk_reversal, pct=True),
            "butterfly_25d": fmt(self.butterfly_25d, pct=True),
            "theta_day":     fmt(self.atm_theta, dp=2) if self.atm_theta else "N/A",
            "exp_move_1sd":  fmt(self.expected_move, dp=2) if self.expected_move else "N/A",
        }


@dataclass
class SkewProfile:
    """Complete skew surface profile for one ticker."""
    ticker:           str
    spot:             float
    change_pct:       float
    div_yield:        float
    risk_free_rate:   float
    run_session:      str             # "morning" or "evening"
    run_time:         str             # timestamp string
    data_source:      str

    daily:            Optional[ExpirySkew] = None
    weekly:           Optional[ExpirySkew] = None
    monthly:          Optional[ExpirySkew] = None

    # Cross-expiry metrics
    term_structure_slope: Optional[float] = None  # monthly_atm - daily_atm
    skew_percentile_weekly: Optional[float] = None
    skew_regime:        str = "UNKNOWN"
    iv_rank:            Optional[float] = None    # filled by history module
    hv_30d:             Optional[float] = None

    errors: List[str] = field(default_factory=list)

    def to_claude_payload(self) -> dict:
        """
        Minimal structured payload for Claude API call.
        Designed to stay under ~1,000 tokens per ticker.
        """
        expirations = {}
        for label, exp in [("daily", self.daily), ("weekly", self.weekly), ("monthly", self.monthly)]:
            if exp is not None:
                expirations[label] = exp.to_compact_dict()

        return {
            "ticker":       self.ticker,
            "spot":         round(self.spot, 2),
            "change_pct":   f"{self.change_pct*100:.2f}%",
            "iv_rank":      f"{self.iv_rank:.1f}" if self.iv_rank is not None else "N/A",
            "hv_30d":       f"{self.hv_30d*100:.1f}%" if self.hv_30d else "N/A",
            "skew_regime":  self.skew_regime,
            "skew_pctile":  f"{self.skew_percentile_weekly:.0f}th" if self.skew_percentile_weekly else "N/A",
            "term_slope":   f"{self.term_structure_slope*100:.1f}%" if self.term_structure_slope else "N/A",
            "expirations":  expirations,
            "session":      self.run_session,
        }


# ─────────────────────────────────────────────
#  HISTORICAL VOLATILITY
# ─────────────────────────────────────────────

def compute_hv30(prices: List[float]) -> Optional[float]:
    """30-day historical volatility from a list of daily closes."""
    if len(prices) < 22:
        return None
    closes = np.array(prices[-31:])
    log_returns = np.diff(np.log(closes))
    return float(np.std(log_returns, ddof=1) * math.sqrt(252))


# ─────────────────────────────────────────────
#  CORE SKEW COMPUTATION
# ─────────────────────────────────────────────

def _get_best_mid(contracts: List[OptionContract], strike: float,
                  option_type: str) -> Optional[float]:
    """Find the best mid-price for a given strike and type."""
    matches = [c for c in contracts if c.option_type == option_type
               and abs(c.strike - strike) < 0.01]
    if not matches:
        # Find nearest strike within 1% of spot
        nearest = min(contracts, key=lambda c: abs(c.strike - strike)
                      if c.option_type == option_type else float('inf'), default=None)
        if nearest and abs(nearest.strike - strike) / strike < 0.015:
            return nearest.mid_price if nearest.mid_price > 0 else None
        return None
    best = max(matches, key=lambda c: c.open_interest)
    return best.mid_price if best.mid_price > 0 else None


def _compute_expiry_skew(ticker: str, spot: float, r: float, q: float,
                         contracts: List[OptionContract],
                         label: str, dte_range: Tuple[int, int]) -> Optional[ExpirySkew]:
    """
    Compute full skew metrics for one expiry bucket.
    Finds the nearest expiration within the DTE range.
    """
    in_range = [c for c in contracts if dte_range[0] <= c.dte <= dte_range[1]]
    if not in_range:
        logger.debug(f"{ticker} {label}: no contracts in DTE range {dte_range}")
        return None

    # Pick the expiry with the most open interest
    exp_oi: Dict[date, int] = {}
    for c in in_range:
        exp_oi[c.expiry] = exp_oi.get(c.expiry, 0) + c.open_interest
    best_expiry = max(exp_oi, key=exp_oi.get)
    exp_contracts = [c for c in in_range if c.expiry == best_expiry]
    dte = (best_expiry - date.today()).days
    T   = max(dte / 365.0, 1 / 365.0)

    # ── ATM IV ──────────────────────────────────────────────────
    atm_strike = min(exp_contracts, key=lambda c: abs(c.strike - spot),
                     default=None)
    if atm_strike is None:
        return None

    # Use midpoint of nearest call and put at ATM
    atm_iv = None
    for otype in ["call", "put"]:
        mid = _get_best_mid(exp_contracts, atm_strike.strike, otype)
        if mid and mid > 0:
            iv = compute_iv(mid, spot, atm_strike.strike, T, r, q, otype)
            if iv:
                atm_iv = iv
                break

    if atm_iv is None:
        logger.debug(f"{ticker} {label}: could not compute ATM IV")
        return None

    # ── IV at target deltas ──────────────────────────────────────
    # Approximate target strikes using ATM vol as seed
    delta_ivs: Dict[str, Optional[float]] = {}

    for delta_target, otype in [
        (0.10, "put"), (0.15, "put"), (0.20, "put"), (0.25, "put"),
        (0.10, "call"), (0.15, "call"), (0.20, "call"), (0.25, "call"),
    ]:
        key = f"{otype}_{int(delta_target*100)}d"
        delta_ivs[key] = None

        # Find the contract whose delta is closest to target
        type_contracts = [c for c in exp_contracts if c.option_type == otype and c.mid_price > 0]
        if not type_contracts:
            continue

        best_contract = None
        best_delta_diff = float('inf')

        for c in type_contracts:
            iv_c = compute_iv(c.mid_price, spot, c.strike, T, r, q, otype)
            if iv_c is None:
                continue
            d = bs_delta(spot, c.strike, T, r, q, iv_c, otype)
            diff = abs(abs(d) - delta_target)
            if diff < best_delta_diff:
                best_delta_diff = diff
                best_contract   = (c, iv_c)

        if best_contract and best_delta_diff < 0.08:   # within 8 delta points
            delta_ivs[key] = best_contract[1]

    # ── ATM Greeks ─────────────────────────────────────────────
    atm_theta = bs_theta(spot, atm_strike.strike, T, r, q, atm_iv, "call")
    atm_gamma = bs_gamma(spot, atm_strike.strike, T, r, q, atm_iv)

    # 1σ expected move in dollars
    expected_move = spot * atm_iv * math.sqrt(T)

    # ── Risk Reversal & Butterfly ───────────────────────────────
    rr = compute_risk_reversal(delta_ivs.get("put_25d"), delta_ivs.get("call_25d"))
    bf = compute_butterfly(delta_ivs.get("put_25d"), delta_ivs.get("call_25d"), atm_iv)

    return ExpirySkew(
        label=label, dte=dte, expiry_date=best_expiry,
        atm_iv=atm_iv,
        iv_put_10d=delta_ivs.get("put_10d"),
        iv_put_15d=delta_ivs.get("put_15d"),
        iv_put_25d=delta_ivs.get("put_25d"),
        iv_call_10d=delta_ivs.get("call_10d"),
        iv_call_15d=delta_ivs.get("call_15d"),
        iv_call_25d=delta_ivs.get("call_25d"),
        risk_reversal=rr,
        butterfly_25d=bf,
        atm_theta=atm_theta,
        atm_gamma=atm_gamma,
        expected_move=expected_move,
    )


# ─────────────────────────────────────────────
#  MAIN ENTRY POINT
# ─────────────────────────────────────────────

def build_skew_profile(chain: OptionsChain, r: float, q_override: Optional[float],
                       session: str, skew_history: List[float],
                       price_history: List[float]) -> SkewProfile:
    """
    Build a complete SkewProfile from a raw OptionsChain.

    Parameters
    ----------
    chain         : raw data from data_fetcher
    r             : risk-free rate
    q_override    : override dividend yield (use None to use chain value)
    session       : "morning" or "evening"
    skew_history  : list of past weekly 25Δ RR floats for percentile calc
    price_history : list of past daily closes for HV30
    """
    from datetime import datetime
    spot = chain.underlying.price
    q    = q_override if q_override is not None else chain.underlying.div_yield

    profile = SkewProfile(
        ticker=chain.underlying.ticker,
        spot=spot,
        change_pct=chain.underlying.change_pct,
        div_yield=q,
        risk_free_rate=r,
        run_session=session,
        run_time=datetime.now().strftime("%Y-%m-%d %H:%M"),
        data_source=chain.source,
    )

    if spot <= 0:
        profile.errors.append("Invalid spot price")
        return profile

    # ── Compute each expiry bucket ───────────────────────────
    from config import TARGET_EXPIRATIONS
    for label, dte_range in TARGET_EXPIRATIONS.items():
        try:
            exp_skew = _compute_expiry_skew(
                chain.underlying.ticker, spot, r, q,
                chain.contracts, label, dte_range
            )
            setattr(profile, label, exp_skew)
        except Exception as e:
            profile.errors.append(f"{label} computation error: {e}")
            logger.error(f"{chain.underlying.ticker} {label}: {e}", exc_info=True)

    # ── Cross-expiry metrics ─────────────────────────────────
    if profile.daily and profile.monthly:
        if profile.daily.atm_iv and profile.monthly.atm_iv:
            profile.term_structure_slope = profile.monthly.atm_iv - profile.daily.atm_iv

    # ── Skew regime and percentile ───────────────────────────
    rr_weekly = profile.weekly.risk_reversal if profile.weekly else None
    if rr_weekly:
        profile.skew_regime     = classify_skew_regime(rr_weekly, None)
        profile.skew_percentile_weekly = compute_skew_percentile(rr_weekly, skew_history)

    # ── HV30 ────────────────────────────────────────────────
    profile.hv_30d = compute_hv30(price_history) if len(price_history) >= 22 else None

    # ── IV Rank (requires history module to fill) ────────────
    # Left as None here; filled by history_manager before Claude call

    return profile
