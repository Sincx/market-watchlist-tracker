"""Phase 9 / spec §11a: pure-SQL strategy & portfolio performance review —
the deterministic half of the weekly strategy-feedback-loop. Reads
v_strategy_performance and v_portfolio_performance across every portfolio
(real, paper, shadow) and produces a structured report. The LLM's job on
top of this (in the strategy-feedback-loop scheduled task) is judgment
only: for each underperforming strategy, is it bad luck (small sample) or
a real edge problem — written to model-portfolio-management.md's "Strategy
Performance Notes" section. This script does not write anything itself and
never makes that judgment call — it only aggregates.

Run standalone: python strategy_review.py
"""
from __future__ import annotations

import json

import db

MIN_SAMPLE_SIZE = 3  # per instructions.md's own threshold for daily-paper-trader's Step 3G


def build_report() -> dict:
    client = db.get_client()
    try:
        strategy_rows = db.query(client, "SELECT * FROM v_strategy_performance ORDER BY portfolio_id, trades_closed DESC;")
        portfolio_rows = db.query(client, """
            SELECT p.portfolio_id, p.name, p.kind, p.mirrors_investor_id,
                   vp.open_positions, vp.closed_positions, vp.open_cost_basis
            FROM portfolios p
            LEFT JOIN v_portfolio_performance vp ON vp.portfolio_id = p.portfolio_id
            WHERE p.active = 1
            ORDER BY p.kind, p.portfolio_id;
        """)
    finally:
        client.close()

    strategies = []
    for r in strategy_rows:
        if r["strategy_id"] is None:
            continue  # shadow portfolios have no strategy_id — they're not "a strategy", see portfolio-level comparison instead
        win_rate = round(r["wins"] / r["trades_closed"], 3) if r["trades_closed"] else None
        # v_strategy_performance.avg_return_pct is actually a raw fraction
        # despite its name (the view's SQL has no *100) — e.g. -0.093 means
        # -9.3%, not -0.093%. Multiplying here so this report's numbers mean
        # what they say; a misread here would directly corrupt the LLM's
        # bad-luck-vs-real-edge-problem judgment, which is the whole point
        # of this report.
        avg_return_pct = round(r["avg_return_pct"] * 100, 2) if r["avg_return_pct"] is not None else None
        strategies.append({
            "portfolio_id": r["portfolio_id"], "strategy_id": r["strategy_id"],
            "trades_total": r["trades_total"], "trades_closed": r["trades_closed"],
            "wins": r["wins"], "win_rate": win_rate,
            "avg_return_pct": avg_return_pct,
            "avg_holding_days": round(r["avg_holding_days"], 1) if r["avg_holding_days"] is not None else None,
            "sample_size_adequate": r["trades_closed"] >= MIN_SAMPLE_SIZE,
        })

    # Portfolio-average win rate per portfolio_id, for judging a strategy
    # against ITS OWN portfolio's baseline (an A-G strategy shouldn't be
    # compared against the Burry shadow's win rate, they're different games).
    by_portfolio: dict[str, list[dict]] = {}
    for s in strategies:
        by_portfolio.setdefault(s["portfolio_id"], []).append(s)
    portfolio_avg_win_rate = {}
    for pid, rows in by_portfolio.items():
        adequate = [r["win_rate"] for r in rows if r["sample_size_adequate"] and r["win_rate"] is not None]
        portfolio_avg_win_rate[pid] = round(sum(adequate) / len(adequate), 3) if adequate else None

    for s in strategies:
        avg = portfolio_avg_win_rate.get(s["portfolio_id"])
        if s["sample_size_adequate"] and avg is not None and s["win_rate"] is not None:
            delta = s["win_rate"] - avg
            s["vs_portfolio_avg"] = "above" if delta > 0.1 else ("below" if delta < -0.1 else "in-line")
        else:
            s["vs_portfolio_avg"] = "insufficient_sample"

    return {
        "min_sample_size_for_judgment": MIN_SAMPLE_SIZE,
        "portfolios": portfolio_rows,
        "strategies": strategies,
        "portfolio_avg_win_rate": portfolio_avg_win_rate,
    }


def signals_delta(days: int = 7) -> list[dict]:
    """Spec §11b's "Magic Formula screen deltas" piece — new magic-formula-
    pass signals in the trailing `days`, for the strategy-feedback-loop's
    Pending Strategy Reviews half (wiki-research -> proposed rule changes).
    Separate from build_report()'s 11a aggregation on purpose — these feed
    two different sections (Strategy Performance Notes vs Pending Strategy
    Reviews) so the two kinds of feedback don't get conflated, per spec.
    """
    client = db.get_client()
    try:
        rows = db.query(client, """
            SELECT s.ticker, s.exchange, s.flagged_date, u.sector, f.pe, f.earnings_yield, f.roic
            FROM signals s
            LEFT JOIN universe u ON u.ticker = s.ticker AND u.exchange = s.exchange
            LEFT JOIN fundamentals f ON f.ticker = s.ticker AND f.exchange = s.exchange
              AND f.as_of_date = (SELECT MAX(as_of_date) FROM fundamentals f2 WHERE f2.ticker=s.ticker AND f2.exchange=s.exchange)
            WHERE s.source = 'magic-formula-pass'
              AND s.flagged_date >= date('now', :days_ago)
            ORDER BY s.flagged_date DESC;
        """, {"days_ago": f"-{days} days"})
    finally:
        client.close()
    return rows


if __name__ == "__main__":
    report = build_report()
    report["new_magic_formula_signals_7d"] = signals_delta(7)
    print(json.dumps(report, indent=2, default=str))
