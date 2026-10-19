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

Live since 2026-09-20 for Buffett (Berkshire) and Ackman (Pershing Square);
investor_refresh.py then keeps every active 13F investor current monthly, so
adding another investor is one backfill_investor() call, no code edit. This
module also owns the `investor:<id>` signal rows (investor_signal_row /
ensure_investor_signals) — shared with burry_backfill.py and investor_refresh.py
so all tracked investors, prose-sourced or 13F, corroborate the same way in
v_investment_opportunities and the Signal badge.
"""
from __future__ import annotations

import re

import json

import equibles as _equibles
import db


def _parse_num(text: str) -> float | None:
    """'227,917,808' -> 227917808.0, '65,950.3' -> 65950.3; None if unparseable."""
    try:
        return float(str(text).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


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
        value_m = _parse_num(value)
        # Same dot/space -> dash normalisation universe.py applies, so a
        # holding like "BRK.B" keys the same as universe/prices' "BRK-B"
        # (a mismatch here is exactly the unpriced-ticker bug fixed 2026-09-20).
        ticker = re.sub(r"[ .]", "-", ticker)
        rows.append({
            "ticker": ticker, "company": company, "type": typ,
            "shares": shares, "value_millions": value, "pct_of_portfolio": pct,
            # Parsed copies for storage/trim detection (the strings above stay
            # as Equibles printed them).
            "shares_num": _parse_num(shares),
            "value_usd": value_m * 1_000_000 if value_m is not None else None,
        })
    return rows


def investor_signal_row(investor_id: str, investor_name: str, ticker: str, exchange: str,
                         direction: str, as_of: str, source_ref: str | None,
                         pct_of_portfolio: str | None = None) -> dict:
    """One `signals` row (source='investor:<id>') for a currently-held position.

    Deliberately omits status/status_updated_at: those columns only mean
    something for source='briefing-recommendation', and leaving them out means
    re-writing an existing row never resets anything. signal_id is stable per
    (investor, ticker, exchange, disclosed_date), so this is idempotent.
    """
    return {
        "signal_id": f"investor-{investor_id}-{ticker}-{exchange}-{as_of}",
        "ticker": ticker, "exchange": exchange, "source": f"investor:{investor_id}",
        "detail": json.dumps({"investor_name": investor_name, "direction": direction,
                              "as_of": as_of, "pct_of_portfolio": pct_of_portfolio}),
        "flagged_date": as_of, "source_ref": source_ref,
    }


def ensure_investor_signals(client) -> int:
    """Write an `investor:<id>` signal for every current ('open'/'trimmed')
    position of every active tracked investor that doesn't have one yet.
    Returns the number of signal rows written.

    Why this exists (Master spec Phase 15c, found 2026-10-19): the spec always
    intended every tracked-investor position to corroborate in
    v_investment_opportunities / the Signal badge, but backfill_investor() and
    burry_backfill.py only ever wrote investor_positions — the signals table
    held zero `investor:*` rows. Idempotent, so it's safe to call from every
    backfill and every monthly refresh as the catch-all.
    """
    rows = db.query(client, """
        SELECT ip.investor_id, ti.name, ip.ticker, ip.exchange, ip.direction,
               ip.disclosed_date, ip.source_ref
        FROM investor_positions ip
        JOIN tracked_investors ti ON ti.investor_id = ip.investor_id
        WHERE ip.status IN ('open', 'trimmed') AND ti.active = 1
          AND NOT EXISTS (
              SELECT 1 FROM signals s
              WHERE s.signal_id = 'investor-' || ip.investor_id || '-' || ip.ticker
                                  || '-' || ip.exchange || '-' || ip.disclosed_date
          );
    """)
    signals = [investor_signal_row(r["investor_id"], r["name"], r["ticker"], r["exchange"],
                                    r["direction"], r["disclosed_date"], r["source_ref"])
               for r in rows]
    return db.upsert(client, "signals", signals) if signals else 0


def backfill_investor(investor_id: str, name: str, institution_name: str, source_ref: str,
                       as_of_date: str, display_name: str | None = None, dry_run: bool = False) -> int:
    """Write one tracked_investors row (source_type='13F'), one
    investor_positions row per current holding (with share count / USD value),
    and the matching `investor:<id>` signals, sourced from Equibles.
    Returns the number of position rows written (0 on failure).

    direction: 'short' for Put rows (bearish thesis), 'long' for everything
    else (Common/Call/Principal) — matches the trades.direction convention
    used elsewhere in the schema.

    `display_name` is the surname shown in the UI ("Buffett (L)") — pass it
    explicitly rather than having it parsed out of `name`. `source_ref` should
    be 'cik:<n>': investor_refresh.py uses that CIK to find the investor's
    Equibles firm name, so no code edit is needed to keep a new investor current.

    `as_of_date` must be the 13F's own quarter-end date (list_super_investors()'s
    `as_of` field, e.g. "2026-06-30" — strip any trailing " (stale)" flag
    first) and is stored as disclosed_date, NOT today's date. A real bug
    caught 2026-09-20 backfilling Buffett/Ackman: with disclosed_date=TODAY,
    shadow_portfolio.py's entry_price_hint=None fallback tries to fetch a
    historical close for [today, today+7d] — a future window with no data —
    so every single position silently got skipped and 0 shadow trades were
    generated. Equibles' 13F holdings table gives current share count/value,
    not a per-share cost basis, so entry_price_hint has to stay None and rely
    on that historical-close fallback resolving against the REAL disclosure
    date instead.
    """
    holdings = fetch_institution_portfolio(institution_name)
    if not holdings:
        print(f"  [investors] {institution_name}: no holdings returned, nothing written")
        return 0

    investor_row = {
        "investor_id": investor_id, "name": name,
        "source_type": "13F", "source_ref": source_ref,
        "display_name": display_name or name.split()[-1], "active": 1,
        "last_as_of": as_of_date,
    }
    position_rows = []
    for h in holdings:
        position_rows.append({
            "investor_id": investor_id, "ticker": h["ticker"], "exchange": "US",
            "direction": "short" if h["type"] == "Put" else "long",
            "disclosed_date": as_of_date, "entry_price_hint": None,
            "status": "open", "source_ref": source_ref,
            "shares": h["shares_num"], "value_usd": h["value_usd"],
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
        sig = ensure_investor_signals(client)
        print(f"  [investors] {institution_name}: wrote 1 tracked_investors row + {n} investor_positions rows"
              f" + {sig} investor signals")
        return n
    finally:
        client.close()
