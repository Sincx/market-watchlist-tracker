"""Fiscal-year average share prices and split flags, from Yahoo monthly history.

Why this exists: the SBC true-cost maths (owners_earnings.py) values the shares
a company issued to employees at the price they were issued at. SEC filings do
not carry that, and the local `prices` table only holds ~1 year. So for each
annual period already in financial_statement_facts this writes two pseudo-facts
next to the SEC ones (source='yfinance'):

  AvgMarketPrice       mean month-end close over the fiscal year, expressed in
                       the share basis AS REPORTED AT THE TIME (Yahoo's closes
                       are adjusted to today's split basis; multiplying by the
                       splits that came later undoes that, so the price lines
                       up with the share counts in the 10-K of that year)
  SplitRatioInPeriod   product of any split ratios inside the fiscal year
                       (1.0 = none) — SEC share counts straddling a split are
                       on two different bases, so owners_earnings skips those
                       years, as Burry's protocol does for rows that fail a
                       consistency check

Months that contain a split are left out of the average (the month's close
could be on either side). A year with fewer than 9 usable months gets no price.

US tickers only for now, matching filings.py.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone

import db

MIN_MONTHS = 9


# ── pure ─────────────────────────────────────────────────────────────────────

def asreported_avg(closes: list[tuple[str, float]], splits: list[tuple[str, float]],
                   start: str, end: str) -> float | None:
    """Mean of month-end closes in (start, end], restored to the share basis in
    force during the period. closes/splits are (YYYY-MM-DD, value). Pure."""
    vals = []
    for d, c in closes:
        if not (start < d <= end) or c is None or c <= 0:
            continue
        month = d[:7]
        if any(sd[:7] == month for sd, _ in splits):
            continue  # a split fell inside this month
        later = 1.0
        for sd, ratio in splits:
            if sd[:7] > month:
                later *= ratio
        vals.append(c * later)
    return sum(vals) / len(vals) if len(vals) >= MIN_MONTHS else None


def split_ratio_in(splits: list[tuple[str, float]], start: str, end: str) -> float:
    r = 1.0
    for sd, ratio in splits:
        if start < sd <= end:
            r *= ratio
    return r


# ── I/O ──────────────────────────────────────────────────────────────────────

def fetch_history(yahoo_ticker: str) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    import fetchers  # noqa: F401  (side effect: loads .env, CA-bundle settings)
    import yfinance as yf
    tk = yf.Ticker(yahoo_ticker)
    h = tk.history(period="max", interval="1mo", auto_adjust=False)
    if h is None or h.empty:
        return [], []
    closes = [(str(i.date()), float(r["Close"])) for i, r in h.iterrows() if r["Close"] == r["Close"]]
    sp = tk.splits
    splits = [(str(i.date()), float(v)) for i, v in sp.items()] if sp is not None and len(sp) else []
    return closes, splits


def enrich(client, ticker: str, exchange: str = "US", yahoo_ticker: str | None = None,
           history=None) -> int:
    """Write AvgMarketPrice / SplitRatioInPeriod facts for every annual period of
    `ticker` already on file. Returns the number of fact rows written."""
    periods = db.query(client, """
        SELECT DISTINCT period_start, period_end FROM financial_statement_facts
        WHERE ticker = :t AND exchange = :e AND concept = 'NetIncomeLoss' AND period_start IS NOT NULL;
    """, {"t": ticker, "e": exchange})
    if not periods:
        return 0
    closes, splits = history if history is not None else fetch_history(yahoo_ticker or ticker.replace(".", "-"))
    if not closes:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for p in periods:
        s, e = p["period_start"], p["period_end"]
        base = {"ticker": ticker, "exchange": exchange, "period_start": s, "period_end": e,
                "fiscal_year": int(e[:4]), "fiscal_period": "FY", "source": "yfinance", "fetched_at": now}
        avg = asreported_avg(closes, splits, s, e)
        if avg is not None:
            rows.append({**base, "concept": "AvgMarketPrice", "value": avg, "unit": "USD"})
        rows.append({**base, "concept": "SplitRatioInPeriod", "value": split_ratio_in(splits, s, e), "unit": "ratio"})
    db.upsert(client, "financial_statement_facts", rows)
    return len(rows)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", nargs="+", required=True)
    a = ap.parse_args()
    c = db.get_client()
    for t in a.ticker:
        print(t, enrich(c, t.upper()))
    sys.stdout.flush()
    os._exit(0)
