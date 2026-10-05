"""Writes one `signals` row (source='briefing-recommendation') per new-
position idea the morning briefing actually includes in its INVESTMENT
OPPORTUNITIES section — the shared write path both the Phase 3 spec
(2026-09-19, §3.1 point 1 — Pending Trade Ideas' human-gated queue) and
the Recommended Trades spec (§3.3 step D — the mechanical shadow
portfolio's first-mention trigger) explicitly call for building once.

Called by portfolio-management-briefing's Step 6, once per candidate
carried into today's writeup (not once per candidate CONSIDERED — only
the ones actually written up as opportunities). Idempotent per (ticker,
exchange, flagged_date): re-running for the same ticker on the same day
replaces that day's row rather than creating a duplicate.

**Cross-day dedup (added 2026-10-05, real incident):** signal_id includes
TODAY's date, so a ticker that keeps scoring well and gets carried forward
on consecutive days used to get a brand-new signals row — and therefore a
brand-new recommended-trades position via recommended_trades_sync.py's
seed_new_recommendations() — every single day, with no cap. Found live:
IAG had 8 separate open recommended-trades positions and 5 duplicate
Pending Trade Idea cards, all from one idea restated daily for two weeks.
Fixed by skipping the write entirely when the ticker already has an
active (status='new' or 'snoozed') briefing-recommendation signal — the
ORIGINAL pending idea is retained as-is, not refreshed with today's
price/conviction text, so Mike sees one card per idea, not one per day
it keeps looking good. Does not revive an already-expired/rejected idea;
those are allowed to generate a fresh signal (rejection's own
conviction-rises escape hatch in trading_portfolio_candidates.py still
governs whether a rejected ticker can resurface at all).

Run standalone:
  python record_briefing_recommendation.py --ticker MO --exchange US \
    --entry 69.52 --stop 62.00 --size 1000 \
    --conviction "MF#9; Magic Formula pass; corroborated by llm-research" \
    --thesis "Altria: durable FCF, MF#9 pass, RSI healthy at 58..."
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone

import db


def _already_pending(client, ticker: str, exchange: str, signal_id: str) -> bool:
    """True if a DIFFERENT day's signal for this ticker is still pending.
    Excludes `signal_id` itself so a same-day re-run (the original
    idempotent-replace use case) still goes through — only a genuinely
    new day's write gets suppressed.
    """
    rows = db.query(client, """
        SELECT 1 FROM signals
        WHERE source = 'briefing-recommendation' AND ticker = :t AND exchange = :e
          AND status IN ('new', 'snoozed') AND signal_id != :sid
        LIMIT 1;
    """, {"t": ticker, "e": exchange, "sid": signal_id})
    return bool(rows)


def record(ticker: str, exchange: str, entry: float, stop: float, size: float,
           conviction: str, thesis: str, dry_run: bool = False) -> dict:
    today = date.today().isoformat()
    signal_id = f"briefing-rec-{ticker}-{exchange}-{today}"
    detail = json.dumps({"entry": entry, "stop": stop, "size": size, "conviction": conviction, "thesis": thesis})
    row = {
        "signal_id": signal_id, "ticker": ticker, "exchange": exchange,
        "source": "briefing-recommendation", "detail": detail,
        "flagged_date": today, "source_ref": "portfolio-management-briefing",
        "status": "new", "status_updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if dry_run:
        return {"would_write": row}
    client = db.get_client()
    try:
        if _already_pending(client, ticker, exchange, signal_id):
            return {"skipped": "already an active pending idea for this ticker", "ticker": ticker, "exchange": exchange}
        db.upsert(client, "signals", [row])
    finally:
        client.close()
    return {"written": row}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--exchange", required=True)
    parser.add_argument("--entry", type=float, required=True)
    parser.add_argument("--stop", type=float, required=True)
    parser.add_argument("--size", type=float, required=True)
    parser.add_argument("--conviction", required=True)
    parser.add_argument("--thesis", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = record(args.ticker, args.exchange, args.entry, args.stop, args.size,
                     args.conviction, args.thesis, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
