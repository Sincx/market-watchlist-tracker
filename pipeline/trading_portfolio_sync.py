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
import hashlib
import json
from datetime import date

import db
from fetchers import fetch_fx_rates

PORTFOLIO_ID = "trading-portfolio"
TODAY = date.today().isoformat()


def _to_eur(amount: float, currency: str, fx: dict) -> float:
    """Convert a native-currency amount to EUR via the USD cross-rate every
    fetch_fx_rates() entry already carries (usd_rate = USD per 1 unit of
    that currency). GBX (pence) is first scaled to its GBP value.
    """
    native = amount * 0.01 if currency == "GBX" else amount
    base_ccy = "GBP" if currency == "GBX" else currency
    if base_ccy == "EUR":
        return native
    usd_rate = fx.get(base_ccy)
    eur_usd_rate = fx.get("EUR")
    if not usd_rate or not eur_usd_rate:
        raise ValueError(f"No FX rate available for {base_ccy} — cannot convert to EUR")
    return native * usd_rate / eur_usd_rate


def _write_cash_entry(client, entry_date: str, amount: float, entry_type: str, trade_id: str | None, note: str) -> None:
    """Trading Portfolio's cash_ledger convention (schema.sql, backfilled
    2026-09-19 via trading_cash_backfill.py): opening a short writes NO
    entry (margin, not cash); a long buy/sell is an ordinary cash movement;
    a short close's REALIZED P&L only is one 'short_close_pnl' entry.
    Amount is always EUR — the portfolio's base_currency — never the
    trade's own native currency.
    """
    entry_id = "tp-cash-" + hashlib.sha1(f"{entry_date}|{trade_id}|{entry_type}|{amount}".encode()).hexdigest()[:16]
    db.upsert(client, "cash_ledger", [{
        "entry_id": entry_id, "portfolio_id": PORTFOLIO_ID, "entry_date": entry_date,
        "amount": round(amount, 2), "entry_type": entry_type, "trade_id": trade_id, "note": note,
    }])


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
    trade_id = f"{PORTFOLIO_ID}-{ticker}-{direction}-{TODAY}"
    row = {
        "trade_id": trade_id,
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
        # Cash convention (schema.sql): a long buy debits cash; opening a
        # short posts margin, not cash, so it writes no ledger row at all.
        # Options aren't cash-ledger-integrated here yet — no premium/
        # contracts params on this path, and no evidence this script is
        # used for option opens today (the 2 existing option positions came
        # from the Phase 7c backfill) — a real gap if that changes, not
        # silently guessed at.
        if direction == "long" and instrument_type == "equity":
            fx = fetch_fx_rates()
            amount_eur = -_to_eur(entry_price * shares, currency, fx)
            _write_cash_entry(client, TODAY, amount_eur, "buy", trade_id, f"{ticker} buy")
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
        currency = open_trade["currency"]
        fx = fetch_fx_rates() if currency != "EUR" else {}
        if open_trade["direction"] == "long":
            amount_eur = _to_eur(trim_price * trim_shares, currency, fx) if currency != "EUR" else trim_price * trim_shares
            _write_cash_entry(client, TODAY, amount_eur, "sell", closed_row["trade_id"], f"{ticker} trim")
        else:
            realized_native = (open_trade["entry_price"] - trim_price) * trim_shares
            amount_eur = _to_eur(realized_native, currency, fx) if currency != "EUR" else realized_native
            _write_cash_entry(client, TODAY, amount_eur, "short_close_pnl", closed_row["trade_id"],
                               f"{ticker} short trim close, realized {'gain' if amount_eur >= 0 else 'loss'}")
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
        currency = open_trade["currency"]
        fx = fetch_fx_rates() if currency != "EUR" else {}
        shares = open_trade["shares"]
        if open_trade["direction"] == "long":
            amount_eur = _to_eur(exit_price * shares, currency, fx) if currency != "EUR" else exit_price * shares
            _write_cash_entry(client, TODAY, amount_eur, "sell", open_trade["trade_id"], f"{ticker} exit")
        else:
            realized_native = (open_trade["entry_price"] - exit_price) * shares
            amount_eur = _to_eur(realized_native, currency, fx) if currency != "EUR" else realized_native
            _write_cash_entry(client, TODAY, amount_eur, "short_close_pnl", open_trade["trade_id"],
                               f"{ticker} short close, realized {'gain' if amount_eur >= 0 else 'loss'}")
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
