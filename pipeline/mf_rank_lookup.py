"""Master spec Phase 14a — look up v_magic_formula_latest.mf_rank for a
specific list of tickers, for portfolio-management-briefing's Position
Signals table (existing HELD positions, not new candidates — Step 6's
candidate generation already reads v_investment_opportunities/
v_magic_formula_latest directly via trading_portfolio_candidates.py).

Confirmed two OTHER, separate Magic Formula populations exist before this
fix: sheets.py's compute_and_write_mf_ranks() (curated ~183-ticker Sheet
path) and mf-update's own mf_update.py (a THIRD, independent computation —
scrapes ROIC/EY directly from stockanalysis.com for just the ~15-30
tickers actually held across Equity + Trading portfolios, writing "MF
Rank" into wiki table). The briefing's existing "MF# lookup for position
signals" step reads THAT wiki output. This script replaces it with the
full ~1,350-ticker Turso ranking (v_magic_formula_latest), same source
Step 6's new-candidate ranking already uses — so a ticker's MF# means
the same thing whether it's an existing holding or a new idea.

Rank numbers will not match mf-update's old wiki output for the same
ticker (different universe size/composition) — expected on cutover, not
a bug.

Run standalone: python mf_rank_lookup.py TICKER1,EXCHANGE1 TICKER2,EXCHANGE2 ...
  e.g. python mf_rank_lookup.py APH,US IQV,US KLR,UK
"""
from __future__ import annotations

import json
import sys

import db


def lookup(pairs: list[tuple[str, str]]) -> dict[str, int | None]:
    client = db.get_client()
    try:
        out = {}
        for ticker, exchange in pairs:
            rows = db.query(client, "SELECT mf_rank FROM v_magic_formula_latest WHERE ticker = :t AND exchange = :e;",
                             {"t": ticker, "e": exchange})
            out[f"{ticker}.{exchange}"] = rows[0]["mf_rank"] if rows else None
        return out
    finally:
        client.close()


if __name__ == "__main__":
    pairs = []
    for arg in sys.argv[1:]:
        if "," not in arg:
            print(f"Skipping malformed arg (expected TICKER,EXCHANGE): {arg}", file=sys.stderr)
            continue
        t, e = arg.split(",", 1)
        pairs.append((t.strip(), e.strip()))
    print(json.dumps(lookup(pairs), indent=2))
