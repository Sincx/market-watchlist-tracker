"""
Orchestration: run EU or US scope end-to-end.

EU scope (1AM CEST):
  - FTSE100  (rows 27–51)
  - Morningstar-EU (rows 102–115)

US scope (7AM CEST):
  - DJI           (rows 2–26)
  - NASDAQ        (rows 52–76)
  - S&P500        (rows 77–101)
  - Morningstar-US (rows 116–137)
  - Dashboard
  - Buy Opportunities (5-star scoring)
"""

import os
from datetime import datetime
from dotenv import load_dotenv

from config import GROUPS, EU_SCOPE, US_SCOPE
from fetchers import (
    fetch_fx_rates,
    fetch_us_ticker,
    fetch_ftse_ticker,
    fetch_eu_morningstar_ticker,
    StaleDataError,
)
from indicators import compute_all
import sheets
from fundamentals import fetch_fundamentals

load_dotenv()

VERBOSE = os.getenv("VERBOSE", "0") == "1"
SKIP_TICKERS = {t.strip().upper() for t in os.getenv("SKIP_TICKERS", "").split(",") if t.strip()}


def _log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def _is_weekly_run() -> bool:
    """True on Monday UTC, plus late Sunday night to catch EU runs at 1AM CEST (= 11PM Sunday UTC)."""
    day = datetime.today().weekday()
    hour = datetime.today().hour
    return day == 0 or (day == 6 and hour >= 23)


# ── Ticker processors ─────────────────────────────────────────────────────────

def _process_us_ticker(ticker, group_name, cfg, fx):
    if ticker in SKIP_TICKERS:
        _log(f"  SKIP {ticker}")
        return None
    idx = cfg["tickers"].index(ticker)
    sheet_row = cfg["start_row"] + idx
    try:
        closes, opens, highs, lows, volumes = fetch_us_ticker(ticker)
    except StaleDataError as e:
        _log(f"  {ticker}: STALE DATA, skipping — {e}")
        return None
    if not closes:
        _log(f"  {ticker}: no data")
        return None
    ind = compute_all(closes, opens, highs, lows, volumes, currency="USD", usd_rate=1.0)
    if not ind:
        _log(f"  {ticker}: compute failed")
        return None
    _log(f"  {ticker}: {ind['price']} USD  {ind['rating']}")
    return (ticker, group_name, ind, sheet_row, 1.0)


def _process_ftse_ticker(ticker, cfg, fx):
    if ticker in SKIP_TICKERS:
        _log(f"  SKIP {ticker}")
        return None
    idx = cfg["tickers"].index(ticker)
    sheet_row = cfg["start_row"] + idx
    dual = cfg.get("dual_listed", set())
    collisions = cfg.get("collision_tickers", set())
    try:
        closes, opens, highs, lows, volumes, price_ccy = fetch_ftse_ticker(ticker, dual, collisions)
    except StaleDataError as e:
        _log(f"  {ticker}: STALE DATA, skipping — {e}")
        return None
    if not closes:
        _log(f"  {ticker}: no data")
        return None
    usd_rate = fx.get(price_ccy, fx.get("GBX", 0.0))
    ind = compute_all(closes, opens, highs, lows, volumes, currency=price_ccy, usd_rate=usd_rate)
    if not ind:
        _log(f"  {ticker}: compute failed")
        return None
    _log(f"  {ticker}: {ind['price']} {price_ccy}  {ind['rating']}")
    return (ticker, "FTSE100", ind, sheet_row, usd_rate)


def _process_eu_ms_ticker(ticker, cfg, fx):
    if ticker in SKIP_TICKERS:
        _log(f"  SKIP {ticker}")
        return None
    idx = cfg["tickers"].index(ticker)
    sheet_row = cfg["start_row"] + idx
    yahoo_ticker = cfg["yahoo_map"][ticker]
    currency = cfg["currency_map"].get(ticker, "EUR")
    try:
        closes, opens, highs, lows, volumes = fetch_eu_morningstar_ticker(ticker, yahoo_ticker)
    except StaleDataError as e:
        _log(f"  {ticker}: STALE DATA, skipping — {e}")
        return None
    if not closes:
        _log(f"  {ticker}: no data")
        return None
    usd_rate = fx.get(currency, 1.0)
    ind = compute_all(closes, opens, highs, lows, volumes, currency=currency, usd_rate=usd_rate)
    if not ind:
        _log(f"  {ticker}: compute failed")
        return None
    _log(f"  {ticker}: {ind['price']} {currency}  {ind['rating']}")
    return (ticker, "Morningstar-EU", ind, sheet_row, usd_rate)


def _write_fundamentals_safe(ticker, group_name, exchange, sa_prefix=""):
    try:
        fund = fetch_fundamentals(ticker, exchange, sa_prefix)
        if fund:
            cfg = GROUPS[group_name]
            sheet_row = cfg["start_row"] + cfg["tickers"].index(ticker)
            sheets.write_fundamentals_row(ticker, group_name, fund, sheet_row)
            _log(f"  {ticker} fund: PE={fund.get('pe')}, val={fund.get('valuation')}")
    except Exception as e:
        _log(f"  {ticker} fund error: {e}")


def _verify_written(rows):
    if not rows:
        return
    check = rows[-3:] if len(rows) >= 3 else rows
    ok = 0
    for (ticker, _, _, sheet_row, _) in check:
        vals = sheets.read_pt_row(sheet_row)
        if vals and len(vals) > 2 and vals[2]:
            ok += 1
            _log(f"  VERIFY {ticker} row {sheet_row}: {vals[2]!r} OK")
        else:
            _log(f"  VERIFY {ticker} row {sheet_row}: MISSING — write may have failed!")
    _log(f"Verification: {ok}/{len(check)} spot-checks passed.")


# ── EU Run ────────────────────────────────────────────────────────────────────

def run_eu():
    _log("=== EU RUN START ===")
    _log("Fetching FX rates...")
    fx = fetch_fx_rates()
    _log(f"FX: {fx}")

    all_write_rows = []
    do_fund = _is_weekly_run()
    # Note: EU runs at 1AM CEST = 11PM UTC Sunday — _is_weekly_run() catches this.

    _log("\n--- FTSE100 ---")
    cfg = GROUPS["FTSE100"]
    for ticker in cfg["tickers"]:
        result = _process_ftse_ticker(ticker, cfg, fx)
        if result:
            all_write_rows.append(result)
            if do_fund:
                _write_fundamentals_safe(ticker, "FTSE100", "UK")

    _log("\n--- Morningstar-EU ---")
    cfg = GROUPS["Morningstar-EU"]
    for ticker in cfg["tickers"]:
        result = _process_eu_ms_ticker(ticker, cfg, fx)
        if result:
            all_write_rows.append(result)
            if do_fund:
                sa_prefix = cfg["sa_prefix_map"].get(ticker, "")
                _write_fundamentals_safe(ticker, "Morningstar-EU", "EU", sa_prefix)

    _log(f"\nWriting {len(all_write_rows)} rows to Price & Technicals...")
    if all_write_rows:
        sheets.write_pt_rows_batch(all_write_rows)

    _verify_written(all_write_rows)
    _log("=== EU RUN COMPLETE ===")


# ── US Run ────────────────────────────────────────────────────────────────────

def run_us():
    _log("=== US RUN START ===")
    fx = {"USD": 1.0}

    all_write_rows = []
    do_fund = _is_weekly_run()

    for group_name in US_SCOPE:
        _log(f"\n--- {group_name} ---")
        cfg = GROUPS[group_name]
        for ticker in cfg["tickers"]:
            result = _process_us_ticker(ticker, group_name, cfg, fx)
            if result:
                all_write_rows.append(result)
                if do_fund:
                    _write_fundamentals_safe(ticker, group_name, "US")

    _log(f"\nWriting {len(all_write_rows)} rows to Price & Technicals...")
    if all_write_rows:
        sheets.write_pt_rows_batch(all_write_rows)

    _verify_written(all_write_rows)

    # Read back full P&T (all 136 rows incl. EU rows written at 1AM)
    _log("\nReading full P&T tab for Dashboard + Buy Opps...")
    pt_rows = sheets.read_pt_all_rows()

    # Read fundamentals for Buy Opps cross-reference
    _log("Reading Fundamentals tab...")
    fund_map = sheets.read_fund_all_rows()
    _log(f"  {len(fund_map)} fundamentals rows loaded")

    # Buy Opportunities (5-star scoring + full formatting)
    _log("\nBuilding Buy Opportunities tab...")
    buy_summary = sheets.update_buy_opportunities(pt_rows, fund_map, "US")
    _log(f"  Buy Opps: {buy_summary.get('total', 0)} stocks - "
         f"5*:{buy_summary.get('by_score',{}).get(5,0)} "
         f"4*:{buy_summary.get('by_score',{}).get(4,0)} "
         f"3*:{buy_summary.get('by_score',{}).get(3,0)} "
         f"2*:{buy_summary.get('by_score',{}).get(2,0)}")

    # Magic Formula ranks (only when fundamentals ran, i.e. weekly run)
    if _is_weekly_run():
        _log("\nComputing Magic Formula ranks...")
        mf_ranks = sheets.compute_and_write_mf_ranks(fund_map)
        if mf_ranks:
            top5 = sorted(mf_ranks.items(), key=lambda x: x[1])[:5]
            top5_str = ", ".join(f"{t}(#{r})" for t, r in top5)
            _log(f"  MF ranks: {len(mf_ranks)} eligible stocks | Top 5: {top5_str}")
        else:
            _log("  MF ranks: insufficient data (need ROIC + EY for 2+ non-financial stocks)")

    # Dashboard
    _log("\nUpdating Dashboard...")
    sheets.update_dashboard(pt_rows, buy_summary, "US")

    _log("=== US RUN COMPLETE ===")
