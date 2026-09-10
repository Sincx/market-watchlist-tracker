"""Phase 8: one-time Burry backfill into Turso's `tracked_investors`/
`investor_positions` (spec §10). Per the spec, this is a prose-driven,
one-off LLM-assisted extraction, not a deterministic parser — the data
below was read and structured by hand from
wiki/finance/michael-burry/trading-posts.md's "Running Position Tracker"
table (2026-09-10), which is itself already a reconstructed, semi-structured
summary (more current than config.py's BURRY_POSITIONS — 11 shorts vs 7,
20 longs vs 15, since config.py is a stale partial snapshot per its own
docstring). Reviewed before being committed, per the spec's own workflow.

Scope decisions made during extraction:
  - Excluded SOXX and QQQ shorts (index/ETF options, not single-stock
    positions) — matches config.py's own established convention: "ETF
    shorts (not tracked as individual rows): SOXX, QQQ."
  - Excluded GOOGL, VLO, MO, JPM — the wiki page explicitly states these
    are legacy DRIP-only personal-account holdings Burry himself excludes
    from the tracked portfolio ("none are at prices I would buy today").
  - Where the source gives multiple add dates/prices for one ticker (e.g.
    MU: Jul 24, Jul 30 ~$880, Aug 12 ~$924, Aug 13 ~$1,000), this is
    collapsed to ONE investor_positions row using the first concrete
    date/price mentioned — matching the shadow-portfolio's own "one $1,000
    position per ticker" sizing convention (spec §10 step 4), not modeling
    Burry's own scaling in/out.
  - Where the source's "First Mentioned" is vague ("Pre-Jul 2026"), the
    earliest CONCRETE date mentioned anywhere in that ticker's row is used
    instead, since disclosed_date is part of the primary key and can't be
    left NULL — flagged per-row below where this applies.
  - entry_price_hint left None wherever the source itself says no price was
    disclosed (JD, BBW, HCA, DKNG's true first mention, FLUT) — same
    "don't fabricate" principle as Phase 7b/7c, not guessed.

Run standalone: python burry_backfill.py [--dry-run]
"""
from __future__ import annotations

import argparse

import db

INVESTOR_ID = "burry"
SOURCE_REF = "finance/michael-burry/trading-posts.md"

# (ticker, exchange, direction, disclosed_date, entry_price_hint, status, note)
POSITIONS = [
    # ── Shorts ──────────────────────────────────────────────────────────────
    ("NVDA", "US", "short", "2026-07-17", None, "open", "options + short; multiple strikes, no single equity price"),
    ("MU",   "US", "short", "2026-07-24", None, "open", "first mention no price; later adds ~$880/$924/$1000"),
    ("CAT",  "US", "short", "2026-07-24", None, "open", "trimmed 25% Aug 13, re-shorted Aug 26 — net still open"),
    ("TSLA", "US", "short", "2026-07-24", None, "closed", "closed Aug 13, 2026 (covered for a gain)"),
    ("AMAT", "US", "short", "2026-08-04", None, "closed", "closed Aug 13, 2026 (covered for a gain)"),
    ("PLTR", "US", "short", "2026-08-10", None, "open", "options-based (puts); re-entered Mar 2027 puts low-mid $100s"),
    ("ORCL", "US", "short", "2026-08-06", 144.63, "open", "outright short; puts closed Aug 4 for a profit"),
    ("NBIS", "US", "short", "2026-08-06", 211.77, "open", None),
    ("CRWV", "US", "short", "2026-08-20", None, "open", "new short, first disclosed ~Aug 18-20"),
    # ── Longs ───────────────────────────────────────────────────────────────
    ("BBW",  "US", "long", "2026-08-27", None, "open", "pre-existing position, first disclosed Aug 27; entry price not disclosed"),
    ("BIRK", "US", "long", "2026-08-20", 35.0, "open", "full 5.2% position by mid-Aug, mid-$30s"),
    ("SFM",  "US", "long", "2026-08-20", 75.0, "open", "full 5.2% position, high $70s"),
    ("0700", "HK", "long", "2026-07-23", 448.60, "open", "Tencent"),
    ("3690", "HK", "long", "2026-07-23", 87.60, "open", "Meituan"),
    ("TPW",  "AU", "long", "2026-07-23", 5.00, "open", "Temple & Webster; grew to ~9% by Aug 18-20"),
    ("FLUT", "US", "long", "2026-08-05", None, "open", "made full position Aug 5, 2026; no exact price disclosed"),
    ("DKNG", "US", "long", "2026-07-08", None, "open", "first mention (Short Thoughts post) had no price; added $25s Jul 17"),
    ("MOH",  "US", "long", "2026-07-24", 197.0, "open", None),
    ("ZTS",  "US", "long", "2026-07-30", 76.0, "open", "repeatedly averaged down through Aug, avg cost $83 by Aug 12"),
    ("LULU", "US", "long", "2026-07-30", 118.0, "open", "now largest position (17.4%) after Aug 21 add; crashed ~-20% post-earnings Sep 3-4"),
    ("HCA",  "US", "long", "2026-06-08", None, "open", "low-normal position size; no exact price disclosed"),
    ("ADBE", "US", "long", "2026-06-12", 199.59, "open", "full position; one of two largest holdings with JD"),
    ("BABA", "US", "long", "2026-06-12", 111.90, "open", "full position"),
    ("PYPL", "US", "long", "2026-06-12", 40.98, "open", "trimmed to 4% Aug 13"),
    ("VEEV", "US", "long", "2026-06-12", 159.05, "open", "trimmed to 4.8% Aug 13"),
    ("FISV", "US", "long", "2026-08-06", 48.0, "open", "avg cost, first disclosed Aug 6 but predates the post"),
    ("MELI", "US", "long", "2026-08-06", 1611.0, "open", "avg cost, first disclosed Aug 6 but predates the post; now 12%"),
    ("JD",   "US", "long", "2026-08-06", None, "open", "no entry price disclosed; one of the two largest positions"),
    ("FMCC", "US", "long", "2026-08-07", 5.43, "open", "Freddie Mac, GSE reform thesis"),
]


def run(dry_run: bool = False) -> None:
    investor_row = {
        "investor_id": INVESTOR_ID, "name": "Michael Burry",
        "source_type": "substack", "source_ref": "finance/michael-burry/",
    }
    position_rows = [{
        "investor_id": INVESTOR_ID, "ticker": t, "exchange": ex, "direction": d,
        "disclosed_date": date, "entry_price_hint": price, "status": status,
        "source_ref": SOURCE_REF,
    } for (t, ex, d, date, price, status, _note) in POSITIONS]

    print(f"tracked_investors: 1 row (burry)")
    print(f"investor_positions: {len(position_rows)} rows "
          f"({sum(1 for p in POSITIONS if p[2]=='short')} shorts, {sum(1 for p in POSITIONS if p[2]=='long')} longs, "
          f"{sum(1 for p in POSITIONS if p[5]=='closed')} already closed)")
    with_price = sum(1 for p in POSITIONS if p[4] is not None)
    print(f"  {with_price}/{len(POSITIONS)} have an entry_price_hint; {len(POSITIONS)-with_price} left NULL (no price disclosed in source)")

    if dry_run:
        for t, ex, d, date, price, status, note in POSITIONS:
            print(f"  {t:6s} {ex:2s} {d:5s} {date} price={price} status={status}" + (f"  # {note}" if note else ""))
        print("  ... (dry run, nothing written)")
        return

    client = db.get_client()
    try:
        db.upsert(client, "tracked_investors", [investor_row])
        n = db.upsert(client, "investor_positions", position_rows)
        print(f"Upserted 1 tracked_investors row, {n} investor_positions rows.")
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
