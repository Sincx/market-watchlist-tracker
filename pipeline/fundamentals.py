"""
Fundamental data scraper from stockanalysis.com (primary) with Shibui Finance MCP backup.
Returns PE, EPS growth, revenue growth, dividend yield, market cap, sector, valuation band.
"""

import json
import re
import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_SA_BASE = "https://stockanalysis.com"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def _get_soup(url: str):
    try:
        r = requests.get(url, headers=_HEADERS, timeout=15, verify=False)
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


def fetch_shibui_us(ticker: str) -> dict:
    """
    Backup source: fetch US fundamentals from Shibui Finance via MCP (free, no API key).
    Uses MCP streamable-HTTP transport. Returns {} on any failure so callers fall through.
    Note: Shibui is NL/SQL query-based — the response text is parsed with regex heuristics
    and may need tuning if Shibui changes its output format.
    """
    sid = None
    try:
        # 1. Initialize MCP session
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

        # 2. Confirm initialized (fire-and-forget notification)
        h = dict(_SHIBUI_HEADERS)
        if sid:
            h["Mcp-Session-Id"] = sid
        try:
            requests.post(_SHIBUI_URL, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                          headers=h, timeout=10)
        except Exception:
            pass

        # 3. Query fundamentals via natural language (avoids needing exact schema)
        query = (
            f"For stock ticker {ticker.upper()}: return P/E ratio, EPS growth percent (TTM or YoY), "
            f"revenue growth percent (TTM or YoY), dividend yield percent, market cap, and sector. "
            f"Most recent available data, one row."
        )
        resp, sid = _shibui_post({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "stock_data_query", "arguments": {"query": query}},
        }, sid)
        if not resp or "error" in resp:
            return {}

        # 4. Extract text content from MCP response
        content = resp.get("result", {}).get("content", [])
        text = " ".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if not text:
            return {}

        # 5. Parse values from the formatted text response
        def _first(patterns):
            for p in patterns:
                m = re.search(p, text, re.I)
                if m:
                    return m.group(1)
            return None

        result = {}

        pe_raw = _first([r"P/?E(?:\s+Ratio)?[:\s|]+([0-9.]+)", r"([0-9.]+)\s+P/?E"])
        result["pe"] = float(pe_raw) if pe_raw else None

        eps_raw = _first([r"EPS\s+Growth[:\s|]+([+-]?[0-9.]+)", r"Earnings\s+Growth[:\s|]+([+-]?[0-9.]+)"])
        result["eps_growth"] = float(eps_raw) if eps_raw else None

        rev_raw = _first([r"Revenue\s+Growth[:\s|]+([+-]?[0-9.]+)"])
        result["rev_growth"] = float(rev_raw) if rev_raw else None

        div_raw = _first([r"Dividend\s+Yield[:\s|]+([0-9.]+)", r"Div(?:idend)?\s+Yield[:\s|]+([0-9.]+)"])
        result["div_yield"] = float(div_raw) if div_raw else None

        mkt_raw = _first([r"Market\s+Cap[:\s|]+(\$?[\d.,]+\s*[BKMT]?)"])
        result["mkt_cap"] = mkt_raw.strip() if mkt_raw else None

        sec_raw = _first([r"Sector[:\s|]+([A-Za-z &/]+?)(?:\s{2,}|\||$)"])
        result["sector"] = sec_raw.strip() if sec_raw else None

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

        print(f"  [fund/shibui] {ticker}: pe={result.get('pe')} rev_growth={result.get('rev_growth')}")
        return result

    except Exception as e:
        print(f"  [fund/shibui] {ticker} error: {e}")
        return {}


def fetch_us(ticker: str) -> dict:
    """Fetch fundamentals for a US-listed ticker. Falls back to Shibui Finance if stockanalysis returns empty."""
    base = f"{_SA_BASE}/stocks/{ticker.lower()}"
    result = _scrape_page(f"{base}/", f"{base}/financials/ratios/")
    if not any(v is not None for v in result.values()):
        print(f"  [fund] {ticker}: stockanalysis empty, trying Shibui Finance backup")
        result = fetch_shibui_us(ticker)
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
