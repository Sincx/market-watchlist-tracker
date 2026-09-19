"""Daily technicals refresh for the full Turso `universe` — batched yfinance
primary path (100-150 tickers/call), computing the same indicators.py
framework the Google Sheet pipeline already uses, writing into Turso's
`prices` table.

This is a NEW, separate path from pipeline.py's existing run_eu()/run_us()
(which stay untouched and keep writing the Google Sheet for the curated
~183-ticker universe) — per the 2026-09-10 decision to parallel-run the old
and new paths rather than cut over directly. Once the dashboard reads from
Turso (Phase 5) and the Sheet is confirmed redundant (Phase 12), the two
paths can be reconciled; until then this module only adds a write path, it
doesn't replace anything.

Scope: covers every ACTIVE universe row that has a yahoo_ticker (a small
number of STOXX 600 rows don't, per universe.py's country-suffix gaps -
skipped, not treated as an error). FX conversion reuses fetchers.fetch_fx_rates()
- Polygon retained for FX only, per spec §6.

Run standalone: python technicals.py [--dry-run] [--limit N]
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

import db
import indicators
from fetchers import fetch_fx_rates

CHUNK_SIZE = 150
CHUNK_RETRY_DELAY = 5.0


def _bars_from_yf_df(df: pd.DataFrame) -> list[dict]:
    """Convert one ticker's slice of a yf.download() multi-ticker DataFrame
    into the newest-first bar-list shape indicators.py expects.
    """
    if df is None or df.empty:
        return []
    df = df.dropna(subset=["Close", "Open", "High", "Low"])
    if df.empty:
        return []
    bars = []
    for idx, row in df.sort_index(ascending=False).iterrows():
        c = float(row["Close"])
        if math.isnan(c) or c <= 0:
            continue
        bars.append({
            "date": str(idx.date()) if hasattr(idx, "date") else str(idx),
            "open": float(row["Open"]), "high": float(row["High"]),
            "low": float(row["Low"]), "close": c,
            "volume": float(row["Volume"]) if not math.isnan(row["Volume"]) else 0.0,
        })
    return bars


def fetch_batch(yahoo_tickers: list[str], period: str = "1y") -> dict[str, list[dict]]:
    """Batched yf.download() across all tickers, chunked to CHUNK_SIZE/call.
    Returns {yahoo_ticker: bars}. A chunk that raises is retried once after
    CHUNK_RETRY_DELAY, then skipped (its tickers just get no bars) rather
    than failing the whole run — partial coverage over zero coverage.
    """
    results: dict[str, list[dict]] = {}
    chunks = [yahoo_tickers[i : i + CHUNK_SIZE] for i in range(0, len(yahoo_tickers), CHUNK_SIZE)]

    for i, chunk in enumerate(chunks):
        for attempt in (1, 2):
            try:
                data = yf.download(
                    tickers=chunk, period=period, group_by="ticker",
                    auto_adjust=True, threads=True, progress=False,
                )
                break
            except Exception as e:
                print(f"  [technicals] chunk {i+1}/{len(chunks)} attempt {attempt} failed: {e}", file=sys.stderr)
                if attempt == 2:
                    data = None
                else:
                    time.sleep(CHUNK_RETRY_DELAY)

        if data is None:
            continue

        if len(chunk) == 1:
            # yf.download with a single ticker doesn't use a MultiIndex.
            results[chunk[0]] = _bars_from_yf_df(data)
            continue

        for ticker in chunk:
            try:
                sub = data[ticker]
            except (KeyError, TypeError):
                continue
            results[ticker] = _bars_from_yf_df(sub)

        print(f"  [technicals] chunk {i+1}/{len(chunks)}: {len(chunk)} tickers requested, "
              f"{sum(1 for t in chunk if results.get(t))} returned bars")

    return results


def refresh_technicals(dry_run: bool = False, limit: int | None = None) -> tuple[int, int]:
    """Returns (fetched, universe_count) — used by _run_and_record to report
    an accurate same-run count to task_registry, rather than re-deriving it
    from a global MAX(date) query, which conflates this run's coverage with
    whatever was already in the table (real bug found 2026-09-19 testing
    this very wrapper — a 30-ticker test run reported "6/30" because most of
    the 30 test tickers' prices.date already matched an earlier fuller run's
    MAX(date) from other tickers).
    """
    client = db.get_client()
    try:
        universe_rows = db.query(
            client,
            "SELECT ticker, exchange, yahoo_ticker, currency FROM universe "
            "WHERE active = 1 AND yahoo_ticker IS NOT NULL;",
        )
    finally:
        client.close()

    if limit:
        universe_rows = universe_rows[:limit]
    print(f"Universe rows to fetch: {len(universe_rows)}")

    print("Fetching FX rates...")
    fx = fetch_fx_rates()
    print(f"FX: {fx}")

    yahoo_tickers = [r["yahoo_ticker"] for r in universe_rows]
    bars_by_yahoo = fetch_batch(yahoo_tickers)

    fetched_at = datetime.now(timezone.utc).isoformat()
    price_rows = []
    skipped_no_data = 0
    for u in universe_rows:
        bars = bars_by_yahoo.get(u["yahoo_ticker"])
        if not bars:
            skipped_no_data += 1
            continue
        closes = [b["close"] for b in bars]
        opens = [b["open"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        volumes = [b["volume"] for b in bars]

        currency = u["currency"] or "USD"
        usd_rate = fx.get(currency, 1.0 if currency == "USD" else 0.0)
        ind = indicators.compute_all(closes, opens, highs, lows, volumes, currency=currency, usd_rate=usd_rate)
        if not ind:
            skipped_no_data += 1
            continue

        latest = bars[0]
        price_rows.append({
            "ticker": u["ticker"], "exchange": u["exchange"], "date": latest["date"],
            "open": latest["open"], "high": latest["high"], "low": latest["low"],
            "close": latest["close"], "volume": latest["volume"],
            "currency": currency, "usd_rate": ind["usd_rate"],
            "ma20": ind["ma20"] if isinstance(ind["ma20"], (int, float)) else None,
            "ma50": ind["ma50"] if isinstance(ind["ma50"], (int, float)) else None,
            "ma200": ind["ma200"] if isinstance(ind["ma200"], (int, float)) else None,
            "rsi14": ind["rsi14"] if isinstance(ind["rsi14"], (int, float)) else None,
            "macd_signal": ind["macd"],
            "vol_ratio": ind["vol_ratio"] if isinstance(ind["vol_ratio"], (int, float)) else None,
            "technical_rating": ind["rating"],
            # atr14 IS computed by indicators.compute_all() (indicators.atr(),
            # Wilder's ATR) but was silently dropped here — never written to
            # Turso at all despite being fully available. Found 2026-09-14
            # migrating portfolio-management-briefing's stop-loss calc
            # (entry − 1.5×ATR14) to read Turso instead of portfolio_update.py.
            "atr14": ind["atr14"] if isinstance(ind["atr14"], (int, float)) else None,
            "fetched_at": fetched_at,
        })

    print(f"Computed indicators for {len(price_rows)} tickers ({skipped_no_data} skipped — no data or compute failed)")

    if dry_run:
        for row in price_rows[:10]:
            print(" ", row)
        print("  ... (dry run, nothing written)")
        return len(price_rows), len(universe_rows)

    client = db.get_client()
    try:
        n = db.upsert(client, "prices", price_rows)
        print(f"Upserted {n} rows into prices.")
    finally:
        client.close()
    return len(price_rows), len(universe_rows)


_TASK_ID = "refresh-technicals"
_TASK_META = dict(
    kind="daily",
    schedule_cron="15 7 * * *",
    description="Daily — refreshes prices/technical indicators in Turso for the full universe (equities + crypto)",
    entry_point="technicals.py",
)


def _run_and_record(dry_run: bool, limit: int | None) -> None:
    """CLI entry point wrapper — records success/failure to task_registry
    on every real (non-dry-run) invocation, so a scheduled run that crashes
    partway still leaves a same-day trace instead of silently vanishing.
    """
    if dry_run:
        refresh_technicals(dry_run=dry_run, limit=limit)
        return
    try:
        fetched, universe_count = refresh_technicals(dry_run=False, limit=limit)
    except Exception as e:
        c = db.get_client()
        try:
            db.record_task_run(c, _TASK_ID, f"error: {type(e).__name__}: {e}", **_TASK_META)
        finally:
            c.close()
        raise
    else:
        c = db.get_client()
        try:
            db.record_task_run(c, _TASK_ID, f"success: {fetched}/{universe_count} tickers", **_TASK_META)
        finally:
            c.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="Limit to first N universe rows (testing)")
    args = parser.parse_args()
    _run_and_record(dry_run=args.dry_run, limit=args.limit)
