"""Fundamentals refresh for the full Turso `universe` — dispatches each
active ticker to fundamentals.py's existing fetch_us/fetch_uk/fetch_eu
(FMP opportunistic + stockanalysis.com + Shibui, per Phase 3), writing
results into Turso's `fundamentals` table.

Monthly full-sweep cadence per spec §7; this script itself doesn't enforce
that cadence, the caller (a scheduled task) does. Given the ~1,350-ticker
scale and per-ticker scraping (stockanalysis.com has no batch endpoint,
unlike technicals.py's batched yfinance path), a full run takes a while —
run in the background, not interactively.

Writes incrementally (every WRITE_EVERY tickers), not just at the end — a
~30-45 minute per-ticker scrape has real crash/timeout risk, and the first
version of this script held everything in memory until a single upsert at
the very end, meaning a crash at ticker 1300 would have lost all of it.

Run standalone: python fundamentals_refresh.py [--dry-run] [--limit N] [--exchange US|UK|EU]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

import db
from fundamentals import fetch_fundamentals

TODAY = date.today().isoformat()
WRITE_EVERY = 50


def _mkt_cap_to_number(v) -> float | None:
    """mkt_cap arrives as either a raw number (FMP) or a string like
    '1.2B'/'450M' (stockanalysis.com's sibling-parse fallback path).
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().upper().replace(",", "").replace("$", "")
    mult = 1.0
    if s.endswith("T"):
        mult, s = 1e12, s[:-1]
    elif s.endswith("B"):
        mult, s = 1e9, s[:-1]
    elif s.endswith("M"):
        mult, s = 1e6, s[:-1]
    elif s.endswith("K"):
        mult, s = 1e3, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def refresh_fundamentals(dry_run: bool = False, limit: int | None = None, exchange_filter: str | None = None) -> None:
    client = db.get_client()
    try:
        sql = "SELECT ticker, exchange, sa_prefix FROM universe WHERE active = 1"
        if exchange_filter:
            sql += f" AND exchange = '{exchange_filter}'"
        sql += ";"
        universe_rows = db.query(client, sql)
    finally:
        client.close()

    if limit:
        universe_rows = universe_rows[:limit]
    print(f"Universe rows to fetch: {len(universe_rows)}")

    write_client = None if dry_run else db.get_client()
    pending: list[dict] = []
    written_total = 0
    fetched_total = 0
    skipped = 0

    def flush():
        nonlocal pending, written_total
        if pending and write_client:
            n = db.upsert(write_client, "fundamentals", pending)
            written_total += n
        pending = []

    try:
        for i, u in enumerate(universe_rows):
            ticker, exch, sa_prefix = u["ticker"], u["exchange"], u.get("sa_prefix")
            if exch in ("UK", "EU") and not sa_prefix:
                skipped += 1
                continue
            try:
                fund = fetch_fundamentals(ticker, exch, sa_prefix or "")
            except Exception as e:
                print(f"  {ticker} ({exch}): error {e}", file=sys.stderr)
                skipped += 1
                continue

            if not fund or not any(v is not None for k, v in fund.items() if k != "source"):
                skipped += 1
                continue

            fetched_total += 1
            row = {
                "ticker": ticker, "exchange": exch, "as_of_date": TODAY,
                "pe": fund.get("pe"), "fwd_pe": fund.get("fwd_pe"),
                "eps_growth": fund.get("eps_growth"), "rev_growth": fund.get("rev_growth"),
                "div_yield": fund.get("div_yield"), "mkt_cap": _mkt_cap_to_number(fund.get("mkt_cap")),
                "sector": fund.get("sector"), "earnings_yield": fund.get("earnings_yield"),
                "roic": fund.get("roic"), "roe": fund.get("roe"), "ev_ebit": fund.get("ev_ebit"),
                "source": fund.get("source"), "fetched_at": TODAY,
            }
            if dry_run:
                if fetched_total <= 10:
                    print(" ", row)
            else:
                pending.append(row)
                if len(pending) >= WRITE_EVERY:
                    flush()

            if (i + 1) % 50 == 0:
                print(f"  ...{i+1}/{len(universe_rows)} processed, {fetched_total} with data "
                      f"({written_total} written so far), {skipped} skipped")

        flush()
    finally:
        if write_client:
            write_client.close()

    print(f"Done. Fetched fundamentals for {fetched_total} tickers ({skipped} skipped — no sa_prefix or no data).")
    if dry_run:
        print("  (dry run, nothing written)")
    else:
        print(f"Total written to fundamentals: {written_total}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--exchange", choices=["US", "UK", "EU"], default=None)
    args = parser.parse_args()
    refresh_fundamentals(dry_run=args.dry_run, limit=args.limit, exchange_filter=args.exchange)
