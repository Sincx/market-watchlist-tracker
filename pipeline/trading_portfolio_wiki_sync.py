"""The "Turso → wiki" write-back job Mike asked for (2026-09-14): "Turso
should be the master system now" for Trading Portfolio.

Deliberately narrow scope, not a full-file regeneration: `trading-
portfolio.md` is extremely dense with hand-curated analysis — every
position's Notes cell mixes generated RSI/SMA/MACD summaries with real
judgment (patience-override reasoning, free-ride trim history, thesis
notes), the Structural Risk Notes (Burry Lens) table is 100% qualitative
commentary, and Cash Position's "Movement Log" is a hand-appended running
narrative. None of that has a Turso equivalent, and a script "regenerating"
the page would destroy it. So this script only touches the objectively
computable cells — Last Price, Mkt Value / Short Value / options Current
Price+Mkt Value, and P&L% — by splitting each table row on `|` and
replacing ONLY those column indices by position, never by scanning cell
text for numbers (which would risk corrupting a Notes cell that happens to
mention a price, e.g. "trimmed 11 sh @ $99.17"). Signal and Notes stay
exactly as portfolio-management-briefing (or Mike) last wrote them.

Matches wiki rows to Turso positions by ticker (each ticker appears at
most once per table section) and reuses trading_portfolio_turso_view.py's
already-computed price/pl_pct/eur_value — not a second independent
calculation — so this and the briefing task can never disagree about what
Turso says.

Run standalone:
  python trading_portfolio_wiki_sync.py --dry-run   # prints a diff, writes nothing
  python trading_portfolio_wiki_sync.py --write     # actually writes the file
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from trading_portfolio_turso_view import get_portfolio_view

WIKI_PATH = Path(r"C:\Users\Mike\Documents\Fred\Fred\wiki\finance\trading-portfolio.md")

OPEN_HEADER = "| Company | Ticker | Exchange | Currency | Shares | Entry | Cost Basis | Last Price | Mkt Value | P&L% | Signal | Notes |"
SHORT_HEADER = "| Company | Ticker | Exchange | Currency | Shares Short | Entry | Short Value | Signal | Notes |"
OPTIONS_HEADER = "| Underlying | Ticker | Type | Strike | Expiry | Contracts | Shares | Premium Paid | Total Cost (€) | Current Price | Mkt Value (€) | Signal | Notes |"


def _split_row(line: str) -> list[str]:
    """Markdown table row -> list of cell strings (no leading/trailing empties)."""
    parts = line.strip().split("|")
    return [p.strip() for p in parts[1:-1]]


def _join_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _reformat_like(old_cell: str, new_value: float) -> str:
    """Reproduce the old cell's prefix/suffix/decimals around a new number,
    e.g. old="$1,174.88" new=1234.5 -> "$1,234.50"; old="2,992.00p"
    new=3100.126 -> "3,100.13p". Thousands-separator is always applied
    (not "learned" from the old cell) — found 2026-09-14: a value that
    crosses the $1,000 line but whose OLD cell was under 1,000 (so had no
    comma to learn the convention from) was rendering as "$1144.87"
    instead of "$1,144.87".
    """
    m = re.match(r"^([^\d\-]*)([\d,]*\.?\d*)([^\d]*)$", old_cell.strip())
    prefix, number_part, suffix = (m.group(1), m.group(2), m.group(3)) if m else ("", old_cell, "")
    decimals = len(number_part.split(".")[1]) if "." in number_part else 2
    formatted = f"{new_value:,.{decimals}f}"
    return f"{prefix}{formatted}{suffix}"


def _reformat_pct(old_cell: str, new_value: float) -> str:
    sign = "+" if new_value >= 0 else ""
    return f"{sign}{new_value:.2f}%"


def sync(dry_run: bool = True) -> list[str]:
    view = get_portfolio_view()
    by_ticker = {p["ticker"]: p for p in view["positions"]}

    text = WIKI_PATH.read_text(encoding="utf-8")
    lines = text.split("\n")
    changes: list[str] = []

    def process_table(header_line: str, price_idx: int, value_idx: int, pct_idx: int | None):
        try:
            header_i = lines.index(header_line)
        except ValueError:
            changes.append(f"WARNING: header not found, skipped a table: {header_line[:60]}...")
            return
        i = header_i + 2  # skip header + separator row
        while i < len(lines) and lines[i].strip().startswith("|"):
            cells = _split_row(lines[i])
            ticker = cells[1].strip() if len(cells) > 1 else None
            pos = by_ticker.get(ticker)
            if pos is None or pos["price"] is None:
                i += 1
                continue
            old_line = lines[i]
            new_price_cell = _reformat_like(cells[price_idx], pos["price"])
            # Mkt Value in the wiki is in the position's OWN currency (matches
            # its "Currency" column) — e.g. KLR shows "£299.20", SAP shows
            # "€1,240.82", not a EUR-converted figure for every row. Use
            # native_value, not eur_value (found 2026-09-14 on a second
            # dry-run review: eur_value only happened to look right for
            # EUR-denominated rows like SAP/EDEN — every USD/GBP/GBX row was
            # silently wrong, e.g. APH would have shown its EUR value under a
            # "$" prefix).
            new_value_cell = _reformat_like(cells[value_idx], abs(pos["native_value"])) if pos["native_value"] is not None else cells[value_idx]
            cells[price_idx] = new_price_cell
            cells[value_idx] = new_value_cell
            if pct_idx is not None and pos["pl_pct"] is not None:
                cells[pct_idx] = _reformat_pct(cells[pct_idx], pos["pl_pct"])
            new_line = _join_row(cells)
            if new_line != old_line:
                changes.append(f"  {ticker}:\n    - {old_line}\n    + {new_line}")
                lines[i] = new_line
            i += 1

    # Open Positions: Last Price=idx7, Mkt Value=idx8, P&L%=idx9
    process_table(OPEN_HEADER, price_idx=7, value_idx=8, pct_idx=9)
    # Short Positions: "Short Value" (idx6) is entry-based notional in the
    # current file (Entry × Shares, not mark-to-market) — there's no Last
    # Price column here at all, so nothing safe to update mechanically
    # without changing this table's own schema. Skipped, not guessed.
    #
    # Options Positions: deliberately NOT auto-updated. Found the hard way
    # in --dry-run testing 2026-09-14: "Current Price"/"Mkt Value (€)" here
    # are Black-Scholes premium ESTIMATES, not the underlying's stock price
    # — Turso has no options-pricing model at all, only the underlying's
    # close. An early version of this script matched the Options table's
    # ORCL row against the equity/short ORCL position's stock price and
    # silently overwrote a real "$5.79/sh" options estimate with "$143.81/sh"
    # (the underlying's price) — wrong in a way that would have looked
    # plausible enough to miss without a diff review. Options mark-to-market
    # stays exactly what portfolio-management-briefing (or Mike) last wrote,
    # same as Signal/Notes elsewhere in this script.

    if not changes:
        changes.append("No changes — all matched positions already up to date.")

    if not dry_run:
        WIKI_PATH.write_text("\n".join(lines), encoding="utf-8")

    return changes


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="Actually write the wiki file (default is dry-run)")
    args = parser.parse_args()
    for c in sync(dry_run=not args.write):
        print(c)
    if not args.write:
        print("\n(dry run — wiki file not written; pass --write to apply)")
