"""
Force fundamentals refresh + Buy Opportunities rebuild without re-fetching prices.
Usage: python force_fund_update.py
Scrapes all stocks' fundamentals (main page + ratios page), writes to Fundamentals tab,
computes Magic Formula ranks, then rebuilds Buy Opportunities and Dashboard.
"""

import sys
import os
_PIPELINE_DIR = os.path.dirname(os.path.abspath(__file__))
if _PIPELINE_DIR not in sys.path:
    sys.path.insert(0, _PIPELINE_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(_PIPELINE_DIR, ".env"))

from datetime import datetime
from config import GROUPS, EU_SCOPE, US_SCOPE
from fundamentals import fetch_fundamentals
import sheets


def _log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _write_fund(ticker, group_name, exchange, sa_prefix=""):
    try:
        fund = fetch_fundamentals(ticker, exchange, sa_prefix)
        if fund:
            cfg = GROUPS[group_name]
            sheet_row = cfg["start_row"] + cfg["tickers"].index(ticker)
            sheets.write_fundamentals_row(ticker, group_name, fund, sheet_row)
            _log(f"  {ticker}: PE={fund.get('pe')}, EY={fund.get('earnings_yield')}%, ROIC={fund.get('roic')}%, val={fund.get('valuation')}")
        else:
            _log(f"  {ticker}: no data returned")
    except Exception as e:
        _log(f"  {ticker} error: {e}")


_log("=== FORCE FUND UPDATE START ===")

# ── US groups ──────────────────────────────────────────────────────────────────
for group_name in US_SCOPE:
    _log(f"\n--- {group_name} ---")
    cfg = GROUPS[group_name]
    for ticker in cfg["tickers"]:
        _write_fund(ticker, group_name, "US")

# ── EU groups ──────────────────────────────────────────────────────────────────
for group_name in EU_SCOPE:
    _log(f"\n--- {group_name} ---")
    cfg = GROUPS[group_name]
    exchange = cfg["exchange"]
    for ticker in cfg["tickers"]:
        sa_prefix = cfg.get("sa_prefix_map", {}).get(ticker, "")
        _write_fund(ticker, group_name, exchange, sa_prefix)

# ── Read back fund_map and existing P&T ───────────────────────────────────────
_log("\nReading Fundamentals and P&T tabs...")
fund_map = sheets.read_fund_all_rows()
pt_rows = sheets.read_pt_all_rows()
_log(f"  {len(fund_map)} fund rows, {len(pt_rows)} P&T rows loaded")

# ── Magic Formula ranks ────────────────────────────────────────────────────────
_log("\nComputing Magic Formula ranks...")
mf_ranks = sheets.compute_and_write_mf_ranks(fund_map)
if mf_ranks:
    top10 = sorted(mf_ranks.items(), key=lambda x: x[1])[:10]
    _log(f"  {len(mf_ranks)} eligible stocks ranked")
    _log(f"  Top 10: {', '.join(f'{t}(#{r})' for t, r in top10)}")
else:
    _log("  No eligible stocks (need ROIC + EY for 2+ non-financial stocks)")

# Re-read fund_map to pick up the MF ranks just written
fund_map = sheets.read_fund_all_rows()

# ── Buy Opportunities ─────────────────────────────────────────────────────────
_log("\nBuilding Buy Opportunities tab...")
buy_summary = sheets.update_buy_opportunities(pt_rows, fund_map, "US")
_log(
    f"  {buy_summary.get('total', 0)} stocks: "
    f"5*:{buy_summary.get('by_score', {}).get(5, 0)} "
    f"4*:{buy_summary.get('by_score', {}).get(4, 0)} "
    f"3*:{buy_summary.get('by_score', {}).get(3, 0)} "
    f"2*:{buy_summary.get('by_score', {}).get(2, 0)}"
)

# ── Dashboard ─────────────────────────────────────────────────────────────────
_log("\nUpdating Dashboard...")
sheets.update_dashboard(pt_rows, buy_summary, "US")

_log("\n=== FORCE FUND UPDATE COMPLETE ===")
