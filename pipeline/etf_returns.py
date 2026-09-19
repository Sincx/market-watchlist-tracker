"""Quarterly ETF returns + P/E, computed in Python instead of WebFetching
ETFreplay/stockanalysis.com — Phase 6 redesign of growth-value-tracker and
sector-performance-tracker (spec §12): "Python computes quarterly returns
from tracked price history; LLM writes narrative only."

These 32 ETFs (7 style + 25 sector) aren't part of the main equity universe
and Turso has no historical depth for them yet (technicals.py only started
2026-09-10), so this module does its own longer-period yfinance fetch
(period="2y", enough for current-YTD + one full prior year) rather than
reading from the `prices` table — there's nothing there to read yet for
these tickers. A future pass could add them to universe.py's curated groups
so they accumulate in Turso like everything else and this module reads from
there instead; not done now to keep this change scoped to the one thing
asked (stop the ETFreplay/stockanalysis.com WebFetch loop).

Run standalone for a quick check: python etf_returns.py [style|sector]
"""
from __future__ import annotations

import sys
from datetime import date

import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup

STYLE_ETFS = ["QQQ", "IWF", "SPY", "IWD", "IVE", "IWM", "IWN"]
SECTOR_ETFS = [
    "GDX", "XME", "GLD", "SMH", "ITA", "ICLN", "XBI", "XLK", "XLC", "XLI",
    "XLU", "XLF", "XLV", "CIBR", "JETS", "IYT", "KRE", "XLB", "KIE", "XRT",
    "XLE", "XLY", "AMLP", "XLRE", "XLP",
]

STYLE_LABELS = {
    "QQQ": ("Mega-cap growth", "NASDAQ 100"), "IWF": ("Large-cap growth", "Russell 1000 Growth"),
    "SPY": ("Broad market", "S&P 500"), "IWD": ("Large-cap value", "Russell 1000 Value"),
    "IVE": ("S&P 500 value", "S&P 500 Value"), "IWM": ("Small cap blend", "Russell 2000"),
    "IWN": ("Small cap value", "Russell 2000 Value"),
}
SECTOR_LABELS = {
    "GDX": "Gold Miners", "XME": "Metals & Mining", "GLD": "Gold", "SMH": "Semiconductors",
    "ITA": "Aerospace & Defense", "ICLN": "Clean Energy", "XBI": "Biotech (equal-weight)",
    "XLK": "Technology", "XLC": "Communication Services", "XLI": "Industrials",
    "XLU": "Utilities", "XLF": "Financials", "XLV": "Health Care", "CIBR": "Cybersecurity",
    "JETS": "Airlines", "IYT": "Transportation", "KRE": "Regional Banks", "XLB": "Materials",
    "KIE": "Insurance", "XRT": "Retail", "XLE": "Energy", "XLY": "Consumer Discretionary",
    "AMLP": "MLP / Midstream Energy", "XLRE": "Real Estate", "XLP": "Consumer Staples",
}

# SECTOR_LABELS above is a display label describing each ETF's theme (used
# in the sector-performance-tracker wiki table) — it does NOT match the GICS
# `sector` strings stored in Turso's `fundamentals` table (e.g. "Gold
# Miners" vs the real GICS sector "Materials"). candidates.py's Strategy E
# needs the latter to actually find stocks in the leading sector, hence a
# separate mapping. GLD has no entry — it holds physical bullion, not
# equities, so there's no "stock in that sector" for it; Strategy E's
# candidate search must exclude it from "leading sector" consideration
# entirely (GDX, the miners ETF, is the equity proxy for gold strength).
SECTOR_TO_GICS = {
    "GDX": "Materials", "XME": "Materials", "SMH": "Information Technology",
    "ITA": "Industrials", "ICLN": "Utilities", "XBI": "Health Care",
    "XLK": "Information Technology", "XLC": "Communication Services", "XLI": "Industrials",
    "XLU": "Utilities", "XLF": "Financials", "XLV": "Health Care", "CIBR": "Information Technology",
    "JETS": "Industrials", "IYT": "Industrials", "KRE": "Financials", "XLB": "Materials",
    "KIE": "Financials", "XRT": "Consumer Discretionary", "XLE": "Energy", "XLY": "Consumer Discretionary",
    "AMLP": "Energy", "XLRE": "Real Estate", "XLP": "Consumer Staples",
}

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}


def _quarter_end_closes(df: pd.DataFrame) -> dict[tuple[int, int], float]:
    """Last close on or before each calendar quarter's end, keyed by (year, quarter)."""
    df = df.dropna(subset=["Close"])
    q = df["Close"].resample("QE").last()
    return {(ts.year, (ts.month - 1) // 3 + 1): float(v) for ts, v in q.items() if pd.notna(v)}


def fetch_quarterly_returns(tickers: list[str], period: str = "2y") -> dict[str, dict]:
    """Returns {ticker: {(year, quarter): pct_return_that_quarter, ...,
    "latest_date": "YYYY-MM-DD", "latest_close": float}}. A quarter's return
    is (quarter-end close / prior-quarter-end close - 1) * 100; the most
    recent (possibly partial) quarter uses the latest available close as its
    "end". Missing prior-quarter data (start of the fetch window) yields no
    entry for that quarter rather than a wrong number from a missing base.
    """
    data = yf.download(tickers=tickers, period=period, group_by="ticker", auto_adjust=True, threads=True, progress=False)
    results = {}
    for t in tickers:
        try:
            df = data[t] if len(tickers) > 1 else data
        except (KeyError, TypeError):
            results[t] = {}
            continue
        df = df.dropna(subset=["Close"])
        if df.empty:
            results[t] = {}
            continue
        q_closes = _quarter_end_closes(df)
        quarters_sorted = sorted(q_closes.keys())
        returns = {}
        for i, key in enumerate(quarters_sorted):
            if i == 0:
                continue  # no prior-quarter base within the fetch window
            prev_close = q_closes[quarters_sorted[i - 1]]
            returns[key] = round((q_closes[key] / prev_close - 1) * 100, 2)
        latest_date = df.index[-1]
        results[t] = {
            "quarterly_returns": returns,
            "latest_date": str(latest_date.date()),
            "latest_close": round(float(df["Close"].iloc[-1]), 4),
        }
    return results


def fetch_etf_pe(ticker: str) -> float | None:
    """P/E for an ETF from stockanalysis.com's /etf/ page — reuses the same
    SvelteKit-hydration-blob approach as fundamentals.py's equity scraper,
    just against the ETF URL pattern. Returns None on any failure (n/a for
    GLD/XBI-style ETFs with no P/E is expected, not an error).
    """
    import re
    try:
        r = requests.get(f"https://stockanalysis.com/etf/{ticker.lower()}/", headers=_HEADERS, timeout=15, verify=False)
        r.raise_for_status()
    except Exception:
        return None
    soup = BeautifulSoup(r.text, "lxml")
    for script in soup.find_all("script"):
        text = script.string or ""
        if "peRatio" not in text:
            continue
        m = re.search(r'"?peRatio"?\s*:\s*"([^"]*)"', text)
        if m and m.group(1) not in ("", "n/a", "-"):
            try:
                return float(m.group(1))
            except ValueError:
                return None
    return None


def _report(group: str, with_pe: bool = False) -> dict:
    tickers = STYLE_ETFS if group == "style" else SECTOR_ETFS + ["SPY"]
    returns = fetch_quarterly_returns(tickers)
    out = {}
    for t in tickers:
        r = returns.get(t, {})
        entry = {
            "quarterly_returns": {f"{y}-Q{q}": v for (y, q), v in r.get("quarterly_returns", {}).items()},
            "latest_date": r.get("latest_date"), "latest_close": r.get("latest_close"),
        }
        if group == "style":
            entry["label"], entry["benchmark"] = STYLE_LABELS.get(t, (t, t))
        else:
            entry["label"] = SECTOR_LABELS.get(t, t)
        if with_pe:
            entry["pe"] = fetch_etf_pe(t)
        out[t] = entry
    return out


if __name__ == "__main__":
    import json

    import data_quality as dq
    import db

    group = sys.argv[1] if len(sys.argv) > 1 else "style"
    with_pe = "--pe" in sys.argv
    report = _report(group, with_pe=with_pe)
    print(json.dumps(report, indent=2))

    # Recorded only for the CLI/scheduled-task entry point, not the
    # importable fetch_quarterly_returns()/_report() functions themselves —
    # candidates.py imports those directly for its own per-candidate use,
    # and a Turso write on every such call would be a surprise side effect
    # for a caller that never asked for one.
    client = db.get_client()
    try:
        dq_rows = [{"ticker": t, "exchange": "US", "data_type": "etf_returns",
                    "status": "ok" if e.get("latest_close") is not None else "error",
                    "error": None if e.get("latest_close") is not None else "no quarterly returns computed"}
                   for t, e in report.items()]
        dq.record_batch(client, dq_rows)
    finally:
        client.close()
