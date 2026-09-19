"""Row-level companion to check_pipeline_health.py — Master spec Phase 13
§4/§5. That script answers "did the job run" (task_registry); this answers
"which specific tickers failed" (data_quality), and takes the one bounded,
deterministic action the spec allows: retry each failing row exactly once
via retry_ticker.py, never an LLM-improvised fetch.

For the briefing task to call before writing position signals or
Investment Opportunities — report every non-'ok' row, attempt one retry
each, report what recovered and what's still failing. Flags anything at
3+ consecutive failures for manual review rather than silently retrying
forever.

Exit code always 0 — this reports, it doesn't fail the run (same
convention as check_pipeline_health.py).

Run standalone: python check_data_quality.py [--no-retry]
"""
from __future__ import annotations

import argparse
import subprocess
import sys

import db

MANUAL_REVIEW_THRESHOLD = 3


def check(attempt_retry: bool = True) -> dict:
    client = db.get_client()
    try:
        rows = db.query(client, """
            SELECT ticker, exchange, data_type, status, error_message, consecutive_failures
            FROM data_quality WHERE status != 'ok';
        """)
    finally:
        client.close()

    recovered, still_failing, needs_review = [], [], []
    for r in rows:
        label = f"{r['ticker']} ({r['exchange']}, {r['data_type']})"
        if not attempt_retry:
            still_failing.append(f"{label}: {r['status']} — {r['error_message']}")
            continue

        proc = subprocess.run(
            [sys.executable, "retry_ticker.py", r["ticker"], r["exchange"], r["data_type"]],
            capture_output=True, text=True, cwd=__file__.rsplit("\\", 1)[0] or ".",
        )
        if proc.returncode == 0:
            recovered.append(f"{label}: {proc.stdout.strip().splitlines()[-1] if proc.stdout else 'recovered'}")
        else:
            still_failing.append(f"{label}: {proc.stdout.strip().splitlines()[-1] if proc.stdout else 'still failing'}")
            if r["consecutive_failures"] >= MANUAL_REVIEW_THRESHOLD:
                needs_review.append(f"{label}: {r['consecutive_failures']} consecutive failures — {r['error_message']}")

    return {"recovered": recovered, "still_failing": still_failing, "needs_review": needs_review}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-retry", action="store_true", help="report only, don't attempt retry_ticker.py")
    args = parser.parse_args()

    result = check(attempt_retry=not args.no_retry)
    if not result["recovered"] and not result["still_failing"]:
        print("OK — no data_quality rows in a non-'ok' state.")
    else:
        if result["recovered"]:
            print(f"AUTO-RECOVERED ({len(result['recovered'])}):")
            for line in result["recovered"]:
                print(f"  {line}")
        if result["still_failing"]:
            print(f"STILL FAILING ({len(result['still_failing'])}):")
            for line in result["still_failing"]:
                print(f"  {line}")
        if result["needs_review"]:
            print(f"NEEDS MANUAL REVIEW ({len(result['needs_review'])} at {MANUAL_REVIEW_THRESHOLD}+ consecutive failures):")
            for line in result["needs_review"]:
                print(f"  {line}")
