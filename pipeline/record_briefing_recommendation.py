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
