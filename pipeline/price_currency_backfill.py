"""One-time backfill of Turso `prices.currency` / `prices.usd_rate` for the
continental-EU rows that were stored mislabelled.

Root cause (found 2026-10-05, fixed in universe.py 2026-10-17): fetch_stoxx600
wrote currency=None for every STOXX 600 row, technicals.py defaults a missing
currency to USD, so ~350 continental tickers' closes were stored with
currency='USD', usd_rate=1.0 even though the close values themselves are
native (EUR/CHF/SEK/...). The close prices were never wrong — only the label
and the FX rate beside them — so this updates just those two columns.

For each affected (ticker, exchange, date) row: currency becomes the ticker's
universe.currency, and usd_rate becomes that currency's fx_rates rate on or
before the row's own date (same "latest known rate on/before the date"
convention app/api/portfolios/route.ts uses), NOT today's rate, so each
historical row keeps a rate consistent with its own date. A row whose
currency has no rate on/before its date is skipped and reported rather than
written with a guess.

Only touches rows whose (ticker, exchange) is currently ACTIVE in universe
with a known non-USD currency. Rows for tickers universe.py has since
deactivated (the old EU-keyed duplicates of UK securities) are left alone —
they're not read by anything.

Run standalone: python price_currency_backfill.py [--dry-run]
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from bisect import bisect_right

import libsql_client

import db


def _load_fx(client) -> dict[str, tuple[list[str], list[float]]]:
    series: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for r in db.query(client, "SELECT date, currency, usd_rate FROM fx_rates ORDER BY date ASC;"):
        series[r["currency"]].append((r["date"], r["usd_rate"]))
    return {c: ([d for d, _ in pts], [v for _, v in pts]) for c, pts in series.items()}


def _rate_on_or_before(fx, currency: str, date: str) -> float | None:
    if currency == "GBX":
        gbp = _rate_on_or_before(fx, "GBP", date)
        return gbp / 100 if gbp is not None else None
    if currency not in fx:
        return None
    dates, vals = fx[currency]
    i = bisect_right(dates, date)
    return vals[i - 1] if i else None


def run(dry_run: bool = False) -> None:
    client = db.get_client()
    try:
        universe = {
            (r["ticker"], r["exchange"]): r["currency"]
            for r in db.query(client, "SELECT ticker, exchange, currency FROM universe WHERE active = 1;")
        }
        fx = _load_fx(client)
        rows = db.query(client, "SELECT ticker, exchange, date FROM prices WHERE exchange = 'EU' AND currency = 'USD';")
        print(f"Mislabelled candidates (exchange=EU, currency=USD): {len(rows)}")

        updates, skipped = [], Counter()
        per_currency = Counter()
        for r in rows:
            ccy = universe.get((r["ticker"], r["exchange"]))
            if ccy is None:
                skipped["no active universe row / currency unknown"] += 1
                continue
            if ccy == "USD":
                skipped["universe currency is genuinely USD"] += 1
                continue
            rate = _rate_on_or_before(fx, ccy, r["date"])
            if rate is None:
                skipped[f"no {ccy} fx rate on/before date"] += 1
                continue
            updates.append((r["ticker"], r["exchange"], r["date"], ccy, rate))
            per_currency[ccy] += 1

        print(f"To update: {len(updates)}  by currency: {dict(per_currency)}")
        print(f"Skipped: {dict(skipped)}")
        for u in updates[:5]:
            print("  sample:", u)

        if dry_run:
            print("(dry run, nothing written)")
            return

        statements = [
            libsql_client.Statement(
                "UPDATE prices SET currency = :c, usd_rate = :r WHERE ticker = :t AND exchange = :e AND date = :d",
                {"c": ccy, "r": rate, "t": t, "e": e, "d": d},
            )
            for (t, e, d, ccy, rate) in updates
        ]
        for i in range(0, len(statements), 200):
            client.batch(statements[i : i + 200])
        print(f"Updated {len(statements)} prices rows.")
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
