"""Phase 2 (P2.2) — deterministic company-name -> ticker resolution for the
wiki-mention watchlist scanner.

Split per the spec: extraction (deciding whether prose names a real,
investable company) is LLM judgment, done by the `wiki-company-scanner`
scheduled task before this script ever runs. Resolution (name -> ticker) is
deterministic Python, which is what this module does.

Primary source: SEC's free `company_tickers.json` (~10,000 US-listed
companies, no key required, cached locally). `~/.edgar` and the
`mcp__edgartools__*` MCP server were both evaluated and rejected for this —
`.edgar` is just an inert HTTP response cache (no ticker list in it, the
`edgartools` package isn't even installed), and the MCP server (a) is
unauthenticated and (b) wouldn't be callable from a standalone script like
this one anyway. A direct SEC fetch is self-contained and matches the
spec's own "deterministic Python" design intent.

Scope decision (deviates from the spec's literal text, noted here rather
than silently): the spec says non-US names should fall back to a yfinance
search lookup. yfinance has no stable, version-independent search-by-name
API to build a deterministic resolver on, and the Open Risks section itself
flags false-positive ticker matches as the main danger of this whole
feature. So non-US / SEC-unmatched candidates go to the pending-review
queue unconditionally rather than attempting a fuzzier automated guess —
conservative by design, per the spec's own "a wrong auto-added ticker is
worse than a slightly-delayed manual confirmation" principle.

Run standalone: python resolve_ticker.py <candidates.json> [--dry-run]
Candidates JSON shape: [{"company_name": str, "wiki_page": str, "context_snippet": str}, ...]
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import certifi
import requests

import db
from universe import _normalize_ticker

TODAY = date.today().isoformat()


def _build_ssl_verify() -> str:
    """A CA bundle trusting both certifi's normal roots AND Norton's
    injected root. Norton intercepts TLS for SOME domains on this machine
    (replacing the real cert chain with one signed by its own root) but not
    others (the real chain applies unmodified) — found the hard way here:
    CoinGecko needed Norton's cert alone (it's intercepted), but SEC.gov
    failed with Norton's cert alone (it ISN'T intercepted — verify=<path>
    replaces the trusted set entirely rather than adding to it, so a
    single-cert bundle only covers one of the two cases). A merged bundle
    covers both without needing to guess which applies per domain.
    """
    norton_cert = os.getenv("REQUESTS_CA_BUNDLE") or os.getenv("CURL_CA_BUNDLE")
    if not norton_cert or not Path(norton_cert).exists():
        return certifi.where()
    combined_path = Path(__file__).parent / ".combined_ca_bundle.pem"
    certifi_path = Path(certifi.where())
    if not combined_path.exists() or combined_path.stat().st_mtime < certifi_path.stat().st_mtime:
        combined_path.write_bytes(certifi_path.read_bytes() + b"\n" + Path(norton_cert).read_bytes())
    return str(combined_path)


_SSL_VERIFY = _build_ssl_verify()

_SEC_CACHE_PATH = Path(__file__).parent / ".sec_company_tickers.json"
_SEC_CACHE_MAX_AGE_DAYS = 30
_SEC_URL = "https://www.sec.gov/files/company_tickers.json"
# SEC's fair-access policy asks for an identifying User-Agent (name + contact)
# so they can reach out if a script causes load issues, rather than just
# blocking it outright.
_SEC_HEADERS = {"User-Agent": "Fred Finance System sinclair.ma77@gmail.com"}

# Fuzzy-match cutoff for treating a name match as high-confidence. Deliberately
# strict per the spec's own flagged risk (common words/acronyms fuzzy-matching
# the wrong ticker) — loosen later if the pending-review queue turns out too
# noisy with genuinely-correct near-misses, per the spec's own suggested tuning.
_FUZZY_CUTOFF = 0.92

_SUFFIX_RE = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|llc|plc|group|holdings?|the)\b\.?",
    re.IGNORECASE,
)


def _normalize_name(name: str) -> str:
    n = name.lower()
    n = _SUFFIX_RE.sub("", n)
    n = re.sub(r"[^a-z0-9]+", " ", n).strip()
    return n


def fetch_sec_tickers(force_refresh: bool = False) -> dict[str, dict]:
    """Returns {normalized_name: {"ticker": str, "cik": str, "title": str}}.
    Cached to disk since this is a ~1,000-company-per-100KB list that
    changes rarely — refetched if the cache is missing or stale.
    """
    if not force_refresh and _SEC_CACHE_PATH.exists():
        age_days = (datetime.now() - datetime.fromtimestamp(_SEC_CACHE_PATH.stat().st_mtime)).days
        if age_days < _SEC_CACHE_MAX_AGE_DAYS:
            raw = json.loads(_SEC_CACHE_PATH.read_text())
            return _index_sec_raw(raw)

    resp = requests.get(_SEC_URL, headers=_SEC_HEADERS, timeout=30, verify=_SSL_VERIFY)
    resp.raise_for_status()
    raw = resp.json()
    _SEC_CACHE_PATH.write_text(json.dumps(raw))
    return _index_sec_raw(raw)


def _index_sec_raw(raw: dict) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for row in raw.values():
        title = row.get("title", "")
        ticker = row.get("ticker", "")
        if not title or not ticker:
            continue
        key = _normalize_name(title)
        if key and key not in index:  # first (lowest CIK index) wins on collision
            index[key] = {"ticker": ticker, "cik": row.get("cik_str"), "title": title}
    return index


def resolve_us_company(name: str, sec_index: dict[str, dict]) -> dict | None:
    """Exact normalized-name match first, then a strict fuzzy match. Returns
    None (-> pending review) rather than a low-confidence guess.
    """
    key = _normalize_name(name)
    if not key:
        return None
    if key in sec_index:
        return {**sec_index[key], "match_type": "exact"}
    close = difflib.get_close_matches(key, sec_index.keys(), n=1, cutoff=_FUZZY_CUTOFF)
    if close:
        return {**sec_index[close[0]], "match_type": "fuzzy", "matched_name": close[0]}
    return None


def _already_tracked(client, ticker: str, exchange: str) -> bool:
    rows = db.query(client, "SELECT 1 FROM universe WHERE ticker = :t AND exchange = :e AND active = 1;",
                     {"t": ticker, "e": exchange})
    return bool(rows)


def resolve_candidates(candidates: list[dict], dry_run: bool = False) -> dict:
    sec_index = fetch_sec_tickers()
    client = db.get_client()
    resolved, already_tracked, pending = [], [], []
    try:
        for c in candidates:
            name = c["company_name"]
            match = resolve_us_company(name, sec_index)
            if match is None:
                pending.append({**c, "reason": "no confident SEC match (non-US or not a public US company)"})
                continue
            ticker = _normalize_ticker(match["ticker"])
            if _already_tracked(client, ticker, "US"):
                already_tracked.append({**c, "ticker": ticker})
                continue
            resolved.append({**c, "ticker": ticker, "exchange": "US", "sec_title": match["title"],
                              "match_type": match["match_type"]})

        if not dry_run and resolved:
            universe_rows = [{
                "ticker": r["ticker"], "exchange": r["exchange"], "index_membership": "WIKI_MENTIONED",
                "yahoo_ticker": r["ticker"], "currency": "USD",
                "sector": "", "added_date": TODAY, "active": 1, "asset_class": "equity",
            } for r in resolved]
            db.upsert(client, "universe", universe_rows)

            signal_rows = [{
                "signal_id": str(uuid.uuid4()), "ticker": r["ticker"], "exchange": r["exchange"],
                "source": "wiki-mention",
                "detail": json.dumps({"company_name": r["company_name"], "context_snippet": r["context_snippet"],
                                       "sec_title": r["sec_title"], "match_type": r["match_type"]}),
                "flagged_date": TODAY, "source_ref": r["wiki_page"],
            } for r in resolved]
            db.upsert(client, "signals", signal_rows)
    finally:
        client.close()

    return {"resolved": resolved, "already_tracked": already_tracked, "pending": pending}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("candidates_file", help="JSON file: [{company_name, wiki_page, context_snippet}, ...]")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--refresh-sec-cache", action="store_true", help="Force-refetch the SEC ticker list")
    args = parser.parse_args()

    if args.refresh_sec_cache:
        fetch_sec_tickers(force_refresh=True)

    candidates = json.loads(Path(args.candidates_file).read_text())
    result = resolve_candidates(candidates, dry_run=args.dry_run)

    print(f"Resolved (auto-added): {len(result['resolved'])}")
    for r in result["resolved"]:
        print(f"  {r['ticker']} — {r['sec_title']} ({r['match_type']} match on \"{r['company_name']}\")")
    print(f"Already tracked (skipped): {len(result['already_tracked'])}")
    for r in result["already_tracked"]:
        print(f"  {r['ticker']} — {r['company_name']}")
    print(f"Pending review (no confident match): {len(result['pending'])}")
    for r in result["pending"]:
        print(f"  {r['company_name']} — {r['reason']}")

    print(json.dumps(result, indent=2))
