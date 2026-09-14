"""Trading Portfolio's Turso sync — the piece missing since the one-time
Phase 7c backfill (2026-09-10). That backfill parsed the wiki's Transaction
Log once and wrote real trades/closed-trims into Turso, but nothing has
kept it in sync since: `portfolio-management-briefing` (the ongoing task
that manages this portfolio) only ever wrote to
`wiki/finance/trading-portfolio.md`, never to Turso. Found 2026-09-14 —
Mike reported the dashboard's Open Positions Value didn't reflect a real
trade made that same day.

Mirrors paper_trader.py's record_trade()/close_trade() shape, but for a
portfolio that is NOT $1,000-normalized (real share counts, native
multi-currency prices) and DOES model trims as their own closed rows
(Phase 7c's Transaction Log parsing already established this convention —
a trim is a fully separate closed trade for the sold shares, not a
same-row shares reduction the way Paper Trading P1's T1/T2 partial exits
are).

Three operations cover everything Step 5 (sell/trim) and Step 6 (buy/add)
of portfolio-management-briefing's SKILL.md need:
  - record_open()      — a brand new long or short position
  - record_trim()      — sell/cover PART of an existing open position
  - record_full_exit() — sell/cover the ENTIRE remaining open position

Run standalone for manual corrections:
  python trading_portfolio_sync.py open   --ticker X --exchange US --direction long --entry-price 100 --shares 10 --currency USD
  python trading_portfolio_sync.py trim   --ticker X --exchange US --trim-shares 3 --trim-price 110
  python trading_portfolio_sync.py exit   --ticker X --exchange US --exit-price 95
"""
from __future__ import annotations

import argparse
import json
from datetime import date

import db

PORTFOLIO_ID = "trading-portfolio"
TODAY = date.today().isoformat()


def _get_open_trade(client, ticker: str, exchange: str) -> dict:
    rows = db.query(client, """
        SELECT * FROM trades
        WHERE portfolio_id = :p AND ticker = :t AND exchange = :e AND status = 'open';
    """, {"p": PORTFOLIO_ID, "t": ticker, "e": exchange})
    if not rows:
        raise ValueError(f"No open trade found for {ticker}/{exchange} in {PORTFOLIO_ID}")
    if len(rows) > 1:
        raise ValueError(f"Multiple open trades found for {ticker}/{exchange} — resolve manually, "
                          f"this script assumes exactly one open row per (ticker, exchange)")
    return rows[0]


def record_open(ticker: str, exchange: str, direction: str, entry_price: float, shares: float,
                 currency: str, instrument_type: str = "equity", thesis: str | None = None,
                 dry_run: bool = False) -> dict:
    row = {
        "trade_id": f"{PORTFOLIO_ID}-{ticker}-{direction}-{TODAY}",
        "portfolio_id": PORTFOLIO_ID, "ticker": ticker, "exchange": exchange,
        "instrument_type": instrument_type, "direction": direction,
        "entry_date": TODAY, "entry_price": entry_price, "shares": shares, "currency": currency,
        "status": "open", "thesis": thesis,
    }
    if dry_run:
        return {"would_write": row}
    client = db.get_client()
    try:
        db.upsert(client, "trades", [row])
    finally:
        client.close()
    return {"written": row}


def record_trim(ticker: str, exchange: str, trim_shares: float, trim_price: float,
                 dry_run: bool = False) -> dict:
    """Sells/covers part of an existing open position. Writes the trimmed
    portion as its own new closed trade (same entry_price/entry_date/
    direction/currency as the open row, its own shares/exit_price/exit_date)
    and reduces the existing open row's shares by trim_shares — same shape
    Phase 7c's backfill already established for every historical trim.
    """
    client = db.get_client()
    try:
        open_trade = _get_open_trade(client, ticker, exchange)
        remaining = open_trade["shares"] - trim_shares
        if remaining < 0:
            raise ValueError(f"trim_shares ({trim_shares}) exceeds open shares ({open_trade['shares']})")

        closed_row = {
            "trade_id": f"{PORTFOLIO_ID}-{ticker}-trim-{TODAY}",
            "portfolio_id": PORTFOLIO_ID, "ticker": ticker, "exchange": exchange,
            "instrument_type": open_trade["instrument_type"], "direction": open_trade["direction"],
            "entry_date": open_trade["entry_date"], "entry_price": open_trade["entry_price"],
            "shares": trim_shares, "currency": open_trade["currency"],
            "exit_date": TODAY, "exit_price": trim_price, "status": "closed",
        }
        if dry_run:
            return {"would_write_closed": closed_row, "would_update_open_shares_to": remaining}

        db.upsert(client, "trades", [closed_row])
        client.execute(
            "UPDATE trades SET shares = :s WHERE trade_id = :id;",
            {"s": remaining, "id": open_trade["trade_id"]},
        )
    finally:
        client.close()
    return {"closed": closed_row, "open_shares_now": remaining}


def record_full_exit(ticker: str, exchange: str, exit_price: float, dry_run: bool = False) -> dict:
    """Sells/covers the entire remaining open position. Works for both a
    long sell and a short cover — direction is whatever the open row
    already has, nothing to specify.
    """
    client = db.get_client()
    try:
        open_trade = _get_open_trade(client, ticker, exchange)
        if dry_run:
            return {"would_close": {"trade_id": open_trade["trade_id"], "exit_date": TODAY, "exit_price": exit_price}}
        client.execute(
            "UPDATE trades SET status = 'closed', exit_date = :d, exit_price = :p WHERE trade_id = :id;",
            {"d": TODAY, "p": exit_price, "id": open_trade["trade_id"]},
        )
    finally:
        client.close()
    return {"closed": {"trade_id": open_trade["trade_id"], "exit_date": TODAY, "exit_price": exit_price}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    op = sub.add_parser("open")
    op.add_argument("--ticker", required=True)
    op.add_argument("--exchange", required=True)
    op.add_argument("--direction", required=True, choices=["long", "short"])
    op.add_argument("--entry-price", type=float, required=True)
    op.add_argument("--shares", type=float, required=True)
    op.add_argument("--currency", required=True)
    op.add_argument("--thesis", default=None)
    op.add_argument("--dry-run", action="store_true")

    trim = sub.add_parser("trim")
    trim.add_argument("--ticker", required=True)
    trim.add_argument("--exchange", required=True)
    trim.add_argument("--trim-shares", type=float, required=True)
    trim.add_argument("--trim-price", type=float, required=True)
    trim.add_argument("--dry-run", action="store_true")

    exit_p = sub.add_parser("exit")
    exit_p.add_argument("--ticker", required=True)
    exit_p.add_argument("--exchange", required=True)
    exit_p.add_argument("--exit-price", type=float, required=True)
    exit_p.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    if args.cmd == "open":
        result = record_open(args.ticker, args.exchange, args.direction, args.entry_price,
                              args.shares, args.currency, thesis=args.thesis, dry_run=args.dry_run)
    elif args.cmd == "trim":
        result = record_trim(args.ticker, args.exchange, args.trim_shares, args.trim_price, dry_run=args.dry_run)
    else:
        result = record_full_exit(args.ticker, args.exchange, args.exit_price, dry_run=args.dry_run)

    print(json.dumps(result, indent=2))
