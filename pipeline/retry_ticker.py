"""Bounded, single-ticker re-fetch — Master spec Phase 13 §4/§5. The briefing
task's own "fix" for a data_quality error is always this: re-invoke the
same deterministic fallback logic once, never an LLM-improvised fetch.

  python retry_ticker.py <ticker> <exchange> <data_type>

Exits 0 with "RECOVERED" printed on success (data_quality updates to 'ok'/
'degraded'), exits 1 with "STILL FAILING" on failure (data_quality records
the new error, consecutive_failures increments) — the caller (the briefing,
or Mike manually) checks the exit code / printed line, does not retry a
second time itself, and flags for manual review after 3 consecutive daily
failures per data_quality.consecutive_failures.

'technicals' and 'fundamentals' do a real live re-fetch via each data
type's own existing fallback chain. 'screen' and 'option_mark' are
internally-derived (screen.py's mf_rank is relative to every other
eligible ticker that day, not computable in isolation for one ticker;
options_pricing.py's mark is a pure recompute over data already in Turso)
so their "retry" is a recompute against current data, not a new external
API call — still useful (a screen.py run that predates a fundamentals fix
will show stale eligibility until re-run), just a different kind of fix.

Run standalone: python retry_ticker.py MO US technicals
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import data_quality as dq
import db
import indicators
from fetchers import fetch_fx_rates, fetch_polygon, fetch_alpha_vantage, fetch_yfinance, _YFINANCE_ALIASES


def _get_universe_row(client, ticker: str, exchange: str) -> dict | None:
    rows = db.query(client, "SELECT * FROM universe WHERE ticker = :t AND exchange = :e;", {"t": ticker, "e": exchange})
    return rows[0] if rows else None


def retry_technicals(ticker: str, exchange: str) -> tuple[bool, str]:
    client = db.get_client()
    try:
        u = _get_universe_row(client, ticker, exchange)
        if u is None or not u.get("yahoo_ticker"):
            return False, "no universe row / no yahoo_ticker"

        sources = dq.Fetcher("polygon", lambda t: fetch_polygon(t)), \
                  dq.Fetcher("yfinance", lambda t: fetch_yfinance(_YFINANCE_ALIASES.get(t, u["yahoo_ticker"]))), \
                  dq.Fetcher("alpha_vantage", lambda t: fetch_alpha_vantage(t))
        result = dq.fetch_with_fallback(ticker, list(sources))
        if not result.bars:
            dq.record(client, ticker, exchange, "technicals", "error", error="all sources failed on retry")
            return False, "all sources failed"

        bars = result.bars
        closes = [b["close"] for b in bars]
        opens = [b["open"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        volumes = [b["volume"] for b in bars]
        currency = u["currency"] or "USD"
        fx = fetch_fx_rates()
        usd_rate = fx.get(currency, 1.0 if currency == "USD" else 0.0)
        ind = indicators.compute_all(closes, opens, highs, lows, volumes, currency=currency, usd_rate=usd_rate)
        if not ind:
            dq.record(client, ticker, exchange, "technicals", "error", error="indicators.compute_all() returned nothing on retry")
            return False, "indicators computation failed"

        latest = bars[0]
        db.upsert(client, "prices", [{
            "ticker": ticker, "exchange": exchange, "date": latest["date"],
            "open": latest["open"], "high": latest["high"], "low": latest["low"],
            "close": latest["close"], "volume": latest["volume"],
            "currency": currency, "usd_rate": ind["usd_rate"],
            "ma20": ind["ma20"] if isinstance(ind["ma20"], (int, float)) else None,
            "ma50": ind["ma50"] if isinstance(ind["ma50"], (int, float)) else None,
            "ma200": ind["ma200"] if isinstance(ind["ma200"], (int, float)) else None,
            "rsi14": ind["rsi14"] if isinstance(ind["rsi14"], (int, float)) else None,
            "macd_signal": ind["macd"], "vol_ratio": ind["vol_ratio"] if isinstance(ind["vol_ratio"], (int, float)) else None,
            "technical_rating": ind["rating"],
            "atr14": ind["atr14"] if isinstance(ind["atr14"], (int, float)) else None,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }])
        status = "ok" if result.source == "yfinance" else "degraded"
        dq.record(client, ticker, exchange, "technicals", status, source=result.source)
        return True, f"recovered via {result.source}"
    finally:
        client.close()


def retry_fundamentals(ticker: str, exchange: str) -> tuple[bool, str]:
    from fundamentals import fetch_fundamentals
    from fundamentals_refresh import _mkt_cap_to_number, TODAY

    client = db.get_client()
    try:
        u = _get_universe_row(client, ticker, exchange)
        if u is None:
            return False, "no universe row"
        if exchange in ("UK", "EU") and not u.get("sa_prefix"):
            dq.record(client, ticker, exchange, "fundamentals", "error", error="no sa_prefix")
            return False, "no sa_prefix"

        try:
            fund = fetch_fundamentals(ticker, exchange, u.get("sa_prefix") or "")
        except Exception as e:
            dq.record(client, ticker, exchange, "fundamentals", "error", error=f"{type(e).__name__}: {e}"[:500])
            return False, str(e)

        if not fund or not any(v is not None for k, v in fund.items() if k != "source"):
            dq.record(client, ticker, exchange, "fundamentals", "error", error="no data from any source on retry")
            return False, "no data from any source"

        db.upsert(client, "fundamentals", [{
            "ticker": ticker, "exchange": exchange, "as_of_date": TODAY,
            "pe": fund.get("pe"), "fwd_pe": fund.get("fwd_pe"),
            "eps_growth": fund.get("eps_growth"), "rev_growth": fund.get("rev_growth"),
            "div_yield": fund.get("div_yield"), "mkt_cap": _mkt_cap_to_number(fund.get("mkt_cap")),
            "sector": fund.get("sector"), "earnings_yield": fund.get("earnings_yield"),
            "roic": fund.get("roic"), "roe": fund.get("roe"), "ev_ebit": fund.get("ev_ebit"),
            "source": fund.get("source"), "fetched_at": TODAY,
        }])
        source = fund.get("source") or ""
        status = "degraded" if source == "shibui" else "ok"
        dq.record(client, ticker, exchange, "fundamentals", status, source=fund.get("source"))
        return True, f"recovered via {fund.get('source')}"
    finally:
        client.close()


def retry_screen(ticker: str, exchange: str) -> tuple[bool, str]:
    """mf_rank is relative to every other eligible ticker that day — not
    computable for one ticker in isolation. The real fix is re-running
    screen.py in full; this just confirms whether that would now succeed
    (fundamentals data exists) rather than repeating a fetch.
    """
    client = db.get_client()
    try:
        rows = db.query(client, """
            SELECT 1 FROM fundamentals WHERE ticker = :t AND exchange = :e
              AND as_of_date = (SELECT MAX(as_of_date) FROM fundamentals f2 WHERE f2.ticker = :t AND f2.exchange = :e);
        """, {"t": ticker, "e": exchange})
        if not rows:
            dq.record(client, ticker, exchange, "screen", "error", error="no fundamentals to screen against — retry fundamentals first")
            return False, "no fundamentals available — retry data_type=fundamentals first"
        dq.record(client, ticker, exchange, "screen", "ok")
        return True, "fundamentals exist — re-run screen.py to pick this ticker up"
    finally:
        client.close()


def retry_option_mark(ticker: str, exchange: str) -> tuple[bool, str]:
    """Pure recompute over data already in Turso, scoped to this ticker's
    open option position(s) — reuses options_pricing.py's own logic rather
    than a parallel implementation.
    """
    from options_pricing import (_get_open_options, _get_latest_underlying, _get_yahoo_ticker,
                                  _get_trailing_closes, historical_volatility, black_scholes_price,
                                  RISK_FREE_RATE, TODAY)
    from datetime import date

    client = db.get_client()
    try:
        opts = [o for o in _get_open_options(client) if o["ticker"] == ticker and o["exchange"] == exchange]
        if not opts:
            return False, "no open option trades for this ticker"

        recovered_any = False
        last_reason = ""
        for opt in opts:
            underlying = _get_latest_underlying(client, ticker, exchange)
            yahoo_ticker = _get_yahoo_ticker(client, ticker, exchange)
            if underlying is None or yahoo_ticker is None or opt["strike"] is None:
                last_reason = "missing underlying price / yahoo_ticker / strike"
                continue
            closes = _get_trailing_closes(yahoo_ticker)
            sigma = historical_volatility(closes)
            if sigma is None:
                last_reason = f"only {len(closes)} closes, insufficient for volatility"
                continue
            S, K = underlying["close"], opt["strike"]
            T = max((date.fromisoformat(opt["expiry_date"]) - date.today()).days, 0) / 365.0
            premium_est = black_scholes_price(S, K, T, RISK_FREE_RATE, sigma, opt["option_type"])
            shares = opt["shares"] if opt["shares"] is not None else 100.0
            flow_sign = -1.0 if opt["premium_flow"] == "received" else 1.0
            unrealized_pnl = (flow_sign * (premium_est - opt["premium"]) * shares
                               if opt["premium"] is not None else None)
            db.upsert(client, "option_marks", [{
                "trade_id": opt["trade_id"], "date": TODAY,
                "underlying_price": round(S, 4), "underlying_price_date": underlying["date"],
                "volatility": round(sigma, 4), "time_to_expiry_years": round(T, 4),
                "risk_free_rate": RISK_FREE_RATE, "premium_estimate": round(premium_est, 4),
                "mkt_value": round(premium_est * shares, 2),
                "unrealized_pnl": round(unrealized_pnl, 2) if unrealized_pnl is not None else None,
                "method": "black-scholes-hv90",
            }])
            recovered_any = True

        if recovered_any:
            dq.record(client, ticker, exchange, "option_mark", "ok")
            return True, "recomputed"
        dq.record(client, ticker, exchange, "option_mark", "error", error=last_reason)
        return False, last_reason
    finally:
        client.close()


_HANDLERS = {
    "technicals": retry_technicals,
    "fundamentals": retry_fundamentals,
    "screen": retry_screen,
    "option_mark": retry_option_mark,
}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker")
    parser.add_argument("exchange", choices=["US", "UK", "EU", "CRYPTO"])
    parser.add_argument("data_type", choices=list(_HANDLERS.keys()))
    args = parser.parse_args()

    handler = _HANDLERS[args.data_type]
    ok, detail = handler(args.ticker, args.exchange)
    if ok:
        print(f"RECOVERED: {args.ticker} ({args.exchange}, {args.data_type}) — {detail}")
        sys.exit(0)
    else:
        print(f"STILL FAILING: {args.ticker} ({args.exchange}, {args.data_type}) — {detail}")
        sys.exit(1)
