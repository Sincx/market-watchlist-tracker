"""Replaces portfolio_update.py + wiki-parsing as the data source for
portfolio-management-briefing's Steps 1-3, per Mike's 2026-09-14 request:
"Turso should be the master system now" for Trading Portfolio.

Old flow: Step 1 parsed wiki/finance/trading-portfolio.md for positions;
Steps 2-3 ran portfolio_update.py, which re-fetched prices live via
Polygon/Alpha Vantage/yfinance directly (bypassing Turso entirely) and
recomputed RSI/MACD/SMA/ATR itself. New flow: this script reads positions
straight from Turso `trades`, joins the latest `prices` row (already
computed daily by technicals.py — RSI/MACD/SMA/ATR14 included), and
recomputes the same _signal() classification portfolio_update.py used
(exact thresholds preserved, ported over unchanged).

Known gap, not solved here (deliberately, not silently faked): `cash_eur`
requires reading the wiki's Cash Position section directly — Turso's
`cash_ledger` table exists but was never populated for Trading Portfolio
(a live-task bookkeeping gap that predates this change, documented
separately). This script returns `cash_eur: null`; the calling task must
still read Cash Position from the wiki for that one figure until
cash_ledger tracking is built.

Run standalone: python trading_portfolio_turso_view.py [--pretty]
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone

import db
from fetchers import fetch_fx_rates

PORTFOLIO_ID = "trading-portfolio"
TODAY = date.today().isoformat()


def _gbx_scale(currency: str | None) -> float:
    return 0.01 if currency == "GBX" else 1.0


def _to_eur(amount: float, currency: str, fx_usd: dict) -> float:
    """fx_usd is {currency: USD-per-1-unit}, e.g. {"EUR": 1.156, "GBP": 1.351}.
    GBX (pence) is GBP/100 — same convention used everywhere else in this
    pipeline (schema.sql's v_portfolio_performance, this repo's own
    fred-dashboard fxScale()).
    """
    scale = _gbx_scale(currency)
    ccy = "GBP" if currency == "GBX" else currency
    usd = amount * scale * fx_usd.get(ccy, 1.0 if ccy == "USD" else 0.0)
    return usd / fx_usd.get("EUR", 1.0)


def _signal(rsi, macd_dir, price, sma50, weight_pct):
    """Ported unchanged from portfolio_update.py's _signal() — same
    thresholds, so Step 4's downstream logic (which quotes these Signal
    values) doesn't need to change at all.
    """
    if rsi is None or sma50 is None or price is None:
        return "Watch"
    below = price < sma50
    bearish = macd_dir == "Bearish"
    bullish = macd_dir == "Bullish"

    if rsi < 40 and below and bearish:
        return "Exit"
    if rsi > 70 and weight_pct is not None and weight_pct >= 20:
        return "Trim"
    if 35 <= rsi <= 50 and not below and bullish:
        return "Add"

    neg = sum([rsi < 40, below, bearish])
    if neg >= 1:
        return "Watch"
    return "Hold"


def get_portfolio_view() -> dict:
    client = db.get_client()
    try:
        rows = db.query(client, """
            SELECT t.trade_id, t.ticker, t.exchange, t.instrument_type, t.direction,
                   t.entry_date, t.entry_price, t.shares, t.currency, t.status,
                   t.option_type, t.strike, t.expiry_date, t.premium, t.contracts, t.premium_flow,
                   p.close, p.rsi14, p.macd_signal, p.ma20, p.ma50, p.atr14, p.technical_rating,
                   p.date AS price_date,
                   m.premium_estimate, m.mkt_value AS option_mkt_value, m.unrealized_pnl AS option_unrealized_pnl,
                   m.volatility, m.time_to_expiry_years, m.date AS mark_date
            FROM trades t
            LEFT JOIN (
                SELECT ticker, exchange, close, rsi14, macd_signal, ma20, ma50, atr14,
                       technical_rating, date,
                       ROW_NUMBER() OVER (PARTITION BY ticker, exchange ORDER BY date DESC) rn
                FROM prices
            ) p ON p.ticker = t.ticker AND p.exchange = t.exchange AND p.rn = 1
            LEFT JOIN (
                SELECT trade_id, premium_estimate, mkt_value, unrealized_pnl, volatility, time_to_expiry_years, date,
                       ROW_NUMBER() OVER (PARTITION BY trade_id ORDER BY date DESC) rn
                FROM option_marks
            ) m ON m.trade_id = t.trade_id AND m.rn = 1
            WHERE t.portfolio_id = :pid AND t.status = 'open';
        """, {"pid": PORTFOLIO_ID})
    finally:
        client.close()

    fx_usd = fetch_fx_rates()

    equity_positions = [r for r in rows if r["instrument_type"] == "equity"]
    option_positions = [r for r in rows if r["instrument_type"] == "option"]

    # First pass: eur_value per equity position (needed for weight_pct, so
    # every position needs the portfolio total before its own weight_pct
    # can be computed).
    for r in equity_positions:
        sign = -1 if r["direction"] == "short" else 1
        r["eur_value"] = (_to_eur(r["shares"] * r["close"], r["currency"], fx_usd) * sign
                           if r["shares"] is not None and r["close"] is not None else None)
        # Value in the position's OWN currency (GBX still scaled to GBP,
        # matching the wiki's "Mkt Value" column convention — e.g. KLR
        # shows "£299.20", not "29,920p" or a EUR figure). Distinct from
        # eur_value, which is only used for portfolio-level totals/weights.
        r["native_value"] = (r["shares"] * r["close"] * _gbx_scale(r["currency"]) * sign
                              if r["shares"] is not None and r["close"] is not None else None)
    total_eur = sum(r["eur_value"] for r in equity_positions if r["eur_value"] is not None)

    positions = []
    for r in equity_positions:
        weight_pct = (abs(r["eur_value"]) / total_eur * 100) if r["eur_value"] and total_eur else None
        sign = -1 if r["direction"] == "short" else 1
        pl_pct = (sign * (r["close"] - r["entry_price"]) / r["entry_price"] * 100
                  if r["close"] is not None and r["entry_price"] not in (None, 0) else None)
        stop_loss = (r["entry_price"] - 1.5 * r["atr14"] if r["direction"] == "long" and r["atr14"] is not None and r["entry_price"] is not None
                     else r["entry_price"] + 1.5 * r["atr14"] if r["direction"] == "short" and r["atr14"] is not None and r["entry_price"] is not None
                     else None)
        positions.append({
            "trade_id": r["trade_id"], "ticker": r["ticker"], "exchange": r["exchange"],
            "direction": r["direction"], "entry_date": r["entry_date"], "entry_price": r["entry_price"],
            "shares": r["shares"], "currency": r["currency"],
            "price": r["close"], "price_date": r["price_date"],
            "eur_value": round(r["eur_value"], 2) if r["eur_value"] is not None else None,
            "native_value": round(r["native_value"], 2) if r["native_value"] is not None else None,
            "pl_pct": round(pl_pct, 2) if pl_pct is not None else None,
            "weight_pct": round(weight_pct, 2) if weight_pct is not None else None,
            "rsi14": r["rsi14"], "macd": r["macd_signal"], "sma50": r["ma50"], "atr14": r["atr14"],
            "technical_rating": r["technical_rating"],
            "stop_loss_suggestion": round(stop_loss, 4) if stop_loss is not None else None,
            "signal": _signal(r["rsi14"], r["macd_signal"], r["close"], r["ma50"], weight_pct),
        })

    options = [{
        "trade_id": r["trade_id"], "ticker": r["ticker"], "exchange": r["exchange"],
        "option_type": r["option_type"], "strike": r["strike"], "expiry_date": r["expiry_date"],
        "premium": r["premium"], "contracts": r["contracts"], "shares": r["shares"],
        "premium_flow": r["premium_flow"],
        "underlying_price": r["close"], "underlying_price_date": r["price_date"],
        # Mark-to-market computed daily by options_pricing.py (added
        # 2026-09-14) into `option_marks` — Black-Scholes off the underlying's
        # own tracked price, using historical volatility as an IV proxy (no
        # live options-chain/IV source exists in this pipeline; see that
        # script's docstring). `mark_date` may lag `price_date` if
        # options_pricing.py hasn't run yet today — check it before treating
        # premium_estimate as fresh.
        "premium_estimate": r["premium_estimate"],
        "mkt_value": r["option_mkt_value"],
        "unrealized_pnl": r["option_unrealized_pnl"],
        "volatility_used": r["volatility"], "time_to_expiry_years": r["time_to_expiry_years"],
        "mark_date": r["mark_date"],
        "note": (None if r["premium_estimate"] is not None else
                 "No mark yet for this position — options_pricing.py hasn't priced it "
                 "(check for a 'skipped' entry in its own output, e.g. insufficient "
                 "yfinance history for the underlying)."),
    } for r in option_positions]

    return {
        "date": TODAY, "fx_usd": fx_usd,
        "cash_eur": None,  # known gap — see module docstring
        "total_invested_eur": round(sum(abs(p["eur_value"]) for p in positions if p["eur_value"] is not None), 2),
        "total_eur": round(total_eur, 2),
        "positions": positions,
        "options": options,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--output", help="Write JSON to this file instead of stdout")
    args = parser.parse_args()

    result = get_portfolio_view()
    out_str = json.dumps(result, indent=2 if args.pretty else None)
    if args.output:
        from pathlib import Path
        Path(args.output).write_text(out_str, encoding="utf-8")
    else:
        print(out_str)
