"""Phase 8: generate the Burry shadow portfolio's synthetic `trades` from
`investor_positions` (spec §10 step 4). Sizing: $1,000-per-position
equal-weight, matching the real paper-trading portfolios' own convention
(confirmed with Mike 2026-09-10).

entry_price = investor_positions.entry_price_hint where given; where NULL
(12 of 29 Burry positions), fetches the actual close on disclosed_date via
yfinance directly — Turso's `prices` table has no historical depth back to
June/July/August 2026 (technicals.py only started 2026-09-10), so there's
nothing to read there for these dates yet.

Run standalone: python shadow_portfolio.py [--dry-run] [--investor burry]
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta

import yfinance as yf

import db

SIZE_PER_POSITION_USD = 1000.0

# investor_positions.ticker -> yahoo_ticker, for the ones that don't match
# the plain ticker (HK/AU listings, and any US ticker whose Yahoo symbol
# differs from its investor_positions spelling).
YAHOO_TICKER_MAP = {
    "0700": "0700.HK", "3690": "3690.HK", "TPW": "TPW.AX",
}


def _historical_close(yahoo_ticker: str, on_date: str) -> float | None:
    """Close on or shortly after `on_date` (YYYY-MM-DD) — a few days'
    padding in case the exact date was a non-trading day, taking the first
    available bar on/after it.
    """
    try:
        start = date.fromisoformat(on_date)
        end = start + timedelta(days=7)
        df = yf.Ticker(yahoo_ticker).history(start=str(start), end=str(end), auto_adjust=True)
        df = df.dropna(subset=["Close"])
        if df.empty:
            return None
        return round(float(df["Close"].iloc[0]), 4)
    except Exception as e:
        print(f"  [shadow] {yahoo_ticker} @ {on_date}: fetch error {e}")
        return None


def generate_shadow_trades(investor_id: str = "burry") -> tuple[dict, list[dict]]:
    client = db.get_client()
    try:
        investor = db.query(client, "SELECT * FROM tracked_investors WHERE investor_id = :i;", {"i": investor_id})
        positions = db.query(client, "SELECT * FROM investor_positions WHERE investor_id = :i;", {"i": investor_id})
    finally:
        client.close()

    if not investor:
        raise ValueError(f"No tracked_investors row for {investor_id} — run the backfill first.")

    portfolio_id = f"{investor_id}-shadow"
    portfolio_row = {
        "portfolio_id": portfolio_id, "name": f"{investor[0]['name']} (Shadow)",
        "kind": "shadow", "mirrors_investor_id": investor_id,
        "base_currency": "USD", "created_date": date.today().isoformat(), "active": 1,
    }

    trade_rows = []
    for p in positions:
        yahoo_ticker = YAHOO_TICKER_MAP.get(p["ticker"], p["ticker"])
        entry_price = p["entry_price_hint"]
        if entry_price is None:
            entry_price = _historical_close(yahoo_ticker, p["disclosed_date"])
            if entry_price is None:
                print(f"  [shadow] {p['ticker']}: no price hint AND no historical close found — skipping, can't size a trade with no price")
                continue

        is_closed = p["status"] == "closed"
        # Same "don't fabricate" fallback as entry_price above, applied to
        # the exit leg — added 2026-09-20 after finding Burry's own TSLA/AMAT
        # closed rows had NULL exit_price/exit_date entirely (not just a
        # missing hint), so they contributed $0 to realized P&L despite the
        # source explicitly saying both were covered "for a gain."
        exit_price = None
        if is_closed:
            exit_price = p.get("exit_price_hint")
            if exit_price is None and p.get("exit_date"):
                exit_price = _historical_close(yahoo_ticker, p["exit_date"])
            if exit_price is None:
                print(f"  [shadow] {p['ticker']}: status=closed but no exit price/date resolvable — trade will carry no exit_price")

        trade_rows.append({
            "trade_id": f"{portfolio_id}-{p['ticker']}-{p['disclosed_date']}",
            "portfolio_id": portfolio_id, "strategy_id": None,
            "ticker": p["ticker"], "exchange": p["exchange"], "instrument_type": "equity",
            "direction": p["direction"], "entry_date": p["disclosed_date"], "entry_price": entry_price,
            "shares": round(SIZE_PER_POSITION_USD / entry_price, 6), "currency": "USD",
            "status": "closed" if is_closed else "open",
            "exit_date": p.get("exit_date") if is_closed else None,
            "exit_price": exit_price,
            "thesis": f"Mirrors {investor[0]['name']}'s disclosed {p['direction']} position (source: {p['source_ref']})",
            "source_signal_id": None,
        })
    return portfolio_row, trade_rows


def run(investor_id: str = "burry", dry_run: bool = False) -> None:
    portfolio_row, trade_rows = generate_shadow_trades(investor_id)
    print(f"Portfolio: {portfolio_row['portfolio_id']} ({portfolio_row['name']})")
    print(f"Generated {len(trade_rows)} synthetic trades (${SIZE_PER_POSITION_USD:.0f}/position)")

    if dry_run:
        for t in trade_rows:
            print(f"  {t['ticker']:6s} {t['direction']:5s} entry={t['entry_price']:.2f} "
                  f"shares={t['shares']:.4f} status={t['status']} ({t['entry_date']})")
        print("  ... (dry run, nothing written)")
        return

    client = db.get_client()
    try:
        db.upsert(client, "portfolios", [portfolio_row])
        n = db.upsert(client, "trades", trade_rows)
        print(f"Upserted 1 portfolio, {n} trades.")
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--investor", default="burry")
    args = parser.parse_args()
    run(investor_id=args.investor, dry_run=args.dry_run)
