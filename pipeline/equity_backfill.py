"""Phase 7b: one-time backfill of the Equity/Pension portfolio into Turso's
`portfolios`/`trades` tables. Source: wiki/finance/portfolio-overview.md's
Equity Holdings table.

Real money, not rotation-strategy-driven (unlike Phase 7's paper-trading
backfill) — no strategy attribution beyond a single generic 'buy-and-hold'
row, added purely so v_strategy_performance/v_portfolio_performance have
something to group on, not because these were picked via any of the A-G
rotation logic.

Known gaps, deliberately left NULL rather than fabricated (confirmed with
Mike 2026-09-10 for `shares`; `entry_date` extended from the same principle
since it has the identical problem — no source of truth exists for either):
  - `shares`: neither the wiki page nor the source CSV
    (raw/Pension Portfolio.csv) has quantities, only entry price per unit.
  - `entry_date`: the wiki table has no date column at all; the page's
    frontmatter `updated:` date is when the page was last edited, not when
    any position was actually entered — using it would be wrong, not just
    imprecise, so it's not used as a fallback.
Both mean `open_cost_basis` in v_portfolio_performance will be NULL for
this portfolio's positions until real quantities/dates are supplied.

Scope: only the 19 holdings in the wiki's own Equity Holdings table, per
Mike's 2026-09-10 decision — the 3 CSV holdings the wiki page itself already
flags as unmatched (Kyndryl, NAVYA SA, Valterra Platinum) and the blank
"SIPP cash - GBP" line are deliberately skipped, not silently added.

Run standalone: python equity_backfill.py [--dry-run]
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import db

WIKI_PATH = Path(r"C:\Users\Mike\Documents\Fred\Fred\wiki\finance\portfolio-overview.md")
PORTFOLIO_ID = "equity-pension"
STRATEGY_ID = "BUY-AND-HOLD"

_EXCHANGE_MAP = {
    "NASDAQ": "US", "NYSE": "US", "OTC": "US",
    "LSE": "UK", "Euronext Paris": "EU",
}


def _parse_price(raw: str) -> tuple[float | None, str | None]:
    """Returns (price, currency_hint). Handles "$106.62 *" (strips footnote
    marker), "€112.60" (EUR), "5,045p" (GBp, pence — no symbol prefix)."""
    raw = raw.strip().rstrip("*").strip()
    if raw.endswith("p"):
        m = re.match(r"([\d,]+\.?\d*)p", raw)
        return (float(m.group(1).replace(",", "")) if m else None), "GBX"
    m = re.match(r"[€$]?([\d,]+\.?\d*)", raw)
    if not m:
        return None, None
    currency = "EUR" if raw.startswith("€") else ("USD" if raw.startswith("$") else None)
    return float(m.group(1).replace(",", "")), currency


def parse_equity_holdings() -> list[dict]:
    text = WIKI_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("| Company | Ticker"))
    trades = []
    for line in lines[start + 2 :]:
        line = line.strip()
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 6:
            continue
        _company, ticker, exchange_raw, currency_col, entry_raw, _last_raw = cells[:6]
        entry_price, price_currency = _parse_price(entry_raw)
        if entry_price is None:
            continue
        exchange = _EXCHANGE_MAP.get(exchange_raw.strip(), "US")
        trades.append({
            "trade_id": f"{PORTFOLIO_ID}-{ticker}",
            "portfolio_id": PORTFOLIO_ID, "strategy_id": STRATEGY_ID,
            "ticker": ticker, "exchange": exchange, "instrument_type": "equity",
            "direction": "long", "entry_date": None, "entry_price": entry_price,
            "shares": None,
            "status": "open", "thesis": None, "source_signal_id": None,
        })
    return trades


def run(dry_run: bool = False) -> None:
    strategy_row = {
        "strategy_id": STRATEGY_ID, "name": "Buy and Hold",
        "instrument_type": "equity_long",
        "description": "Long-term pension holdings — not rotation-strategy-driven; grouped here only so performance views have something to key on.",
        "rules_ref": None, "active": 1,
    }
    portfolio_row = {
        "portfolio_id": PORTFOLIO_ID, "name": "Equity / Pension Portfolio",
        "kind": "real", "mirrors_investor_id": None,
        "base_currency": "GBP", "created_date": None, "active": 1,
    }

    trades = parse_equity_holdings()
    print(f"Parsed {len(trades)} holdings")
    if len(trades) != 19:
        print(f"  WARN: expected 19 holdings (per the wiki table as of 2026-09-10), got {len(trades)} "
              f"— check the table format hasn't changed before trusting this run.")

    if dry_run:
        print(f"Strategy: {strategy_row['strategy_id']}")
        print(f"Portfolio: {portfolio_row}")
        for t in trades:
            print(" ", t)
        print("  ... (dry run, nothing written)")
        return

    client = db.get_client()
    try:
        db.upsert(client, "strategies", [strategy_row])
        db.upsert(client, "portfolios", [portfolio_row])
        n = db.upsert(client, "trades", trades)
        print(f"Upserted 1 strategy, 1 portfolio, {n} trades.")
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
