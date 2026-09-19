"""Magic Formula screening engine — generalized over the full Turso universe
(spec §8). Same ranking algorithm as sheets.py's compute_and_write_mf_ranks
(unchanged, per spec): rank by Earnings Yield (EBIT/EV %) + ROIC %, excluding
Financials/Banks/Insurance/Utilities/Real Estate per Greenblatt. What's new
here: it runs over however many tickers have fundamentals data (not the old
~183-ticker hardcoded universe), writes to `screen_results` (append-only,
one row per run_date so history is preserved), and writes a `signals` row
(source='magic-formula-pass') for every ticker that newly passes the
MF_THRESHOLDS gate — i.e. passed today but didn't pass on the previous run,
which is what feeds the Screener view and the strategy-feedback-loop (§11)
without re-flagging a ticker that's been passing for months.

Run standalone: python screen.py [--dry-run]
"""
from __future__ import annotations

import argparse
import uuid
from datetime import date

import data_quality as dq
import db
from config import MF_THRESHOLDS

TODAY = date.today().isoformat()

# "Financials" (no "Services") added 2026-09-10 — found via candidates.py's
# F/G small-cap screens letting UK investment trusts through (JUP, LWDB, ...
# all sector "Financials", not "Financial Services"). The original substring
# check (`excl.lower() in sector.lower()`) only catches an exclude term that
# is a SUBSTRING of the row's sector — "financial services" is not a
# substring of "financials", so rows using the shorter GICS label slipped
# through undetected in every screen.py run before this fix, not just the
# new candidates.py queries.
_MF_EXCLUDE_SECTORS = {"Financial Services", "Financials", "Banks", "Insurance", "Utilities", "Real Estate"}


def _latest_fundamentals(client) -> list[dict]:
    """One row per active (ticker, exchange): its most recent fundamentals
    snapshot, joined with universe for exchange/active filtering.
    """
    return db.query(client, """
        SELECT f.ticker, f.exchange, f.pe, f.fwd_pe, f.earnings_yield, f.roic, f.roe, f.ev_ebit, f.sector
        FROM fundamentals f
        JOIN universe u ON u.ticker = f.ticker AND u.exchange = f.exchange
        WHERE u.active = 1
          AND f.as_of_date = (
              SELECT MAX(f2.as_of_date) FROM fundamentals f2
              WHERE f2.ticker = f.ticker AND f2.exchange = f.exchange
          );
    """)


def compute_ranks(fund_rows: list[dict]) -> list[dict]:
    """Same algorithm as sheets.py's compute_and_write_mf_ranks: filter to
    eligible (non-excluded-sector, EY>0, ROIC>0) tickers, rank each metric
    (rank 1 = best), combine, re-rank the combined score. Returns one dict
    per eligible ticker with ey_rank/roic_rank/mf_rank/passes_thresholds.
    """
    eligible = []
    for r in fund_rows:
        sector = r.get("sector") or ""
        if any(excl.lower() in sector.lower() for excl in _MF_EXCLUDE_SECTORS):
            continue
        ey = r.get("earnings_yield")
        if ey is None:
            pe = r.get("pe")
            if pe and pe > 0:
                ey = round(100.0 / pe, 2)
        roic = r.get("roic")
        if ey is None or ey <= 0 or roic is None or roic <= 0:
            continue
        eligible.append({**r, "earnings_yield": ey, "roic": roic})

    if len(eligible) < 2:
        return []

    ey_sorted = sorted(eligible, key=lambda x: x["earnings_yield"], reverse=True)
    ey_ranks = {(r["ticker"], r["exchange"]): i + 1 for i, r in enumerate(ey_sorted)}

    roic_sorted = sorted(eligible, key=lambda x: x["roic"], reverse=True)
    roic_ranks = {(r["ticker"], r["exchange"]): i + 1 for i, r in enumerate(roic_sorted)}

    for r in eligible:
        key = (r["ticker"], r["exchange"])
        r["ey_rank"] = ey_ranks[key]
        r["roic_rank"] = roic_ranks[key]
        r["_combined"] = ey_ranks[key] + roic_ranks[key]

    mf_sorted = sorted(eligible, key=lambda x: x["_combined"])
    for i, r in enumerate(mf_sorted):
        r["mf_rank"] = i + 1
        r["passes_thresholds"] = 1 if (
            r.get("ev_ebit") is not None and r["ev_ebit"] <= MF_THRESHOLDS["ev_ebit_max"]
            and r.get("fwd_pe") is not None and r["fwd_pe"] <= MF_THRESHOLDS["fwd_pe_max"]
            and r["roic"] >= MF_THRESHOLDS["roic_min"]
            and r.get("roe") is not None and r["roe"] >= MF_THRESHOLDS["roe_min"]
        ) else 0

    return mf_sorted


def _previously_passing(client) -> set[tuple[str, str]]:
    rows = db.query(client, """
        SELECT ticker, exchange FROM screen_results
        WHERE run_date = (SELECT MAX(run_date) FROM screen_results WHERE run_date < :today)
          AND passes_thresholds = 1;
    """, {"today": TODAY})
    return {(r["ticker"], r["exchange"]) for r in rows}


def run_screen(dry_run: bool = False) -> None:
    client = db.get_client()
    try:
        fund_rows = _latest_fundamentals(client)
        print(f"Fundamentals rows available: {len(fund_rows)}")

        ranked = compute_ranks(fund_rows)
        print(f"Eligible (non-excluded-sector, EY>0, ROIC>0): {len(ranked)}")

        screen_rows = [{
            "run_date": TODAY, "ticker": r["ticker"], "exchange": r["exchange"],
            "earnings_yield": r["earnings_yield"], "roic": r["roic"],
            "ey_rank": r["ey_rank"], "roic_rank": r["roic_rank"],
            "mf_rank": r["mf_rank"], "passes_thresholds": r["passes_thresholds"],
        } for r in ranked]

        passing_now = {(r["ticker"], r["exchange"]) for r in ranked if r["passes_thresholds"]}
        passing_before = _previously_passing(client) if not dry_run else set()
        newly_passing = passing_now - passing_before

        signal_rows = [{
            "signal_id": str(uuid.uuid4()), "ticker": t, "exchange": e,
            "source": "magic-formula-pass",
            "detail": None, "flagged_date": TODAY, "source_ref": None,
        } for (t, e) in newly_passing]

        print(f"Passing thresholds today: {len(passing_now)} | newly passing (new signals): {len(newly_passing)}")

        if dry_run:
            top5 = sorted(ranked, key=lambda x: x["mf_rank"])[:5]
            for r in top5:
                print(f"  #{r['mf_rank']} {r['ticker']} ({r['exchange']}) EY={r['earnings_yield']}% ROIC={r['roic']}% pass={r['passes_thresholds']}")
            print("  ... (dry run, nothing written)")
            return

        # screen_results is append-only ACROSS days (one snapshot per
        # run_date, history preserved), but re-running on the SAME day must
        # be idempotent — upsert alone never removes a ticker that was
        # eligible on an earlier run today but isn't anymore (found via this
        # exact bug: re-running after the sector-exclusion fix left 6 stale
        # "Financial Services" rows from the pre-fix run sitting in Turso,
        # since they were never in the second run's write batch for upsert
        # to replace). Clear today's own rows before writing the fresh set.
        client.execute("DELETE FROM screen_results WHERE run_date = :d;", {"d": TODAY})
        n1 = db.upsert(client, "screen_results", screen_rows)
        print(f"Upserted {n1} rows into screen_results.")
        if signal_rows:
            n2 = db.upsert(client, "signals", signal_rows)
            print(f"Upserted {n2} rows into signals (magic-formula-pass).")

        # 'ok' here means "screen.py successfully derived a real outcome for
        # this ticker" — ranked-and-passing, ranked-not-passing, or correctly
        # excluded by sector/non-positive EY-ROIC are all legitimate
        # categorizations, not data-quality failures. A ticker with no
        # fundamentals row at all never reaches fund_rows in the first place
        # (data_type='fundamentals' already covers that gap) — recording it
        # again here would be redundant, not additive.
        dq_rows = [{"ticker": r["ticker"], "exchange": r["exchange"], "data_type": "screen", "status": "ok"}
                   for r in fund_rows]
        dq.record_batch(client, dq_rows)
        print(f"Recorded data_quality for {len(dq_rows)} tickers.")
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run_screen(dry_run=args.dry_run)
