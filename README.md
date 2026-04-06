# Akuna-Style Skew Analyzer — Local Daily Runner

Automated options volatility skew analysis for up to 10 ETFs,
running at 9:45am and 3:30pm Mon–Fri, producing a full
Akuna-style HTML dashboard with Claude AI narrative analysis.

---

## Project Structure

```
skew_analyzer/
├── config.py               ← ALL settings live here (tickers, times, API keys)
├── run_analysis.py         ← Main pipeline orchestrator
├── scheduler_setup.py      ← One-time scheduler installation
├── requirements.txt
├── core/
│   ├── iv_engine.py        ← Black-Scholes IV solver, Greeks, skew metrics
│   ├── data_fetcher.py     ← Options data (yfinance / Tradier / Polygon)
│   ├── skew_engine.py      ← Skew surface computation per expiry
│   ├── history_manager.py  ← Rolling CSV database for percentiles
│   ├── claude_client.py    ← Anthropic API (batched, cached, cost-optimized)
│   └── report_generator.py ← HTML dashboard renderer
├── cache/
│   └── skew_history.csv    ← Auto-created; grows daily (252-day rolling window)
├── output/
│   └── skew_report_*.html  ← Generated reports (one per run)
└── logs/
    └── run_*.log           ← Execution logs
```

---

## Quick Start

### 1. Install dependencies

```bash
cd skew_analyzer
pip install -r requirements.txt
```

### 2. Set environment variables

**macOS / Linux:**
```bash
export ANTHROPIC_API_KEY="sk-ant-..."

# Optional: for real-time data (recommended for production)
export TRADIER_API_KEY="your_tradier_token"
```

Add these to your `~/.zshrc` or `~/.bashrc` so they persist across sessions.

**Windows (Command Prompt):**
```cmd
setx ANTHROPIC_API_KEY "sk-ant-..."
setx TRADIER_API_KEY "your_tradier_token"
```

**Windows (PowerShell):**
```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
[System.Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY","sk-ant-...","User")
```

### 3. Configure tickers and settings

Edit `config.py`:
```python
TICKERS = ["SPY", "QQQ", "IWM", ...]   # your 10 ETFs
DATA_SOURCE = "yfinance"                 # or "tradier" / "polygon"
```

### 4. Run manually (first test)

```bash
# Auto-detect session from time
python run_analysis.py

# Force morning or evening
python run_analysis.py --session morning
python run_analysis.py --session evening

# Skip Claude API (data only, no narrative)
python run_analysis.py --no-claude

# Custom ticker list
python run_analysis.py --tickers SPY QQQ GLD
```

### 5. Install automated schedule

```bash
# macOS / Linux — installs cron jobs
python scheduler_setup.py

# Windows — installs Task Scheduler jobs
python scheduler_setup.py

# Verify what was installed
python scheduler_setup.py   # shows current cron

# Remove scheduled jobs
python scheduler_setup.py --remove

# Alternative: pure Python scheduler (run in terminal / screen)
python scheduler_setup.py --python
```

---

## Data Source Options

| Source    | Cost     | Latency   | Quality  | Setup                        |
|-----------|----------|-----------|----------|------------------------------|
| yfinance  | Free     | 15min     | Dev/test | None — works out of the box  |
| Tradier   | ~$10/mo  | Real-time | Good     | tradier.com → Developer plan |
| Polygon   | ~$29/mo  | Real-time | Best     | polygon.io → Starter plan    |

**To switch to Tradier:**
1. Sign up at tradier.com → Developer/Brokerage account
2. Get your API token
3. Set `TRADIER_API_KEY` env variable
4. Set `DATA_SOURCE = "tradier"` in config.py

---

## Cost Optimization Details

Claude API calls are minimized through three mechanisms:

1. **Pre-computation**: All IV, delta, skew metrics computed locally in Python.
   Claude receives only a compact JSON summary (~800-1200 tokens per ticker).

2. **Prompt caching**: The system prompt (strategy definitions, rules) is
   sent with `cache_control=ephemeral`. After the first call, it's served
   from Anthropic's cache at ~10% of normal token cost.

3. **Batching**: ETFs are sent 3 at a time per API call, reducing round-trips.

**Estimated daily cost (10 ETFs × 2 sessions):**
- With caching: ~$0.15–0.30/day
- Without caching: ~$0.40–0.60/day
- Cost log saved to: `output/usage_log.jsonl`

---

## Historical Data & Percentiles

On first run, you have no history — percentile columns will show "N/A".
This is expected. The database builds automatically over time:

- After 20 days: basic percentiles available
- After 60 days: meaningful regime classification
- After 252 days: full 1-year percentile context

The rolling window is set to 252 days (1 trading year) in config.py.
Data is stored in `cache/skew_history.csv` — back this file up periodically.

---

## Troubleshooting

**"No option expirations found"**
yfinance occasionally has outages. Wait 5 minutes and retry.
If persistent, switch to Tradier as your data source.

**"ATM IV = N/A" for some expirations**
Usually caused by wide bid/ask spreads (illiquid ETF) or stale last-price data.
IV solver requires a valid mid-price. Switch to Tradier for real bid/ask.

**"ANTHROPIC_API_KEY not set"**
Script will run and generate the data report but skip Claude narrative analysis.
The HTML report will still render with all computed numbers.

**Cron job not running**
Check `logs/cron_morning.log` and `logs/cron_evening.log`.
Common issue: cron doesn't inherit your shell environment variables.
Fix: Add API keys directly in the crontab line, or source a .env file:
```
45 9 * * 1-5 source ~/.zshrc && cd /path/to/skew_analyzer && python run_analysis.py --session morning
```

**Windows Task Scheduler not running**
Ensure "Run whether user is logged on or not" is NOT required for a simple local run.
Right-click task → Properties → "Run only when user is logged on" is safest.

---

## Extending the System

**Add a new data source:**
Implement `_fetch_mybroker()` in `core/data_fetcher.py` following the same
`OptionsChain` return signature, then add it to `fetch_options_chain()`.

**Add a new strategy type:**
Add to the `SYSTEM_PROMPT` in `core/claude_client.py` — Claude will
automatically incorporate it into its analysis.

**Change schedule times:**
Edit `MORNING_RUN_TIME` and `EVENING_RUN_TIME` in `config.py`,
then re-run `python scheduler_setup.py` to reinstall.

**Run on a cloud VM for reliability:**
Upload the project to an EC2 t3.micro or similar.
Use `screen` or `tmux` with the Python scheduler:
```bash
screen -S skew_analyzer
python scheduler_setup.py --python
# Ctrl+A, D to detach
```

---

## Disclaimer

This tool is for educational and research purposes.
Options trading involves substantial risk of loss.
Nothing produced by this system constitutes investment advice.
