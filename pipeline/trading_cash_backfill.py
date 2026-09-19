"""One-time backfill of Turso `cash_ledger` for the Trading Portfolio, from
the hand-maintained Movement Log in trading-portfolio.md's Cash Position
table — the only complete, dated record of every cash flow since the
portfolio's actual inception (parsing this beats trying to re-derive flows
from `trades`, since `trades.entry_date` was never populated for this
portfolio's Phase 7c backfill — a documented pre-existing gap, not fixed
here).

Built for Phase 3 spec (2026-09-19) §3.1's Approve-flow cash guardrail,
which needs a real, live-queryable cash balance (v_portfolio_cash_balance)
rather than a manual confirmation field — Mike's explicit call over the
simpler alternative.

Parses one line: "€3,000 start + €517 IQV trim (07-09) + ... "
  - "€3,000 start" has no date — treated as a 'deposit' one day before the
    first dated entry (the log's own earliest movement is 07-09, so this
    lands on 2026-07-08; the real account-open date isn't recorded anywhere,
    this is a reasonable placeholder, not a fabricated fact — flagged in
    the written row's note).
  - Every other entry already carries its own +/− sign in the source text
    (a realized short-close LOSS is written as "− €176 ... (08-12)", a gain
    as "+ €50 ... (09-08)") — used directly as the ledger amount's sign,
    not re-derived from keyword matching.
  - entry_type inferred from the description text: 'buy'/'add' -> 'buy',
    'trim'/'exit' -> 'sell', 'short ... close' -> 'short_close_pnl',
    'put' -> 'option_premium', 'deposit' -> 'deposit'. Matches the
    short-cash convention already documented in schema.sql (opening a
    short writes no ledger row; only the close's realized P&L does).

Idempotent: entry_id is a stable hash of (date, description), so re-running
after a wiki edit only touches what actually changed (INSERT OR REPLACE).

Run standalone: python trading_cash_backfill.py [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import re
from datetime import date, timedelta

import db

WIKI_PATH = r"C:\Users\Mike\Documents\Fred\Fred\wiki\finance\trading-portfolio.md"
PORTFOLIO_ID = "trading-portfolio"
YEAR = 2026

_ENTRY_RE = re.compile(
    r"([+\u2212-])\s*\u20ac([\d,]+(?:\.\d+)?)\s+([^()]+?)(?:\s*\((\d{2})-(\d{2})\))?(?=\s*[+\u2212-]\s*\u20ac|$)"
)


def _entry_type(desc: str) -> str:
    d = desc.lower()
    if "deposit" in d or "start" in d:
        return "deposit"
    if "short" in d and "close" in d:
        return "short_close_pnl"
    if "put" in d or "call" in d:
        return "option_premium"
    if "buy" in d or "add" in d:
        return "buy"
    if "trim" in d or "exit" in d:
        return "sell"
    return "fee"  # unrecognized — surfaced for manual review, not guessed silently wrong


def parse_movement_log(text: str) -> list[dict]:
    # The very first entry ("€3,000 start") has no leading +/- sign since
    # it's first in the sequence, not a delta off a prior running total —
    # an implicit positive. Without this, the regex (which requires a sign
    # to match at all) silently drops it entirely, understating the parsed
    # total by exactly that amount (caught 2026-09-19 via the printed
    # running-balance check not matching the wiki's own stated €4,196.60).
    text = text.strip()
    if text.startswith("€"):
        text = "+ " + text

    entries = []
    first_date: date | None = None
    matches = list(_ENTRY_RE.finditer(text))
    for m in matches:
        sign, amount_str, desc, mm, dd = m.groups()
        amount = float(amount_str.replace(",", ""))
        signed = -amount if sign in ("\u2212", "-") else amount
        desc = desc.strip().rstrip(",").strip()
        if mm and dd:
            d = date(YEAR, int(mm), int(dd))
            if first_date is None or d < first_date:
                first_date = d
            entries.append({"date": d, "desc": desc, "amount": signed})
        else:
            entries.append({"date": None, "desc": desc, "amount": signed})

    # Resolve the undated "start" entry now that we know the earliest real date.
    for e in entries:
        if e["date"] is None:
            e["date"] = (first_date - timedelta(days=1)) if first_date else date(YEAR, 7, 1)
    return entries


def build_rows(entries: list[dict]) -> list[dict]:
    rows = []
    for e in entries:
        entry_type = _entry_type(e["desc"])
        note = e["desc"] if entry_type != "deposit" else f"{e['desc']} (backfilled from wiki Movement Log; date is a placeholder — real account-open date not recorded)" if "start" in e["desc"].lower() else e["desc"]
        entry_id = "tp-cash-" + hashlib.sha1(f"{e['date']}|{e['desc']}|{e['amount']}".encode()).hexdigest()[:16]
        rows.append({
            "entry_id": entry_id,
            "portfolio_id": PORTFOLIO_ID,
            "entry_date": e["date"].isoformat(),
            "amount": round(e["amount"], 2),
            "entry_type": entry_type,
            "trade_id": None,
            "note": note,
        })
    return rows


def run(dry_run: bool = False) -> None:
    with open(WIKI_PATH, encoding="utf-8") as f:
        content = f.read()
    m = re.search(r"\|\s*EUR\s*\|\s*\*\*([^*]+)\*\*\s*\|\s*(.+?)\s*\|\s*\n", content)
    if not m:
        raise RuntimeError("Could not find the Cash Position Movement Log row in trading-portfolio.md")
    stated_balance = float(m.group(1).replace("€", "").replace(",", "").strip())
    entries = parse_movement_log(m.group(2))
    rows = build_rows(entries)

    print(f"Parsed {len(rows)} cash movements. Running balance check:")
    running = 0.0
    for r in sorted(rows, key=lambda x: x["entry_date"]):
        running += r["amount"]
        print(f"  {r['entry_date']}  {r['amount']:+8.2f}  {r['entry_type']:16s}  {r['note'][:60]}")
    delta = running - stated_balance
    print(f"Computed final balance: EUR {running:,.2f}  |  wiki's stated balance: EUR {stated_balance:,.2f}  |  delta: {delta:+.2f}")
    if abs(delta) > 10:
        print(f"WARNING: delta exceeds EUR 10 — this is larger than expected whole-euro display rounding "
              f"in the log (each entry appears rounded to the nearest euro vs. the wiki's own more precise "
              f"prose footnotes, e.g. -EUR176.39 shown as -EUR176 in the log). Investigate before trusting this backfill.")

    unrecognized = [r for r in rows if r["entry_type"] == "fee" and "fee" not in r["note"].lower()]
    if unrecognized:
        print(f"\n{len(unrecognized)} entries had an unrecognized description (mapped to 'fee' as a fallback — review):")
        for r in unrecognized:
            print(f"  {r['entry_date']}  {r['note']}")

    if dry_run:
        print("\n(dry run, nothing written)")
        return

    client = db.get_client()
    try:
        n = db.upsert(client, "cash_ledger", rows)
        print(f"\nUpserted {n} rows into cash_ledger.")
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
