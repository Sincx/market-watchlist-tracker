"""Replaces the Google Sheets "Buy Opportunities" tab as the source for
portfolio-management-briefing's Step 6 (new-position candidates), per
Mike's 2026-09-14 "Turso should be the master system" request ("same again
for recommendations").

Known, deliberate scope difference from the old Score/5 (documented, not
silently faked): the old score's 2-point "Undervalued" tier came from a
Morningstar valuation call. That data was never wired into Turso —
clippings-sorter only ever wrote Morningstar picks into
wiki/finance/morningstar/morningstar-watchlist.md and config.py's GROUPS
dict, never a `signals` row (confirmed: every one of the 123 rows in
`signals` today is source='magic-formula-pass', zero morningstar-*). So
this version's value-tier uses Greenblatt's Magic Formula pass/fail
instead (`screen_results.passes_thresholds`, a real quantitative test
already computed daily) — arguably a stronger criterion than Morningstar's
star rating, and it's what Turso actually has. If Morningstar signals get
wired into Turso later (a separate piece of work), this scoring can fold
that back in as an additional point.

Score /5 (adapted):
  +2  passes_thresholds = 1   (real Magic Formula pass — EY/ROIC clear
                                Greenblatt's thresholds)
  +1  has an mf_rank at all but doesn't pass thresholds (ranked, not a
      pass — analogous to the old "Fair value" tier)
  +1  technical_rating starts with "Buy"  (mandatory gate, same as before —
      a ticker only becomes a candidate if this is already true)
  +1  RSI14 in [40, 70]
  +1  MACD bullish

Run standalone: python trading_portfolio_candidates.py [--limit N]
"""
from __future__ import annotations

import argparse
import json

import db


def get_candidates(limit: int = 10) -> list[dict]:
    client = db.get_client()
    try:
        # m.sector comes from v_magic_formula_latest, which already reads
        # fundamentals.sector (the field screen.py's own exclusion filter
        # uses) rather than universe.sector (Wikipedia-sourced, blank for
        # many US tickers) — a real bug already fixed there once before
        # (2026-09-10), don't reintroduce it by pulling u.sector here instead.
        rows = db.query(client, """
            SELECT u.ticker, u.exchange, u.index_membership, m.sector,
                   p.close, p.rsi14, p.macd_signal, p.technical_rating, p.date AS price_date,
                   m.mf_rank, m.earnings_yield, m.roic, m.pe, m.div_yield, m.passes_thresholds
            FROM universe u
            JOIN (
                SELECT ticker, exchange, close, rsi14, macd_signal, technical_rating, date,
                       ROW_NUMBER() OVER (PARTITION BY ticker, exchange ORDER BY date DESC) rn
                FROM prices
            ) p ON p.ticker = u.ticker AND p.exchange = u.exchange AND p.rn = 1
            JOIN v_magic_formula_latest m ON m.ticker = u.ticker AND m.exchange = u.exchange
            WHERE u.active = 1 AND p.technical_rating LIKE 'Buy%';
        """)
    finally:
        client.close()

    scored = []
    for r in rows:
        score = 0
        if r["passes_thresholds"]:
            score += 2
        elif r["mf_rank"] is not None:
            score += 1
        score += 1  # technical Buy — guaranteed by the WHERE filter, same as the old sheet logic
        rsi = r["rsi14"]
        if rsi is not None and 40 <= rsi <= 70:
            score += 1
        if r["macd_signal"] == "Bullish":
            score += 1

        parts = []
        if r["mf_rank"] is not None:
            parts.append(f"MF#{int(r['mf_rank'])}")
        parts.append("Magic Formula pass" if r["passes_thresholds"] else "Magic Formula ranked (no pass)")
        if r["macd_signal"]:
            parts.append(f"{r['macd_signal'].lower()} MACD")
        if rsi is not None:
            band = "healthy" if 40 <= rsi <= 70 else "extended" if rsi > 70 else "low"
            parts.append(f"RSI {band} at {rsi:.0f}")
        conviction_note = "; ".join(parts) + "."

        scored.append({
            "ticker": r["ticker"], "exchange": r["exchange"], "sector": r["sector"],
            "index_membership": r["index_membership"], "price": r["close"], "price_date": r["price_date"],
            "rsi14": rsi, "macd": r["macd_signal"], "technical_rating": r["technical_rating"],
            "mf_rank": r["mf_rank"], "earnings_yield": r["earnings_yield"], "roic": r["roic"],
            "pe": r["pe"], "div_yield": r["div_yield"], "passes_thresholds": bool(r["passes_thresholds"]),
            "score": score, "conviction_note": conviction_note,
        })

    scored.sort(key=lambda x: (-x["score"], x["mf_rank"] if x["mf_rank"] is not None else 999999, x["ticker"]))
    return scored[:limit]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    print(json.dumps(get_candidates(args.limit), indent=2))
