"""Master spec Phase 15 — monthly automated re-sync for every active 13F
tracked investor (Buffett, Ackman today; NOT Burry, who stays wiki-prose-
sourced per investors.py's own docstring). 13F filings only update
quarterly, but checking monthly is cheap — most months are a no-op — and a
new quarter's filing gets picked up within ~4 weeks of being available.

DB-driven, no hard-coded investor list: an investor is "a tracked_investors
row with source_type='13F', active=1 and source_ref='cik:<n>'". The CIK is
matched against Equibles' list_super_investors() to find the firm name and the
latest filing's as_of date, so adding a new investor is one
investors.backfill_investor() call and nothing here changes.

Per investor, on every run:
  - SAME filing as last time (as_of == tracked_investors.last_as_of): no state
    changes; only fills in share counts/values that are still NULL on existing
    rows (legacy rows from before those columns existed).
  - NEW filing: diff Equibles' current holdings against the investor's open/
    trimmed positions, keyed (ticker, direction):
      new      -> an 'open' investor_positions row (disclosed_date = the new as_of)
      closed   -> status='closed', exit_date = the new as_of. The date is an
                  approximation (a 13F only proves the exit happened between
                  two quarterly snapshots); exit_price_hint stays NULL and
                  shadow_portfolio.py resolves it from a historical close.
      trimmed  -> a still-held position whose share count fell by more than
                  TRIM_THRESHOLD vs. the stored count: status='trimmed'. This
                  means "trimmed in the LATEST filing" — the next filing resets
                  it to 'open' unless it was cut again. Share count and value
                  are refreshed on every continuing row either way, which is
                  what the next filing is compared against.
      held     -> unchanged apart from refreshed shares/value_usd.
    Newly-held tickers are also registered in `universe` immediately
    (INVESTOR_FLAGGED) so they get a price from the next technicals run rather
    than waiting for universe.py's monthly rebuild — which now also reads
    investor_positions, so a rebuild doesn't deactivate them.
Every run, for ALL tracked investors (Burry included), ensure_investor_signals()
back-fills any missing `investor:<id>` signal, then the candidates wiki page is
regenerated from list_super_investors() so there's always a current menu of who
could be tracked next.

Run standalone: python investor_refresh.py [--dry-run]
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import db
import investors
import shadow_portfolio

TRIM_THRESHOLD = 0.25  # share count down more than 25% filing-to-filing => 'trimmed'
CANDIDATES_PAGE = Path(r"C:\Users\Mike\Documents\Fred\Fred\wiki\finance\tracked-investors-candidates.md")


def _direction(holding: dict) -> str:
    return "short" if holding["type"] == "Put" else "long"


def _clean_as_of(raw: str) -> str:
    return raw.replace(" (stale)", "").strip()


def _status_for(old_shares: float | None, new_shares: float | None) -> str:
    if old_shares and new_shares is not None and new_shares < old_shares * (1 - TRIM_THRESHOLD):
        return "trimmed"
    return "open"


def diff_positions(existing: list[dict], holdings: list[dict]) -> dict:
    """Pure function (no DB, no network) — compares an investor's stored
    open/trimmed positions against the current filing's holdings.

    Returns {"new": [holding], "closed": [existing row], "continuing":
    [{"row", "holding", "status", "changed"}]}; `changed` is False when shares,
    value and status all already match, so callers can skip no-op writes.
    """
    held = {(h["ticker"], _direction(h)): h for h in holdings}
    have = {(r["ticker"], r["direction"]): r for r in existing}

    new = [h for k, h in held.items() if k not in have]
    closed = [r for k, r in have.items() if k not in held]
    continuing = []
    for k, row in have.items():
        h = held.get(k)
        if h is None:
            continue
        status = _status_for(row.get("shares"), h.get("shares_num"))
        changed = (row.get("shares") != h.get("shares_num")
                   or row.get("value_usd") != h.get("value_usd")
                   or row.get("status") != status)
        continuing.append({"row": row, "holding": h, "status": status, "changed": changed})
    return {"new": new, "closed": closed, "continuing": continuing}


def fill_missing_numbers(existing: list[dict], holdings: list[dict]) -> list[dict]:
    """Pure function. For the SAME filing already stored: the (pk + shares +
    value_usd) update rows for existing positions whose shares/value are still
    NULL. Never touches status — no new information about holdings changing.
    """
    held = {(h["ticker"], _direction(h)): h for h in holdings}
    out = []
    for row in existing:
        h = held.get((row["ticker"], row["direction"]))
        if h is None or (row.get("shares") is not None and row.get("value_usd") is not None):
            continue
        out.append({
            "investor_id": row["investor_id"], "ticker": row["ticker"],
            "exchange": row["exchange"], "disclosed_date": row["disclosed_date"],
            "shares": h.get("shares_num"), "value_usd": h.get("value_usd"),
        })
    return out


def _pk(row: dict) -> dict:
    return {k: row[k] for k in ("investor_id", "ticker", "exchange", "disclosed_date")}


def refresh_investor(client, inv: dict, supers_by_cik: dict, dry_run: bool = False) -> dict:
    investor_id = inv["investor_id"]
    cik = (inv.get("source_ref") or "").removeprefix("cik:").lstrip("0")
    sup = supers_by_cik.get(cik)
    if not sup:
        return {"investor_id": investor_id, "error": f"CIK {cik!r} not found in list_super_investors() — skipped"}
    as_of, institution = _clean_as_of(sup["as_of"]), sup["firm"]

    last = inv.get("last_as_of")
    if last and as_of < last:
        return {"investor_id": investor_id, "as_of": as_of, "note": f"Equibles as_of {as_of} is older than stored {last}, ignored"}

    holdings = investors.fetch_institution_portfolio(institution)
    if not holdings:
        return {"investor_id": investor_id, "error": "no holdings returned from Equibles — skipped"}

    existing = db.query(client, """
        SELECT investor_id, ticker, exchange, direction, disclosed_date, status, shares, value_usd, source_ref
        FROM investor_positions WHERE investor_id = :i AND status IN ('open', 'trimmed');
    """, {"i": investor_id})

    if last == as_of:
        fills = fill_missing_numbers(existing, holdings)
        if fills and not dry_run:
            db.upsert(client, "investor_positions", fills)
        note = "already up to date, no new filing"
        if fills:
            note += f", {'would fill' if dry_run else 'filled'} {len(fills)} missing share counts"
        return {"investor_id": investor_id, "as_of": as_of, "note": note, "new": 0, "closed": 0, "trimmed": 0}

    d = diff_positions(existing, holdings)
    trimmed = [c for c in d["continuing"] if c["status"] == "trimmed"]
    print(f"[{investor_id}] new filing as_of={as_of} (was {last}): {len(d['new'])} new, {len(d['closed'])} closed, "
          f"{len(trimmed)} trimmed, {sum(1 for c in d['continuing'] if not c['changed'])} unchanged")
    for h in d["new"]:
        print(f"  + {h['ticker']} (new {_direction(h)})")
    for r in d["closed"]:
        print(f"  - {r['ticker']} (closed as of {as_of})")
    for c in trimmed:
        print(f"  ~ {c['row']['ticker']} (trimmed: {c['row'].get('shares'):,.0f} -> {c['holding']['shares_num']:,.0f} shares)")

    result = {"investor_id": investor_id, "as_of": as_of, "new": len(d["new"]),
              "closed": len(d["closed"]), "trimmed": len(trimmed)}
    if dry_run:
        return result

    source_ref = inv["source_ref"]
    writes = [{
        "investor_id": investor_id, "ticker": h["ticker"], "exchange": "US", "direction": _direction(h),
        "disclosed_date": as_of, "entry_price_hint": None, "status": "open", "source_ref": source_ref,
        "shares": h.get("shares_num"), "value_usd": h.get("value_usd"),
    } for h in d["new"]]
    writes += [{**_pk(r), "status": "closed", "exit_date": as_of} for r in d["closed"]]
    writes += [{**_pk(c["row"]), "shares": c["holding"].get("shares_num"),
                "value_usd": c["holding"].get("value_usd"), "status": c["status"]}
               for c in d["continuing"] if c["changed"]]
    db.upsert(client, "investor_positions", writes)

    if d["new"]:
        _register_in_universe(client, [h["ticker"] for h in d["new"]], as_of)
    db.upsert(client, "tracked_investors", [{"investor_id": investor_id, "last_as_of": as_of}])
    if d["new"] or d["closed"]:
        shadow_portfolio.run(investor_id=investor_id, dry_run=False)
    return result


def _register_in_universe(client, tickers: list[str], as_of: str) -> None:
    """Make newly-held tickers priceable now (INVESTOR_FLAGGED, plain US
    ticker, USD — 13F filers only report US-listed securities). Existing rows
    keep their other columns (db.upsert is column-preserving), and any ticker
    already in universe just gets its membership label extended, never
    replaced."""
    for t in tickers:
        existing = db.query(client, "SELECT index_membership FROM universe WHERE ticker = :t AND exchange = 'US';", {"t": t})
        if existing:
            labels = set(existing[0]["index_membership"].split(",")) | {"INVESTOR_FLAGGED"}
            db.upsert(client, "universe", [{"ticker": t, "exchange": "US",
                                            "index_membership": ",".join(sorted(labels)), "active": 1}])
        else:
            db.upsert(client, "universe", [{
                "ticker": t, "exchange": "US", "index_membership": "INVESTOR_FLAGGED",
                "yahoo_ticker": t, "currency": "USD", "sector": "", "added_date": as_of,
                "active": 1, "asset_class": "equity",
            }])


def write_candidates_page(supers: list[dict], tracked_ciks: set[str]) -> bool:
    """Regenerate wiki/finance/tracked-investors-candidates.md — a menu of
    every investor Equibles' GetSuperInvestors offers, marking who's already
    tracked, so picking the next one doesn't need a code lookup. Returns True
    if the file changed (the 'updated' date alone doesn't count as a change)."""
    lines = [
        "# Tracked Investor Candidates", "",
        "*Generated by investor_refresh.py from Equibles' GetSuperInvestors (13F data). "
        "Don't hand-edit — it's rewritten whenever the list changes.*", "",
        "To track one: call `investors.backfill_investor(investor_id, name, firm, 'cik:<cik>', as_of, display_name=...)`; "
        "the monthly refresh then keeps it current automatically.", "",
        "| Manager | Firm | CIK | Portfolio value | Positions | QoQ | Filing as of | Tracked |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in supers:
        tracked = "yes" if r["cik"].lstrip("0") in tracked_ciks else ""
        lines.append(f"| {r['manager']} | {r['firm']} | {r['cik']} | {r['portfolio_value']} | "
                     f"{r['positions']} | {r['qoq_change']} | {r['as_of']} | {tracked} |")
    body = "\n".join(lines) + "\n"

    existing_body = ""
    if CANDIDATES_PAGE.exists():
        text = CANDIDATES_PAGE.read_text(encoding="utf-8")
        existing_body = text.split("\n---\n", 1)[1].lstrip("\n") if text.startswith("---") and "\n---\n" in text else text
    if existing_body.strip() == body.strip():
        return False

    frontmatter = (
        "---\ntitle: Tracked Investor Candidates\ndomain: finance\ntype: live\n"
        f"tags: [investors, 13f, tracking, candidates]\nupdated: {date.today().isoformat()}\n---\n\n"
    )
    CANDIDATES_PAGE.write_text(frontmatter + body, encoding="utf-8")
    return True


def refresh_all(dry_run: bool = False) -> list[dict]:
    client = db.get_client()
    try:
        tracked = db.query(client, """
            SELECT investor_id, name, source_ref, last_as_of FROM tracked_investors
            WHERE source_type = '13F' AND active = 1;
        """)
        supers = investors.list_super_investors()
        if not supers:
            return [{"investor_id": t["investor_id"], "error": "Equibles list_super_investors() returned nothing — skipped"}
                    for t in tracked]
        supers_by_cik = {s["cik"].lstrip("0"): s for s in supers}

        results = [refresh_investor(client, t, supers_by_cik, dry_run=dry_run) for t in tracked]

        if not dry_run:
            n = investors.ensure_investor_signals(client)
            if n:
                print(f"[signals] wrote {n} missing investor:<id> signals")
            tracked_ciks = {(t["source_ref"] or "").removeprefix("cik:").lstrip("0") for t in tracked}
            if write_candidates_page(supers, tracked_ciks):
                print(f"[candidates] regenerated {CANDIDATES_PAGE.name}")
        return results
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    for r in refresh_all(dry_run=args.dry_run):
        print(r)
