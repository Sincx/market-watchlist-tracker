"""Daily options mark-to-market — the piece Mike asked for 2026-09-14 after
noticing the dashboard's Options table showed "—" for Value/P&L everywhere:
"The options portion of the trade-portfolio isnt holding the actual value or
profit/loss on the options - can you add the functions to calculate that and
to store it in Turso."

Every option position recorded so far (Phase 7c backfill: ORCL/PLTR long
puts) has entry_price/current-premium fields but no ongoing repricing —
portfolio-management-briefing's Notes column carried a hand-written
Black-Scholes estimate instead, redone by an LLM each run from whatever
implied vol it could reverse-engineer out of a live quote. This script makes
that deterministic and Turso-resident instead:

  1. Black-Scholes fair value (pure Python, no scipy — math.erf for the
     normal CDF), using the underlying's own tracked `prices.close`.
  2. Volatility input: there is no live options-chain/IV data source
     anywhere in this pipeline, so this uses HISTORICAL volatility of the
     underlying (annualized stdev of trailing daily log returns) as a proxy
     for implied vol. This is a real, named approximation, not a silent one
     — realized vol usually runs a bit below true IV (no vol-risk-premium
     priced in), so this will tend to slightly UNDERVALUE options relative
     to what a live options chain would show. Fetched live via
     fetchers.fetch_yfinance() (same helper the rest of this pipeline uses),
     NOT from Turso's own `prices` table — Turso's accumulated price history
     is only as old as this whole migration (started 2026-09-10, so at most
     a handful of calendar-day rows per ticker right now, nowhere near
     enough for a variance estimate). This mirrors technicals.py's own
     design: it re-fetches a full year via yfinance every run for its
     rolling indicators rather than depending on Turso's day-by-day
     accumulation, for exactly the same reason.
  3. Risk-free rate: RISK_FREE_RATE below is a constant, not fetched live —
     Black-Scholes is not very sensitive to r at these tenors (weeks to ~18
     months) relative to the volatility-proxy limitation above, so a fetched
     treasury yield wouldn't meaningfully change the estimate's accuracy;
     not worth the extra API dependency for this. Revisit if a specific
     position's vega/rho profile ever makes this matter.

Run standalone:
  python options_pricing.py --dry-run   # prints what would be written
  python options_pricing.py             # writes today's marks to Turso
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import date

import db
from fetchers import fetch_yfinance

TODAY = date.today().isoformat()
RISK_FREE_RATE = 0.04   # documented constant — see module docstring point 3
MIN_VOL_POINTS = 20     # minimum daily log returns required to trust a historical-vol estimate
VOL_LOOKBACK_DAYS = 120 # calendar days of yfinance history fetched (~80 trading days)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes_price(S: float, K: float, T: float, r: float, sigma: float, option_type: str) -> float:
    """European option fair value. At/after expiry (T<=0) or degenerate
    inputs, falls back to intrinsic value rather than dividing by zero.
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(0.0, (K - S) if option_type == "put" else (S - K))
    sqrt_t = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    if option_type == "put":
        return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)
    return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)


def historical_volatility(closes: list[float], trading_days_per_year: int = 252,
                           min_points: int = MIN_VOL_POINTS) -> float | None:
    """Annualized stdev of daily log returns. Returns None (not a guess)
    when there isn't enough price history to trust the estimate.
    """
    pairs = [(closes[i - 1], closes[i]) for i in range(1, len(closes))
             if closes[i - 1] and closes[i] and closes[i - 1] > 0 and closes[i] > 0]
    if len(pairs) < min_points:
        return None
    log_returns = [math.log(b / a) for a, b in pairs]
    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    return math.sqrt(variance) * math.sqrt(trading_days_per_year)


def _get_open_options(client) -> list[dict]:
    return db.query(client, """
        SELECT trade_id, ticker, exchange, option_type, strike, expiry_date,
               premium, contracts, shares, currency, premium_flow
        FROM trades
        WHERE instrument_type = 'option' AND status = 'open';
    """)


def _get_latest_underlying(client, ticker: str, exchange: str) -> dict | None:
    rows = db.query(client, """
        SELECT close, date FROM prices WHERE ticker = :t AND exchange = :e AND close IS NOT NULL
        ORDER BY date DESC LIMIT 1;
    """, {"t": ticker, "e": exchange})
    return rows[0] if rows else None


def _get_yahoo_ticker(client, ticker: str, exchange: str) -> str | None:
    rows = db.query(client, """
        SELECT yahoo_ticker FROM universe WHERE ticker = :t AND exchange = :e;
    """, {"t": ticker, "e": exchange})
    return rows[0]["yahoo_ticker"] if rows and rows[0]["yahoo_ticker"] else None


def _get_trailing_closes(yahoo_ticker: str, lookback_days: int = VOL_LOOKBACK_DAYS) -> list[float]:
    """Live yfinance fetch, chronological order — see module docstring
    point 2 for why this doesn't read Turso's own `prices` history.
    """
    bars = fetch_yfinance(yahoo_ticker, days=lookback_days)  # newest-first
    return [b["close"] for b in reversed(bars)]


def price_options(dry_run: bool = False) -> list[dict]:
    client = db.get_client()
    try:
        options = _get_open_options(client)
        results = []
        for opt in options:
            ticker, exchange = opt["ticker"], opt["exchange"]
            underlying = _get_latest_underlying(client, ticker, exchange)
            if underlying is None:
                results.append({"trade_id": opt["trade_id"], "skipped": "no underlying price in Turso"})
                continue
            if opt["strike"] is None or opt["expiry_date"] is None or opt["option_type"] is None:
                results.append({"trade_id": opt["trade_id"], "skipped": "missing strike/expiry/option_type"})
                continue

            yahoo_ticker = _get_yahoo_ticker(client, ticker, exchange)
            if yahoo_ticker is None:
                results.append({"trade_id": opt["trade_id"], "skipped": "no yahoo_ticker in universe for underlying"})
                continue
            closes = _get_trailing_closes(yahoo_ticker)
            sigma = historical_volatility(closes)
            if sigma is None:
                results.append({"trade_id": opt["trade_id"],
                                 "skipped": f"only {len(closes)} yfinance closes for {yahoo_ticker}, need {MIN_VOL_POINTS}+ returns"})
                continue

            S = underlying["close"]
            K = opt["strike"]
            T = max((date.fromisoformat(opt["expiry_date"]) - date.today()).days, 0) / 365.0
            premium_est = black_scholes_price(S, K, T, RISK_FREE_RATE, sigma, opt["option_type"])
            shares = opt["shares"] if opt["shares"] is not None else 100.0
            mkt_value = premium_est * shares

            premium_paid = opt["premium"]
            flow_sign = -1.0 if opt["premium_flow"] == "received" else 1.0
            unrealized_pnl = (flow_sign * (premium_est - premium_paid) * shares
                               if premium_paid is not None else None)

            results.append({
                "trade_id": opt["trade_id"], "date": TODAY,
                "underlying_price": round(S, 4), "underlying_price_date": underlying["date"],
                "volatility": round(sigma, 4), "time_to_expiry_years": round(T, 4),
                "risk_free_rate": RISK_FREE_RATE,
                "premium_estimate": round(premium_est, 4),
                "mkt_value": round(mkt_value, 2),
                "unrealized_pnl": round(unrealized_pnl, 2) if unrealized_pnl is not None else None,
                "method": "black-scholes-hv90",
            })

        if not dry_run:
            writable = [r for r in results if "skipped" not in r]
            if writable:
                db.upsert(client, "option_marks", writable)
    finally:
        client.close()
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    for r in price_options(dry_run=args.dry_run):
        print(json.dumps(r, indent=2))
