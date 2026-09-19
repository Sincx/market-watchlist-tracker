"""One-time historical backfill of Turso `fx_rates` — daily USD-per-1-unit
rate for every currency the pipeline's portfolios actually use (GBP, EUR,
CHF, DKK, SEK; USD is trivially 1.0 every day). Built for Recommended
Trades spec (2026-09-19) §1a: converting a trade's P&L to EUR correctly
needs the rate as of THAT trade's own entry/exit date, not just today's —
Trading Portfolio positions span back to 2026-07, and `prices.usd_rate`
(the only other place a historical rate might have lived) only goes back
to 2026-09-10.

Uses fetch_yfinance() — already proven reliable elsewhere in this pipeline
— against Yahoo's FX tickers (GBPUSD=X etc.), which carry full daily
history, not just a live quote.

Kept current going forward by technicals.py's existing daily
fetch_fx_rates() call (see the small addition there) — this script is
for the one-time historical gap only, not a recurring job.

Run standalone: python fx_rates_backfill.py [--dry-run] [--days N]
"""
from __future__ import annotations

import argparse

import db
from fetchers import fetch_yfinance

# Same currency set fetch_fx_rates() already covers.
CURRENCIES = ["GBP", "EUR", "CHF", "DKK", "SEK"]


def backfill(days: int = 100, dry_run: bool = False) -> None:
    rows = []
    for ccy in CURRENCIES:
        bars = fetch_yfinance(f"{ccy}USD=X", days=days)
        print(f"{ccy}USD=X: {len(bars)} daily bars")
        for b in bars:
            rows.append({"date": b["date"], "currency": ccy, "usd_rate": b["close"]})

    # USD itself is always 1.0 — needed so _to_eur()-style conversions never
    # special-case the base currency; derive its date range from whichever
    # currency actually returned bars, rather than assuming completeness.
    dates = sorted({r["date"] for r in rows})
    for d in dates:
        rows.append({"date": d, "currency": "USD", "usd_rate": 1.0})

    print(f"Total rows to write: {len(rows)} across {len(dates)} dates, {len(CURRENCIES) + 1} currencies")

    if dry_run:
        print("(dry run, nothing written)")
        return

    client = db.get_client()
    try:
        n = db.upsert(client, "fx_rates", rows)
        print(f"Upserted {n} rows into fx_rates.")
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--days", type=int, default=100, help="How many trading days of history to fetch (default 100, covers back to ~early July)")
    args = parser.parse_args()
    backfill(days=args.days, dry_run=args.dry_run)
