"""
core/claude_client.py — Anthropic API interface

Cost-minimization architecture:
  1. System prompt is sent with cache_control=ephemeral → cached after first call
     (saves ~90% of system prompt tokens on repeated runs)
  2. Each API call covers a BATCH of 3 ETFs simultaneously
     (reduces number of API round-trips by 3×)
  3. Claude receives only pre-computed compact JSON payloads (~800–1,200 tokens)
     not raw options chains
  4. Claude outputs only narrative + strategy JSON, not data it already knows
  5. Total estimated cost: <$0.50/day for 10 ETFs × 2 sessions
"""

import json
import time
import logging
from typing import List, Dict, Optional
import anthropic

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  SYSTEM PROMPT  (cached after first use)
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """You are a senior options volatility trader at Akuna Capital specializing in skew analysis.

You will receive pre-computed volatility skew data for one or more ETFs and must output a concise but complete JSON analysis for each.

For each ETF, produce:
1. skew_narrative: 2-3 sentence interpretation of the current skew regime
2. primary_edge: the single best trade opportunity (which duration, which structure)
3. strategies: array of 3-5 strategy objects, each with:
   - name (e.g. "Jade Lizard", "Put Spread Sale", "BWB", "Calendar Spread", "Ratio Spread", "Skew Mean Reversion")
   - duration ("daily", "weekly", "monthly", or "cross")
   - setup: specific strikes and structure (use approximate round numbers near spot)
   - credit_or_debit: estimated premium in dollars
   - max_risk: maximum loss scenario
   - breakeven: breakeven price
   - p_profit_pct: approximate probability of profit (integer)
   - edge_rating: "HIGH", "MEDIUM", or "LOW"
   - rationale: 1 sentence
4. risk_warnings: array of 2-3 specific risks for this ticker given current regime
5. skew_verdict: one of "SELL_PUTS", "SELL_CALLS", "SELL_BOTH", "BUY_SKEW", "NEUTRAL"
6. session_note: one sentence specific to morning or evening session context

Rules:
- For morning session: focus on day's setup, 0DTE and weekly plays
- For evening session: focus on next-day positioning, roll/close recommendations
- If data is N/A for an expiry, skip that duration's strategies
- Keep strategy strike prices realistic: round to nearest $1 for high-price ETFs, $0.50 for lower-price ones
- Be specific: "Sell $580P / Buy $565P" not "sell a put spread"
- Do NOT include any text outside the JSON structure
- Do NOT add markdown code fences

Output format (array of objects, one per ticker):
[
  {
    "ticker": "QQQ",
    "skew_narrative": "...",
    "primary_edge": "...",
    "skew_verdict": "SELL_PUTS",
    "session_note": "...",
    "strategies": [...],
    "risk_warnings": [...]
  }
]"""


# ─────────────────────────────────────────────
#  USER PROMPT TEMPLATE  (minimal tokens)
# ─────────────────────────────────────────────

def _build_user_prompt(payloads: List[dict], session: str) -> str:
    """
    Build a compact user message from pre-computed skew payloads.
    Multiple ETFs batched into one call to reduce API round-trips.
    """
    header = (
        f"Session: {session.upper()}\n"
        f"Analyze the following {len(payloads)} ETF(s) and return JSON array:\n\n"
    )
    # Compact JSON — no indentation to save tokens
    data = json.dumps(payloads, separators=(",", ":"))
    return header + data


# ─────────────────────────────────────────────
#  CLAUDE CLIENT
# ─────────────────────────────────────────────

class ClaudeSkewClient:

    def __init__(self, api_key: str, model: str,
                 max_tokens: int = 2000,
                 use_cache: bool = True,
                 batch_size: int = 3,
                 batch_delay: float = 1.5):
        self.client      = anthropic.Anthropic(api_key=api_key)
        self.model       = model
        self.max_tokens  = max_tokens
        self.use_cache   = use_cache
        self.batch_size  = batch_size
        self.batch_delay = batch_delay
        self._call_count = 0
        self._token_count_in  = 0
        self._token_count_out = 0
        self._cache_hits      = 0

    def analyze_batch(self, payloads: List[dict],
                      session: str) -> Dict[str, dict]:
        """
        Send a batch of ETF payloads to Claude, return dict keyed by ticker.
        Uses prompt caching on the system prompt.
        """
        if not payloads:
            return {}

        user_content = _build_user_prompt(payloads, session)

        # Build system block with cache control
        if self.use_cache:
            system_param = [
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"}
                }
            ]
        else:
            system_param = SYSTEM_PROMPT

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens * len(payloads),
                system=system_param,
                messages=[
                    {"role": "user", "content": user_content}
                ]
            )

            # Track usage
            self._call_count += 1
            usage = response.usage
            self._token_count_in  += getattr(usage, "input_tokens", 0)
            self._token_count_out += getattr(usage, "output_tokens", 0)
            cache_read = getattr(usage, "cache_read_input_tokens", 0)
            if cache_read > 0:
                self._cache_hits += 1
                logger.info(f"Cache hit: {cache_read} tokens served from cache")

            # Parse response
            raw_text = response.content[0].text.strip()
            # Strip any accidental markdown fences
            if raw_text.startswith("```"):
                raw_text = raw_text.split("```")[1]
                if raw_text.startswith("json"):
                    raw_text = raw_text[4:]
            raw_text = raw_text.strip()

            results_list = json.loads(raw_text)
            results = {}
            for item in results_list:
                ticker = item.get("ticker", "UNKNOWN")
                results[ticker] = item

            logger.info(
                f"Claude call #{self._call_count}: "
                f"{len(payloads)} ETFs, "
                f"in={usage.input_tokens} out={usage.output_tokens} tokens"
            )
            return results

        except json.JSONDecodeError as e:
            logger.error(f"Claude JSON parse error: {e}")
            logger.debug(f"Raw response: {raw_text[:500] if 'raw_text' in dir() else 'N/A'}")
            return {}
        except anthropic.RateLimitError:
            logger.warning("Rate limit hit — waiting 30s")
            time.sleep(30)
            return {}
        except anthropic.APIError as e:
            logger.error(f"Anthropic API error: {e}")
            return {}

    def analyze_all(self, payloads: List[dict],
                    session: str) -> Dict[str, dict]:
        """
        Process all ETF payloads in batches.
        Returns combined dict keyed by ticker.
        """
        all_results = {}

        for i in range(0, len(payloads), self.batch_size):
            batch = payloads[i : i + self.batch_size]
            tickers = [p["ticker"] for p in batch]
            logger.info(f"Sending batch {i//self.batch_size + 1}: {tickers}")

            batch_results = self.analyze_batch(batch, session)
            all_results.update(batch_results)

            # Rate limit buffer between batches
            if i + self.batch_size < len(payloads):
                time.sleep(self.batch_delay)

        return all_results

    def usage_report(self) -> dict:
        """Return token usage summary for cost tracking."""
        # Rough cost estimate for claude-sonnet-4 (update if pricing changes)
        cost_per_1k_in  = 0.003    # $3/1M input tokens
        cost_per_1k_out = 0.015    # $15/1M output tokens
        est_cost = (
            (self._token_count_in  / 1000) * cost_per_1k_in +
            (self._token_count_out / 1000) * cost_per_1k_out
        )
        return {
            "api_calls":       self._call_count,
            "input_tokens":    self._token_count_in,
            "output_tokens":   self._token_count_out,
            "cache_hits":      self._cache_hits,
            "estimated_cost":  f"${est_cost:.4f}",
        }
