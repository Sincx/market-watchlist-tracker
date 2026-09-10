"""Financial Modeling Prep (FMP) — free-tier fundamentals source for US-listed
tickers. Evaluated 2026-09-10 (see fred-finance-system-spec.md §7) as an
alternative to Polygon's fundamentals tier: unlike stockanalysis.com scraping,
FMP's key-metrics-ttm endpoint returns EY/ROIC *already computed*, so no
manual EBIT/EV/invested-capital math is needed for US names.

Free-tier limits (as tested 2026-09-10): US-listed only (non-US symbols
return 402 Premium Query Parameter), 250 requests/day shared across all
endpoints. Not usable for UK/EU tickers — fundamentals.py keeps the existing
stockanalysis.com chain for those.
"""
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

_ENV_LOADED = False
_BASE = "https://financialmodelingprep.com/stable"


def _ensure_env() -> None:
    global _ENV_LOADED
    if not _ENV_LOADED:
        load_dotenv(Path(__file__).parent / ".env")
        _ENV_LOADED = True


def _get(path: str, symbol: str) -> list | None:
    _ensure_env()
    key = os.environ.get("FMP_API_KEY")
    if not key:
        return None
    try:
        r = requests.get(f"{_BASE}{path}", params={"symbol": symbol, "apikey": key}, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        return data if isinstance(data, list) and data else None
    except Exception as e:
        print(f"  [fund/fmp] {symbol} {path} error: {e}")
        return None


def fetch_us(ticker: str) -> dict:
    """Fetch US-listed fundamentals from FMP's free tier. Returns {} on any
    failure or non-US-coverage response so callers fall through to the next
    source in the chain — never raises.
    """
    profile = _get("/profile", ticker)
    key_metrics = _get("/key-metrics-ttm", ticker)
    ratios = _get("/ratios-ttm", ticker)

    if not profile and not key_metrics and not ratios:
        return {}

    result: dict = {}

    if profile:
        p = profile[0]
        result["mkt_cap"] = p.get("marketCap")
        result["sector"] = p.get("sector")

    if key_metrics:
        km = key_metrics[0]
        ey = km.get("earningsYieldTTM")
        roic = km.get("returnOnInvestedCapitalTTM")
        roe = km.get("returnOnEquityTTM")
        ev_sales = km.get("evToSalesTTM")
        if ey is not None:
            result["earnings_yield"] = round(ey * 100, 2)
            if ey > 0:
                result["ev_ebit"] = round(1 / ey, 2)  # EV/EBIT = 1 / earnings yield
        if roic is not None:
            result["roic"] = round(roic * 100, 2)
        if roe is not None:
            result["roe"] = round(roe * 100, 2)

    if ratios:
        rt = ratios[0]
        pe = rt.get("priceToEarningsRatioTTM")
        if pe is not None:
            result["pe"] = round(pe, 2)
        div_yield = rt.get("dividendYieldTTM")
        if div_yield is not None:
            result["div_yield"] = round(div_yield * 100, 2)

    # FMP's free tier has no forward-looking estimates endpoint (Plus tier
    # only) — fwd_pe and eps_growth/rev_growth stay unset here; the existing
    # stockanalysis.com fallback still supplies those if this source is used.

    if result.get("pe") is not None:
        pe_v = result["pe"]
        if pe_v <= 0:
            result["valuation"] = "Loss-making"
        elif pe_v < 12:
            result["valuation"] = "Undervalued"
        elif pe_v < 20:
            result["valuation"] = "Fair value"
        elif pe_v < 30:
            result["valuation"] = "Slightly overvalued"
        else:
            result["valuation"] = "Overvalued"

    return result
