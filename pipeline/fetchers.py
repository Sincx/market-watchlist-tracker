"""
OHLCV data fetchers from Polygon, Alpha Vantage, and yfinance.
All return data in newest-first order: [{date, open, high, low, close, volume}, ...]
"""

import math
import os
import time
import certifi
import requests
import yfinance as yf
from datetime import date as _date, datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

POLYGON_KEY = os.getenv("POLYGON_API_KEY", "")
AV_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "")
POLYGON_TIER = os.getenv("POLYGON_TIER", "free").lower()
VERBOSE = os.getenv("VERBOSE", "0") == "1"

# Use windows CA bundle if set (handles Norton SSL interception); fall back to certifi
_SSL_VERIFY = os.getenv("REQUESTS_CA_BUNDLE") or os.getenv("CURL_CA_BUNDLE") or certifi.where()

_POLYGON_BASE = "https://api.polygon.io/v2"
_AV_BASE = "https://www.alphavantage.co/query"
_LAST_POLYGON_CALL = 0.0
_POLYGON_MIN_GAP = 13.0  # seconds between calls on free tier


class StaleDataError(RuntimeError):
    """Raised when the most recent bar date is older than the expected last trading day."""
    pass


# NYSE/NASDAQ full-day closures. Extend this list yearly.
_US_MARKET_HOLIDAYS = {
    # 2025
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}


def _expected_last_trading_day() -> str:
    """Return the last US market trading day strictly before today as YYYY-MM-DD."""
    d = _date.today() - timedelta(days=1)
    while d.weekday() >= 5 or str(d) in _US_MARKET_HOLIDAYS:
        d -= timedelta(days=1)
    return str(d)


def _log(msg):
    if VERBOSE:
        print(f"  [{datetime.now().strftime('%H:%M:%S')}] {msg}")


def _polygon_wait():
    global _LAST_POLYGON_CALL
    if POLYGON_TIER == "free":
        elapsed = time.time() - _LAST_POLYGON_CALL
        if elapsed < _POLYGON_MIN_GAP:
            time.sleep(_POLYGON_MIN_GAP - elapsed)
    _LAST_POLYGON_CALL = time.time()


def fetch_polygon(ticker: str, days: int = 260) -> list:
    """Fetch daily OHLCV from Polygon. Returns newest-first list or []."""
    if not POLYGON_KEY:
        return []
    _polygon_wait()
    end = datetime.today().date()
    start = end - timedelta(days=days + 60)
    url = f"{_POLYGON_BASE}/aggs/ticker/{ticker}/range/1/day/{start}/{end}"
    params = {"adjusted": "true", "sort": "desc", "limit": days, "apiKey": POLYGON_KEY}
    try:
        r = requests.get(url, params=params, timeout=15, verify=_SSL_VERIFY)
        r.raise_for_status()
        data = r.json()
        results = data.get("results", [])
        out = []
        for bar in results:
            c = bar.get("c")
            if c is None or (isinstance(c, float) and math.isnan(c)):
                continue
            out.append({
                "date": bar.get("t", 0),
                "open": bar["o"], "high": bar["h"],
                "low": bar["l"], "close": c,
                "volume": bar.get("v", 0),
            })
        _log(f"Polygon {ticker}: {len(out)} bars")
        return out
    except Exception as e:
        _log(f"Polygon {ticker} error: {e}")
        return []


def fetch_alpha_vantage(ticker: str, days: int = 260) -> list:
    """Fetch daily OHLCV from Alpha Vantage (TIME_SERIES_DAILY_ADJUSTED). Newest-first."""
    if not AV_KEY:
        return []
    params = {
        "function": "TIME_SERIES_DAILY_ADJUSTED",
        "symbol": ticker,
        "outputsize": "full",
        "apikey": AV_KEY,
    }
    try:
        r = requests.get(_AV_BASE, params=params, timeout=20, verify=_SSL_VERIFY)
        r.raise_for_status()
        data = r.json()
        series = data.get("Time Series (Daily)", {})
        if not series:
            _log(f"AV {ticker}: no data — {list(data.keys())}")
            return []
        out = []
        for date_str, vals in sorted(series.items(), reverse=True)[:days]:
            try:
                out.append({
                    "date": date_str,
                    "open": float(vals["1. open"]),
                    "high": float(vals["2. high"]),
                    "low": float(vals["3. low"]),
                    "close": float(vals["5. adjusted close"]),
                    "volume": float(vals["6. volume"]),
                })
            except (KeyError, ValueError):
                continue
        _log(f"AV {ticker}: {len(out)} bars")
        return out
    except Exception as e:
        _log(f"AV {ticker} error: {e}")
        return []


def fetch_yfinance(yahoo_ticker: str, days: int = 260) -> list:
    """Fetch daily OHLCV via yfinance. Newest-first."""
    try:
        period = f"{min(days + 60, 730)}d"
        tk = yf.Ticker(yahoo_ticker)
        hist = tk.history(period=period, auto_adjust=True)
        if hist.empty:
            _log(f"yfinance {yahoo_ticker}: empty")
            return []
        # Drop rows with NaN prices (gaps, pre-market, non-trading days)
        hist = hist.dropna(subset=["Close", "Open", "High", "Low"])
        out = []
        for idx, row in hist.sort_index(ascending=False).iterrows():
            c = float(row["Close"])
            if math.isnan(c) or c <= 0:
                continue
            out.append({
                "date": str(idx.date()),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": c,
                "volume": float(row["Volume"]),
            })
        _log(f"yfinance {yahoo_ticker}: {len(out)} bars")
        return out[:days]
    except Exception as e:
        _log(f"yfinance {yahoo_ticker} error: {e}")
        return []


def _bars_to_lists(bars: list):
    """Split bar list into (closes, opens, highs, lows, volumes) newest-first."""
    closes = [b["close"] for b in bars]
    opens = [b["open"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    volumes = [b["volume"] for b in bars]
    return closes, opens, highs, lows, volumes


# ── FX Rates ─────────────────────────────────────────────────────────────────

def fetch_fx_rates() -> dict:
    """
    Returns dict of {currency: usd_rate}.
    GBX (pence) = GBP/100.
    Fetches: GBPUSD, EURUSD, CHFUSD, DKKUSD, SEKUSD from Polygon forex.
    """
    pairs = {
        "GBP": "C:GBPUSD",
        "EUR": "C:EURUSD",
        "CHF": "C:CHFUSD",
        "DKK": "C:DKKUSD",
        "SEK": "C:SEKUSD",
    }
    rates = {"USD": 1.0}
    for ccy, pair in pairs.items():
        bars = fetch_polygon(pair, days=5)
        rate = bars[0]["close"] if bars else 0.0
        if not rate:
            # Fallback 1: AV forex
            rate = _fetch_av_fx(ccy)
        if not rate:
            # Fallback 2: yfinance (e.g. GBPUSD=X)
            fx_bars = fetch_yfinance(f"{ccy}USD=X", days=5)
            rate = fx_bars[0]["close"] if fx_bars else 0.0
        if rate:
            rates[ccy] = rate
            if ccy == "GBP":
                rates["GBX"] = rate / 100.0
    return rates


def _fetch_av_fx(from_ccy: str) -> float:
    """Fallback FX rate from Alpha Vantage CURRENCY_EXCHANGE_RATE."""
    if not AV_KEY:
        return 0.0
    params = {
        "function": "CURRENCY_EXCHANGE_RATE",
        "from_currency": from_ccy,
        "to_currency": "USD",
        "apikey": AV_KEY,
    }
    try:
        r = requests.get(_AV_BASE, params=params, timeout=10, verify=_SSL_VERIFY)
        data = r.json()
        val = data.get("Realtime Currency Exchange Rate", {}).get("5. Exchange Rate")
        return float(val) if val else 0.0
    except Exception:
        return 0.0


# Tickers where yfinance uses a different symbol than Polygon/standard
_YFINANCE_ALIASES = {
    "BRK.B": "BRK-B",
    "BRK.A": "BRK-A",
}


# ── Per-group fetch dispatch ──────────────────────────────────────────────────

def fetch_us_ticker(ticker: str) -> tuple:
    """
    Fetch OHLCV for a standard US ticker.
    Returns (closes, opens, highs, lows, volumes) or ([], [], [], [], []).
    Raises StaleDataError if the most recent bar is older than the expected last trading day
    (catches the yfinance NaN-drop bug where a missing close silently returns the prior day).
    """
    expected = _expected_last_trading_day()

    # Polygon primary
    bars = fetch_polygon(ticker)
    if len(bars) >= 30:
        if bars[0]["date"] < expected:
            raise StaleDataError(f"{ticker}: Polygon newest bar {bars[0]['date']} < expected {expected}")
        return _bars_to_lists(bars)

    # yfinance fallback (use alias if needed, e.g. BRK.B → BRK-B)
    yf_ticker = _YFINANCE_ALIASES.get(ticker, ticker)
    bars = fetch_yfinance(yf_ticker)
    if bars:
        if bars[0]["date"] < expected:
            raise StaleDataError(f"{ticker}: yfinance newest bar {bars[0]['date']} < expected {expected}")
        return _bars_to_lists(bars)

    return [], [], [], [], []


def fetch_ftse_ticker(ticker: str, dual_listed: set, collision_tickers: set) -> tuple:
    """
    Fetch OHLCV for FTSE100 ticker.
    Dual-listed ADRs → Polygon USD.
    Collision tickers → skip Polygon, go AV then yfinance.
    Others → AV .LON, then yfinance .L.
    Returns (closes, opens, highs, lows, volumes, price_currency)
    where price_currency is GBX unless overridden.
    """
    price_ccy = "GBX"

    if ticker in dual_listed:
        # Polygon USD price (price already in USD)
        bars = fetch_polygon(ticker)
        if len(bars) >= 30:
            price_ccy = "USD"
            return (*_bars_to_lists(bars), price_ccy)
        # Fallback to yfinance .L (prices in GBX)
        bars = fetch_yfinance(f"{ticker}.L")
        if bars:
            return (*_bars_to_lists(bars), "GBX")
        return [], [], [], [], [], price_ccy

    if ticker in collision_tickers:
        # Skip Polygon entirely
        bars = fetch_alpha_vantage(f"{ticker}.LON")
        if len(bars) >= 30:
            # Detect GBP vs GBX from AV price
            latest = bars[0]["close"]
            price_ccy = "GBP" if latest < 100 else "GBX"
            return (*_bars_to_lists(bars), price_ccy)
        bars = fetch_yfinance(f"{ticker}.L")
        if bars:
            return (*_bars_to_lists(bars), "GBX")
        return [], [], [], [], [], price_ccy

    # Standard FTSE100 path
    bars = fetch_alpha_vantage(f"{ticker}.LON")
    if len(bars) >= 30:
        latest = bars[0]["close"]
        price_ccy = "GBP" if latest < 100 else "GBX"
        return (*_bars_to_lists(bars), price_ccy)

    bars = fetch_yfinance(f"{ticker}.L")
    if bars:
        return (*_bars_to_lists(bars), "GBX")

    return [], [], [], [], [], price_ccy


def fetch_eu_morningstar_ticker(ticker: str, yahoo_ticker: str) -> tuple:
    """
    Fetch OHLCV for EU Morningstar ticker via yfinance.
    Returns (closes, opens, highs, lows, volumes) or ([], [], [], [], []).
    """
    bars = fetch_yfinance(yahoo_ticker)
    if bars:
        return _bars_to_lists(bars)
    return [], [], [], [], []
