"""Daily deterministic bookkeeping for the 'recommended-trades' shadow
portfolio — Recommended Trades spec (2026-09-19) §3.3. Answers: if every
briefing recommendation had been taken mechanically, with zero discretion,
how would that book have done vs. the real Trading Portfolio?

Two responsibilities, both pure Python, no LLM cost:

1. seed_new_recommendations() — the ONE step that needs the briefing's own
   output: for every signals(source='briefing-recommendation') row that
   doesn't already have a matching recommended-trades trade (matched via
   source_signal_id, so re-running is idempotent), open a position at the
   signal's own recorded entry/size. Shares are the smaller resulting
   count of the real portfolio's own convention — the same
   record_briefing_recommendation.py-written detail JSON is the shared
   write path Phase 3's Pending Trade Ideas queue also consumes, so this
   runs regardless of whether Mike ever approves/rejects that idea for
   the REAL book — the two portfolios are meant to diverge.

2. apply_mechanical_signals() — reuses trading_portfolio_turso_view.py's
   own _signal()/_to_eur() unchanged (the spec named portfolio_update.py
   for this reuse, which is stale as of 2026-09-14's Turso-master switch;
   trading_portfolio_turso_view.py is the current home of that logic).
   Exit: close, no patience-override, ever — the entire point of this
   book. Trim: fixed 50% (Mike's explicit confirmation, 2026-09-19, over
   25%/100% alternatives — the spec's own design left this unspecified
   for the mechanical case, real Trading Portfolio trims are Mike's own
   discretionary 25/50/100%). Add: a flat ADD_SIZE_EUR (Mike's explicit
   confirmation, 2026-09-19, of a "same as normal position sizing"
   default — the spec gave no sizing rule for a mechanical add) — one
   new lot (separate trade row, same convention as a real add), capped
   to at most one add-lot per ticker per ADD_COOLDOWN_DAYS so a signal
   that stays true for a week of consecutive runs doesn't add every
   single day.

Run standalone: python recommended_trades_sync.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta

import db
from fetchers import fetch_fx_rates
from trading_portfolio_turso_view import _signal, _to_eur, _gbx_scale

PORTFOLIO_ID = "recommended-trades"
TODAY = date.today().isoformat()
TRIM_FRACTION = 0.5
ADD_SIZE_EUR = 1000.0
ADD_COOLDOWN_DAYS = 7


def seed_new_recommendations(fx: dict, dry_run: bool = False) -> list[dict]:
    client = db.get_client()
    try:
        signals = db.query(client, """
            SELECT signal_id, ticker, exchange, detail, flagged_date FROM signals
            WHERE source = 'briefing-recommendation'
              AND signal_id NOT IN (
                  SELECT source_signal_id FROM trades
                  WHERE portfolio_id = :pid AND source_signal_id IS NOT NULL
              );
        """, {"pid": PORTFOLIO_ID})
        written = []
        for s in signals:
            try:
                detail = json.loads(s["detail"] or "{}")
            except json.JSONDecodeError:
                continue
            entry, size = detail.get("entry"), detail.get("size")
            if entry is None or size is None:
                continue

            univ = db.query(client, "SELECT currency FROM universe WHERE ticker = :t AND exchange = :e;",
                             {"t": s["ticker"], "e": s["exchange"]})
            currency = univ[0]["currency"] if univ else "USD"

            native_size = size if currency == "EUR" else (
                size * fx.get("EUR", 1.0) / fx.get("GBP" if currency == "GBX" else currency, 1.0)
                * (100 if currency == "GBX" else 1)
            )
            shares = int(native_size // entry)
            if shares < 1:
                continue

            trade_id = f"{PORTFOLIO_ID}-{s['ticker']}-{s['exchange']}-{s['flagged_date']}"
            row = {
                "trade_id": trade_id, "portfolio_id": PORTFOLIO_ID,
                "ticker": s["ticker"], "exchange": s["exchange"],
                "instrument_type": "equity", "direction": "long",
                "entry_date": s["flagged_date"], "entry_price": entry, "shares": shares,
                "currency": currency, "status": "open",
                "thesis": detail.get("thesis"), "source_signal_id": s["signal_id"],
            }
            if not dry_run:
                db.upsert(client, "trades", [row])
            written.append(row)
        return written
    finally:
        client.close()


def apply_mechanical_signals(fx: dict, dry_run: bool = False) -> dict:
    client = db.get_client()
    try:
        rows = db.query(client, """
            SELECT t.trade_id, t.ticker, t.exchange, t.entry_price, t.shares, t.currency,
                   p.close, p.rsi14, p.macd_signal, p.ma50
            FROM trades t
            LEFT JOIN (
                SELECT ticker, exchange, close, rsi14, macd_signal, ma50, date,
                       ROW_NUMBER() OVER (PARTITION BY ticker, exchange ORDER BY date DESC) rn
                FROM prices
            ) p ON p.ticker = t.ticker AND p.exchange = t.exchange AND p.rn = 1
            WHERE t.portfolio_id = :pid AND t.status = 'open';
        """, {"pid": PORTFOLIO_ID})

        for r in rows:
            r["eur_value"] = (_to_eur(r["shares"] * r["close"], r["currency"], fx)
                               if r["shares"] is not None and r["close"] is not None else None)
        total_eur = sum(r["eur_value"] for r in rows if r["eur_value"] is not None)

        exited, trimmed, added, held = [], [], [], []
        for r in rows:
            if r["close"] is None:
                continue
            weight_pct = (abs(r["eur_value"]) / total_eur * 100) if r["eur_value"] and total_eur else None
            sig = _signal(r["rsi14"], r["macd_signal"], r["close"], r["ma50"], weight_pct)

            if sig == "Exit":
                if not dry_run:
                    client.execute(
                        "UPDATE trades SET status = 'closed', exit_date = :d, exit_price = :p WHERE trade_id = :id;",
                        {"d": TODAY, "p": r["close"], "id": r["trade_id"]},
                    )
                exited.append({"ticker": r["ticker"], "exit_price": r["close"]})
            elif sig == "Trim":
                trim_shares = round(r["shares"] * TRIM_FRACTION, 4)
                remaining = r["shares"] - trim_shares
                if not dry_run:
                    closed_row = {
                        "trade_id": f"{r['trade_id']}-trim-{TODAY}", "portfolio_id": PORTFOLIO_ID,
                        "ticker": r["ticker"], "exchange": r["exchange"], "instrument_type": "equity",
                        "direction": "long", "entry_date": None, "entry_price": r["entry_price"],
                        "shares": trim_shares, "currency": r["currency"],
                        "exit_date": TODAY, "exit_price": r["close"], "status": "closed",
                    }
                    db.upsert(client, "trades", [closed_row])
                    client.execute("UPDATE trades SET shares = :s WHERE trade_id = :id;",
                                    {"s": remaining, "id": r["trade_id"]})
                trimmed.append({"ticker": r["ticker"], "trim_shares": trim_shares, "trim_price": r["close"]})
            elif sig == "Add":
                cutoff = (date.today() - timedelta(days=ADD_COOLDOWN_DAYS)).isoformat()
                recent_adds = db.query(client, """
                    SELECT 1 FROM trades
                    WHERE portfolio_id = :pid AND ticker = :t AND exchange = :e
                      AND trade_id LIKE :pattern AND entry_date >= :cutoff;
                """, {"pid": PORTFOLIO_ID, "t": r["ticker"], "e": r["exchange"],
                      "pattern": f"{PORTFOLIO_ID}-{r['ticker']}-{r['exchange']}-add-%", "cutoff": cutoff})
                if recent_adds:
                    added.append({"ticker": r["ticker"], "skipped": f"added within last {ADD_COOLDOWN_DAYS}d"})
                    continue
                native_add_size = ADD_SIZE_EUR if r["currency"] == "EUR" else (
                    ADD_SIZE_EUR * fx.get("EUR", 1.0) / fx.get("GBP" if r["currency"] == "GBX" else r["currency"], 1.0)
                    * (100 if r["currency"] == "GBX" else 1)
                )
                add_shares = int(native_add_size // r["close"])
                if add_shares >= 1:
                    add_trade_id = f"{PORTFOLIO_ID}-{r['ticker']}-{r['exchange']}-add-{TODAY}"
                    if not dry_run:
                        db.upsert(client, "trades", [{
                            "trade_id": add_trade_id, "portfolio_id": PORTFOLIO_ID,
                            "ticker": r["ticker"], "exchange": r["exchange"], "instrument_type": "equity",
                            "direction": "long", "entry_date": TODAY, "entry_price": r["close"],
                            "shares": add_shares, "currency": r["currency"], "status": "open",
                        }])
                    added.append({"ticker": r["ticker"], "add_shares": add_shares, "add_price": r["close"]})
            else:
                held.append({"ticker": r["ticker"], "signal": sig})

        return {"exited": exited, "trimmed": trimmed, "added": added, "held": held, "total_eur": round(total_eur, 2)}
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # Fetched once and shared — calling this twice (or once per signal, an
    # earlier bug in this same file) meant every extra call paid its full,
    # sometimes 50s+, live-API cost again for no reason.
    print("Fetching FX rates...")
    fx = fetch_fx_rates()

    seeded = seed_new_recommendations(fx, dry_run=args.dry_run)
    print(f"Seeded {len(seeded)} new recommended-trades position(s):")
    for r in seeded:
        print(f"  {r['ticker']} ({r['exchange']}): {r['shares']} shares @ {r['entry_price']} {r['currency']}")

    result = apply_mechanical_signals(fx, dry_run=args.dry_run)
    print(f"\nMechanical signals applied — portfolio total: EUR {result['total_eur']:,.2f}")
    print(f"  Exited: {result['exited']}")
    print(f"  Trimmed: {result['trimmed']}")
    print(f"  Added: {result['added']}")
    print(f"  Held/Watch: {result['held']}")
    if args.dry_run:
        print("\n(dry run, nothing written)")
