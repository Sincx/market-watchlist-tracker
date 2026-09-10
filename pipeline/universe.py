"""refresh_universe.py logic — build/refresh the `universe` table in Turso.

Sources:
  - S&P 500:  Wikipedia "List of S&P 500 companies"
  - FTSE 350: Wikipedia "FTSE 100 Index" + "FTSE 250 Index" (350 = 100 + 250)
  - STOXX 600: Wikipedia "STOXX Europe 600"
  - Curated (DJI, NASDAQ, Morningstar-EU/US, the existing hand-picked
    S&P500 subset): read directly from market-watchlist-tracker's own
    config.py GROUPS dict, so the existing curated lists keep flowing in
    without being re-typed here — per spec §5, "keep the existing curated
    Morningstar-flagged + Burry-flagged lists alongside" the full indices.
  - Investor-flagged (Burry today): from config.py BURRY_POSITIONS.
    This is a stopgap for index_membership='INVESTOR_FLAGGED' until Phase
    8's investor_positions backfill exists — per spec §5, once that table
    is populated, this becomes data-driven instead of reading BURRY_POSITIONS
    directly. Until then, this is the only source for it.

A ticker present in multiple sources gets a comma-joined index_membership
(e.g. "SP500,MORNINGSTAR"). Each source is fetched independently and a
failure in one (e.g. STOXX 600's Wikipedia table changing shape) does not
block the others — partial universe coverage is an accepted tradeoff
(spec §16), consistent with the pipeline's existing fallback philosophy.

Run standalone: python universe.py [--dry-run]
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from datetime import date
from typing import Iterable

import libsql_client
import pandas as pd
import requests

import config
import db
import equibles as _equibles

TODAY = date.today().isoformat()

# Country -> Yahoo Finance exchange suffix, for STOXX 600 tickers (which
# Wikipedia lists without one). Countries not in this map still get a row
# (exchange='EU', yahoo_ticker=None) rather than being dropped — the daily
# technicals fetch can skip tickers with no resolvable Yahoo symbol instead
# of the whole universe refresh failing on one unmapped country.
COUNTRY_TO_YAHOO_SUFFIX = {
    "France": "PA", "Germany": "DE", "Netherlands": "AS", "Switzerland": "SW",
    "Spain": "MC", "Italy": "MI", "Sweden": "ST", "Denmark": "CO",
    "Belgium": "BR", "Finland": "HE", "Norway": "OL", "Ireland": "IR",
    "Austria": "VI", "Portugal": "LS", "Luxembourg": "LU", "Poland": "WA",
    "United Kingdom": "L",
}

# Country -> stockanalysis.com's URL exchange-prefix segment (quote/<prefix>/<ticker>),
# for fundamentals.py's fetch_eu(). Confirmed entries carried over from config.py's
# existing Morningstar-EU sa_prefix_map (already verified working there); entries
# marked unconfirmed are a best guess from stockanalysis.com's documented exchange
# list and haven't been spot-checked against a real STOXX 600 fetch yet — left
# unmapped (None) rather than guessed wrong would silently 404 fundamentals.py's
# scrape, so these are included but worth verifying on first real use.
COUNTRY_TO_SA_PREFIX = {
    "United Kingdom": "lon", "France": "epa", "Germany": "xtra", "Netherlands": "ams",
    "Denmark": "cse", "Sweden": "sto", "Switzerland": "swx", "Ireland": "ise",
    "Italy": "bit", "Belgium": "ebr",
    # Unconfirmed — stockanalysis.com's documented prefixes, not yet spot-checked:
    "Spain": "bme", "Norway": "osl", "Finland": "hel", "Austria": "vie",
    "Portugal": "lis", "Luxembourg": "lux", "Poland": "wse",
}


def _normalize_ticker(t: str) -> str:
    """Yahoo-style ticker normalization: dot and space -> dash (e.g.
    "BRK.B" -> "BRK-B", "EPI A" -> "EPI-A"). Must be applied to the bare
    ticker BEFORE any exchange suffix is appended (".ST", ".L", ...) — the
    suffix's own dot is legitimate and must not be touched. Applying this
    after building "TICKER.SUFFIX" instead of before broke Yahoo lookups
    for every share-class STOXX 600 ticker (found 2026-09-10: "EPI A.ST"
    became "EPI-A-ST", not the valid "EPI-A.ST").
    """
    return t.strip().replace(".", "-").replace(" ", "-")


def _get(url: str) -> str:
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.text


def fetch_sp500_equibles() -> list[dict] | None:
    """Primary S&P 500 source: Equibles' GetIndexComposition, sourced from
    IVV's actual daily fund-holdings basket — fresher and more authoritative
    than Wikipedia's community-edited table (confirmed 2026-09-10: caught a
    real Wikipedia staleness case, BF-B still listed there but absent from
    IVV's current basket). Returns None (not []) on any failure, so the
    caller can tell "source unavailable" apart from "index genuinely empty"
    and fall back to Wikipedia.
    """
    session = _equibles.Session()
    if not session.ok:
        return None
    rows_by_ticker: dict[str, dict] = {}
    for offset in (0, 500):
        text = session.call("GetIndexComposition", {"index": "S&P 500", "maxResults": 500, "offset": offset})
        if text is None:
            return None
        for m in re.finditer(r"\|\s*\d+\s*\|\s*([A-Z][A-Z.\-]*)\s*\|", text):
            ticker = _normalize_ticker(m.group(1))
            rows_by_ticker[ticker] = {
                "ticker": ticker, "exchange": "US", "index_membership": "SP500",
                "yahoo_ticker": ticker, "currency": "USD",
                "sector": "", "added_date": TODAY, "active": 1,
            }
    return list(rows_by_ticker.values()) if rows_by_ticker else None


def fetch_sp500_wikipedia() -> list[dict]:
    html = _get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
    table = pd.read_html(io.StringIO(html), attrs={"id": "constituents"})[0]
    rows = []
    for _, r in table.iterrows():
        ticker = _normalize_ticker(str(r["Symbol"]))
        rows.append({
            "ticker": ticker, "exchange": "US", "index_membership": "SP500",
            "yahoo_ticker": ticker, "currency": "USD",
            "sector": str(r.get("GICS Sector", "")), "added_date": TODAY, "active": 1,
        })
    return rows


def fetch_sp500() -> list[dict]:
    rows = fetch_sp500_equibles()
    if rows is not None:
        print(f"  (S&P 500 via Equibles/IVV — fresher than Wikipedia)")
        return rows
    print(f"  (Equibles unavailable, falling back to Wikipedia for S&P 500)")
    return fetch_sp500_wikipedia()


def fetch_ftse350() -> list[dict]:
    rows = []
    for page, label in [("FTSE_100_Index", "FTSE350"), ("FTSE_250_Index", "FTSE350")]:
        html = _get(f"https://en.wikipedia.org/wiki/{page}")
        tables = pd.read_html(io.StringIO(html))
        # The constituent table is the widest one with a "Ticker"-like column.
        table = next(
            (t for t in tables if any("ticker" in str(c).lower() for c in t.columns)),
            None,
        )
        if table is None:
            print(f"  WARN: no constituent table found on {page}", file=sys.stderr)
            continue
        ticker_col = next(c for c in table.columns if "ticker" in str(c).lower())
        sector_col = next((c for c in table.columns if "sector" in str(c).lower()), None)
        for _, r in table.iterrows():
            raw_ticker = str(r[ticker_col]).strip()
            if not raw_ticker or raw_ticker.lower() == "nan":
                continue
            ticker = _normalize_ticker(raw_ticker)
            rows.append({
                "ticker": ticker, "exchange": "UK", "index_membership": label,
                "yahoo_ticker": f"{ticker}.L", "currency": "GBX",
                "sa_prefix": "lon",
                "sector": str(r[sector_col]) if sector_col else "",
                "added_date": TODAY, "active": 1,
            })
    return rows


def fetch_stoxx600() -> list[dict]:
    html = _get("https://en.wikipedia.org/wiki/STOXX_Europe_600")
    tables = pd.read_html(io.StringIO(html))
    table = next(
        (t for t in tables if any("ticker" in str(c).lower() for c in t.columns)
         and any("country" in str(c).lower() for c in t.columns)),
        None,
    )
    if table is None:
        print("  WARN: no STOXX 600 constituent table found", file=sys.stderr)
        return []
    ticker_col = next(c for c in table.columns if "ticker" in str(c).lower())
    country_col = next(c for c in table.columns if "country" in str(c).lower())
    sector_col = next((c for c in table.columns if "sector" in str(c).lower() or "industry" in str(c).lower()), None)
    rows = []
    for _, r in table.iterrows():
        raw_ticker = str(r[ticker_col]).strip()
        country = str(r[country_col]).strip()
        if not raw_ticker or raw_ticker.lower() == "nan":
            continue
        ticker = _normalize_ticker(raw_ticker)
        suffix = COUNTRY_TO_YAHOO_SUFFIX.get(country)
        rows.append({
            "ticker": ticker, "exchange": "EU", "index_membership": "STOXX600",
            "yahoo_ticker": f"{ticker}.{suffix}" if suffix else None,
            "currency": None,
            "sa_prefix": COUNTRY_TO_SA_PREFIX.get(country),
            "sector": str(r[sector_col]) if sector_col else "",
            "added_date": TODAY, "active": 1,
        })
    return rows


def fetch_curated() -> list[dict]:
    """DJI, FTSE100 (curated subset), NASDAQ, S&P500 (curated subset),
    Morningstar-EU, Morningstar-US — from config.py GROUPS, unchanged from
    today's hand-maintained lists.
    """
    rows = []
    for group_name, group in config.GROUPS.items():
        exchange = group.get("exchange", "US")
        currency = group.get("currency")
        currency_map = group.get("currency_map", {})
        yahoo_map = group.get("yahoo_map", {})
        yahoo_suffix = group.get("yahoo_suffix", "")
        dual_listed = group.get("dual_listed", set())
        sa_prefix_map = group.get("sa_prefix_map", {})
        for raw_ticker in group["tickers"]:
            ticker = _normalize_ticker(raw_ticker)
            if raw_ticker in yahoo_map:
                # yahoo_map is keyed by the raw (un-normalized) ticker spelling
                # used in config.py, e.g. "COLO-B": "COLO-B.CO".
                yahoo_ticker = yahoo_map[raw_ticker]
            elif raw_ticker in dual_listed or not yahoo_suffix:
                # Dual-listed ADRs trade under their bare US ticker on Yahoo;
                # groups with no yahoo_suffix (US-listed groups) are already
                # bare US tickers too.
                yahoo_ticker = ticker
            else:
                yahoo_ticker = f"{ticker}{yahoo_suffix}"
            rows.append({
                "ticker": ticker, "exchange": exchange,
                "index_membership": group_name.upper().replace("&", "").replace(" ", ""),
                "yahoo_ticker": yahoo_ticker,
                "currency": currency_map.get(raw_ticker, currency),
                "sa_prefix": sa_prefix_map.get(raw_ticker) or ("lon" if exchange == "UK" else None),
                "sector": "", "added_date": TODAY, "active": 1,
            })
    return rows


def fetch_burry_flagged() -> list[dict]:
    rows = []
    for raw_ticker in config.BURRY_POSITIONS:
        ticker = _normalize_ticker(raw_ticker)
        rows.append({
            "ticker": ticker, "exchange": "US", "index_membership": "INVESTOR_FLAGGED",
            "yahoo_ticker": ticker, "currency": "USD",
            "sector": "", "added_date": TODAY, "active": 1,
        })
    return rows


def merge(sources: Iterable[list[dict]]) -> list[dict]:
    """Merge rows keyed by (ticker, exchange); comma-join index_membership
    when the same ticker/exchange pair appears in more than one source.
    Later sources fill in missing sector/currency/yahoo_ticker but never
    overwrite a value already set.

    Every fetch_*() function normalizes its own tickers via _normalize_ticker()
    (dot/space -> dash) BEFORE building yahoo_ticker, so by the time rows
    reach here, "ticker" is already Yahoo-style and safe to use as the merge
    key directly. That normalization must happen per-source, not here: doing
    it here as a blanket string-replace over an already-built "TICKER.SUFFIX"
    yahoo_ticker corrupts the suffix's own legitimate dot (found as a real
    bug 2026-09-10 — "EPI A.ST" became "EPI-A-ST", not the valid "EPI-A.ST").
    A second real bug this same normalization work caught: config.py's
    curated S&P500 group spells Berkshire "BRK.B" while Wikipedia/Equibles
    use "BRK-B" — without normalizing both to the same key, they landed as
    two separate rows for the same security.
    """
    merged: dict[tuple[str, str], dict] = {}
    for source_rows in sources:
        for row in source_rows:
            key = (row["ticker"], row["exchange"])
            if key not in merged:
                merged[key] = dict(row)
                continue
            existing = merged[key]
            labels = set(existing["index_membership"].split(",")) | {row["index_membership"]}
            existing["index_membership"] = ",".join(sorted(labels))
            for field in ("yahoo_ticker", "currency", "sector", "sa_prefix"):
                if not existing.get(field) and row.get(field):
                    existing[field] = row[field]
    return list(merged.values())


def run(dry_run: bool = False) -> None:
    sources = []
    for name, fetch_fn in [
        ("S&P 500", fetch_sp500),
        ("FTSE 350", fetch_ftse350),
        ("STOXX 600", fetch_stoxx600),
        ("curated groups", fetch_curated),
        ("Burry-flagged", fetch_burry_flagged),
    ]:
        try:
            rows = fetch_fn()
            print(f"{name}: {len(rows)} rows")
            sources.append(rows)
        except Exception as e:
            print(f"  FAILED ({name}): {e}", file=sys.stderr)

    universe_rows = merge(sources)
    print(f"\nMerged universe: {len(universe_rows)} unique (ticker, exchange) rows")

    if dry_run:
        for row in universe_rows[:10]:
            print(" ", row)
        print("  ... (dry run, nothing written)")
        return

    client = db.get_client()
    try:
        n = db.upsert(client, "universe", universe_rows)
        print(f"Upserted {n} rows into universe.")

        # upsert only adds/replaces — a ticker that dropped out of every
        # source this run (e.g. an index change, like BF-B leaving the S&P
        # 500) would otherwise sit forever as a stale active=1 row. Deactivate
        # anything currently active that isn't in this run's merged set.
        # Safe because universe.py is the only writer of this table today —
        # if that ever changes (e.g. a manual entry added directly), this
        # would need to scope the deactivation to universe.py-sourced rows.
        new_keys = {(r["ticker"], r["exchange"]) for r in universe_rows}
        currently_active = db.query(client, "SELECT ticker, exchange FROM universe WHERE active = 1;")
        stale = [r for r in currently_active if (r["ticker"], r["exchange"]) not in new_keys]
        if stale:
            statements = [
                libsql_client.Statement(
                    "UPDATE universe SET active = 0 WHERE ticker = :ticker AND exchange = :exchange",
                    {"ticker": r["ticker"], "exchange": r["exchange"]},
                )
                for r in stale
            ]
            for i in range(0, len(statements), 200):
                client.batch(statements[i : i + 200])
            print(f"Deactivated {len(stale)} rows no longer in any source: "
                  f"{', '.join(r['ticker'] for r in stale[:10])}{' ...' if len(stale) > 10 else ''}")
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
