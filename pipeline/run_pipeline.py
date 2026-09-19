"""Orchestrator for the Turso-only pipeline scripts that don't yet have
their own task_registry-recording wrapper — Master spec Phase 13 §2/§7.
`technicals.py` and `crypto_prices.py` already self-record via this exact
pattern (`_run_and_record()` + `db.record_task_run()`); this brings
`fundamentals_refresh.py`, `screen.py`, `options_pricing.py`, and
`etf_returns.py` (both ETF groups) up to the same standard.

This is NOT a single "run everything" job — the four steps have genuinely
different cadences (fundamentals monthly, screen/options daily, ETF
weekly), so each gets its own Windows Task Scheduler entry calling this
file with a different --step, sharing one task_registry-recording
implementation instead of four copies of the same wrapper boilerplate.
It is also NOT a replacement for pipeline.py's still-live run_eu()/run_us()
old-path orchestration (Phase 14 retires that separately, once Sheets is
confirmed safe to decommission).

Run standalone:
  python run_pipeline.py --step fundamentals [--dry-run] [--limit N] [--exchange US|UK|EU]
  python run_pipeline.py --step screen [--dry-run]
  python run_pipeline.py --step options [--dry-run]
  python run_pipeline.py --step etf [--dry-run]
"""
from __future__ import annotations

import argparse

import db

_STEP_META = {
    "fundamentals": dict(
        task_id="fundamentals-refresh", kind="monthly", schedule_cron="0 8 1 * *",
        description="Monthly — full-universe fundamentals refresh via fundamentals_refresh.py",
        entry_point="run_pipeline.py --step fundamentals",
    ),
    "screen": dict(
        task_id="magic-formula-screen", kind="daily", schedule_cron="0 9 * * *",
        description="Daily — Magic Formula screen over the full universe via screen.py",
        entry_point="run_pipeline.py --step screen",
    ),
    "options": dict(
        task_id="options-mark-to-market", kind="daily", schedule_cron="30 7 * * *",
        description="Daily — Black-Scholes mark-to-market for open options via options_pricing.py",
        entry_point="run_pipeline.py --step options",
    ),
    "etf": dict(
        task_id="etf-returns-refresh", kind="weekly", schedule_cron="0 8 * * 1",
        description="Weekly — quarterly ETF returns/P-E for style+sector groups via etf_returns.py",
        entry_point="run_pipeline.py --step etf",
    ),
}


def _run_fundamentals(dry_run: bool, limit: int | None, exchange: str | None) -> str:
    from fundamentals_refresh import refresh_fundamentals
    refresh_fundamentals(dry_run=dry_run, limit=limit, exchange_filter=exchange)
    return "success"


def _run_screen(dry_run: bool) -> str:
    from screen import run_screen
    run_screen(dry_run=dry_run)
    return "success"


def _run_options(dry_run: bool) -> str:
    from options_pricing import price_options
    results = price_options(dry_run=dry_run)
    skipped = sum(1 for r in results if "skipped" in r)
    return f"success: {len(results) - skipped}/{len(results)} priced"


def _run_etf(dry_run: bool) -> str:
    from etf_returns import _report
    import data_quality as dq
    counts = {}
    for group in ("style", "sector"):
        report = _report(group, with_pe=(group == "sector"))
        counts[group] = len(report)
        if not dry_run:
            client = db.get_client()
            try:
                dq_rows = [{"ticker": t, "exchange": "US", "data_type": "etf_returns",
                            "status": "ok" if e.get("latest_close") is not None else "error",
                            "error": None if e.get("latest_close") is not None else "no quarterly returns computed"}
                           for t, e in report.items()]
                dq.record_batch(client, dq_rows)
            finally:
                client.close()
    return f"success: style={counts['style']}, sector={counts['sector']}"


_RUNNERS = {
    "fundamentals": lambda a: _run_fundamentals(a.dry_run, a.limit, a.exchange),
    "screen": lambda a: _run_screen(a.dry_run),
    "options": lambda a: _run_options(a.dry_run),
    "etf": lambda a: _run_etf(a.dry_run),
}


def run_step(step: str, args: argparse.Namespace) -> None:
    meta = _STEP_META[step]
    if args.dry_run:
        _RUNNERS[step](args)
        return
    try:
        status = _RUNNERS[step](args)
    except Exception as e:
        c = db.get_client()
        try:
            db.record_task_run(c, meta["task_id"], f"error: {type(e).__name__}: {e}", **{k: v for k, v in meta.items() if k != "task_id"})
        finally:
            c.close()
        raise
    else:
        c = db.get_client()
        try:
            db.record_task_run(c, meta["task_id"], status, **{k: v for k, v in meta.items() if k != "task_id"})
        finally:
            c.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--step", required=True, choices=list(_STEP_META.keys()))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="fundamentals step only")
    parser.add_argument("--exchange", choices=["US", "UK", "EU"], default=None, help="fundamentals step only")
    args = parser.parse_args()
    run_step(args.step, args)
