"""
core/history_manager.py — Rolling historical skew database

Persists daily skew readings to a local CSV file.
Used to compute RR percentile, IV rank, and mean-reversion signals.
Entirely local — no external calls.
"""

import os
import csv
import logging
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class HistoryRecord:
    date:          str      # YYYY-MM-DD
    ticker:        str
    spot:          float
    atm_iv_weekly: Optional[float]
    rr_25d_weekly: Optional[float]   # risk reversal (negative)
    atm_iv_monthly:Optional[float]
    rr_25d_monthly:Optional[float]
    hv_30d:        Optional[float]
    session:       str      # "morning" or "evening"


FIELDNAMES = [
    "date", "ticker", "spot", "atm_iv_weekly", "rr_25d_weekly",
    "atm_iv_monthly", "rr_25d_monthly", "hv_30d", "session"
]


class HistoryManager:
    """
    Manages the rolling skew history CSV.
    Thread-safe for single-process use (no concurrent writes needed here).
    """

    def __init__(self, path: str, rolling_days: int = 252):
        self.path         = path
        self.rolling_days = rolling_days
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            self._init_csv()
        self._cache: Optional[List[HistoryRecord]] = None

    def _init_csv(self):
        with open(self.path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
        logger.info(f"Initialized skew history at {self.path}")

    def _load(self) -> List[HistoryRecord]:
        records = []
        try:
            with open(self.path, "r", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        records.append(HistoryRecord(
                            date=row["date"],
                            ticker=row["ticker"],
                            spot=float(row["spot"] or 0),
                            atm_iv_weekly=float(row["atm_iv_weekly"]) if row["atm_iv_weekly"] else None,
                            rr_25d_weekly=float(row["rr_25d_weekly"]) if row["rr_25d_weekly"] else None,
                            atm_iv_monthly=float(row["atm_iv_monthly"]) if row["atm_iv_monthly"] else None,
                            rr_25d_monthly=float(row["rr_25d_monthly"]) if row["rr_25d_monthly"] else None,
                            hv_30d=float(row["hv_30d"]) if row["hv_30d"] else None,
                            session=row.get("session", "unknown"),
                        ))
                    except (ValueError, KeyError):
                        continue
        except FileNotFoundError:
            self._init_csv()
        return records

    def _save(self, records: List[HistoryRecord]):
        with open(self.path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            for r in records:
                writer.writerow({
                    "date":           r.date,
                    "ticker":         r.ticker,
                    "spot":           r.spot,
                    "atm_iv_weekly":  r.atm_iv_weekly or "",
                    "rr_25d_weekly":  r.rr_25d_weekly or "",
                    "atm_iv_monthly": r.atm_iv_monthly or "",
                    "rr_25d_monthly": r.rr_25d_monthly or "",
                    "hv_30d":         r.hv_30d or "",
                    "session":        r.session,
                })

    def append(self, record: HistoryRecord):
        """Add a new record and prune old entries beyond rolling_days."""
        records = self._load()
        records.append(record)

        # Prune: keep only the last rolling_days per ticker
        from collections import defaultdict
        by_ticker: Dict[str, List[HistoryRecord]] = defaultdict(list)
        for r in records:
            by_ticker[r.ticker].append(r)

        pruned = []
        for ticker, recs in by_ticker.items():
            recs.sort(key=lambda x: x.date)
            pruned.extend(recs[-self.rolling_days:])

        pruned.sort(key=lambda x: (x.date, x.ticker))
        self._save(pruned)
        self._cache = None   # invalidate cache
        logger.debug(f"Appended history record for {record.ticker} ({record.date})")

    def get_rr_history(self, ticker: str, session: str = "evening") -> List[float]:
        """Return list of 25Δ weekly risk reversal values for a ticker."""
        records = self._load()
        return [
            r.rr_25d_weekly for r in records
            if r.ticker == ticker
            and r.rr_25d_weekly is not None
            and r.session == session
        ]

    def get_iv_history(self, ticker: str) -> List[float]:
        """Return list of ATM weekly IV values for IV rank computation."""
        records = self._load()
        return [
            r.atm_iv_weekly for r in records
            if r.ticker == ticker and r.atm_iv_weekly is not None
        ]

    def compute_iv_rank(self, ticker: str, current_iv: float) -> Optional[float]:
        """
        IV Rank = (current_iv - 52W_low) / (52W_high - 52W_low) * 100
        Returns None if insufficient history.
        """
        history = self.get_iv_history(ticker)
        if len(history) < 20:
            return None
        iv_high = max(history)
        iv_low  = min(history)
        if iv_high <= iv_low:
            return None
        rank = (current_iv - iv_low) / (iv_high - iv_low) * 100
        return round(max(0.0, min(100.0, rank)), 1)

    def get_price_history(self, ticker: str) -> List[float]:
        """Return historical spot prices for HV30 computation."""
        records = self._load()
        prices  = [r.spot for r in records if r.ticker == ticker and r.spot > 0]
        return prices

    def record_count(self, ticker: str) -> int:
        records = self._load()
        return sum(1 for r in records if r.ticker == ticker)

    def summary(self) -> Dict[str, int]:
        """Count records per ticker."""
        from collections import Counter
        records = self._load()
        return dict(Counter(r.ticker for r in records))
