"""Tracked-investor backfill — populates `tracked_investors` and
`investor_positions` (spec §10) for any investor tracked via structured 13F
data through Equibles.

Burry is the one exception: his 13F data proved too sparse and stale to be
useful (evaluated 2026-09-10 — only 8 positions, dated 2025-09-30, over a
year behind the wiki's same-day Substack trading-post tracking), so he stays
sourced from wiki prose extraction per the original spec design (§10 steps
2-3), not this module. This module is for a second/future tracked investor
(Buffett, etc.) where Equibles' current, structured 13F data is a better fit
than building a new prose-extraction pipeline each time.

Nothing in here has been run against a real second investor yet — built and
tested against live data (Buffett/Berkshire) on 2026-09-10, but no
tracked_investors/investor_positions rows have been written. Call
backfill_investor() when Mike names who to add next.
"""
from __future__ import annotations

import re
from datetime import date

import equibles as _equibles
import db

TODAY = date.today().isoformat()


def list_super_investors() -> list[dict]:
    """Parse GetSuperInvestors' markdown table into structured rows:
    manager, firm, cik, portfolio_value, positions, qoq_change, as_of.
    Returns [] if the Equibles session can't be established.
    """
    session = _equibles.Session()
    if not session.ok:
        return []
    text = session.call("GetSuperInvestors", {})
    if not text:
        return []

    rows = []
    for line in text.splitlines():
        if not line.startswith("|") or line.startswith("|---") or "Manager" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 7:
            continue
        manager, firm, cik, value, positions, qoq, as_of = cells
        rows.append({
            "manager": manager, "firm": firm, "cik": cik,
            "portfolio_value": value, "positions": positions,
            "qoq_change": qoq, "as_of": as_of,
        })
    return rows


def fetch_institution_portfolio(institution_name: str) -> list[dict]:
    """Parse GetInstitutionPortfolio's holdings table for one institution
    into structured rows: ticker, company, type, shares, value_millions, pct.
    `type` is one of 'Common' | 'Put' | 'Call' | 'Principal' (direction in
    the trades/investor_positions sense: Common/Call = long-ish, Put = short
    thesis — see the schema note on trades.direction for options).
    Returns [] on any failure.
    """
    session = _equibles.Session()
    if not session.ok:
        return []
    text = session.call("GetInstitutionPortfolio", {"institutionName": institution_name})
    if not text:
        return []

    rows = []
    for line in text.splitlines():
        if not line.startswith("|") or line.startswith("|---") or "Ticker" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 7:
            continue
        _, ticker, company, typ, shares, value, pct = cells
        if not re.match(r"^[A-Z]", ticker):
            continue
        rows.append({
            "ticker": ticker, "company": company, "type": typ,
            "shares": shares, "value_millions": value, "pct_of_portfolio": pct,
        })
    return rows


def backfill_investor(investor_id: str, name: str, institution_name: str, source_ref: str, dry_run: bool = False) -> int:
    """Write one tracked_investors row (source_type='13F') and one
    investor_positions row per current holding, sourced from Equibles.
    Returns the number of position rows written (0 on failure).

    direction: 'short' for Put rows (bearish thesis), 'long' for everything
    else (Common/Call/Principal) — matches the trades.direction convention
    used elsewhere in the schema.
    """
    holdings = fetch_institution_portfolio(institution_name)
    if not holdings:
        print(f"  [investors] {institution_name}: no holdings returned, nothing written")
        return 0

    investor_row = {
        "investor_id": investor_id, "name": name,
        "source_type": "13F", "source_ref": source_ref,
    }
    position_rows = []
    for h in holdings:
        position_rows.append({
            "investor_id": investor_id, "ticker": h["ticker"], "exchange": "US",
            "direction": "short" if h["type"] == "Put" else "long",
            "disclosed_date": TODAY, "entry_price_hint": None,
            "status": "open", "source_ref": source_ref,
        })

    if dry_run:
        print(f"  [DRY RUN] would write tracked_investors row: {investor_row}")
        for r in position_rows[:5]:
            print(f"  [DRY RUN] would write investor_positions row: {r}")
        return len(position_rows)

    client = db.get_client()
    try:
        db.upsert(client, "tracked_investors", [investor_row])
        n = db.upsert(client, "investor_positions", position_rows)
        print(f"  [investors] {institution_name}: wrote 1 tracked_investors row + {n} investor_positions rows")
        return n
    finally:
        client.close()
