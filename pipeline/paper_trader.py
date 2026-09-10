"""daily-paper-trader's Python side (Phase 6 redesign, spec §12): rotation
logic (Step 4), capital-cap check (Step 1B), and the `trades` DB write
(replacing Step 7's markdown-only record). Candidate generation itself is
candidates.py — this module is what decides WHICH strategy to use today and
records the LLM's final pick.

Scope note: this covers the NEW-TRADE-SELECTION flow only (Steps 1B, 4-7 of
the current instructions.md) — not the daily discretionary exit-monitoring
of the 35 existing open positions (Steps 2-3), which still re-fetches prices
per-ticker today. Since most of those holdings are large/mid-caps already in
the active `universe` (tracked daily by technicals.py), that monitoring
could likely also read from Turso `prices` instead of its own WebFetch/
Massive/yfinance calls per ticker — a real follow-up opportunity, flagged
here but not built in this pass to keep this deliverable scoped to what was
asked.

Run standalone: python paper_trader.py select-strategy | capital-check | record-trade ...
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

import db
from fetchers import fetch_fx_rates

PORTFOLIO_ID = "paper-trading-p1"
CAPITAL_CAP_EUR = 30_000
TODAY = date.today().isoformat()
STRATEGIES = ["A", "B", "C", "D", "E", "F", "G"]


def select_strategy() -> dict:
    """Step 4's logic: avoid yesterday's strategy, weight by this week's
    performance lean (strategies with >=3 closed trades and a win rate
    clearly above/below the portfolio average get favoured/de-emphasised,
    per instructions.md's Step 3G rule — never fully excluded, to avoid
    overfitting a small sample).
    """
    client = db.get_client()
    try:
        last_trade = db.query(client, """
            SELECT strategy_id, entry_date FROM trades
            WHERE portfolio_id = :p AND entry_date IS NOT NULL
            ORDER BY entry_date DESC LIMIT 1;
        """, {"p": PORTFOLIO_ID})
        last_strategy = last_trade[0]["strategy_id"] if last_trade else None

        perf = db.query(client, "SELECT * FROM v_strategy_performance WHERE portfolio_id = :p;", {"p": PORTFOLIO_ID})
    finally:
        client.close()

    perf_by_strategy = {p["strategy_id"]: p for p in perf if p["strategy_id"] in STRATEGIES}
    eligible_wins = [p["wins"] / p["trades_closed"] for p in perf_by_strategy.values() if p["trades_closed"] >= 3]
    portfolio_avg_win_rate = sum(eligible_wins) / len(eligible_wins) if eligible_wins else None

    candidates = [s for s in STRATEGIES if s != last_strategy]
    scored = []
    for s in candidates:
        p = perf_by_strategy.get(s)
        lean = "neutral"
        if p and p["trades_closed"] >= 3 and portfolio_avg_win_rate is not None:
            win_rate = p["wins"] / p["trades_closed"]
            if win_rate > portfolio_avg_win_rate + 0.1:
                lean = "favour"
            elif win_rate < portfolio_avg_win_rate - 0.1:
                lean = "de-emphasise"
        scored.append({"strategy_id": s, "lean": lean,
                        "trades_closed": p["trades_closed"] if p else 0,
                        "win_rate": round(p["wins"] / p["trades_closed"], 3) if p and p["trades_closed"] else None})

    order = {"favour": 0, "neutral": 1, "de-emphasise": 2}
    scored.sort(key=lambda x: order[x["lean"]])

    return {
        "excluded_yesterday": last_strategy,
        "portfolio_avg_win_rate": round(portfolio_avg_win_rate, 3) if portfolio_avg_win_rate else None,
        "candidates_ranked": scored,
        "suggested": scored[0]["strategy_id"] if scored else None,
        "note": "Suggested = top of the lean-sorted list, not a forced pick — the day's market context "
                "(per instructions.md Step 4.4) should still override when a different eligible strategy fits better.",
    }


def capital_check() -> dict:
    """Step 1B's logic: sum open cost basis (USD) as shares*entry_price.

    The 7 positions that had already banked a T1/T2 partial exit as of the
    2026-09-10 backfill (CRWD/HPQ at 2/3 remaining, AMAT/TNET/RDW/MRNA/CMCL
    at 1/3) had their `shares` corrected by hand at that point to reflect
    actual remaining size — without that, this check overstated cost basis
    by ~€4,000+ and would have permanently blocked new trades (open_cost_usd
    was a flat $35,000 for 35 full-sized positions instead of the real
    ~$31,000). That was a one-time data fix, not an ongoing mechanism: the
    underlying gap remains — trades doesn't model tranches, so any FUTURE
    T1/T2 trigger needs the same kind of manual `shares` correction until
    the exit-monitoring flow (Steps 2-3, not redesigned in this pass — see
    module docstring) is taught to apply it automatically when it detects a
    new T1/T2 trigger.
    """
    client = db.get_client()
    try:
        rows = db.query(client, """
            SELECT shares, entry_price FROM trades
            WHERE portfolio_id = :p AND status = 'open' AND instrument_type = 'equity';
        """, {"p": PORTFOLIO_ID})
    finally:
        client.close()

    open_cost_usd = sum((r["shares"] or 0) * (r["entry_price"] or 0) for r in rows)
    fx = fetch_fx_rates()
    eur_usd = fx.get("EUR", 1.0)
    open_cost_eur = open_cost_usd / eur_usd if eur_usd else open_cost_usd
    headroom_eur = CAPITAL_CAP_EUR - open_cost_eur

    return {
        "open_positions": len(rows), "open_cost_usd": round(open_cost_usd, 2),
        "eur_usd_rate": eur_usd, "open_cost_eur": round(open_cost_eur, 2),
        "capital_cap_eur": CAPITAL_CAP_EUR, "headroom_eur": round(headroom_eur, 2),
        "can_enter_new_trade": headroom_eur >= 900,  # ~$1,000 needs ~€900-950 per instructions.md
        "note": "open_cost_usd assumes each open position is still full-sized (shares*entry_price) — "
                "overstates cost basis for positions that already banked a T1/T2 partial exit, since "
                "trades doesn't model tranches. Cross-check against the wiki page's own Capital Cap line "
                "if a trade is borderline.",
    }


def record_trade(ticker: str, exchange: str, strategy_id: str, entry_price: float,
                  thesis: str, stop_loss: float | None = None, target1: float | None = None,
                  target2: float | None = None, source_signal_id: str | None = None,
                  dry_run: bool = False) -> dict:
    """Step 7's DB-write: one new open trades row, $1,000 nominal (matching
    the portfolio's own sizing convention — shares = 1000/entry_price).
    """
    row = {
        "trade_id": f"{PORTFOLIO_ID}-{ticker}-{TODAY}",
        "portfolio_id": PORTFOLIO_ID, "strategy_id": strategy_id,
        "ticker": ticker, "exchange": exchange, "instrument_type": "equity",
        "direction": "long", "entry_date": TODAY, "entry_price": entry_price,
        "shares": round(1000.0 / entry_price, 6), "currency": "USD",
        "stop_loss": stop_loss, "target1": target1, "target2": target2,
        "status": "open", "thesis": thesis, "source_signal_id": source_signal_id,
    }
    if dry_run:
        return {"would_write": row}

    client = db.get_client()
    try:
        db.upsert(client, "trades", [row])
    finally:
        client.close()
    return {"written": row}


def close_trade(ticker: str, entry_date: str, exit_price: float, dry_run: bool = False) -> dict:
    """Marks an existing open trade closed — stop-loss, signal exit, or
    discretionary exit. `entry_date` must match the original trade's
    entry_date (part of its trade_id: paper-trading-p1-{ticker}-{entry_date}).
    Without this, Turso's `trades` table would only ever grow with new
    entries and never reflect a real exit — v_strategy_performance and
    v_portfolio_performance would silently drift from reality the moment
    any position actually closed.
    """
    trade_id = f"{PORTFOLIO_ID}-{ticker}-{entry_date}"
    if dry_run:
        return {"would_close": {"trade_id": trade_id, "exit_date": TODAY, "exit_price": exit_price}}

    client = db.get_client()
    try:
        client.execute(
            "UPDATE trades SET status = 'closed', exit_date = :d, exit_price = :p WHERE trade_id = :id;",
            {"d": TODAY, "p": exit_price, "id": trade_id},
        )
    finally:
        client.close()
    return {"closed": {"trade_id": trade_id, "exit_date": TODAY, "exit_price": exit_price}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("select-strategy")
    sub.add_parser("capital-check")
    rec = sub.add_parser("record-trade")
    rec.add_argument("--ticker", required=True)
    rec.add_argument("--exchange", required=True)
    rec.add_argument("--strategy", required=True)
    rec.add_argument("--entry-price", type=float, required=True)
    rec.add_argument("--thesis", required=True)
    rec.add_argument("--stop-loss", type=float, default=None)
    rec.add_argument("--target1", type=float, default=None)
    rec.add_argument("--target2", type=float, default=None)
    rec.add_argument("--signal-id", default=None)
    rec.add_argument("--dry-run", action="store_true")

    close = sub.add_parser("close-trade")
    close.add_argument("--ticker", required=True)
    close.add_argument("--entry-date", required=True, help="Original entry_date, e.g. 2026-06-01 — part of the trade_id")
    close.add_argument("--exit-price", type=float, required=True)
    close.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    if args.cmd == "select-strategy":
        print(json.dumps(select_strategy(), indent=2))
    elif args.cmd == "capital-check":
        print(json.dumps(capital_check(), indent=2))
    elif args.cmd == "record-trade":
        result = record_trade(args.ticker, args.exchange, args.strategy, args.entry_price,
                               args.thesis, args.stop_loss, args.target1, args.target2,
                               args.signal_id, dry_run=args.dry_run)
        print(json.dumps(result, indent=2))
    elif args.cmd == "close-trade":
        result = close_trade(args.ticker, args.entry_date, args.exit_price, dry_run=args.dry_run)
        print(json.dumps(result, indent=2))
