"""Candidate generation for daily-paper-trader's 7-strategy rotation (A-G),
computed in Python from Turso instead of the scheduled task's own
WebSearch/WebFetch loop — Phase 6 redesign (spec §12): "Python computes
candidate list + signals from prices/screen_results/signals; LLM only picks
the trade (rotation logic + judgment) and writes the thesis."

Strategy data sources:
  A  Value              — screen_results / fundamentals (low P/E, positive EY)
  B  Momentum           — own yfinance fetch (3-month return; Turso has no
                           historical depth yet for this, same reasoning as
                           etf_returns.py)
  C  Volume Spike       — prices.vol_ratio (already computed daily by
                           technicals.py, a same-day snapshot metric)
  D  Earnings Catalyst  — Claude API + server-side web_search tool (no
                           structured earnings-surprise data source exists
                           anywhere in this pipeline; this is the one
                           strategy that genuinely needs live judgment, so
                           Python gets it via one API call instead of the
                           scheduled task's own agentic WebSearch loop)
  E  Sector Rotation    — etf_returns.py's sector ETFs (leading sector this
                           quarter) + a liquid universe stock in that sector
  F  Small Cap Value    — fundamentals (mkt_cap $300M-$2B, low P/E, P/B<2)
  G  Small Cap High Growth — fundamentals (mkt_cap $300M-$2B, rev_growth>20%)

Each fetch_candidates_X() returns a list of dicts with at least `ticker`,
`exchange`, and strategy-specific fields — always empty list on failure or
no matches, never raises, so a WebSearch/data hiccup on one strategy doesn't
block the others (same partial-coverage philosophy as the rest of the
pipeline).

Run standalone for a quick check: python candidates.py <A|B|C|D|E|F|G>
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import yfinance as yf
from dotenv import load_dotenv

import db
import etf_returns
from screen import _MF_EXCLUDE_SECTORS

_ENV_LOADED = False

# Sectors to exclude from F/G's small-cap screens, same list screen.py uses
# for the Magic Formula (Financials/Banks/Insurance/Utilities/Real Estate) —
# reused here after finding UK closed-end investment trusts (JUP, FCSS, BRWM,
# ...) slipping through with suspiciously low P/E + null ROIC, all sector
# 'Financials' or blank. A NULL sector is excluded too as a safety net: every
# false-positive found during testing was either 'Financials' or NULL, never
# a populated non-financial sector.
_SMALL_CAP_EXCLUDE_SQL = " AND f.sector IS NOT NULL AND " + " AND ".join(
    f"f.sector NOT LIKE '%{s}%'" for s in _MF_EXCLUDE_SECTORS
)


def _ensure_env():
    global _ENV_LOADED
    if not _ENV_LOADED:
        load_dotenv(Path(__file__).parent / ".env")
        _ENV_LOADED = True


# ── A: Value ─────────────────────────────────────────────────────────────────

def fetch_candidates_A(limit: int = 10) -> list[dict]:
    """Large/mid-cap, low P/E vs sector, positive earnings, near 52-week low.
    Uses the Magic Formula screen's own ranking (EY+ROIC) as the value proxy,
    filtered to large/mid caps (>$2B) since screen_results doesn't distinguish
    cap size.
    """
    client = db.get_client()
    try:
        rows = db.query(client, """
            SELECT v.ticker, v.exchange, v.pe, v.earnings_yield, v.roic, v.mf_rank, f.mkt_cap
            FROM v_magic_formula_latest v
            JOIN fundamentals f ON f.ticker = v.ticker AND f.exchange = v.exchange
              AND f.as_of_date = (SELECT MAX(as_of_date) FROM fundamentals f2 WHERE f2.ticker=v.ticker AND f2.exchange=v.exchange)
            WHERE f.mkt_cap > 2e9 AND v.pe > 0
            ORDER BY v.mf_rank ASC LIMIT :limit;
        """, {"limit": limit})
        return rows
    finally:
        client.close()


# ── B: Momentum ──────────────────────────────────────────────────────────────

def fetch_candidates_B(sample_size: int = 300, limit: int = 10) -> list[dict]:
    """Up 15-40% over 3 months, still in uptrend, not extended (>40% excluded).
    Turso's `prices` has no 3-month depth yet (technicals.py only started
    2026-09-10) — does its own yfinance batch fetch over a sample of the
    active universe, same pattern as etf_returns.py.
    """
    client = db.get_client()
    try:
        universe = db.query(client, """
            SELECT ticker, yahoo_ticker FROM universe
            WHERE active = 1 AND yahoo_ticker IS NOT NULL AND exchange = 'US'
            ORDER BY RANDOM() LIMIT :n;
        """, {"n": sample_size})
    finally:
        client.close()

    tickers = [u["yahoo_ticker"] for u in universe]
    if not tickers:
        return []
    data = yf.download(tickers=tickers, period="4mo", group_by="ticker", auto_adjust=True, threads=True, progress=False)

    candidates = []
    for u in universe:
        t = u["yahoo_ticker"]
        try:
            df = data[t] if len(tickers) > 1 else data
            df = df.dropna(subset=["Close"])
        except (KeyError, TypeError):
            continue
        if len(df) < 60:
            continue
        ret_3m = (df["Close"].iloc[-1] / df["Close"].iloc[0] - 1) * 100
        # "still in uptrend": latest close above its own 50-day-ish midpoint
        if 15 <= ret_3m <= 40 and df["Close"].iloc[-1] > df["Close"].iloc[len(df) // 2]:
            candidates.append({"ticker": u["ticker"], "exchange": "US", "return_3m_pct": round(float(ret_3m), 2)})
    candidates.sort(key=lambda c: c["return_3m_pct"], reverse=True)
    return candidates[:limit]


# ── C: Volume Spike ───────────────────────────────────────────────────────────

def fetch_candidates_C(limit: int = 10) -> list[dict]:
    """2x+ average daily volume today — vol_ratio is already computed daily
    by technicals.py (a same-day snapshot metric, no historical depth needed).
    """
    client = db.get_client()
    try:
        rows = db.query(client, """
            SELECT p.ticker, p.exchange, p.vol_ratio, p.close, p.technical_rating
            FROM prices p
            WHERE p.date = (SELECT MAX(date) FROM prices) AND p.vol_ratio >= 2.0
            ORDER BY p.vol_ratio DESC LIMIT :limit;
        """, {"limit": limit})
        return rows
    finally:
        client.close()


# ── D: Earnings Catalyst — Claude API + web_search ────────────────────────────

def fetch_candidates_D(limit: int = 5) -> list[dict]:
    """No structured earnings-surprise data source exists in this pipeline
    (fundamentals.py/FMP/stockanalysis.com/Shibui all give current-snapshot
    ratios, not estimates-vs-actual history) — this is the one strategy that
    genuinely needs live web judgment. Uses the Claude API's server-side
    web_search tool directly (one API call, real-time search + synthesis),
    so Python handles this the same as the other 6 strategies instead of the
    scheduled task needing its own agentic WebSearch loop.
    """
    _ensure_env()
    import anthropic
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return []
    client = anthropic.Anthropic(api_key=key)
    try:
        resp = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=2048,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
            messages=[{
                "role": "user",
                "content": (
                    f"Find up to {limit} US or major European stocks that beat both EPS and revenue "
                    "estimates AND raised forward guidance in the last 14 days (as of today). "
                    "Exclude any that have already moved more than 20% since their earnings report. "
                    "Respond with ONLY a JSON array (no other text), each element: "
                    '{"ticker": "...", "exchange": "US|UK|EU", "report_date": "YYYY-MM-DD", '
                    '"beat_detail": "one sentence — EPS/revenue beat % and guidance detail"}'
                ),
            }],
        )
    except Exception as e:
        print(f"  [candidates/D] API error: {e}", file=sys.stderr)
        return []

    text = " ".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    m = text[text.find("[") : text.rfind("]") + 1]
    try:
        return json.loads(m)
    except (ValueError, json.JSONDecodeError):
        print(f"  [candidates/D] could not parse JSON from response: {text[:300]}", file=sys.stderr)
        return []


# ── E: Sector Rotation ────────────────────────────────────────────────────────

def fetch_candidates_E(limit: int = 10) -> list[dict]:
    """Most liquid, well-known stock in today's leading sector — leading
    sector determined from etf_returns.py's sector ETF quarterly returns
    (current, partial quarter), then a large-cap universe stock in that
    GICS sector, ranked by market cap.
    """
    sector_returns = etf_returns.fetch_quarterly_returns(etf_returns.SECTOR_ETFS, period="3mo")
    best_etf, best_return = None, None
    for etf, r in sector_returns.items():
        if etf not in etf_returns.SECTOR_TO_GICS:
            continue  # e.g. GLD — physical commodity, no equity sector to pick a stock from
        q = r.get("quarterly_returns", {})
        if not q:
            continue
        latest_q_return = list(q.values())[-1]
        if best_return is None or latest_q_return > best_return:
            best_etf, best_return = etf, latest_q_return
    if not best_etf:
        return []
    gics_sector = etf_returns.SECTOR_TO_GICS[best_etf]

    client = db.get_client()
    try:
        rows = db.query(client, """
            SELECT f.ticker, f.exchange, f.sector, f.mkt_cap
            FROM fundamentals f
            WHERE f.sector = :sector
              AND f.as_of_date = (SELECT MAX(as_of_date) FROM fundamentals f2 WHERE f2.ticker=f.ticker AND f2.exchange=f.exchange)
            ORDER BY f.mkt_cap DESC LIMIT :limit;
        """, {"sector": gics_sector, "limit": limit})
        for r in rows:
            r["leading_sector"] = gics_sector
            r["sector_etf"] = best_etf
            r["sector_return_pct"] = round(best_return, 2)
        return rows
    finally:
        client.close()


# ── F: Small Cap Value ────────────────────────────────────────────────────────

def fetch_candidates_F(limit: int = 10) -> list[dict]:
    client = db.get_client()
    try:
        rows = db.query(client, f"""
            SELECT f.ticker, f.exchange, f.sector, f.pe, f.mkt_cap, f.roic, f.earnings_yield
            FROM fundamentals f
            WHERE f.mkt_cap BETWEEN 300e6 AND 2e9
              AND f.pe > 0 AND f.pe < 20
              AND f.roic IS NOT NULL
              {_SMALL_CAP_EXCLUDE_SQL}
              AND f.as_of_date = (SELECT MAX(as_of_date) FROM fundamentals f2 WHERE f2.ticker=f.ticker AND f2.exchange=f.exchange)
            ORDER BY f.earnings_yield DESC LIMIT :limit;
        """, {"limit": limit})
        return rows
    finally:
        client.close()


# ── G: Small Cap High Growth ──────────────────────────────────────────────────

def fetch_candidates_G(limit: int = 10) -> list[dict]:
    client = db.get_client()
    try:
        rows = db.query(client, f"""
            SELECT f.ticker, f.exchange, f.sector, f.rev_growth, f.mkt_cap, f.pe
            FROM fundamentals f
            WHERE f.mkt_cap BETWEEN 300e6 AND 2e9
              AND f.rev_growth > 20 AND f.rev_growth < 200
              {_SMALL_CAP_EXCLUDE_SQL}
              AND f.as_of_date = (SELECT MAX(as_of_date) FROM fundamentals f2 WHERE f2.ticker=f.ticker AND f2.exchange=f.exchange)
            ORDER BY f.rev_growth DESC LIMIT :limit;
        """, {"limit": limit})
        return rows
    finally:
        client.close()


FETCHERS = {
    "A": fetch_candidates_A, "B": fetch_candidates_B, "C": fetch_candidates_C,
    "D": fetch_candidates_D, "E": fetch_candidates_E, "F": fetch_candidates_F,
    "G": fetch_candidates_G,
}

if __name__ == "__main__":
    strategy = sys.argv[1].upper() if len(sys.argv) > 1 else "C"
    result = FETCHERS[strategy]()
    print(json.dumps(result, indent=2, default=str))
