"""Master spec Phase 15 — monthly automated re-sync for every 13F-sourced
tracked investor (currently buffett, ackman — NOT burry, who stays
wiki-prose-sourced per investors.py's own docstring). 13F filings only
update quarterly, but this is cheap to run monthly: most months it's a
no-op, and it means a new quarter's filing gets picked up within ~4 weeks
of being available rather than only when someone remembers to re-run
investors.py by hand.

For each tracked investor, diffs Equibles' CURRENT holdings against the
most recent investor_positions snapshot already in Turso:
  - A ticker that's newly present -> new investor_positions row (status=
    'open') AND a `signals` row (source='investor:<investor_id>') so it
    corroborates in v_investment_opportunities/trading_portfolio_candidates.py
    the same way a magic-formula-pass or llm-research signal already does.
  - A ticker that's no longer present -> the existing open row is marked
    status='closed', exit_date=the new filing's as_of date. This is an
    approximation of WHEN the exit happened (13F only proves it happened
    somewhere between the last two quarterly snapshots, not the exact
    date) — same "best available anchor, don't fabricate precision the
    source doesn't have" principle used throughout this pipeline.
    exit_price_hint is left NULL; shadow_portfolio.py resolves it via a
    historical close on exit_date, same fallback already built for entries.
  - Unchanged tickers are left alone entirely (no rewrite, no churn).

After diffing, regenerates that investor's shadow portfolio via
shadow_portfolio.run() so any new/closed position is reflected immediately
rather than waiting for a separate manual step.

Run standalone: python investor_refresh.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import db
import investors
import shadow_portfolio

# (investor_id, name, institution_name, source_ref) — burry deliberately
# excluded, see module docstring.
TRACKED_13F_INVESTORS = [
    ("buffett", "Warren Buffett", "Berkshire Hathaway Inc", "cik:1067983"),
    ("ackman", "Bill Ackman", "Pershing Square Capital Management, L.P.", "cik:1336528"),
]


def _latest_as_of(institution_name: str) -> str | None:
    for row in investors.list_super_investors():
        if row["firm"] == institution_name:
            return row["as_of"].replace(" (stale)", "")
    return None


def refresh_investor(investor_id: str, name: str, institution_name: str, source_ref: str,
                      dry_run: bool = False) -> dict:
    as_of = _latest_as_of(institution_name)
    if not as_of:
        return {"investor_id": investor_id, "error": "not found in list_super_investors() — skipped"}

    current_holdings = investors.fetch_institution_portfolio(institution_name)
    if not current_holdings:
        return {"investor_id": investor_id, "error": "no holdings returned from Equibles — skipped"}
    current_tickers = {h["ticker"] for h in current_holdings}
    current_by_ticker = {h["ticker"]: h for h in current_holdings}

    client = db.get_client()
    try:
        existing = db.query(
            client, "SELECT * FROM investor_positions WHERE investor_id = :i AND status = 'open';",
            {"i": investor_id},
        )
    finally:
        client.close()
    existing_tickers = {r["ticker"] for r in existing}

    if as_of == (existing[0]["disclosed_date"] if existing else None):
        return {"investor_id": investor_id, "as_of": as_of, "note": "already up to date, no new filing"}

    new_tickers = current_tickers - existing_tickers
    dropped_tickers = existing_tickers - current_tickers

    new_position_rows = []
    new_signal_rows = []
    now = datetime.now(timezone.utc).isoformat()
    for t in new_tickers:
        h = current_by_ticker[t]
        new_position_rows.append({
            "investor_id": investor_id, "ticker": t, "exchange": "US",
            "direction": "short" if h["type"] == "Put" else "long",
            "disclosed_date": as_of, "entry_price_hint": None,
            "status": "open", "source_ref": source_ref,
            "exit_date": None, "exit_price_hint": None,
        })
        new_signal_rows.append({
            "signal_id": f"investor-{investor_id}-{t}-US-{as_of}",
            "ticker": t, "exchange": "US", "source": f"investor:{investor_id}",
            "detail": json.dumps({"investor_name": name, "as_of": as_of, "pct_of_portfolio": h.get("pct_of_portfolio")}),
            "flagged_date": as_of, "source_ref": source_ref,
            "status": "new", "status_updated_at": now,
        })

    closed_position_updates = [
        {"investor_id": investor_id, "ticker": t, "exchange": r["exchange"], "disclosed_date": r["disclosed_date"],
         "status": "closed", "exit_date": as_of}
        for t in dropped_tickers for r in existing if r["ticker"] == t
    ]

    print(f"[{investor_id}] as_of={as_of}: {len(new_tickers)} new, {len(dropped_tickers)} closed, "
          f"{len(current_tickers & existing_tickers)} unchanged")
    for t in new_tickers:
        print(f"  + {t} (new)")
    for t in dropped_tickers:
        print(f"  - {t} (closed as of {as_of})")

    if dry_run or (not new_position_rows and not closed_position_updates):
        return {"investor_id": investor_id, "as_of": as_of, "new": len(new_tickers), "closed": len(dropped_tickers)}

    client = db.get_client()
    try:
        if new_position_rows:
            db.upsert(client, "investor_positions", new_position_rows)
        if new_signal_rows:
            db.upsert(client, "signals", new_signal_rows)
        for upd in closed_position_updates:
            client.execute(
                "UPDATE investor_positions SET status = :status, exit_date = :exit_date "
                "WHERE investor_id = :investor_id AND ticker = :ticker AND exchange = :exchange "
                "AND disclosed_date = :disclosed_date;",
                upd,
            )
    finally:
        client.close()

    if new_position_rows or closed_position_updates:
        shadow_portfolio.run(investor_id=investor_id, dry_run=False)

    return {"investor_id": investor_id, "as_of": as_of, "new": len(new_tickers), "closed": len(dropped_tickers)}


def refresh_all(dry_run: bool = False) -> list[dict]:
    return [refresh_investor(*args, dry_run=dry_run) for args in TRACKED_13F_INVESTORS]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    results = refresh_all(dry_run=args.dry_run)
    for r in results:
        print(r)
