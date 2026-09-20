"""Detects any (ticker, exchange) that a portfolio or tracked investor has
taken a position in but that isn't covered by an active `universe` row —
i.e. a ticker that will NEVER get a price from technicals.py, silently
showing "—" and being excluded from net_pnl/open_value on the dashboard.

This is exactly the class of bug found live 2026-09-20 twice in one day:
MSTR/NGLOY/GLNCY/CHIP on the Equity Portfolio, then 9 more positions on
Paper Trading (P1) — both because a ticker got added to `trades` (by a
scheduled task like "Paper Trader", or by hand) with no corresponding step
that registers it into this pipeline's tracked universe. Buffett/Ackman's
freshly-backfilled `investor_positions` are just as exposed to the same
gap, so this checks both tables, not just `trades`.

Deliberately REPORT-ONLY, not auto-fixing — the CHIP case that same day
showed why: the "obvious" guess (bare ticker, or its most obvious exchange
suffix) can resolve to a completely unrelated instrument (CHIP.L is a
KraneShares China ETF, not the semiconductor fund Mike actually holds).
Guessing the right yahoo_ticker/currency for a non-US listing needs a human
(or an LLM session) to actually verify against yfinance's returned company
name before it's safe to add to universe.py's PORTFOLIO_SPECIFIC_TICKERS —
so this script's job is only to surface the gap clearly enough that that
verification step doesn't get skipped.

For US-exchange tickers only, it also does a cheap live yfinance sanity
check (does the bare ticker resolve at all) since that's usually a safe,
unambiguous mapping — included as a hint to speed up manual triage, not
as an auto-fix.

Run standalone: python check_untracked_instruments.py
"""
from __future__ import annotations

import db

try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:
    _YF_AVAILABLE = False


def _find_untracked() -> list[dict]:
    client = db.get_client()
    try:
        active_universe = db.query(client, "SELECT ticker, exchange FROM universe WHERE active = 1;")
        tracked = {(r["ticker"], r["exchange"]) for r in active_universe}

        trade_holdings = db.query(client, "SELECT DISTINCT ticker, exchange, portfolio_id FROM trades;")
        investor_holdings = db.query(client, "SELECT DISTINCT ticker, exchange, investor_id FROM investor_positions;")
    finally:
        client.close()

    rows = [
        {"ticker": r["ticker"], "exchange": r["exchange"], "origin": f"trades:{r['portfolio_id']}"}
        for r in trade_holdings if (r["ticker"], r["exchange"]) not in tracked
    ] + [
        {"ticker": r["ticker"], "exchange": r["exchange"], "origin": f"investor_positions:{r['investor_id']}"}
        for r in investor_holdings if (r["ticker"], r["exchange"]) not in tracked
    ]

    # Multiple portfolios/investors can hold the same untracked ticker —
    # collapse to one row per (ticker, exchange) with all origins listed,
    # so the same gap isn't reported N times.
    merged: dict[tuple[str, str], list[str]] = {}
    for r in rows:
        key = (r["ticker"], r["exchange"])
        merged.setdefault(key, []).append(r["origin"])
    return [{"ticker": t, "exchange": e, "origins": origins} for (t, e), origins in merged.items()]


def _yfinance_hint(ticker: str, exchange: str) -> str:
    if exchange != "US" or not _YF_AVAILABLE:
        return "not checked (non-US tickers need a verified exchange suffix — see module docstring)"
    try:
        info = yf.Ticker(ticker).info
        name = info.get("longName") or info.get("shortName")
        if name:
            return f"bare ticker resolves OK on Yahoo as '{name}' — likely safe to add as-is"
        return "bare ticker returned no company name — verify before adding"
    except Exception as e:
        return f"bare ticker lookup failed ({e}) — do not assume it's safe to add"


def check() -> list[str]:
    gaps = _find_untracked()
    lines = []
    for g in gaps:
        hint = _yfinance_hint(g["ticker"], g["exchange"])
        lines.append(f"{g['ticker']} ({g['exchange']}) — held by {', '.join(g['origins'])} — {hint}")
    return lines


if __name__ == "__main__":
    problems = check()
    if not problems:
        print("OK — every held ticker across all portfolios/investors is in the tracked universe.")
    else:
        print(f"UNTRACKED: {len(problems)} ticker(s) held but not priced — add to universe.py's "
              "PORTFOLIO_SPECIFIC_TICKERS after verifying the correct yahoo_ticker/currency:")
        for p in problems:
            print(f"  {p}")
