"""Phase 7c: one-time backfill of the Trading Portfolio into Turso's
`portfolios`/`trades` tables. Source: wiki/finance/trading-portfolio.md.

Real money, actively discretionary-traded (not rotation-strategy-driven like
daily-paper-trader, not passive like the Equity/Pension backfill) — one
generic 'DISCRETIONARY-TRADING' strategy row, same reasoning as Phase 7b's
'BUY-AND-HOLD' row.

Unlike Equity/Pension, this file has a clean **Transaction Log** table — one
row per realized trim/exit/short-close, with its own entry/exit/shares/date.
That solves the tranche-blending problem Phase 7's paper-trading backfill
had to document as a limitation: each trim IS its own row here, not blended
into a single figure. The "Closed Positions" table is NOT parsed separately —
it's a redundant summary view of the same full-exit/short-close rows already
in the Transaction Log (confirmed: WOSG's 65-share trim + 135-share full
exit in the Transaction Log sum to the 200 shares in Position Summary;
parsing both tables would double-count).

Known gaps, left NULL rather than fabricated (same principle as Phase 7b):
  - `entry_date`: no date column anywhere in this file (Open/Short/Options
    Positions tables have no Date column; Transaction Log's "Date" column is
    the TRANSACTION date — i.e. exit_date for a trim/close — not when the
    original position was opened).
  - Options `entry_price` (the underlying's price at option entry): not
    recorded anywhere — the Options Positions table only has current
    premium/mark, not the underlying's price on the entry date.

Direction semantics for options (corrected in schema.sql 2026-09-10): a
"Long Put" is bought to open but is a 'short' bet — direction reflects the
directional wager against entry_price/exit_price (the underlying's price),
not whether the contract itself was bought or sold to open.

Run standalone: python trading_backfill.py [--dry-run]
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import db

WIKI_PATH = Path(r"C:\Users\Mike\Documents\Fred\Fred\wiki\finance\trading-portfolio.md")
PORTFOLIO_ID = "trading-portfolio"
STRATEGY_ID = "DISCRETIONARY-TRADING"

_EXCHANGE_MAP = {
    "NASDAQ": "US", "NYSE": "US",
    "LSE": "UK",
    "Euronext": "EU", "Euronext AMS": "EU", "XETRA": "EU", "Xetra": "EU",
}


def _normalize_currency(c: str) -> str:
    """The wiki's own "Currency" column spells pence "GBp"; the rest of this
    pipeline (universe.py, fetchers.py) uses "GBX" — normalize so a query
    joining across portfolios doesn't see both spellings for the same thing.
    """
    c = c.strip()
    return "GBX" if c == "GBp" else c


def _price_and_currency(raw: str) -> tuple[float | None, str | None]:
    """Handles "$71.75" (USD), "2,418p" (GBX, pence), "€136.70" (EUR)."""
    raw = raw.strip()
    if raw.endswith("p"):
        m = re.match(r"([\d,]+\.?\d*)p", raw)
        return (float(m.group(1).replace(",", "")) if m else None), "GBX"
    m = re.match(r"[€$]?([\d,]+\.?\d*)", raw)
    if not m:
        return None, None
    currency = "EUR" if raw.startswith("€") else ("USD" if raw.startswith("$") else None)
    return float(m.group(1).replace(",", "")), currency


def _table_rows(text: str, header_prefix: str) -> list[list[str]]:
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip().startswith(header_prefix)), None)
    if start is None:
        return []
    rows = []
    for line in lines[start + 2 :]:
        line = line.strip()
        if not line.startswith("|"):
            break
        rows.append([c.strip() for c in line.strip("|").split("|")])
    return rows


def _build_exchange_map(text: str) -> dict[str, str]:
    """Transaction Log has no Exchange column, so build ticker -> exchange
    from the tables that do have one (Open/Short Positions, Closed
    Positions) before parsing it. Without this every Transaction Log row
    defaulted to "US", which is wrong for the LSE-listed names that show up
    there (KLR, MGNS, WOSG, DNLM).
    """
    mapping: dict[str, str] = {}
    for header, ticker_idx, exch_idx in [
        ("| Company | Ticker | Exchange | Currency | Shares", 1, 2),
        ("| Company | Ticker | Exchange | Currency | Shares Short", 1, 2),
        ("| Company | Ticker | Exchange | Shares | Entry", 1, 2),
    ]:
        for cells in _table_rows(text, header):
            if len(cells) <= max(ticker_idx, exch_idx):
                continue
            ticker, exch_raw = cells[ticker_idx], cells[exch_idx]
            mapping.setdefault(ticker, _EXCHANGE_MAP.get(exch_raw.strip(), "US"))
    return mapping


def parse_transaction_log(text: str) -> list[dict]:
    """Transaction Log: | Date | Ticker | Action | Shares | Entry | Exit |
    Gross P&L | Gross P&L (€) | CGT | Commission | Net (€) | Note |
    Each row -> one closed trade.
    """
    exchange_map = _build_exchange_map(text)
    rows = _table_rows(text, "| Date | Ticker | Action")
    trades = []
    for i, cells in enumerate(rows):
        if len(cells) < 6:
            continue
        date, ticker, action, shares_raw, entry_raw, exit_raw = cells[:6]
        entry, currency = _price_and_currency(entry_raw)
        exit_price, _ = _price_and_currency(exit_raw)
        try:
            shares = float(shares_raw)
        except ValueError:
            continue
        if entry is None or exit_price is None:
            continue
        direction = "short" if "Short close" in action else "long"
        trades.append({
            "trade_id": f"{PORTFOLIO_ID}-{ticker}-txn{i+1}-{date}",
            "portfolio_id": PORTFOLIO_ID, "strategy_id": STRATEGY_ID,
            "ticker": ticker, "exchange": exchange_map.get(ticker, "US"), "instrument_type": "equity",
            "direction": direction, "entry_date": None, "entry_price": entry,
            "shares": shares, "currency": currency,
            "exit_date": date, "exit_price": exit_price, "status": "closed",
            "thesis": None, "source_signal_id": None,
        })
    return trades


def parse_open_long_positions(text: str) -> list[dict]:
    rows = _table_rows(text, "| Company | Ticker | Exchange | Currency | Shares")
    trades = []
    for cells in rows:
        if len(cells) < 7:
            continue
        _company, ticker, exchange_raw, currency_raw, shares_raw, entry_raw = cells[:6]
        currency = _normalize_currency(currency_raw)
        entry, _ = _price_and_currency(entry_raw)
        if entry is None:
            continue
        try:
            shares = float(shares_raw)
        except ValueError:
            continue
        trades.append({
            "trade_id": f"{PORTFOLIO_ID}-{ticker}-open",
            "portfolio_id": PORTFOLIO_ID, "strategy_id": STRATEGY_ID,
            "ticker": ticker, "exchange": _EXCHANGE_MAP.get(exchange_raw.strip(), "US"),
            "instrument_type": "equity", "direction": "long",
            "entry_date": None, "entry_price": entry, "shares": shares,
            "currency": currency, "status": "open",
            "thesis": None, "source_signal_id": None,
        })
    return trades


def parse_open_short_positions(text: str) -> list[dict]:
    rows = _table_rows(text, "| Company | Ticker | Exchange | Currency | Shares Short")
    trades = []
    for cells in rows:
        if len(cells) < 6:
            continue
        _company, ticker, exchange_raw, currency_raw, shares_raw, entry_raw = cells[:6]
        currency = _normalize_currency(currency_raw)
        entry, _ = _price_and_currency(entry_raw)
        if entry is None:
            continue
        try:
            shares = float(shares_raw)
        except ValueError:
            continue
        trades.append({
            "trade_id": f"{PORTFOLIO_ID}-{ticker}-short-open",
            "portfolio_id": PORTFOLIO_ID, "strategy_id": STRATEGY_ID,
            "ticker": ticker, "exchange": _EXCHANGE_MAP.get(exchange_raw.strip(), "US"),
            "instrument_type": "equity", "direction": "short",
            "entry_date": None, "entry_price": entry, "shares": shares,
            "currency": currency, "status": "open",
            "thesis": None, "source_signal_id": None,
        })
    return trades


def parse_open_options(text: str) -> list[dict]:
    """| Underlying | Ticker | Type | Strike | Expiry | Contracts | Shares |
    Premium Paid | Total Cost (€) | Current Price | Mkt Value (€) | Signal | Notes |
    """
    rows = _table_rows(text, "| Underlying | Ticker | Type")
    trades = []
    for cells in rows:
        if len(cells) < 8:
            continue
        _underlying, ticker, opt_type_raw, strike_raw, expiry, contracts_raw, shares_raw, premium_raw = cells[:8]
        option_type = "put" if "Put" in opt_type_raw else ("call" if "Call" in opt_type_raw else None)
        strike, _ = _price_and_currency(strike_raw)
        premium, prem_currency = _price_and_currency(premium_raw.split("/")[0])
        try:
            contracts = int(contracts_raw)
            shares = float(shares_raw)
        except ValueError:
            contracts, shares = None, None
        # Bought ("Long") = a bearish bet if Put, bullish if Call -> 'short'/'long'
        # respectively, per the direction-is-the-bet convention (schema.sql note).
        direction = "short" if option_type == "put" else "long"
        trades.append({
            "trade_id": f"{PORTFOLIO_ID}-{ticker}-opt-{strike_raw}-{expiry}",
            "portfolio_id": PORTFOLIO_ID, "strategy_id": STRATEGY_ID,
            "ticker": ticker, "exchange": "US", "instrument_type": "option",
            "direction": direction, "entry_date": None, "entry_price": None,
            "shares": shares, "currency": prem_currency,
            "option_type": option_type, "strike": strike, "expiry_date": expiry,
            "premium": premium, "contracts": contracts, "status": "open",
            "thesis": None, "source_signal_id": None,
        })
    return trades


def run(dry_run: bool = False) -> None:
    strategy_row = {
        "strategy_id": STRATEGY_ID, "name": "Discretionary Trading",
        "instrument_type": "equity_long",
        "description": "Actively-managed real-money trading portfolio — discretionary entries/exits/trims, not rotation-strategy-driven; grouped here only so performance views have something to key on.",
        "rules_ref": "finance/models/model-portfolio-management.md", "active": 1,
    }
    portfolio_row = {
        "portfolio_id": PORTFOLIO_ID, "name": "Trading Portfolio",
        "kind": "real", "mirrors_investor_id": None,
        "base_currency": "EUR", "created_date": None, "active": 1,
    }

    text = WIKI_PATH.read_text(encoding="utf-8")
    closed = parse_transaction_log(text)
    open_long = parse_open_long_positions(text)
    open_short = parse_open_short_positions(text)
    open_options = parse_open_options(text)
    all_trades = closed + open_long + open_short + open_options

    print(f"Parsed: {len(closed)} closed (Transaction Log), {len(open_long)} open long, "
          f"{len(open_short)} open short, {len(open_options)} open options — {len(all_trades)} total")
    if len(closed) != 16 or len(open_long) != 15 or len(open_short) != 1 or len(open_options) != 2:
        print("  WARN: parsed counts don't match the expected 16/15/1/2 (as of 2026-09-10) "
              "— check table format hasn't changed before trusting this run.")

    if dry_run:
        print(f"Strategy: {STRATEGY_ID}")
        print(f"Portfolio: {portfolio_row}")
        for t in all_trades:
            print(" ", t)
        print("  ... (dry run, nothing written)")
        return

    client = db.get_client()
    try:
        db.upsert(client, "strategies", [strategy_row])
        db.upsert(client, "portfolios", [portfolio_row])
        n = db.upsert(client, "trades", all_trades)
        print(f"Upserted 1 strategy, 1 portfolio, {n} trades.")
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
