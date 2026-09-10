"""
Fundamental data: FMP free tier (US, opportunistic) + stockanalysis.com scraper
(primary) with Shibui Finance MCP backup (US last resort).
Returns PE, EPS growth, revenue growth, dividend yield, market cap, sector, valuation band.
"""

import json
import re
import time
import requests
import urllib3
from bs4 import BeautifulSoup

import fmp as _fmp

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_SA_BASE = "https://stockanalysis.com"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# stockanalysis.com rate limit — found the hard way 2026-09-10: a full-universe
# fundamentals sweep (2 unthrottled requests/ticker) sailed through the first
# ~550 tickers at 100% success, then hit a 429 that never recovered for the
# rest of that run (785/1353 tickers lost). A fixed minimum gap between
# requests avoids ever triggering the block, same pattern as Polygon's
# _POLYGON_MIN_GAP in fetchers.py. One retry-after-backoff on an actual 429,
# since the block clearly has some cooldown rather than being permanent.
_SA_MIN_GAP = 1.5
_LAST_SA_CALL = 0.0
_SA_429_BACKOFF = 30.0


def _sa_wait():
    global _LAST_SA_CALL
    elapsed = time.time() - _LAST_SA_CALL
    if elapsed < _SA_MIN_GAP:
        time.sleep(_SA_MIN_GAP - elapsed)
    _LAST_SA_CALL = time.time()


def _get_soup(url: str, _retried: bool = False):
    _sa_wait()
    try:
        r = requests.get(url, headers=_HEADERS, timeout=15, verify=False)
        if r.status_code == 429 and not _retried:
            print(f"  [fund] {url}: 429, backing off {_SA_429_BACKOFF}s and retrying once")
            time.sleep(_SA_429_BACKOFF)
            return _get_soup(url, _retried=True)
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")
    except Exception as e:
        print(f"  [fund] fetch error {url}: {e}")
        return None


def _clean_num(text: str):
    """Extract a float from strings like '23.4', '1.2B', '(5.6%)', '-12%'."""
    if not text:
        return None
    text = text.strip().replace(",", "")
    negative = text.startswith("(") or text.startswith("-")
    text = re.sub(r"[()%$£€B M KT]", "", text).strip()
    try:
        val = float(text)
        return -val if negative else val
    except ValueError:
        return None


def _find_stat(soup, label_texts: list):
    """Search for a label in the stats table and return the adjacent value."""
    for label in label_texts:
        tag = soup.find(string=re.compile(re.escape(label), re.I))
        if tag:
            parent = tag.find_parent()
            if parent:
                sibling = parent.find_next_sibling()
                if sibling:
                    return sibling.get_text(strip=True)
    return None


def _extract_sveltekit_data(soup) -> dict:
    """
    stockanalysis.com is a SvelteKit app — stats are injected as a JSON hydration
    blob in a <script> tag. The values are not accessible via adjacent HTML siblings,
    so we pull them directly from the JSON instead.
    """
    for script in soup.find_all("script"):
        text = script.string or ""
        if "peRatio" not in text or "epsGrowth" not in text:
            continue

        def _str_val(key):
            # Handle both quoted keys ("key":"val") and unquoted JS keys (key:"val")
            m = re.search(rf'"?{re.escape(key)}"?\s*:\s*"([^"]*)"', text)
            return m.group(1) if m else None

        def _num_val(key):
            m = re.search(rf'"?{re.escape(key)}"?\s*:\s*(-?[0-9]+(?:\.[0-9]+)?)', text)
            return float(m.group(1)) if m else None

        pe_str = _str_val("peRatio")
        if not pe_str:
            continue

        fwd_pe_str = _str_val("forwardPE")

        # Extract dividend yield directly — "dividend" key appears 3+ times,
        # the relevant one has the form dividend:"$N.NN (N.NN%)"
        div_yield = None
        dm = re.search(r'"?dividend"?\s*:\s*"\$[^(]*\(([0-9.]+)%\)"', text)
        if dm:
            div_yield = float(dm.group(1))

        # Sector in infoTable uses unquoted JS keys: {t:"Sector",v:"Technology",...}
        sector = None
        sm = re.search(r'"?t"?\s*:\s*"Sector"\s*,\s*"?v"?\s*:\s*"([^"]+)"', text)
        if sm:
            sector = sm.group(1)

        # Exchange (e.g. "NYSE", "NASDAQ", "LSE") from infoTable
        exchange_sa = None
        em = re.search(r'"?t"?\s*:\s*"Stock Exchange"\s*,\s*"?v"?\s*:\s*"([^"]+)"', text)
        if em:
            exchange_sa = em.group(1)

        result = {
            "peRatio":       pe_str,
            "forwardPE":     fwd_pe_str,
            "epsGrowth":     _num_val("epsGrowth"),
            "revenueGrowth": _num_val("revenueGrowth"),
            "marketCap":     _str_val("marketCap"),
            "divYield":      div_yield,
            "sector":        sector,
            "exchange":      exchange_sa,
        }
        return result
    return {}


def _scrape_ratios_page(url: str) -> dict:
    """
    Fetch ROIC and EV/EBIT from the stockanalysis.com ratios page.
    Data lives in a SvelteKit JS array — first element is the most recent TTM value.
    Returns {"roic": float (%), "ev_ebit": float, "earnings_yield": float (%)} or {}.
    """
    soup = _get_soup(url)
    if not soup:
        return {}

    for script in soup.find_all("script"):
        text = script.string or ""
        if "roic" not in text or len(text) < 5000:
            continue

        def _first_array_val(key):
            """Extract the first non-null numeric value from a JS array (key:[v1,v2,...])."""
            m = re.search(rf'"?{re.escape(key)}"?\s*:\s*\[([^\]]+)\]', text, re.I)
            if not m:
                return None
            for v in m.group(1).split(","):
                v = v.strip()
                if v and v != "null":
                    try:
                        f = float(v)
                        return f if f != 0 else None
                    except ValueError:
                        pass
            return None

        result = {}
        roic_raw = _first_array_val("roic")
        if roic_raw is not None:
            result["roic"] = round(roic_raw * 100, 2)  # stored as 0.89 → 89.0%

        roe_raw = _first_array_val("roe")
        if roe_raw is not None:
            result["roe"] = round(roe_raw * 100, 2)  # stored as 1.31 → 131.0%

        # Prefer evEbit; fall back to evebitda if evEbit missing
        ev_ebit_raw = _first_array_val("evEbit") or _first_array_val("evebitda")
        if ev_ebit_raw is not None and ev_ebit_raw > 0:
            result["ev_ebit"] = round(ev_ebit_raw, 2)
            result["earnings_yield"] = round(100.0 / ev_ebit_raw, 2)  # EY% = 1/(EV/EBIT)

        return result

    return {}


def _scrape_page(url: str, ratios_url: str = "") -> dict:
    soup = _get_soup(url)
    if not soup:
        return {}

    result = {}
    kit = _extract_sveltekit_data(soup)

    if kit:
        result["pe"] = _clean_num(kit.get("peRatio", ""))
        result["fwd_pe"] = _clean_num(kit.get("forwardPE", ""))
        result["eps_growth"] = kit.get("epsGrowth")
        result["rev_growth"] = kit.get("revenueGrowth")
        result["mkt_cap"] = kit.get("marketCap")
        result["div_yield"] = kit.get("divYield")
        result["sector"] = kit.get("sector")
        result["exchange"] = kit.get("exchange")
    else:
        # Fallback: HTML sibling parsing (works for PE and Sector at least)
        pe = _find_stat(soup, ["P/E Ratio", "PE Ratio", "Price/Earnings"])
        result["pe"] = _clean_num(pe)
        fwd_pe = _find_stat(soup, ["Forward PE", "Forward P/E"])
        result["fwd_pe"] = _clean_num(fwd_pe)
        eps = _find_stat(soup, ["EPS Growth", "EPS (Diluted) Growth", "Earnings Growth"])
        result["eps_growth"] = _clean_num(eps)
        rev = _find_stat(soup, ["Revenue Growth", "Revenue (TTM) Growth"])
        result["rev_growth"] = _clean_num(rev)
        div = _find_stat(soup, ["Dividend Yield", "Div Yield"])
        result["div_yield"] = _clean_num(div)
        mkt = _find_stat(soup, ["Market Cap", "Mkt Cap"])
        result["mkt_cap"] = mkt.strip() if mkt else None
        sector = _find_stat(soup, ["Sector"])
        result["sector"] = sector.strip() if sector else None
        result["exchange"] = None

    # Valuation band from P/E
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
    else:
        result["valuation"] = None

    # Magic Formula data from ratios page
    if ratios_url:
        ratios = _scrape_ratios_page(ratios_url)
        result.update(ratios)
    # Fallback earnings yield from P/E if EV/EBIT not available
    if result.get("earnings_yield") is None and result.get("pe") and result["pe"] > 0:
        result["earnings_yield"] = round(100.0 / result["pe"], 2)

    return result


_SHIBUI_URL = "https://mcp.shibui.finance/mcp"
_SHIBUI_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _shibui_post(payload: dict, session_id: str | None) -> tuple[dict | None, str | None]:
    """POST one JSON-RPC message to the Shibui MCP endpoint. Returns (response, session_id)."""
    h = dict(_SHIBUI_HEADERS)
    if session_id:
        h["Mcp-Session-Id"] = session_id
    try:
        r = requests.post(_SHIBUI_URL, json=payload, headers=h, timeout=25)
        r.raise_for_status()
    except Exception as e:
        print(f"  [fund/shibui] HTTP error: {e}")
        return None, session_id
    new_sid = r.headers.get("Mcp-Session-Id", session_id)
    ct = r.headers.get("Content-Type", "")
    if "text/event-stream" in ct:
        for line in r.text.splitlines():
            if line.startswith("data: "):
                try:
                    return json.loads(line[6:]), new_sid
                except Exception:
                    pass
        return None, new_sid
    try:
        return r.json(), new_sid
    except Exception:
        return None, new_sid


_SHIBUI_SNAPSHOT_SQL = """
WITH latest_val AS (
  SELECT symbol, market_cap,
    ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
  FROM shibui.valuation
  WHERE date >= CURRENT_DATE - INTERVAL '7 days'
),
latest_dd AS (
  SELECT symbol, trailing_pe, ev_ebit, earnings_yield, dividend_yield,
    ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
  FROM shibui.fundamentals_derived_daily
  WHERE date >= CURRENT_DATE - INTERVAL '7 days'
),
latest_dq AS (
  SELECT symbol, return_on_invested_capital, return_on_equity, revenue_growth_yoy, eps_growth_yoy,
    ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
  FROM shibui.fundamentals_derived_quarterly
  WHERE date >= CURRENT_DATE - INTERVAL '6 months'
)
SELECT g.ticker, g.gics_sector,
  v.market_cap, dd.dividend_yield,
  dd.trailing_pe, dd.ev_ebit, dd.earnings_yield,
  dq.return_on_invested_capital, dq.return_on_equity, dq.revenue_growth_yoy, dq.eps_growth_yoy,
  ae.forward_pe
FROM shibui.general_info g
LEFT JOIN latest_val v ON g.symbol = v.symbol AND v.rn = 1
LEFT JOIN latest_dd dd ON g.symbol = dd.symbol AND dd.rn = 1
LEFT JOIN latest_dq dq ON g.symbol = dq.symbol AND dq.rn = 1
LEFT JOIN shibui.analyst_estimates ae ON g.symbol = ae.symbol
WHERE g.ticker = :ticker
LIMIT 1
"""


def fetch_shibui_us(ticker: str) -> dict:
    """
    Backup source: fetch US fundamentals from Shibui Finance via MCP (free, no
    API key beyond the MCP auth header, if any). Returns {} on any failure so
    callers fall through.

    Rewritten 2026-09-10: Shibui's `stock_data_query` tool is DuckDB SQL
    against a documented schema now (`get_database_schema` +
    `get_query_patterns`), not the free-text NL query the old version of this
    function assumed — that older approach had been silently broken (wrong
    argument name, `query` instead of `user_prompt`+`query`) the whole time,
    masked by this environment's separate Norton TLS-interception issue on
    mcp.shibui.finance, so it never actually got exercised successfully
    before now. The new query returns clean `structuredContent.result` JSON —
    no regex parsing of free text needed, unlike the old approach.

    Coverage: NYSE + NASDAQ only (no OTC, no non-US). ROIC/ROE come from
    fundamentals_derived_quarterly (~90% populated); forward_pe from
    analyst_estimates (a snapshot table, no history).
    """
    sid = None
    try:
        init, sid = _shibui_post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "watchlist-pipeline", "version": "1.0"},
            },
        }, sid)
        if not init or "error" in init:
            return {}

        h = dict(_SHIBUI_HEADERS)
        if sid:
            h["Mcp-Session-Id"] = sid
        try:
            requests.post(_SHIBUI_URL, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                          headers=h, timeout=10)
        except Exception:
            pass

        sql = _SHIBUI_SNAPSHOT_SQL.replace(":ticker", f"'{ticker.upper()}'")
        resp, sid = _shibui_post({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "stock_data_query",
                "arguments": {
                    "user_prompt": f"{ticker.upper()} fundamentals for Magic Formula screening",
                    "query": sql,
                },
            },
        }, sid)
        if not resp or "error" in resp:
            return {}

        rows = resp.get("result", {}).get("structuredContent", {}).get("result", [])
        if not rows:
            return {}
        row = rows[0]

        def _pct(v):
            return round(v * 100, 2) if v is not None else None

        result = {
            "pe": round(row["trailing_pe"], 2) if row.get("trailing_pe") is not None else None,
            "fwd_pe": round(row["forward_pe"], 2) if row.get("forward_pe") is not None else None,
            "eps_growth": _pct(row.get("eps_growth_yoy")),
            "rev_growth": _pct(row.get("revenue_growth_yoy")),
            "div_yield": _pct(row.get("dividend_yield")),
            "mkt_cap": row.get("market_cap"),
            "sector": row.get("gics_sector"),
            "roic": _pct(row.get("return_on_invested_capital")),
            "roe": _pct(row.get("return_on_equity")),
            "ev_ebit": round(row["ev_ebit"], 2) if row.get("ev_ebit") is not None else None,
            "earnings_yield": _pct(row.get("earnings_yield")),
        }

        pe_v = result.get("pe")
        if pe_v is not None:
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
        else:
            result["valuation"] = None

        print(f"  [fund/shibui] {ticker}: pe={result.get('pe')} roic={result.get('roic')} sector={result.get('sector')}")
        return result

    except Exception as e:
        print(f"  [fund/shibui] {ticker} error: {e}")
        return {}


def fetch_us(ticker: str) -> dict:
    """Fetch fundamentals for a US-listed ticker.

    Tries FMP's free tier first (its key-metrics-ttm endpoint returns EY/ROIC
    already computed, so no manual EBIT/EV math needed) — but FMP's free tier
    restricts key-metrics-ttm/ratios-ttm to an inconsistent per-symbol
    allowlist (confirmed 2026-09-10: AAPL/MSFT/PLTR/ETSY all work, but so-so
    ORCL/CRM/CAT/BBW get a 402 despite being large, liquid names — the
    restriction doesn't track market cap or sector). So FMP is opportunistic,
    not authoritative: its fields are used where present, and stockanalysis.com
    (then Shibui) fills whatever FMP didn't return.
    """
    fmp_result = {}
    try:
        fmp_result = _fmp.fetch_us(ticker)
    except Exception as e:
        print(f"  [fund/fmp] {ticker} error: {e}")

    base = f"{_SA_BASE}/stocks/{ticker.lower()}"
    sa_result = _scrape_page(f"{base}/", f"{base}/financials/ratios/")

    result = dict(sa_result)
    source_parts = ["stockanalysis"] if any(v is not None for v in sa_result.values()) else []
    for k, v in fmp_result.items():
        if v is not None:
            result[k] = v
    # Only credit FMP in `source` if it actually supplied a Magic-Formula-
    # relevant field — mkt_cap/sector alone (the only fields FMP's free tier
    # returns for a 402'd symbol) shouldn't be labeled as "fmp" data.
    _mf_fields = ("pe", "roic", "roe", "ev_ebit", "earnings_yield")
    if any(fmp_result.get(f) is not None for f in _mf_fields):
        source_parts.insert(0, "fmp")

    if not any(v is not None for v in result.values()):
        print(f"  [fund] {ticker}: FMP + stockanalysis both empty, trying Shibui Finance backup")
        result = fetch_shibui_us(ticker)
        if any(v is not None for v in result.values()):
            source_parts = ["shibui"]

    result["source"] = "+".join(source_parts) if source_parts else None
    return result


def fetch_uk(ticker: str) -> dict:
    """Fetch fundamentals for a UK-listed ticker (LON exchange)."""
    base = f"{_SA_BASE}/quote/lon/{ticker.lower()}"
    return _scrape_page(f"{base}/", f"{base}/financials/ratios/")


def fetch_eu(ticker: str, sa_prefix: str) -> dict:
    """Fetch fundamentals for a European-listed ticker."""
    base = f"{_SA_BASE}/quote/{sa_prefix.lower()}/{ticker.lower()}"
    return _scrape_page(f"{base}/", f"{base}/financials/ratios/")


def fetch_fundamentals(ticker: str, exchange: str, sa_prefix: str = "") -> dict:
    """Dispatch to the right scraper based on exchange."""
    if exchange == "US":
        return fetch_us(ticker)
    elif exchange == "UK":
        return fetch_uk(ticker)
    elif exchange == "EU":
        return fetch_eu(ticker, sa_prefix)
    return {}
