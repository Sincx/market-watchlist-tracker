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
# for fundamentals.py's fetch_eu(). Entries were carried over from config.py's
# existing Morningstar-EU sa_prefix_map on the assumption it had been verified —
# it hadn't, for Germany at least: "xtra" 404s on every real ticker (SAP, ALV,
# ADS, BAS, ...), the correct prefix is "etr". Found 2026-09-10 when 62/62 active
# German STOXX 600 rows had zero fundamentals coverage, a 100% failure rate that
# stood out against every other country's partial (ticker-not-covered, expected)
# gaps. Fixed here and in config.py; this had presumably been silently broken
# for the original Morningstar-EU group's SAP/ALV rows too, pre-dating the Turso
# migration entirely. Lesson: "carried over from an existing map" is not the
# same as "verified" — spot-check each country for real before trusting it.
# Entries marked unconfirmed below are a best guess from stockanalysis.com's
# documented exchange list and haven't been spot-checked against a real STOXX
# 600 fetch yet — left unmapped (None) rather than guessed wrong would silently
# 404 fundamentals.py's scrape, so these are included but worth verifying on
# first real use.
COUNTRY_TO_SA_PREFIX = {
    "United Kingdom": "lon", "France": "epa", "Germany": "etr", "Netherlands": "ams",
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
                "sector": "", "added_date": TODAY, "active": 1, "asset_class": "equity",
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
            "asset_class": "equity",
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
                "added_date": TODAY, "active": 1, "asset_class": "equity",
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
            "added_date": TODAY, "active": 1, "asset_class": "equity",
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
                "sector": "", "added_date": TODAY, "active": 1, "asset_class": "equity",
            })
    return rows


def fetch_burry_flagged() -> list[dict]:
    rows = []
    for raw_ticker in config.BURRY_POSITIONS:
        ticker = _normalize_ticker(raw_ticker)
        rows.append({
            "ticker": ticker, "exchange": "US", "index_membership": "INVESTOR_FLAGGED",
            "yahoo_ticker": ticker, "currency": "USD",
            "sector": "", "added_date": TODAY, "active": 1, "asset_class": "equity",
        })
    return rows


def fetch_wiki_mentioned() -> list[dict]:
    """Phase 2 (P2.2) — tickers auto-added by resolve_ticker.py from the
    weekly wiki-company-scanner task. Unlike fetch_burry_flagged() (which
    reads a static config.py dict), this derives its list from `signals`
    directly — resolve_ticker.py already writes a `source='wiki-mention'`
    row for every ticker it adds, so that table is the live source of
    truth rather than a second static list to keep in sync. Registering
    this in run()'s source list (not just doing a one-off upsert in
    resolve_ticker.py) is what keeps these tickers active across future
    monthly universe refreshes — run()'s staleness sweep deactivates any
    active row absent from every registered source's current output.
    US-only for now, matching resolve_ticker.py's SEC-only resolution scope.
    """
    client = db.get_client()
    try:
        rows = db.query(client, "SELECT DISTINCT ticker, exchange FROM signals WHERE source = 'wiki-mention';")
    finally:
        client.close()
    result = []
    for r in rows:
        ticker = _normalize_ticker(r["ticker"])
        result.append({
            "ticker": ticker, "exchange": r["exchange"], "index_membership": "WIKI_MENTIONED",
            "yahoo_ticker": ticker, "currency": "USD",
            "sector": "", "added_date": TODAY, "active": 1, "asset_class": "equity",
        })
    return result


# Phase 2 (P2.1): crypto reuses universe/prices as-is (asset_class discriminator,
# exchange='CRYPTO') rather than forking parallel tables. Scoped to watchlist-
# only per Mike's 2026-09-11 decision — no synthetic portfolio, since
# wiki/crypto/crypto-portfolio.md has price data but genuinely no share
# quantities or cost basis for any of these 6 assets (worse than the Phase 7b
# pension-portfolio gap, which at least had entry prices).
#
# Three data tiers (per spec P2.1.3), reflected in yahoo_ticker here:
#   - BTC/ETH/LINK: yfinance covers these directly (BTC-USD etc.) — yahoo_ticker
#     set, so technicals.py's existing batched fetch picks them up with zero
#     code changes (its SELECT is `WHERE active=1 AND yahoo_ticker IS NOT NULL`,
#     no equity-specific branching in the fetch/compute path — confirmed by
#     reading it before building this).
#   - EV/LUCKY/TEST: not on yfinance — yahoo_ticker left NULL so
#     technicals.py skips them; crypto_prices.py fetches all three from
#     CoinGecko's free /simple/price instead. TEST was believed to have no
#     working price API at all (the wiki page documented CoinMarketCap
#     failing to render its price) until Mike supplied CoinGecko's actual
#     current page for it 2026-09-12 — its id had migrated from an old
#     "test-2" slug to "test-3", which is why an earlier symbol-based
#     search hadn't found it.
CRYPTO_ASSETS = [
    # ticker, yahoo_ticker (None if not on yfinance), index_membership
    ("BTC",   "BTC-USD",  "CRYPTO_CORE"),
    ("ETH",   "ETH-USD",  "CRYPTO_CORE"),
    ("LINK",  "LINK-USD", "CRYPTO_CORE"),
    ("EV",    None,       "CRYPTO_DEFI"),
    ("LUCKY", None,       "CRYPTO_DEFI"),
    ("TEST",  None,       "CRYPTO_DEFI"),
]


def fetch_crypto() -> list[dict]:
    rows = []
    for ticker, yahoo_ticker, index_membership in CRYPTO_ASSETS:
        rows.append({
            "ticker": ticker, "exchange": "CRYPTO", "index_membership": index_membership,
            "yahoo_ticker": yahoo_ticker, "currency": "USD",
            "sector": None, "added_date": TODAY, "active": 1, "asset_class": "crypto",
        })
    return rows


# Metadata that has no equity equivalent — migrated by hand from
# wiki/crypto/crypto-portfolio.md and company-everything-inc.md /
# company-smardex.md (contract addresses cross-checked live against
# CoinGecko's /coins/{id} endpoint 2026-09-11, both matched exactly).
CRYPTO_META = [
    {"ticker": "BTC", "chain": "Bitcoin", "contract_address": None, "category": "core",
     "protocol_notes": None},
    {"ticker": "ETH", "chain": "Ethereum", "contract_address": None, "category": "core",
     "protocol_notes": None},
    {"ticker": "LINK", "chain": "Ethereum", "contract_address": None, "category": "core",
     "protocol_notes": None},
    {"ticker": "EV", "chain": "Ethereum/Arbitrum/BSC",
     "contract_address": "0xe7e7e741c23a4767831a56a8c99f522c5ac1e7e7", "category": "defi",
     "protocol_notes": "Everything.inc — unified pool: trade + borrow + lend + up to 100x leverage. "
                        "See wiki/crypto/company-everything-inc.md."},
    {"ticker": "LUCKY", "chain": "BNB Smart Chain",
     "contract_address": "0x67b47971426bb2180453b3993ff2ec319e704444", "category": "defi",
     "protocol_notes": "B-Lucky — staking earns 35% of protocol revenue. CertiK score 3.5/10 per "
                        "the wiki page's last manual check; treat as high-risk/thin-liquidity."},
    {"ticker": "TEST", "chain": "BNB Smart Chain",
     "contract_address": "0x86bb94ddd16efc8bc58e6b056e8df71d9e666429", "category": "meme",
     "protocol_notes": "Test Token, deployed via Binance four.meme. CoinMarketCap failed to render "
                        "its price (per the wiki page, pre-migration), but CoinGecko does cover it — "
                        "id 'test-3' (an older 'test-2' slug had migrated), found via Mike's link "
                        "2026-09-12. Priced daily by crypto_prices.py same as EV/LUCKY."},
]


def seed_crypto_meta(dry_run: bool = False) -> None:
    rows = [{**r, "updated_at": TODAY} for r in CRYPTO_META]
    if dry_run:
        for r in rows:
            print(" ", r)
        return
    client = db.get_client()
    try:
        n = db.upsert(client, "crypto_meta", rows)
        print(f"Upserted {n} rows into crypto_meta.")
    finally:
        client.close()


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
        ("crypto", fetch_crypto),
        ("wiki-mentioned", fetch_wiki_mentioned),
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
    parser.add_argument("--seed-crypto-meta", action="store_true",
                         help="Seed/refresh crypto_meta only (one-off, not part of the regular universe refresh)")
    args = parser.parse_args()
    if args.seed_crypto_meta:
        seed_crypto_meta(dry_run=args.dry_run)
    else:
        run(dry_run=args.dry_run)
