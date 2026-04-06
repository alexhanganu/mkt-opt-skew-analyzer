"""
core/report_generator.py — HTML dashboard generator

Takes SkewProfiles + Claude analysis dicts and renders
a full Akuna-style interactive HTML report.
All rendering is local — no API calls.
"""

import json
import os
import logging
from datetime import datetime
from typing import Dict, List, Optional
from .skew_engine import SkewProfile

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  COLOUR / FORMATTING HELPERS
# ─────────────────────────────────────────────

def _pct(v: Optional[float], dp: int = 1) -> str:
    if v is None:
        return "N/A"
    return f"{v*100:.{dp}f}%"

def _price(v: Optional[float], dp: int = 2) -> str:
    if v is None:
        return "N/A"
    return f"${v:.{dp}f}"

def _sign_color(v: Optional[float], invert: bool = False) -> str:
    """Return CSS class for positive/negative/neutral."""
    if v is None:
        return "muted"
    pos = v > 0
    if invert:
        pos = not pos
    return "pos" if pos else "neg"

def _regime_color(regime: str) -> str:
    mapping = {
        "FLAT":     "regime-flat",
        "NORMAL":   "regime-normal",
        "ELEVATED": "regime-elevated",
        "STEEP":    "regime-steep",
        "EXTREME":  "regime-extreme",
        "UNKNOWN":  "muted",
    }
    for k, v in mapping.items():
        if k in regime:
            return v
    return "muted"

def _verdict_color(verdict: str) -> str:
    if "SELL" in verdict:
        return "neg"
    if "BUY" in verdict:
        return "pos"
    return "muted"

def _edge_color(rating: str) -> str:
    return {"HIGH": "edge-high", "MEDIUM": "edge-med", "LOW": "edge-low"}.get(rating, "muted")

def _dur_color(dur: str) -> str:
    return {"daily": "daily", "weekly": "weekly", "monthly": "monthly",
            "cross": "cross"}.get(dur, "muted")


# ─────────────────────────────────────────────
#  TICKER CARD HTML
# ─────────────────────────────────────────────

def _render_expiry_row(label: str, exp) -> str:
    if exp is None:
        return f'<tr class="dim"><td>{label.upper()}</td><td colspan="6">No data</td></tr>'
    rr = exp.risk_reversal
    rr_str = f"{rr*100:.1f}%" if rr is not None else "N/A"
    rr_cls = "neg" if (rr is not None and rr < -0.05) else ("warn" if rr is not None and rr < -0.03 else "pos")
    atm = f"{exp.atm_iv*100:.1f}%" if exp.atm_iv else "N/A"
    p25 = f"{exp.iv_put_25d*100:.1f}%" if exp.iv_put_25d else "N/A"
    c25 = f"{exp.iv_call_25d*100:.1f}%" if exp.iv_call_25d else "N/A"
    p10 = f"{exp.iv_put_10d*100:.1f}%" if exp.iv_put_10d else "N/A"
    em  = f"${exp.expected_move:.2f}" if exp.expected_move else "N/A"
    return f"""
        <tr>
          <td><span class="dur-badge {label}">{label.upper()} ({exp.dte}d)</span></td>
          <td>{atm}</td>
          <td class="neg">{p25}</td>
          <td>{c25}</td>
          <td class="{rr_cls}">{rr_str}</td>
          <td>{p10}</td>
          <td>{em}</td>
        </tr>"""


def _render_strategy_card(strat: dict, idx: int) -> str:
    dur    = strat.get("duration", "weekly")
    name   = strat.get("name", "Strategy")
    setup  = strat.get("setup", "—")
    edge   = strat.get("edge_rating", "MEDIUM")
    credit = strat.get("credit_or_debit", "—")
    risk   = strat.get("max_risk", "—")
    be     = strat.get("breakeven", "—")
    pp     = strat.get("p_profit_pct", "—")
    rat    = strat.get("rationale", "")
    dur_c  = _dur_color(dur)
    edge_c = _edge_color(edge)

    return f"""
    <div class="strat-card" onclick="toggleDetail('strat-{idx}')">
      <div class="sc-top">
        <span class="sc-dur {dur_c}">{dur.upper()}</span>
        <span class="sc-name">{name}</span>
        <span class="edge-badge {edge_c}">{edge}</span>
      </div>
      <div class="sc-setup">{setup}</div>
      <div class="sc-nums">
        <span class="sc-num"><span class="lbl">Credit/Debit</span> <span class="pos">{credit}</span></span>
        <span class="sc-num"><span class="lbl">Max Risk</span> <span class="neg">{risk}</span></span>
        <span class="sc-num"><span class="lbl">BE</span> {be}</span>
        <span class="sc-num"><span class="lbl">P(Profit)</span> <span class="pos">{pp}%</span></span>
      </div>
      <div class="sc-detail" id="strat-{idx}" style="display:none;">
        <div class="sc-rationale">{rat}</div>
      </div>
    </div>"""


def _render_ticker_section(profile: SkewProfile,
                            analysis: Optional[dict],
                            ticker_idx: int) -> str:
    t       = profile.ticker
    spot    = _price(profile.spot)
    chg     = f"{profile.change_pct*100:+.2f}%" if profile.change_pct else "N/A"
    chg_cls = _sign_color(profile.change_pct)
    regime  = profile.skew_regime
    reg_cls = _regime_color(regime)
    ivr     = f"{profile.iv_rank:.0f}" if profile.iv_rank else "—"
    pctile  = f"{profile.skew_percentile_weekly:.0f}th" if profile.skew_percentile_weekly else "—"
    hv      = _pct(profile.hv_30d)
    source  = profile.data_source
    session = profile.run_session.upper()
    ts      = profile.run_time

    # From Claude analysis
    verdict     = analysis.get("skew_verdict",   "NEUTRAL") if analysis else "NEUTRAL"
    narrative   = analysis.get("skew_narrative", "Analysis unavailable.") if analysis else "Analysis unavailable."
    primary     = analysis.get("primary_edge",   "—") if analysis else "—"
    session_note= analysis.get("session_note",   "") if analysis else ""
    strategies  = analysis.get("strategies",     []) if analysis else []
    warnings    = analysis.get("risk_warnings",  []) if analysis else []
    verdict_cls = _verdict_color(verdict)

    # Expiry rows
    exp_rows = (
        _render_expiry_row("daily",   profile.daily)   +
        _render_expiry_row("weekly",  profile.weekly)  +
        _render_expiry_row("monthly", profile.monthly)
    )

    # Strategy cards
    strat_cards = ""
    for i, s in enumerate(strategies):
        strat_cards += _render_strategy_card(s, ticker_idx * 100 + i)

    # Risk warnings
    warn_html = "".join(f'<li>{w}</li>' for w in warnings)

    term_slope = f"{profile.term_structure_slope*100:+.1f}% (monthly vs daily)" if profile.term_structure_slope else "N/A"

    return f"""
  <div class="ticker-section" id="ticker-{t}">
    <!-- TICKER HEADER -->
    <div class="t-header" onclick="toggleTicker('{t}')">
      <div class="t-hl">
        <span class="t-name">{t}</span>
        <span class="t-spot">{spot}</span>
        <span class="t-chg {chg_cls}">{chg}</span>
        <span class="t-regime {reg_cls}">{regime}</span>
        <span class="verdict-badge {verdict_cls}">{verdict}</span>
      </div>
      <div class="t-hr">
        <span class="t-meta">IV Rank: <b>{ivr}</b></span>
        <span class="t-meta">HV30: <b>{hv}</b></span>
        <span class="t-meta">Skew Pctile: <b>{pctile}</b></span>
        <span class="t-meta">Term Slope: <b>{term_slope}</b></span>
        <span class="t-meta t-dim">{session} · {ts} · {source}</span>
        <span class="t-toggle">▼</span>
      </div>
    </div>

    <!-- TICKER BODY (collapsible) -->
    <div class="t-body" id="body-{t}">

      <!-- NARRATIVE -->
      <div class="t-narrative">
        <div class="t-narrative-text">{narrative}</div>
        <div class="t-primary-edge">▶ PRIMARY EDGE: {primary}</div>
        {'<div class="t-session-note">⏱ ' + session_note + '</div>' if session_note else ''}
      </div>

      <!-- SKEW TABLE -->
      <div class="t-table-wrap">
        <table class="skew-table">
          <thead>
            <tr>
              <th>Expiry</th>
              <th>ATM IV</th>
              <th>25Δ Put IV</th>
              <th>25Δ Call IV</th>
              <th>Risk Reversal</th>
              <th>10Δ Put IV</th>
              <th>1σ Move</th>
            </tr>
          </thead>
          <tbody>
            {exp_rows}
          </tbody>
        </table>
      </div>

      <!-- STRATEGIES -->
      <div class="strat-section">
        <div class="strat-label">STRATEGIES</div>
        <div class="strat-grid">
          {strat_cards}
        </div>
      </div>

      <!-- RISK WARNINGS -->
      {'<div class="warn-section"><div class="warn-label">⚠ RISK WARNINGS</div><ul class="warn-list">' + warn_html + '</ul></div>' if warnings else ''}

    </div><!-- /t-body -->
  </div>"""


# ─────────────────────────────────────────────
#  FULL REPORT
# ─────────────────────────────────────────────

def generate_report(profiles: List[SkewProfile],
                    analyses: Dict[str, dict],
                    session: str,
                    output_dir: str,
                    title: str = "Akuna-Style Skew Analysis") -> str:
    """
    Generate the full HTML report and write to disk.
    Returns the output file path.
    """
    now     = datetime.now()
    ts_str  = now.strftime("%Y-%m-%d %H:%M")
    fn_ts   = now.strftime("%Y%m%d_%H%M")
    session_label = session.upper()

    # Aggregate summary stats
    total_tickers = len(profiles)
    sell_puts  = sum(1 for a in analyses.values() if "SELL" in a.get("skew_verdict",""))
    buy_skew   = sum(1 for a in analyses.values() if "BUY"  in a.get("skew_verdict",""))
    n_errors   = sum(1 for p in profiles if p.errors)

    # Build per-ticker sections
    ticker_sections = ""
    for i, profile in enumerate(profiles):
        analysis = analyses.get(profile.ticker)
        ticker_sections += _render_ticker_section(profile, analysis, i)

    # Usage info (injected if available)
    usage_html = ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — {session_label} — {ts_str}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&family=Space+Grotesk:wght@300;400;500;600;700&display=swap');
:root {{
  --bg:#060810; --panel:#0a0d18; --panel2:#0d1020; --border:#161c2e; --grid:#0f1320;
  --daily:#ff6b35; --weekly:#c084fc; --monthly:#22d3ee; --cross:#34d399;
  --red:#f43f5e; --green:#34d399; --yellow:#fbbf24; --white:#e2e8f0; --muted:#475569; --dim:#1e2740;
}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:var(--bg);color:var(--white);font-family:'Space Grotesk',sans-serif;font-weight:300;min-height:100vh}}
body::before{{content:'';position:fixed;inset:0;background:repeating-linear-gradient(0deg,transparent,transparent 3px,rgba(6,8,16,.12) 3px,rgba(6,8,16,.12) 4px);pointer-events:none;z-index:9999}}
/* HEADER */
.hdr{{display:flex;align-items:center;justify-content:space-between;padding:14px 28px;border-bottom:1px solid var(--border);background:var(--panel);position:sticky;top:0;z-index:200}}
.hdr-l{{display:flex;align-items:center;gap:14px}}
.hdr-logo{{font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:15px;letter-spacing:4px;color:var(--white)}}
.hdr-sep{{width:1px;height:24px;background:var(--border)}}
.hdr-sub{{font-family:'IBM Plex Mono',monospace;font-size:9px;color:var(--muted);letter-spacing:2px;line-height:1.5}}
.hdr-r{{display:flex;align-items:center;gap:16px}}
.session-badge{{font-family:'IBM Plex Mono',monospace;font-size:10px;padding:4px 10px;border-radius:2px;letter-spacing:1px;text-transform:uppercase}}
.session-morning{{background:rgba(251,191,36,.15);color:var(--yellow);border:1px solid rgba(251,191,36,.3)}}
.session-evening{{background:rgba(192,132,252,.15);color:var(--weekly);border:1px solid rgba(192,132,252,.3)}}
.hdr-ts{{font-family:'IBM Plex Mono',monospace;font-size:9px;color:var(--muted);letter-spacing:1px}}
/* SUMMARY STRIP */
.summary{{display:grid;grid-template-columns:repeat(5,1fr);border-bottom:1px solid var(--border);background:var(--panel)}}
.ss{{padding:14px 20px;border-right:1px solid var(--border)}}
.ss:last-child{{border-right:none}}
.ss-lbl{{font-family:'IBM Plex Mono',monospace;font-size:8px;color:var(--muted);letter-spacing:1.5px;text-transform:uppercase;margin-bottom:5px}}
.ss-val{{font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:22px;line-height:1}}
.ss-sub{{font-family:'IBM Plex Mono',monospace;font-size:8px;color:var(--muted);margin-top:3px}}
/* TICKER SECTIONS */
.tickers{{padding:0}}
.ticker-section{{border-bottom:1px solid var(--border)}}
.t-header{{display:flex;align-items:center;justify-content:space-between;padding:14px 28px;cursor:pointer;background:var(--panel);transition:background .2s;flex-wrap:wrap;gap:8px}}
.t-header:hover{{background:var(--panel2)}}
.t-hl{{display:flex;align-items:center;gap:12px;flex-wrap:wrap}}
.t-name{{font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:18px;color:var(--white);letter-spacing:2px;min-width:60px}}
.t-spot{{font-family:'IBM Plex Mono',monospace;font-size:14px;color:var(--white)}}
.t-chg{{font-family:'IBM Plex Mono',monospace;font-size:12px}}
.t-regime{{font-family:'IBM Plex Mono',monospace;font-size:9px;padding:3px 8px;border-radius:2px;letter-spacing:1px}}
.t-hr{{display:flex;align-items:center;gap:16px;flex-wrap:wrap}}
.t-meta{{font-family:'IBM Plex Mono',monospace;font-size:9px;color:var(--muted)}}
.t-dim{{opacity:.5}}
.t-toggle{{font-size:12px;color:var(--muted);transition:transform .2s}}
.t-toggle.open{{transform:rotate(180deg)}}
/* BODY */
.t-body{{padding:20px 28px;display:none}}
.t-body.open{{display:block}}
.t-narrative{{background:var(--panel2);border-left:3px solid var(--weekly);padding:14px 16px;margin-bottom:16px;border-radius:0 4px 4px 0}}
.t-narrative-text{{font-size:13px;color:var(--white);line-height:1.6;margin-bottom:8px}}
.t-primary-edge{{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--green);letter-spacing:.5px}}
.t-session-note{{font-family:'IBM Plex Mono',monospace;font-size:9px;color:var(--yellow);margin-top:5px;letter-spacing:.5px}}
/* TABLE */
.t-table-wrap{{overflow-x:auto;margin-bottom:16px}}
.skew-table{{width:100%;border-collapse:collapse;font-size:11px}}
.skew-table th{{font-family:'IBM Plex Mono',monospace;font-size:8px;color:var(--muted);text-align:left;padding:6px 10px;border-bottom:1px solid var(--border);letter-spacing:1px;text-transform:uppercase}}
.skew-table td{{padding:8px 10px;border-bottom:1px solid var(--grid);font-family:'IBM Plex Mono',monospace;font-size:11px}}
.skew-table tr:hover td{{background:rgba(255,255,255,.015)}}
.dur-badge{{font-size:8px;padding:2px 6px;border-radius:2px;letter-spacing:1px}}
/* STRATEGIES */
.strat-section{{margin-bottom:16px}}
.strat-label{{font-family:'IBM Plex Mono',monospace;font-size:8px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:10px}}
.strat-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px}}
.strat-card{{background:var(--panel2);border:1px solid var(--border);border-radius:3px;padding:12px 14px;cursor:pointer;transition:border-color .2s}}
.strat-card:hover{{border-color:var(--weekly)}}
.sc-top{{display:flex;align-items:center;gap:8px;margin-bottom:6px}}
.sc-dur{{font-size:8px;padding:2px 6px;border-radius:2px;letter-spacing:1px;font-family:'IBM Plex Mono',monospace}}
.sc-name{{font-weight:600;font-size:13px;flex:1}}
.sc-setup{{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--white);margin-bottom:8px;line-height:1.6}}
.sc-nums{{display:flex;gap:12px;flex-wrap:wrap}}
.sc-num{{font-family:'IBM Plex Mono',monospace;font-size:10px}}
.lbl{{color:var(--muted);font-size:8px;display:block}}
.sc-detail{{margin-top:10px;padding-top:8px;border-top:1px solid var(--border)}}
.sc-rationale{{font-size:11px;color:var(--muted);line-height:1.5;font-style:italic}}
.edge-badge{{font-family:'IBM Plex Mono',monospace;font-size:8px;padding:2px 5px;border-radius:2px}}
/* WARNINGS */
.warn-section{{background:rgba(244,63,94,.06);border:1px solid rgba(244,63,94,.2);border-radius:3px;padding:12px 16px;margin-bottom:8px}}
.warn-label{{font-family:'IBM Plex Mono',monospace;font-size:8px;color:var(--red);letter-spacing:2px;margin-bottom:8px}}
.warn-list{{list-style:none;padding:0}}
.warn-list li{{font-size:11px;color:#fca5a5;padding:3px 0;padding-left:12px;position:relative}}
.warn-list li::before{{content:"▸";position:absolute;left:0;color:var(--red)}}
/* COLORS */
.pos{{color:var(--green)}} .neg{{color:var(--red)}} .warn{{color:var(--yellow)}} .muted{{color:var(--muted)}}
.daily{{background:rgba(255,107,53,.15);color:var(--daily)}}
.weekly{{background:rgba(192,132,252,.15);color:var(--weekly)}}
.monthly{{background:rgba(34,211,238,.15);color:var(--monthly)}}
.cross{{background:rgba(52,211,153,.15);color:var(--cross)}}
.regime-flat{{background:rgba(52,211,153,.12);color:var(--green);padding:3px 8px;border-radius:2px;font-size:9px;font-family:'IBM Plex Mono',monospace;letter-spacing:1px}}
.regime-normal{{background:rgba(251,191,36,.1);color:var(--yellow);padding:3px 8px;border-radius:2px;font-size:9px;font-family:'IBM Plex Mono',monospace;letter-spacing:1px}}
.regime-elevated,.regime-steep,.regime-extreme{{background:rgba(244,63,94,.12);color:var(--red);padding:3px 8px;border-radius:2px;font-size:9px;font-family:'IBM Plex Mono',monospace;letter-spacing:1px}}
.verdict-badge{{font-family:'IBM Plex Mono',monospace;font-size:9px;padding:3px 8px;border-radius:2px;letter-spacing:1px}}
.edge-high{{background:rgba(52,211,153,.15);color:var(--green)}}
.edge-med{{background:rgba(251,191,36,.15);color:var(--yellow)}}
.edge-low{{background:rgba(244,63,94,.15);color:var(--red)}}
/* FOOTER */
.footer{{padding:12px 28px;border-top:1px solid var(--border);background:var(--panel);display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px}}
.footer span{{font-family:'IBM Plex Mono',monospace;font-size:8px;color:var(--muted);letter-spacing:1px}}
.dim{{opacity:.5}}
@media(max-width:768px){{
  .summary{{grid-template-columns:repeat(2,1fr)}}
  .t-header{{flex-direction:column;align-items:flex-start}}
  .strat-grid{{grid-template-columns:1fr}}
}}
</style>
</head>
<body>

<div class="hdr">
  <div class="hdr-l">
    <span class="hdr-logo">AKUNA</span>
    <div class="hdr-sep"></div>
    <div class="hdr-sub">VOL TRADING DESK<br>AUTOMATED SKEW ANALYSIS</div>
  </div>
  <div class="hdr-r">
    <span class="session-badge session-{session.lower()}">{session_label} SESSION</span>
    <span class="hdr-ts">{ts_str}</span>
  </div>
</div>

<div class="summary">
  <div class="ss">
    <div class="ss-lbl">ETFs Analyzed</div>
    <div class="ss-val" style="color:var(--white)">{total_tickers}</div>
    <div class="ss-sub">This run</div>
  </div>
  <div class="ss">
    <div class="ss-lbl">Sell Vol Signals</div>
    <div class="ss-val" style="color:var(--red)">{sell_puts}</div>
    <div class="ss-sub">SELL PUTS / SELL BOTH</div>
  </div>
  <div class="ss">
    <div class="ss-lbl">Buy Skew Signals</div>
    <div class="ss-val" style="color:var(--green)">{buy_skew}</div>
    <div class="ss-sub">BUY SKEW / NEUTRAL</div>
  </div>
  <div class="ss">
    <div class="ss-lbl">Data Issues</div>
    <div class="ss-val" style="color:{'var(--red)' if n_errors else 'var(--green)'}">{n_errors}</div>
    <div class="ss-sub">Tickers with errors</div>
  </div>
  <div class="ss">
    <div class="ss-lbl">Session</div>
    <div class="ss-val" style="color:var(--yellow)">{session_label}</div>
    <div class="ss-sub">{ts_str}</div>
  </div>
</div>

<div class="tickers">
{ticker_sections}
</div>

{usage_html}

<div class="footer">
  <span>AKUNA-STYLE SKEW ANALYZER · LOCAL RUN · {ts_str}</span>
  <span>NOT INVESTMENT ADVICE · OPTIONS INVOLVE SUBSTANTIAL RISK OF LOSS</span>
  <span>SOURCE CODE: skew_analyzer/ · DATA: {profiles[0].data_source if profiles else "N/A"}</span>
</div>

<script>
function toggleTicker(id) {{
  const body   = document.getElementById('body-' + id);
  const header = body.previousElementSibling;
  const toggle = header.querySelector('.t-toggle');
  if (body.classList.contains('open')) {{
    body.classList.remove('open');
    body.style.display = 'none';
    toggle.classList.remove('open');
  }} else {{
    body.classList.add('open');
    body.style.display = 'block';
    toggle.classList.add('open');
  }}
}}
function toggleDetail(id) {{
  const el = document.getElementById(id);
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
}}
// Auto-open first ticker
document.addEventListener('DOMContentLoaded', () => {{
  const first = document.querySelector('.t-header');
  if (first) first.click();
}});
</script>
</body>
</html>"""

    os.makedirs(output_dir, exist_ok=True)
    filename = f"skew_report_{session.lower()}_{fn_ts}.html"
    filepath = os.path.join(output_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    logger.info(f"Report written to {filepath}")
    return filepath
