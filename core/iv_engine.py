"""
core/iv_engine.py — Implied Volatility & Greeks computation engine

Uses Black-Scholes with Newton-Raphson / Brent's method fallback.
All math is local — no API calls, no external dependencies beyond scipy/numpy.
"""

import math
import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq
from typing import Optional, Tuple
import warnings
warnings.filterwarnings("ignore")


# ─────────────────────────────────────────────
#  BLACK-SCHOLES CORE
# ─────────────────────────────────────────────

def _d1_d2(S: float, K: float, T: float, r: float, q: float, sigma: float) -> Tuple[float, float]:
    """Compute d1 and d2 for Black-Scholes-Merton."""
    if T <= 0 or sigma <= 0:
        return 0.0, 0.0
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return d1, d2


def bs_price(S: float, K: float, T: float, r: float, q: float,
             sigma: float, option_type: str = "call") -> float:
    """
    Black-Scholes-Merton option price.

    Parameters
    ----------
    S           : underlying spot price
    K           : strike price
    T           : time to expiry in years
    r           : risk-free rate (annualized)
    q           : continuous dividend yield (annualized)
    sigma       : implied volatility (annualized)
    option_type : 'call' or 'put'
    """
    if T <= 1e-6:
        if option_type == "call":
            return max(S - K, 0.0)
        else:
            return max(K - S, 0.0)

    d1, d2 = _d1_d2(S, K, T, r, q, sigma)
    if option_type == "call":
        return (S * math.exp(-q * T) * norm.cdf(d1)
                - K * math.exp(-r * T) * norm.cdf(d2))
    else:
        return (K * math.exp(-r * T) * norm.cdf(-d2)
                - S * math.exp(-q * T) * norm.cdf(-d1))


def bs_vega(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    """Vega (derivative of price w.r.t. sigma)."""
    if T <= 1e-6 or sigma <= 0:
        return 1e-10
    d1, _ = _d1_d2(S, K, T, r, q, sigma)
    return S * math.exp(-q * T) * norm.pdf(d1) * math.sqrt(T)


def bs_delta(S: float, K: float, T: float, r: float, q: float,
             sigma: float, option_type: str = "call") -> float:
    """Delta of the option."""
    if T <= 1e-6:
        if option_type == "call":
            return 1.0 if S > K else 0.0
        else:
            return -1.0 if S < K else 0.0
    d1, _ = _d1_d2(S, K, T, r, q, sigma)
    if option_type == "call":
        return math.exp(-q * T) * norm.cdf(d1)
    else:
        return math.exp(-q * T) * (norm.cdf(d1) - 1)


def bs_gamma(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    """Gamma of the option."""
    if T <= 1e-6 or sigma <= 0:
        return 0.0
    d1, _ = _d1_d2(S, K, T, r, q, sigma)
    return (math.exp(-q * T) * norm.pdf(d1)) / (S * sigma * math.sqrt(T))


def bs_theta(S: float, K: float, T: float, r: float, q: float,
             sigma: float, option_type: str = "call") -> float:
    """Theta (per calendar day)."""
    if T <= 1e-6:
        return 0.0
    d1, d2 = _d1_d2(S, K, T, r, q, sigma)
    term1 = -(S * math.exp(-q * T) * norm.pdf(d1) * sigma) / (2 * math.sqrt(T))
    if option_type == "call":
        theta = term1 - r * K * math.exp(-r * T) * norm.cdf(d2) + q * S * math.exp(-q * T) * norm.cdf(d1)
    else:
        theta = term1 + r * K * math.exp(-r * T) * norm.cdf(-d2) - q * S * math.exp(-q * T) * norm.cdf(-d1)
    return theta / 365.0   # per calendar day


# ─────────────────────────────────────────────
#  IMPLIED VOLATILITY SOLVER
# ─────────────────────────────────────────────

def compute_iv(market_price: float, S: float, K: float, T: float,
               r: float, q: float, option_type: str = "call",
               tol: float = 1e-6, max_iter: int = 500) -> Optional[float]:
    """
    Compute implied volatility via Newton-Raphson with Brent's method fallback.

    Returns None if solver fails (e.g., deep ITM with no time value).
    """
    if market_price <= 0 or T <= 1e-6:
        return None

    # Intrinsic value check
    if option_type == "call":
        intrinsic = max(S - K * math.exp(-r * T), 0)
    else:
        intrinsic = max(K * math.exp(-r * T) - S, 0)

    if market_price < intrinsic - 0.01:
        return None   # price below intrinsic — data error

    # ── Newton-Raphson ──────────────────────────────────
    sigma = 0.25   # initial guess
    for _ in range(max_iter):
        price = bs_price(S, K, T, r, q, sigma, option_type)
        vega  = bs_vega(S, K, T, r, q, sigma)
        diff  = price - market_price
        if abs(diff) < tol:
            if 0.001 <= sigma <= 5.0:
                return sigma
            break
        if vega < 1e-10:
            break
        sigma -= diff / vega
        sigma  = max(0.001, min(sigma, 5.0))   # clamp to sane range

    # ── Brent's method fallback ─────────────────────────
    try:
        def objective(s):
            return bs_price(S, K, T, r, q, s, option_type) - market_price

        # Check if root exists in bracket
        lo, hi = 0.001, 5.0
        if objective(lo) * objective(hi) > 0:
            return None

        result = brentq(objective, lo, hi, xtol=tol, maxiter=max_iter)
        return result if 0.001 <= result <= 5.0 else None
    except Exception:
        return None


# ─────────────────────────────────────────────
#  DELTA → STRIKE INTERPOLATION
# ─────────────────────────────────────────────

def find_strike_for_delta(target_delta: float, S: float, T: float, r: float,
                          q: float, sigma_atm: float,
                          option_type: str = "put") -> Optional[float]:
    """
    Given a target absolute delta (e.g., 0.25), find the corresponding strike
    using ATM vol as a seed and bracketing search.

    Returns the strike price, or None if not solvable.
    """
    if target_delta <= 0 or target_delta >= 1 or T <= 1e-6:
        return None

    target = -abs(target_delta) if option_type == "put" else abs(target_delta)

    def delta_diff(K):
        d = bs_delta(S, K, T, r, q, sigma_atm, option_type)
        return d - target

    # Bracket: puts have negative delta, calls positive
    try:
        if option_type == "put":
            K_low  = S * 0.50   # deep OTM put
            K_high = S * 1.10   # slightly ITM put
        else:
            K_low  = S * 0.90
            K_high = S * 1.50

        if delta_diff(K_low) * delta_diff(K_high) > 0:
            return None

        K = brentq(delta_diff, K_low, K_high, xtol=0.01, maxiter=200)
        return round(K, 2)
    except Exception:
        return None


# ─────────────────────────────────────────────
#  SKEW METRIC HELPERS
# ─────────────────────────────────────────────

def compute_risk_reversal(iv_put_25d: Optional[float],
                          iv_call_25d: Optional[float]) -> Optional[float]:
    """
    Risk Reversal = IV(25Δ call) - IV(25Δ put)
    Negative in equity markets (puts more expensive than calls).
    """
    if iv_put_25d is None or iv_call_25d is None:
        return None
    return iv_call_25d - iv_put_25d   # will be negative when puts > calls


def compute_butterfly(iv_put_25d: Optional[float],
                      iv_call_25d: Optional[float],
                      iv_atm: Optional[float]) -> Optional[float]:
    """
    25Δ Butterfly = 0.5 * (IV_put25 + IV_call25) - IV_atm
    Measures curvature / wing richness.
    """
    if any(v is None for v in [iv_put_25d, iv_call_25d, iv_atm]):
        return None
    return 0.5 * (iv_put_25d + iv_call_25d) - iv_atm


def classify_skew_regime(risk_reversal: Optional[float],
                         percentile: Optional[float]) -> str:
    """
    Return a human-readable skew regime label.
    """
    if risk_reversal is None:
        return "UNKNOWN"
    rr_abs = abs(risk_reversal)
    if rr_abs < 0.03:
        return "FLAT (COMPLACENT)"
    elif rr_abs < 0.05:
        return "NORMAL"
    elif rr_abs < 0.07:
        return "ELEVATED (CAUTIOUS)"
    elif rr_abs < 0.09:
        return "STEEP (FEARFUL)"
    else:
        return "EXTREME (CRASH PRICING)"


def compute_skew_percentile(current_rr: float,
                             history: list) -> Optional[float]:
    """
    Percentile rank of current RR vs. historical values.
    history: list of past risk reversal floats (negative values).
    """
    if len(history) < 5:
        return None
    arr = np.array(history)
    # More negative = steeper skew. Percentile of steepness.
    rank = np.sum(arr >= current_rr)   # how many days had flatter (less negative) skew
    return round((rank / len(arr)) * 100, 1)
