"""Consolidated multi-source fallback + row-level freshness/error tracking.
Master spec Phase 13.

fetchers.py already implements per-function fallback (fetch_us_ticker falls
back Polygon->yfinance, fetch_fx_rates falls back AV->yfinance) — the gap
isn't the fallback logic itself, it's that each one is a local try/except
invisible outside that call, and none of it writes anywhere queryable. This
module is the consolidation: one shared fetch_with_fallback() helper with a
config-driven source list, and one place (record()) that every deterministic
job calls to upsert its own per-ticker outcome into `data_quality` —
regardless of whether that job uses fetch_with_fallback() or has its own
existing fetch path (screen.py, options_pricing.py derive from data already
in Turso rather than hitting an external API themselves, so they call
record() directly without needing fetch_with_fallback() at all).

Run standalone for a smoke test: python data_quality.py --dry-run
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, NamedTuple

import libsql_client

import db

_UPSERT_SQL = """
    INSERT INTO data_quality (ticker, exchange, data_type, last_attempt_at, last_success_at,
                               last_source, status, error_message, consecutive_failures)
    VALUES (:ticker, :exchange, :data_type, :now,
            CASE WHEN :status != 'error' THEN :now ELSE NULL END,
            :source, :status, :error,
            CASE WHEN :status = 'error' THEN 1 ELSE 0 END)
    ON CONFLICT(ticker, exchange, data_type) DO UPDATE SET
        last_attempt_at = :now,
        last_success_at = CASE WHEN :status != 'error' THEN :now ELSE last_success_at END,
        last_source = :source,
        status = :status,
        error_message = :error,
        consecutive_failures = CASE WHEN :status = 'error' THEN consecutive_failures + 1 ELSE 0 END;
"""


class FetchResult(NamedTuple):
    bars: list
    source: str | None   # name of the source that actually answered, or None if all failed


class Fetcher(NamedTuple):
    name: str
    fn: Callable[[str], list]


def fetch_with_fallback(ticker: str, sources: list[Fetcher]) -> FetchResult:
    """Try each source in priority order; return the first non-empty result.
    Does not itself write to data_quality — callers record() separately,
    since a caller often knows a richer data_type/context than this
    generic helper does.
    """
    for source in sources:
        try:
            bars = source.fn(ticker)
        except Exception:
            bars = []
        if bars:
            return FetchResult(bars=bars, source=source.name)
    return FetchResult(bars=[], source=None)


def record(client, ticker: str, exchange: str, data_type: str,
           status: str, source: str | None = None, error: str | None = None) -> None:
    """Upsert one data_quality row. status: 'ok' | 'degraded' | 'error'.

    consecutive_failures resets to 0 on 'ok' OR 'degraded' — a fallback
    that succeeded still means real, current data was obtained, so it
    shouldn't count toward a failure streak the same way a true 'error'
    does (Master spec §8 open risk, made explicit here rather than left
    to whoever writes the next caller to guess).
    """
    now = datetime.now(timezone.utc).isoformat()
    client.execute(
        _UPSERT_SQL,
        {"ticker": ticker, "exchange": exchange, "data_type": data_type,
         "now": now, "source": source, "status": status, "error": error},
    )


def record_batch(client, rows: list[dict], batch_size: int = 200) -> int:
    """Batched version of record() — one network round-trip per batch_size
    rows via client.batch(), instead of one round-trip per row. Needed for
    anything running at full-universe scale (technicals.py's ~1,350 tickers
    would otherwise cost ~1,350 individual HTTP calls just for bookkeeping,
    likely doubling that job's runtime for no real benefit — the per-row
    ON CONFLICT DO UPDATE logic is unaffected by batching, since each
    statement in a batch still executes against current DB state and every
    row here is a distinct (ticker, exchange, data_type) key.

    Each row dict: {ticker, exchange, data_type, status, source=None, error=None}.
    """
    if not rows:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    written = 0
    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        statements = [
            libsql_client.Statement(_UPSERT_SQL, {
                "ticker": r["ticker"], "exchange": r["exchange"], "data_type": r["data_type"],
                "now": now, "source": r.get("source"), "status": r["status"], "error": r.get("error"),
            })
            for r in chunk
        ]
        client.batch(statements)
        written += len(chunk)
    return written


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        print("data_quality.py: fetch_with_fallback() and record() are library functions, no standalone run.")
