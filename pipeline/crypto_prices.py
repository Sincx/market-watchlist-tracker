"""Phase 2 (P2.1.3) — long-tail crypto price fetcher for tokens not covered
by yfinance (BTC/ETH/LINK go through technicals.py's existing batched
yfinance path with zero code changes there; this module is only for the
next tier down).

CoinGecko's free public `/simple/price` endpoint (no key required, ~10-30
calls/min) gives a current spot price only — no historical OHLC for these
low-cap tokens without a paid tier. So unlike equities, no technical
indicators (MA/RSI/MACD/rating) are computed here: a `prices` row is written
with open=high=low=close (the spot price) and volume left NULL, matching
the spec's own "tolerate and surface gaps rather than erroring" principle
(Open Risks) rather than fabricating indicators from a single data point.

$TEST has no working price API at all (confirmed: the wiki page already
documented CoinMarketCap failing to render its price before this migration)
— excluded here, stays manual-entry same as options (schema §9).

Run standalone: python crypto_prices.py [--dry-run]
"""
from __future__ import annotations

import argparse
import os
from datetime import date, datetime, timezone
from pathlib import Path

import certifi
import requests

import db

TODAY = date.today().isoformat()


def _build_ssl_verify() -> str:
    """A CA bundle trusting both certifi's normal roots AND Norton's
    injected root. Norton intercepts TLS for SOME domains on this machine
    (replacing the real cert chain with one signed by its own root) but not
    others (the real chain applies unmodified) — found 2026-09-12 building
    resolve_ticker.py: CoinGecko needs Norton's cert alone (it IS
    intercepted), but SEC.gov failed with Norton's cert alone (it ISN'T
    intercepted — verify=<path> replaces the trusted set entirely rather
    than adding to it). A merged bundle covers both without guessing which
    case applies per domain.
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

# ticker -> CoinGecko coin id. Verified 2026-09-11 by cross-checking each
# id's /coins/{id} contract_address against wiki/crypto's documented
# addresses (both matched exactly) — CoinGecko's search-by-symbol is
# unreliable for obscure tokens (many unrelated coins share the same
# ticker), so this mapping is deliberately hardcoded, not looked up live.
COINGECKO_IDS = {
    "EV": "everything",
    "LUCKY": "lucky",
}


def fetch_prices(ids: list[str]) -> dict[str, float]:
    resp = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": ",".join(ids), "vs_currencies": "usd"},
        timeout=20, verify=_SSL_VERIFY,
    )
    resp.raise_for_status()
    data = resp.json()
    return {coingecko_id: v["usd"] for coingecko_id, v in data.items() if "usd" in v}


def run(dry_run: bool = False) -> None:
    prices_by_id = fetch_prices(list(COINGECKO_IDS.values()))
    fetched_at = datetime.now(timezone.utc).isoformat()

    rows = []
    for ticker, coingecko_id in COINGECKO_IDS.items():
        price = prices_by_id.get(coingecko_id)
        if price is None:
            print(f"  NO DATA for {ticker} ({coingecko_id}) — CoinGecko returned nothing, skipping")
            continue
        rows.append({
            "ticker": ticker, "exchange": "CRYPTO", "date": TODAY,
            "open": price, "high": price, "low": price, "close": price, "volume": None,
            "currency": "USD", "usd_rate": 1.0,
            "ma20": None, "ma50": None, "ma200": None, "rsi14": None, "macd_signal": None,
            "vol_ratio": None, "technical_rating": None, "fetched_at": fetched_at,
        })
        print(f"{ticker}: ${price}")

    if dry_run:
        print(f"  ... (dry run, {len(rows)} rows not written)")
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
    args = parser.parse_args()
    run(dry_run=args.dry_run)
