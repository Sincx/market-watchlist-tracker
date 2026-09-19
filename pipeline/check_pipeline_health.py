"""Phase 3 spec (2026-09-19) §3.3 — one-shot pipeline health check for the
morning briefing task to call before writing its output. Reads task_registry
(now written to by technicals.py/crypto_prices.py) and reports any 'daily'
task that's stale (>36h since last recorded run) or whose last run errored.

This is deliberately a thin, separate script rather than folding the check
into portfolio-management-briefing's own Python calls — it needs to run
BEFORE the briefing's other steps so a stale-data warning can sit at the
very top of the output, not buried after everything else already ran.

Prints one line per problem found, or "OK — all tracked tasks healthy." if
none. Exit code 0 always (this reports, it doesn't fail the briefing run).

Run standalone: python check_pipeline_health.py
"""
from __future__ import annotations

from datetime import datetime, timezone

import db

DAILY_STALE_HOURS = 36


def check() -> list[str]:
    client = db.get_client()
    try:
        rows = db.query(client, "SELECT task_id, kind, last_run_at, last_run_status FROM task_registry;")
    finally:
        client.close()

    problems = []
    now = datetime.now(timezone.utc)
    for r in rows:
        task_id, kind, last_run_at, status = r["task_id"], r["kind"], r["last_run_at"], r["last_run_status"]
        is_error = isinstance(status, str) and status.startswith("error")
        hours_since = None
        if last_run_at:
            hours_since = (now - datetime.fromisoformat(last_run_at)).total_seconds() / 3600
        is_stale = kind == "daily" and hours_since is not None and hours_since > DAILY_STALE_HOURS
        if is_error or is_stale:
            age = f"{hours_since:.0f}h ago" if hours_since is not None else "never run"
            reason = status if is_error else f"stale — last successful signal {age}"
            problems.append(f"{task_id}: {reason}")
    return problems


if __name__ == "__main__":
    problems = check()
    if not problems:
        print("OK — all tracked tasks healthy.")
    else:
        for p in problems:
            print(f"STALE/ERROR: {p}")
