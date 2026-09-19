"""One-time historical backfill of SPY's daily closes into `prices`, back to
2026-04-01 — the reference date for Recommended Trades spec (2026-09-19)
§2's "S&P 500 since Apr 1 '26" benchmark tile. `technicals.py`'s daily run
only ever writes ONE row per ticker (today's), so SPY (added to `universe`
as index_membership='BENCHMARK') needs its own historical backfill the
same way fx_rates_backfill.py did for FX.

Indicators (ma/rsi/macd/rating) are left NULL for these backfilled rows —
SPY isn't screened or traded, only referenced for its raw close price, so
computing a full indicator set per historical day isn't worth the effort.
technicals.py's regular daily run will still add fresh rows with full
indicators going forward (SPY has yahoo_ticker set, so it rides the
existing batched fetch with zero special-casing).

Run standalone: python spy_benchmark_backfill.py [--dry-run] [--days N]
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

import db
from fetchers import fetch_yfinance


def backfill(days: int = 140, dry_run: bool = False) -> None:
    bars = fetch_yfinance("SPY", days=days)
    print(f"SPY: {len(bars)} daily bars fetched")
    if not bars:
        print("No bars returned — aborting.")
        return

    fetched_at = datetime.now(timezone.utc).isoformat()
    rows = [{
        "ticker": "SPY", "exchange": "US", "date": b["date"],
        "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"],
        "volume": b["volume"], "currency": "USD", "usd_rate": 1.0,
        "fetched_at": fetched_at,
    } for b in bars]

    earliest = min(r["date"] for r in rows)
    print(f"Earliest date covered: {earliest} (need on/before 2026-04-01)")
    if earliest > "2026-04-01":
        print("WARNING: backfill doesn't reach 2026-04-01 — increase --days.")

    if dry_run:
        print(f"(dry run, {len(rows)} rows not written)")
        return

    client = db.get_client()
    try:
        n = db.upsert(client, "prices", rows)
        print(f"Upserted {n} rows into prices.")
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--days", type=int, default=140)
    args = parser.parse_args()
    backfill(days=args.days, dry_run=args.dry_run)
