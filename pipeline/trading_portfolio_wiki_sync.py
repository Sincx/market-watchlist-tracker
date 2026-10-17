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
computable cells — Open Positions' Last Price/Mkt Value/P&L%, and Short
Positions' Current Price/Unrealized P&L — by splitting each table row on
`|` and replacing ONLY those column indices by position, never by scanning
cell text for numbers (which would risk corrupting a Notes cell that
happens to mention a price, e.g. "trimmed 11 sh @ $99.17"). Options
Positions is deliberately NOT touched (see the comment where it's skipped
below — Turso has no real options pricing, only Black-Scholes estimates
Mike/the briefing task compute by hand). Signal and Notes stay exactly as
portfolio-management-briefing (or Mike) last wrote them.

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

OPEN_HEADER = "| Company | Ticker | Exchange | Currency | Shares | Entry | Entry Date | Cost Basis | Last Price | Mkt Value | P&L% | Signal | Notes |"
SHORT_HEADER = "| Company | Ticker | Exchange | Currency | Shares Short | Entry | Entry Date | Short Value | Current Price | Unrealized P&L | Signal | Notes |"
OPTIONS_HEADER = "| Underlying | Ticker | Type | Strike | Expiry | Contracts | Shares | Premium Paid | Entry Date | Total Cost (€) | Current Price | Mkt Value (€) | Signal | Notes |"
# Found 2026-10-10: these three constants are exact-string header matches
# (see process_table's lines.index(header_line)) — the reconciliation that
# added an "Entry Date" column to all three tables on 2026-10-01 broke the
# match silently (process_table prints a WARNING and skips the table rather
# than crashing, so this went unnoticed for over a week; the briefing task
# worked around it by having the LLM edit Last Price/Mkt Value/P&L% by hand
# instead). If these tables' headers change again, update the constants AND
# the column indices passed to process_table() below in the same commit —
# the indices are not derived from the header string, they're hardcoded
# integers that must stay in sync with it by hand.


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

    Deliberately NOT anchored to the end of the string (found 2026-10-10):
    a Short Positions "Current Price" cell had picked up a hand-added
    "$143.56 (10-07)" date annotation during the week this script was
    broken. The old anchored regex required prefix, number, and suffix
    together to consume the entire cell, so it failed to match at all on
    that trailing " (10-07)" (the suffix group was restricted to
    non-digit characters, and both the parens and the digits inside
    "10-07" violate that once the number itself has already been
    consumed) — the "if m else" fallback then treated the WHOLE old cell
    string as the "number part", and decimals came out as the length of
    everything after the first dot (ten-plus characters), so new_value
    got formatted with about 10 decimal places and no "$" prefix:
    "141.3999938965". Fixed by only matching prefix+number+a short
    immediate unit suffix (letters/%, no space) from the START of the
    cell — any trailing annotation after a space or "(" is simply
    ignored rather than required to round-trip.
    """
    m = re.match(r"^([^\d\-]*)([\d,]+\.?\d*)([a-zA-Z%]*)", old_cell.strip())
    if not m or not m.group(2):
        prefix, number_part, suffix = "", "", ""
    else:
        prefix, number_part, suffix = m.group(1), m.group(2), m.group(3)
    decimals = len(number_part.split(".")[1]) if "." in number_part else 2
    formatted = f"{new_value:,.{decimals}f}"
    return f"{prefix}{formatted}{suffix}"


def _reformat_pct(old_cell: str, new_value: float) -> str:
    sign = "+" if new_value >= 0 else ""
    return f"{sign}{new_value:.2f}%"


def _reformat_pnl_cell(old_cell: str, pnl_native: float, pl_pct: float) -> str:
    """Reproduce the Short Positions 'Unrealized P&L' cell style, e.g.
    "+$82.41 (+10.66%)" or "−$242.48 (−12.93%)" — old cell's currency
    symbol is learned the same way _reformat_like does (first non-digit
    run), everything else about the format is fixed (this column's shape
    doesn't vary row to row the way price cells' decimal counts do).
    """
    m = re.match(r"^[+\-−]?([^\d\-−]*)", old_cell.strip())
    symbol = m.group(1) if m else "$"
    sign = "+" if pnl_native >= 0 else "−"
    pct_sign = "+" if pl_pct >= 0 else "−"
    return f"{sign}{symbol}{abs(pnl_native):,.2f} ({pct_sign}{abs(pl_pct):.2f}%)"


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

    # Open Positions: Last Price=idx8, Mkt Value=idx9, P&L%=idx10 (shifted by
    # +1 from the original 7/8/9 since the 2026-10-01 reconciliation inserted
    # an "Entry Date" column at idx6 — see the header-constant comment above).
    process_table(OPEN_HEADER, price_idx=8, value_idx=9, pct_idx=10)

    # Short Positions: the 2026-10-01 reconciliation added a real "Current
    # Price" column (idx8) and an "Unrealized P&L" column (idx9) — this table
    # was previously skipped entirely because neither existed (see the old
    # comment this replaced; "Short Value" at idx7 is still entry-based
    # notional, Entry × Shares, not touched here). Handled separately from
    # process_table() because the P&L cell is a compound "$X (Y%)" format,
    # not a single reformatted number.
    def process_short_table():
        try:
            header_i = lines.index(SHORT_HEADER)
        except ValueError:
            changes.append(f"WARNING: header not found, skipped the Short Positions table: {SHORT_HEADER[:60]}...")
            return
        i = header_i + 2
        while i < len(lines) and lines[i].strip().startswith("|"):
            cells = _split_row(lines[i])
            ticker = cells[1].strip() if len(cells) > 1 else None
            pos = by_ticker.get(ticker)
            if pos is None or pos["direction"] != "short" or pos["price"] is None or pos["entry_price"] is None:
                i += 1
                continue
            old_line = lines[i]
            pnl_native = (pos["entry_price"] - pos["price"]) * pos["shares"]
            cells[8] = _reformat_like(cells[8], pos["price"])
            if pos["pl_pct"] is not None:
                cells[9] = _reformat_pnl_cell(cells[9], pnl_native, pos["pl_pct"])
            new_line = _join_row(cells)
            if new_line != old_line:
                changes.append(f"  {ticker} (short):\n    - {old_line}\n    + {new_line}")
                lines[i] = new_line
            i += 1

    process_short_table()
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
