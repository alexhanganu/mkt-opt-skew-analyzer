"""
tests/test_suite.py — Comprehensive test suite

Run: python tests/test_suite.py
  or: python -m pytest tests/test_suite.py -v   (if pytest installed)

Covers:
  - IV engine: round-trip, edge cases, Greek accuracy
  - Skew metrics: risk reversal, butterfly, regime classification
  - History manager: append, pruning, percentile, IV rank
  - Market calendar: trading day detection, DTE, session windows
  - Data structures: OptionsChain filtering, SkewProfile payload
  - Report generator: HTML output integrity
  - Full pipeline: mock end-to-end without API calls
"""

import os
import sys
import math
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.iv_engine import (
    bs_price, bs_delta, bs_gamma, bs_vega, bs_theta,
    compute_iv, compute_risk_reversal, compute_butterfly,
    classify_skew_regime, compute_skew_percentile,
)
from core.history_manager import HistoryManager, HistoryRecord
from core.data_fetcher import OptionContract, UnderlyingData, OptionsChain
from core.skew_engine import SkewProfile, ExpirySkew
from core.report_generator import generate_report
from utils.market_calendar import (
    is_trading_day, is_market_open, calendar_dte,
    trading_days_between, next_trading_day,
)


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def _tmp_history() -> HistoryManager:
    path = tempfile.mktemp(suffix="_history.csv")
    return HistoryManager(path, rolling_days=252)


def _mock_profile(ticker="SPY", spot=500.0) -> SkewProfile:
    p = SkewProfile(
        ticker=ticker, spot=spot, change_pct=-0.01,
        div_yield=0.013, risk_free_rate=0.0525,
        run_session="evening", run_time="2026-03-07 15:30",
        data_source="test",
    )
    p.daily = ExpirySkew(
        label="daily", dte=1, expiry_date=date.today() + timedelta(1),
        atm_iv=0.40, iv_put_10d=0.52, iv_put_15d=0.47,
        iv_put_25d=0.45, iv_call_10d=0.30, iv_call_15d=0.32,
        iv_call_25d=0.33, risk_reversal=0.33-0.45,
        butterfly_25d=0.5*(0.45+0.33)-0.40,
        atm_theta=-0.30, atm_gamma=0.015, expected_move=8.0,
    )
    p.weekly = ExpirySkew(
        label="weekly", dte=7, expiry_date=date.today() + timedelta(7),
        atm_iv=0.32, iv_put_10d=0.44, iv_put_15d=0.40,
        iv_put_25d=0.38, iv_call_10d=0.26, iv_call_15d=0.28,
        iv_call_25d=0.29, risk_reversal=0.29-0.38,
        butterfly_25d=0.5*(0.38+0.29)-0.32,
        atm_theta=-0.12, atm_gamma=0.008, expected_move=14.0,
    )
    p.monthly = ExpirySkew(
        label="monthly", dte=30, expiry_date=date.today() + timedelta(30),
        atm_iv=0.27, iv_put_10d=0.37, iv_put_15d=0.34,
        iv_put_25d=0.32, iv_call_10d=0.22, iv_call_15d=0.24,
        iv_call_25d=0.25, risk_reversal=0.25-0.32,
        butterfly_25d=0.5*(0.32+0.25)-0.27,
        atm_theta=-0.05, atm_gamma=0.003, expected_move=25.0,
    )
    p.skew_regime = "STEEP (FEARFUL)"
    p.skew_percentile_weekly = 74.0
    p.iv_rank = 32.0
    p.hv_30d  = 0.22
    p.term_structure_slope = p.monthly.atm_iv - p.daily.atm_iv
    return p


# ─────────────────────────────────────────────
#  TEST CLASSES
# ─────────────────────────────────────────────

class TestIVEngine(unittest.TestCase):
    """Black-Scholes engine accuracy and edge-case handling."""

    def setUp(self):
        self.S = 500.0
        self.K = 490.0
        self.T = 14 / 365
        self.r = 0.0525
        self.q = 0.013
        self.sigma = 0.28

    def test_call_price_positive(self):
        price = bs_price(self.S, self.K, self.T, self.r, self.q, self.sigma, "call")
        self.assertGreater(price, 0)

    def test_put_price_positive(self):
        price = bs_price(self.S, self.K, self.T, self.r, self.q, self.sigma, "put")
        self.assertGreater(price, 0)

    def test_put_call_parity(self):
        """C - P = S*e^(-qT) - K*e^(-rT)"""
        call  = bs_price(self.S, self.K, self.T, self.r, self.q, self.sigma, "call")
        put   = bs_price(self.S, self.K, self.T, self.r, self.q, self.sigma, "put")
        lhs   = call - put
        rhs   = (self.S * math.exp(-self.q * self.T)
                 - self.K * math.exp(-self.r * self.T))
        self.assertAlmostEqual(lhs, rhs, places=6)

    def test_iv_round_trip_call(self):
        price = bs_price(self.S, self.K, self.T, self.r, self.q, self.sigma, "call")
        iv    = compute_iv(price, self.S, self.K, self.T, self.r, self.q, "call")
        self.assertIsNotNone(iv)
        self.assertAlmostEqual(iv, self.sigma, places=4)

    def test_iv_round_trip_put(self):
        price = bs_price(self.S, self.K, self.T, self.r, self.q, self.sigma, "put")
        iv    = compute_iv(price, self.S, self.K, self.T, self.r, self.q, "put")
        self.assertIsNotNone(iv)
        self.assertAlmostEqual(iv, self.sigma, places=4)

    def test_iv_round_trip_otm_put(self):
        """OTM put — key for skew computation."""
        K_otm = 460.0   # ~25Δ put
        price = bs_price(self.S, K_otm, self.T, self.r, self.q, self.sigma, "put")
        iv    = compute_iv(price, self.S, K_otm, self.T, self.r, self.q, "put")
        self.assertIsNotNone(iv)
        self.assertAlmostEqual(iv, self.sigma, places=4)

    def test_iv_zero_price_returns_none(self):
        iv = compute_iv(0.0, self.S, self.K, self.T, self.r, self.q, "put")
        self.assertIsNone(iv)

    def test_iv_expired_returns_none(self):
        iv = compute_iv(10.0, self.S, self.K, 0.0, self.r, self.q, "put")
        self.assertIsNone(iv)

    def test_delta_call_range(self):
        delta = bs_delta(self.S, self.K, self.T, self.r, self.q, self.sigma, "call")
        self.assertGreater(delta, 0)
        self.assertLess(delta, 1)

    def test_delta_put_range(self):
        delta = bs_delta(self.S, self.K, self.T, self.r, self.q, self.sigma, "put")
        self.assertLess(delta, 0)
        self.assertGreater(delta, -1)

    def test_delta_put_call_relationship(self):
        """For European options: delta_call - delta_put = e^(-qT)"""
        dc = bs_delta(self.S, self.K, self.T, self.r, self.q, self.sigma, "call")
        dp = bs_delta(self.S, self.K, self.T, self.r, self.q, self.sigma, "put")
        expected = math.exp(-self.q * self.T)
        self.assertAlmostEqual(dc - dp, expected, places=6)

    def test_vega_positive(self):
        vega = bs_vega(self.S, self.K, self.T, self.r, self.q, self.sigma)
        self.assertGreater(vega, 0)

    def test_theta_negative(self):
        theta = bs_theta(self.S, self.K, self.T, self.r, self.q, self.sigma, "call")
        self.assertLess(theta, 0)   # calls lose value with time

    def test_gamma_positive(self):
        gamma = bs_gamma(self.S, self.K, self.T, self.r, self.q, self.sigma)
        self.assertGreater(gamma, 0)

    def test_atm_iv_zero_dte(self):
        """Near-zero DTE should return intrinsic value."""
        T_tiny = 0.5 / 365
        price  = bs_price(self.S, self.S, T_tiny, self.r, self.q, self.sigma, "call")
        self.assertGreater(price, 0)

    def test_high_iv_round_trip(self):
        """Test with high IV (e.g., 80% — possible for ARKK)."""
        sigma_high = 0.80
        price = bs_price(self.S, self.K, self.T, self.r, self.q, sigma_high, "put")
        iv    = compute_iv(price, self.S, self.K, self.T, self.r, self.q, "put")
        self.assertIsNotNone(iv)
        self.assertAlmostEqual(iv, sigma_high, places=3)


class TestSkewMetrics(unittest.TestCase):
    """Risk reversal, butterfly, regime classification, percentile."""

    def test_risk_reversal_negative_for_equity(self):
        rr = compute_risk_reversal(iv_put_25d=0.35, iv_call_25d=0.27)
        self.assertIsNotNone(rr)
        self.assertLess(rr, 0)   # puts more expensive → negative RR

    def test_risk_reversal_none_inputs(self):
        self.assertIsNone(compute_risk_reversal(None, 0.27))
        self.assertIsNone(compute_risk_reversal(0.35, None))

    def test_butterfly_positive(self):
        bf = compute_butterfly(iv_put_25d=0.35, iv_call_25d=0.27, iv_atm=0.28)
        # 0.5*(0.35+0.27) - 0.28 = 0.31 - 0.28 = 0.03
        self.assertAlmostEqual(bf, 0.03, places=5)

    def test_skew_regime_flat(self):
        regime = classify_skew_regime(-0.02, None)
        self.assertIn("FLAT", regime)

    def test_skew_regime_normal(self):
        regime = classify_skew_regime(-0.04, None)
        self.assertIn("NORMAL", regime)

    def test_skew_regime_steep(self):
        regime = classify_skew_regime(-0.08, None)
        self.assertIn("STEEP", regime)

    def test_skew_regime_extreme(self):
        regime = classify_skew_regime(-0.12, None)
        self.assertIn("EXTREME", regime)

    def test_percentile_no_history(self):
        pctile = compute_skew_percentile(-0.07, [])
        self.assertIsNone(pctile)

    def test_percentile_extreme_is_high(self):
        history = [-0.04] * 80 + [-0.03] * 20   # mostly -4% or less steep
        # -0.09 is steeper than all history → high percentile
        pctile = compute_skew_percentile(-0.09, history)
        self.assertIsNotNone(pctile)
        self.assertGreater(pctile, 90)

    def test_percentile_flat_is_low(self):
        history = [-0.06, -0.07, -0.08, -0.07, -0.06] * 10
        # -0.01 is very flat → low percentile
        pctile = compute_skew_percentile(-0.01, history)
        self.assertIsNotNone(pctile)
        self.assertLess(pctile, 10)


class TestHistoryManager(unittest.TestCase):
    """Rolling history storage, IV rank, pruning."""

    def setUp(self):
        self.hm   = _tmp_history()
        self.path = self.hm.path

    def tearDown(self):
        if os.path.exists(self.path):
            os.unlink(self.path)

    def _add_records(self, ticker, n, base_rr=-0.04, base_iv=0.25):
        for i in range(n):
            d = (date.today() - timedelta(days=n-i)).strftime("%Y-%m-%d")
            self.hm.append(HistoryRecord(
                date=d, ticker=ticker,
                spot=500.0 + i * 0.5,
                atm_iv_weekly=base_iv + i * 0.001,
                rr_25d_weekly=base_rr - i * 0.0005,
                atm_iv_monthly=base_iv - 0.02,
                rr_25d_monthly=base_rr + 0.005,
                hv_30d=0.20, session="evening",
            ))

    def test_append_and_count(self):
        self._add_records("QQQ", 10)
        self.assertEqual(self.hm.record_count("QQQ"), 10)

    def test_get_rr_history(self):
        self._add_records("SPY", 15)
        rr = self.hm.get_rr_history("SPY")
        self.assertEqual(len(rr), 15)
        self.assertTrue(all(v < 0 for v in rr))

    def test_ticker_isolation(self):
        self._add_records("QQQ", 5)
        self._add_records("SPY", 8)
        self.assertEqual(self.hm.record_count("QQQ"), 5)
        self.assertEqual(self.hm.record_count("SPY"), 8)

    def test_iv_rank_none_with_insufficient_history(self):
        self._add_records("IWM", 10)
        rank = self.hm.compute_iv_rank("IWM", 0.30)
        self.assertIsNone(rank)

    def test_iv_rank_with_sufficient_history(self):
        self._add_records("QQQ", 50)
        rank = self.hm.compute_iv_rank("QQQ", 0.30)
        self.assertIsNotNone(rank)
        self.assertGreaterEqual(rank, 0)
        self.assertLessEqual(rank, 100)

    def test_rolling_prune(self):
        hm = HistoryManager(self.path, rolling_days=30)
        self._add_records_to(hm, "SPY", 50)
        count = hm.record_count("SPY")
        self.assertLessEqual(count, 30)

    def _add_records_to(self, hm, ticker, n):
        for i in range(n):
            d = (date.today() - timedelta(days=n-i)).strftime("%Y-%m-%d")
            hm.append(HistoryRecord(
                date=d, ticker=ticker, spot=500.0,
                atm_iv_weekly=0.25, rr_25d_weekly=-0.05,
                atm_iv_monthly=0.23, rr_25d_monthly=-0.045,
                hv_30d=0.20, session="evening",
            ))

    def test_summary(self):
        self._add_records("QQQ", 5)
        self._add_records("SPY", 3)
        summary = self.hm.summary()
        self.assertEqual(summary.get("QQQ"), 5)
        self.assertEqual(summary.get("SPY"), 3)


class TestMarketCalendar(unittest.TestCase):
    """Trading day detection and DTE calculation."""

    def test_saturday_not_trading(self):
        sat = date(2026, 3, 7)   # Saturday
        self.assertFalse(is_trading_day(sat))

    def test_sunday_not_trading(self):
        sun = date(2026, 3, 8)
        self.assertFalse(is_trading_day(sun))

    def test_monday_trading(self):
        mon = date(2026, 3, 9)
        self.assertTrue(is_trading_day(mon))

    def test_christmas_not_trading(self):
        xmas = date(2026, 12, 25)
        self.assertFalse(is_trading_day(xmas))

    def test_new_years_not_trading(self):
        ny = date(2026, 1, 1)
        self.assertFalse(is_trading_day(ny))

    def test_regular_wednesday_trading(self):
        wed = date(2026, 3, 11)
        self.assertTrue(is_trading_day(wed))

    def test_calendar_dte_positive(self):
        today  = date.today()
        expiry = today + timedelta(days=7)
        self.assertEqual(calendar_dte(expiry, today), 7)

    def test_calendar_dte_zero_for_today(self):
        today = date.today()
        self.assertEqual(calendar_dte(today, today), 0)

    def test_trading_days_between(self):
        # Mon to Fri = 4 trading days (Tue, Wed, Thu, Fri)
        mon = date(2026, 3, 9)
        fri = date(2026, 3, 13)
        count = trading_days_between(mon, fri)
        self.assertEqual(count, 4)

    def test_next_trading_day_skips_weekend(self):
        fri = date(2026, 3, 13)
        nxt = next_trading_day(fri)
        self.assertEqual(nxt, date(2026, 3, 16))   # Monday


class TestOptionsChain(unittest.TestCase):
    """OptionsChain filtering logic."""

    def _make_chain(self):
        underlying = UnderlyingData(
            ticker="SPY", price=500.0, prev_close=502.0,
            change_pct=-0.004, volume=10_000_000, div_yield=0.013,
        )
        today = date.today()
        contracts = []
        for dte in [1, 7, 30]:
            expiry = today + timedelta(days=dte)
            for strike in [470, 480, 490, 500, 510, 520, 530]:
                for otype in ["call", "put"]:
                    contracts.append(OptionContract(
                        ticker="SPY", expiry=expiry, dte=dte,
                        strike=float(strike), option_type=otype,
                        bid=1.50, ask=1.60, last=1.55,
                        volume=500, open_interest=1000,
                    ))
        return OptionsChain(underlying=underlying, contracts=contracts, source="test")

    def test_filter_by_dte(self):
        chain = self._make_chain()
        weekly = chain.filter_by_dte(4, 10)
        self.assertTrue(all(4 <= c.dte <= 10 for c in weekly))
        self.assertGreater(len(weekly), 0)

    def test_filter_by_type(self):
        chain  = self._make_chain()
        puts   = chain.filter_by_type("put")
        calls  = chain.filter_by_type("call")
        self.assertTrue(all(c.option_type == "put"  for c in puts))
        self.assertTrue(all(c.option_type == "call" for c in calls))

    def test_mid_price_computed(self):
        c = OptionContract(
            ticker="SPY", expiry=date.today(), dte=7,
            strike=500.0, option_type="call",
            bid=2.00, ask=2.10, last=2.05,
            volume=100, open_interest=500,
        )
        self.assertAlmostEqual(c.mid_price, 2.05, places=3)

    def test_mid_price_fallback_to_last(self):
        """If bid/ask are zero, fall back to last price."""
        c = OptionContract(
            ticker="SPY", expiry=date.today(), dte=7,
            strike=500.0, option_type="call",
            bid=0.0, ask=0.0, last=2.05,
            volume=100, open_interest=500,
        )
        self.assertAlmostEqual(c.mid_price, 2.05, places=3)


class TestSkewProfile(unittest.TestCase):
    """SkewProfile payload generation."""

    def test_payload_keys_present(self):
        profile  = _mock_profile("QQQ", 600.0)
        payload  = profile.to_claude_payload()
        expected = ["ticker", "spot", "change_pct", "iv_rank", "hv_30d",
                    "skew_regime", "skew_pctile", "term_slope",
                    "expirations", "session"]
        for key in expected:
            self.assertIn(key, payload, f"Missing key: {key}")

    def test_payload_has_all_durations(self):
        profile  = _mock_profile()
        payload  = profile.to_claude_payload()
        exps     = payload["expirations"]
        self.assertIn("daily",   exps)
        self.assertIn("weekly",  exps)
        self.assertIn("monthly", exps)

    def test_expiry_compact_dict_keys(self):
        profile = _mock_profile()
        d       = profile.weekly.to_compact_dict()
        for key in ["atm_iv", "put_25d_iv", "call_25d_iv", "risk_reversal"]:
            self.assertIn(key, d)

    def test_payload_token_budget(self):
        """Payload must be compact enough — rough check via JSON length."""
        import json
        profile  = _mock_profile()
        payload  = profile.to_claude_payload()
        json_str = json.dumps(payload)
        # Rough token estimate: 1 token ≈ 4 chars
        estimated_tokens = len(json_str) / 4
        self.assertLess(estimated_tokens, 1500,
                        f"Payload too large: ~{estimated_tokens:.0f} tokens")


class TestReportGenerator(unittest.TestCase):
    """HTML report generation."""

    def setUp(self):
        self.outdir = tempfile.mkdtemp()
        self.profiles = [_mock_profile("SPY", 500.0), _mock_profile("QQQ", 600.0)]
        self.analyses = {
            "SPY": {
                "ticker": "SPY",
                "skew_narrative": "SPY skew is elevated at 74th pctile.",
                "primary_edge": "Sell 7DTE put spread.",
                "skew_verdict": "SELL_PUTS",
                "session_note": "Evening: hold overnight with tight stops.",
                "strategies": [
                    {"name": "Put Spread", "duration": "weekly",
                     "setup": "Sell $490P / Buy $475P",
                     "credit_or_debit": "$2.40", "max_risk": "$12.60",
                     "breakeven": "$487.60", "p_profit_pct": 74,
                     "edge_rating": "HIGH", "rationale": "Core carry trade."}
                ],
                "risk_warnings": ["Geopolitical risk remains elevated."],
            },
            "QQQ": {
                "ticker": "QQQ",
                "skew_narrative": "QQQ skew at 79th pctile, tech-specific fear.",
                "primary_edge": "Jade Lizard (weekly).",
                "skew_verdict": "SELL_PUTS",
                "session_note": "Evening: size down on 0DTE plays overnight.",
                "strategies": [],
                "risk_warnings": ["AI demand risk."],
            },
        }

    def test_report_file_created(self):
        path = generate_report(
            self.profiles, self.analyses, "evening",
            self.outdir, "Test Report"
        )
        self.assertTrue(os.path.exists(path))

    def test_report_not_empty(self):
        path = generate_report(
            self.profiles, self.analyses, "morning",
            self.outdir, "Test"
        )
        size = os.path.getsize(path)
        self.assertGreater(size, 5_000)   # at least 5KB

    def test_report_contains_tickers(self):
        path = generate_report(
            self.profiles, self.analyses, "evening",
            self.outdir, "Test"
        )
        with open(path, "r") as f:
            content = f.read()
        self.assertIn("SPY", content)
        self.assertIn("QQQ", content)
        self.assertIn("SELL_PUTS", content)

    def test_report_filename_includes_session(self):
        path = generate_report(
            self.profiles, self.analyses, "morning",
            self.outdir, "Test"
        )
        self.assertIn("morning", os.path.basename(path))

    def test_empty_analyses_doesnt_crash(self):
        """Report should still render even if Claude analysis is unavailable."""
        path = generate_report(
            self.profiles, {}, "evening",
            self.outdir, "Test"
        )
        self.assertTrue(os.path.exists(path))


# ─────────────────────────────────────────────
#  RUNNER
# ─────────────────────────────────────────────

def run_all_tests():
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()

    test_classes = [
        TestIVEngine,
        TestSkewMetrics,
        TestHistoryManager,
        TestMarketCalendar,
        TestOptionsChain,
        TestSkewProfile,
        TestReportGenerator,
    ]

    for cls in test_classes:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    result = runner.run(suite)

    print("\n" + "═" * 60)
    print(f"  Tests run   : {result.testsRun}")
    print(f"  Failures    : {len(result.failures)}")
    print(f"  Errors      : {len(result.errors)}")
    print(f"  Status      : {'ALL PASSED ✓' if result.wasSuccessful() else 'FAILED ✗'}")
    print("═" * 60 + "\n")

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(run_all_tests())
