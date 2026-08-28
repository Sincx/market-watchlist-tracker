"""
portfolio_update.py — Fetch live prices and TA signals for the trading portfolio.

Reads trading-portfolio.md, fetches OHLCV from Polygon/AV/yfinance, computes
RSI14, MACD, SMA50, ATR14 and assigns a signal per position. Outputs JSON.

Usage:
    python portfolio_update.py [--portfolio PATH] [--output PATH]

Output (stdout or --output file):
    {
      "date": "YYYY-MM-DD",
      "fx": {"EUR_USD": X, "USD_EUR": X, "EUR_GBP": X, "GBp_EUR": X},
      "cash_eur": X,
      "total_invested_eur": X,
      "total_eur": X,
      "positions": {
        "TICKER": {
          "price": X, "currency": "USD|GBX|EUR",
          "shares": X, "entry": X, "cost_basis": X,
          "native_value": X, "eur_value": X, "pl_pct": X,
          "rsi14": X, "macd": "Bullish|Bearish|N/A",
          "sma50": X, "atr14": X, "weight_pct": X,
          "signal": "Exit|Trim|Add|Hold|Watch"
        }, ...
      }
    }

Why Python instead of Claude MCP calls:
  10 positions × (1 prev-price + 1 bars call) = ~20 Massive API calls with 13s waits
  on the free tier. This script runs in ~30-60s with no rate-limit pauses for US tickers
  and uses AV/yfinance for LSE/EU tickers which have no rate limit concerns.
"""

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

# Resolve pipeline directory regardless of CWD
_PIPELINE = Path(__file__).parent
if str(_PIPELINE) not in sys.path:
    sys.path.insert(0, str(_PIPELINE))

from dotenv import load_dotenv
import os

load_dotenv(_PIPELINE / ".env")

# Mirror CA bundle for both curl_cffi (yfinance) and requests (Polygon/AV)
_curl_ca = os.getenv("CURL_CA_BUNDLE")
if _curl_ca:
    os.environ["CURL_CA_BUNDLE"] = _curl_ca
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _curl_ca)

from fetchers import fetch_fx_rates, fetch_us_ticker, fetch_ftse_ticker, fetch_eu_morningstar_ticker, StaleDataError
from indicators import compute_all

# ── Default portfolio path ─────────────────────────────────────────────────────

DEFAULT_PORTFOLIO = Path(r"C:\Users\Mike\Documents\Fred\Fred\wiki\finance\trading-portfolio.md")

# ── Per-ticker fetch routing ───────────────────────────────────────────────────
# For LSE tickers we always want GBp from AV/.LON → yfinance/.L (never the ADR USD path).

TICKER_FETCH = {
    "APH":  {"method": "us"},
    "AVGO": {"method": "us"},
    "IQV":  {"method": "us"},
    "CPB":  {"method": "us"},
    "SAP":  {"method": "yf",   "yahoo": "SAP.DE",  "currency": "EUR"},
    "EDEN": {"method": "yf",   "yahoo": "EDEN.PA", "currency": "EUR"},
    "ACN":  {"method": "yf",   "yahoo": "CSA.DE",  "currency": "EUR"},  # Xetra listing; ISIN IE00B4BNMY34, WKN A0YAQA
    "PRX":  {"method": "yf",   "yahoo": "PRX.AS",  "currency": "EUR"},
    "WKL":  {"method": "yf",   "yahoo": "WKL.AS",  "currency": "EUR"},
    "WOSG": {"method": "ftse"},
    "MGNS": {"method": "ftse"},
    "DNLM": {"method": "ftse"},
    "KLR":  {"method": "ftse"},
    "GSK":  {"method": "ftse"},
}

# ── Markdown parser ────────────────────────────────────────────────────────────

def parse_portfolio(path: Path) -> tuple:
    """
    Returns (positions, cash_eur).
    positions: dict  ticker → {company, exchange, currency, shares, entry, cost_basis}
    """
    text = path.read_text(encoding="utf-8")
    positions = {}
    in_table = False

    for line in text.splitlines():
        s = line.strip()
        if s.startswith("| Company"):
            in_table = True
            continue
        if in_table and s.startswith("| ---"):
            continue
        if in_table and s.startswith("|"):
            cols = [c.strip() for c in s.split("|")[1:-1]]
            if len(cols) < 10:
                continue
            company, ticker, exchange, currency, shares_s, entry_s, cost_s = cols[:7]
            ticker = ticker.strip("*").strip()
            if ticker in ("—", "Cash", ""):
                continue
            try:
                shares = float(shares_s.replace(",", ""))
                entry = float(re.sub(r"[^\d.]", "", entry_s))
                cost = float(re.sub(r"[^\d.]", "", cost_s))
            except ValueError:
                continue
            positions[ticker] = {
                "company": company,
                "exchange": exchange,
                "currency": currency,
                "shares": shares,
                "entry": entry,
                "cost_basis": cost,
            }
        elif in_table and not s.startswith("|"):
            break

    # Extract cash from bold EUR amount: **€813**
    cash_eur = 0.0
    m = re.search(r"\*\*€([\d,]+)\*\*", text)
    if m:
        cash_eur = float(m.group(1).replace(",", ""))

    return positions, cash_eur


# ── Signal logic ──────────────────────────────────────────────────────────────

def _signal(rsi, macd_dir, price, sma50, weight_pct):
    if rsi is None or sma50 is None:
        return "Watch"
    below = price < sma50
    bearish = macd_dir == "Bearish"
    bullish = macd_dir == "Bullish"

    if rsi < 40 and below and bearish:
        return "Exit"
    if rsi > 70 and weight_pct is not None and weight_pct >= 20:
        return "Trim"
    if 35 <= rsi <= 50 and not below and bullish:
        return "Add"

    neg = sum([rsi < 40, below, bearish])
    if neg >= 1:
        return "Watch"
    return "Hold"


# ── Main ──────────────────────────────────────────────────────────────────────

def run(portfolio_path=None, output_path=None):
    path = Path(portfolio_path) if portfolio_path else DEFAULT_PORTFOLIO
    _log(f"Reading {path}")
    positions, cash_eur = parse_portfolio(path)
    _log(f"{len(positions)} positions, €{cash_eur:.0f} cash")

    # FX rates
    _log("Fetching FX rates...")
    fx_raw = fetch_fx_rates()   # {USD:1.0, GBP:GBPUSD, GBX:GBXUSDrate, EUR:EURUSDrate, ...}

    eur_usd = fx_raw.get("EUR", 1.085)   # how many USD per EUR
    gbp_usd = fx_raw.get("GBP", 1.279)  # how many USD per GBP
    eur_gbp = eur_usd / gbp_usd          # EUR/GBP cross
    gbp_eur = 1.0 / eur_gbp
    gbp_eur_pence = gbp_eur / 100        # 1 GBp → EUR

    fx = {
        "EUR_USD": round(eur_usd, 6),
        "USD_EUR": round(1.0 / eur_usd, 6),
        "EUR_GBP": round(eur_gbp, 6),
        "GBp_EUR": round(gbp_eur_pence, 6),
    }
    _log(f"FX: EUR/USD={fx['EUR_USD']}, EUR/GBP={fx['EUR_GBP']:.4f}, 1 GBp=€{fx['GBp_EUR']:.6f}")

    # Fetch OHLCV and compute indicators
    results = {}
    for ticker, pos in positions.items():
        cfg = TICKER_FETCH.get(ticker)
        if cfg is None:
            # Unknown ticker — try US path as default
            _log(f"  {ticker}: no routing config, trying US path")
            cfg = {"method": "us"}

        method = cfg["method"]
        _log(f"  {ticker} ({method})...")

        closes = opens = highs = lows = volumes = []
        fetch_ccy = pos.get("currency", "USD")

        if method == "us":
            try:
                closes, opens, highs, lows, volumes = fetch_us_ticker(ticker)
            except StaleDataError as e:
                _log(f"STALE DATA: {e} — exiting with code 2 so caller can fall back to MCP")
                sys.exit(2)
            fetch_ccy = "USD"

        elif method == "ftse":
            # Pass empty sets so we always take the AV.LON → yfinance.L path (GBp prices)
            closes, opens, highs, lows, volumes, fetch_ccy = fetch_ftse_ticker(
                ticker, dual_listed=set(), collision_tickers=set()
            )

        elif method == "yf":
            from fetchers import fetch_yfinance, _bars_to_lists
            yahoo = cfg.get("yahoo", ticker)
            bars = fetch_yfinance(yahoo)
            if bars:
                closes, opens, highs, lows, volumes = _bars_to_lists(bars)
            fetch_ccy = cfg.get("currency", pos.get("currency", "USD"))

        if not closes:
            _log(f"    {ticker}: no data")
            results[ticker] = {"error": "no data", "company": pos["company"]}
            continue

        ind = compute_all(closes, opens, highs, lows, volumes, currency=fetch_ccy, usd_rate=1.0)
        if not ind:
            _log(f"    {ticker}: compute failed")
            results[ticker] = {"error": "compute failed", "company": pos["company"]}
            continue

        price = closes[0]
        pl_pct = round((price - pos["entry"]) / pos["entry"] * 100, 2)

        # EUR value for portfolio weight
        if fetch_ccy in ("USD",):
            eur_value = price * pos["shares"] * fx["USD_EUR"]
        elif fetch_ccy in ("GBX", "GBp"):
            eur_value = price * pos["shares"] * fx["GBp_EUR"]
        elif fetch_ccy == "EUR":
            eur_value = price * pos["shares"]
        else:
            eur_value = price * pos["shares"]  # fallback

        _log(f"    {ticker}: {price} {fetch_ccy}  RSI={ind['rsi14']}  MACD={ind['macd']}  SMA50={ind['ma50']}  ATR14={ind['atr14']}")

        results[ticker] = {
            "company": pos["company"],
            "exchange": pos["exchange"],
            "currency": fetch_ccy,
            "shares": pos["shares"],
            "entry": pos["entry"],
            "cost_basis": pos["cost_basis"],
            "price": ind["price"],
            "native_value": round(price * pos["shares"], 2),
            "eur_value": round(eur_value, 2),
            "pl_pct": pl_pct,
            "rsi14": ind["rsi14"],
            "macd": ind["macd"],
            "sma50": ind["ma50"] if isinstance(ind["ma50"], (int, float)) else None,
            "sma20": ind["ma20"],
            "atr14": ind["atr14"],
            "rating": ind["rating"],
        }

    # Portfolio weights (second pass — need total first)
    total_invested_eur = sum(r["eur_value"] for r in results.values() if "eur_value" in r)
    total_eur = total_invested_eur + cash_eur

    for ticker, r in results.items():
        if "eur_value" not in r:
            continue
        w = round(r["eur_value"] / total_invested_eur * 100, 1) if total_invested_eur else 0
        r["weight_pct"] = w
        r["signal"] = _signal(r["rsi14"], r["macd"], r["price"], r["sma50"], w)

    output = {
        "date": str(date.today()),
        "fx": fx,
        "cash_eur": cash_eur,
        "total_invested_eur": round(total_invested_eur, 0),
        "total_eur": round(total_eur, 0),
        "positions": results,
    }

    out_str = json.dumps(output, indent=2)
    if output_path:
        Path(output_path).write_text(out_str, encoding="utf-8")
        _log(f"Written to {output_path}")
    else:
        print(out_str)

    return output


def _log(msg):
    print(f"[portfolio_update] {msg}", file=sys.stderr)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Fetch live prices and TA for trading portfolio")
    p.add_argument("--portfolio", help="Path to trading-portfolio.md (default: wiki path)")
    p.add_argument("--output", help="Write JSON to this file instead of stdout")
    args = p.parse_args()
    run(args.portfolio, args.output)
