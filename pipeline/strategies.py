"""Strategy definitions + one-time backfill of existing paper-trading history
into Turso's `strategies`/`portfolios`/`trades` tables (spec §7, Phase 7).

Scope: the daily-paper-trader portfolio's 7-strategy rotation (A-G), read
directly from wiki/finance/paper-trading/paper-trading-portfolio.md's Open
Positions + Closed Positions tables. The Trading and Equity/Pension
portfolios (data/portfolios/trading.md, equity.md on the dashboard side)
aren't rotation-strategy-driven the same way and aren't backfilled here —
a follow-up, not skipped by oversight.

Known simplification: the wiki tracks T1/T2 partial-exit tranches (sell 1/3
at +25%, another 1/3 at +50%) with per-tranche realized $ blended into a
single "Total P&L%"/"Total P&L $" figure. The new trades schema is one
entry + one exit per row, with no tranche sub-structure — so a closed
position's exit_price here is the FINAL exit price, not a tranche-weighted
blend, meaning a naive (exit-entry)/entry calc from this row alone will not
always match the wiki's own blended P&L% for positions that hit T1/T2
before their final exit (e.g. MU: naive calc ~-17.5%, wiki's true blended
result -2.81%). Modeling tranches properly is schema work beyond backfill
scope — flagged here, not silently glossed over.

Run standalone: python strategies.py [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import db

WIKI_PORTFOLIO_PATH = Path(r"C:\Users\Mike\Documents\Fred\Fred\wiki\finance\paper-trading\paper-trading-portfolio.md")
PORTFOLIO_ID = "paper-trading-p1"

STRATEGY_DEFINITIONS = {
    "A": {"name": "Value", "description": "Large/mid-cap stocks near 52-week low, low P/E vs sector, positive earnings."},
    "B": {"name": "Momentum", "description": "Stocks up 15-40% over 3 months, still in uptrend, not extended."},
    "C": {"name": "Volume Spike", "description": "2x+ average daily volume on a liquid stock today."},
    "D": {"name": "Earnings Catalyst", "description": "Beat EPS + revenue estimates in last 14 days AND raised forward guidance."},
    "E": {"name": "Sector Rotation", "description": "Most liquid, well-known stock in today's leading sector."},
    "F": {"name": "Small Cap Value", "description": "$300M-$2B market cap, low P/E vs peers, P/B<2, positive FCF or clear path to profitability, Greenblatt-style EY+ROIC."},
    "G": {"name": "Small Cap High Growth", "description": "$300M-$2B market cap, >20% YoY revenue growth, expanding margins, large addressable market."},
    "MAGIC-FORMULA": {"name": "Magic Formula Value", "description": "Systematic Greenblatt EY+ROIC screen (screen.py) — distinct from the A/Value discretionary strategy."},
}

# Exchange labels in the wiki table -> the (ticker, exchange) convention
# already used by universe.py/trades ("US" | "UK" | "EU").
_EXCHANGE_MAP = {
    "NASDAQ": "US", "NYSE": "US", "NYSE (ADR)": "US", "NYSE American": "US",
    "NASDAQ (AEX)": "US",  # dual-listed ADR, primary trade venue in this book is the US line
    "XETRA (DAX)": "EU",
}


def _parse_price(raw: str) -> float | None:
    """Handles plain "$44.55", split-adjusted "~~$663.46~~ $165.87*" (takes
    the current, non-struck value), and EUR-with-USD-hint "€7.89 (~$9.07)"
    (takes the USD estimate in parens, since trades.entry_price/exit_price
    has no separate currency column — matches how the rest of this pipeline
    treats non-USD paper-trading entries as USD-equivalent, per the
    portfolio's own "$1,000 per trade (USD equivalent)" sizing rule).
    """
    raw = raw.strip()
    usd_hint = re.search(r"\(~?\$([\d,]+\.?\d*)\)", raw)
    if usd_hint:
        return float(usd_hint.group(1).replace(",", ""))
    prices = re.findall(r"\$([\d,]+\.?\d*)", raw)
    if not prices:
        return None
    return float(prices[-1].replace(",", ""))  # last = current/final, not the struck-through original


def _parse_table(text: str, header_marker: str) -> list[list[str]]:
    """Extract a markdown table's data rows (as lists of cell strings) given
    a marker string that appears on the header line.
    """
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if header_marker in l), None)
    if start is None:
        return []
    rows = []
    for line in lines[start + 2 :]:  # skip header + separator row
        line = line.strip()
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip("|").split("|")]
        rows.append(cells)
    return rows


def parse_open_positions(text: str) -> list[dict]:
    rows = _parse_table(text, "| #   | Date")
    trades = []
    for cells in rows:
        if len(cells) < 14:
            continue
        _, date, ticker, _company, exchange_raw, strategy_raw, entry_raw, curr_raw = cells[:8]
        strategy_id = strategy_raw.split("—")[0].strip().split()[0] if strategy_raw.strip() else None
        entry = _parse_price(entry_raw)
        if not entry or not strategy_id:
            continue
        exchange = _EXCHANGE_MAP.get(exchange_raw.strip(), "US")
        trades.append({
            "trade_id": f"{PORTFOLIO_ID}-{ticker}-{date}",
            "portfolio_id": PORTFOLIO_ID, "strategy_id": strategy_id,
            "ticker": ticker, "exchange": exchange, "instrument_type": "equity",
            "direction": "long", "entry_date": date, "entry_price": entry,
            "shares": round(1000.0 / entry, 6),
            "status": "open", "thesis": None, "source_signal_id": None,
        })
    return trades


def parse_closed_positions(text: str) -> list[dict]:
    rows = _parse_table(text, "| # | Date In")
    trades = []
    for cells in rows:
        if len(cells) < 11:
            continue
        _, date_in, date_out, ticker, _company, exchange_raw, strategy_raw, entry_raw, exit_raw = cells[:9]
        strategy_id = strategy_raw.split("—")[0].strip().split()[0] if strategy_raw.strip() else None
        entry = _parse_price(entry_raw)
        exit_price = _parse_price(exit_raw)
        if not entry or not exit_price or not strategy_id:
            continue
        exchange = _EXCHANGE_MAP.get(exchange_raw.strip(), "US")
        trades.append({
            "trade_id": f"{PORTFOLIO_ID}-{ticker}-{date_in}",
            "portfolio_id": PORTFOLIO_ID, "strategy_id": strategy_id,
            "ticker": ticker, "exchange": exchange, "instrument_type": "equity",
            "direction": "long", "entry_date": date_in, "entry_price": entry,
            "shares": round(1000.0 / entry, 6),
            "exit_date": date_out, "exit_price": exit_price,
            "status": "closed", "thesis": None, "source_signal_id": None,
        })
    return trades


def run(dry_run: bool = False) -> None:
    strategy_rows = [
        {"strategy_id": sid, "name": d["name"], "instrument_type": "equity_long",
         "description": d["description"], "rules_ref": "daily-paper-trader/instructions.md#strategy-rotation",
         "active": 1}
        for sid, d in STRATEGY_DEFINITIONS.items()
    ]
    portfolio_row = {
        "portfolio_id": PORTFOLIO_ID, "name": "Paper Trading Portfolio (P1)",
        "kind": "paper", "mirrors_investor_id": None,
        "base_currency": "USD", "created_date": "2026-05-16", "active": 1,
    }

    text = WIKI_PORTFOLIO_PATH.read_text(encoding="utf-8")
    open_trades = parse_open_positions(text)
    closed_trades = parse_closed_positions(text)
    print(f"Parsed {len(open_trades)} open + {len(closed_trades)} closed trades")

    if len(open_trades) < 30 or len(closed_trades) < 15:
        print(f"  WARN: parsed counts look low vs the file's own summary (expect ~35 open, ~23 closed) "
              f"— check the table format hasn't changed before trusting this run.", file=sys.stderr)

    if dry_run:
        print(f"Strategies: {[s['strategy_id'] for s in strategy_rows]}")
        print(f"Portfolio: {portfolio_row}")
        for t in (open_trades + closed_trades)[:5]:
            print(" ", t)
        print("  ... (dry run, nothing written)")
        return

    client = db.get_client()
    try:
        db.upsert(client, "strategies", strategy_rows)
        db.upsert(client, "portfolios", [portfolio_row])
        n = db.upsert(client, "trades", open_trades + closed_trades)
        print(f"Upserted {len(strategy_rows)} strategies, 1 portfolio, {n} trades.")
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
