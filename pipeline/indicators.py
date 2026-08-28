"""
Pure-Python technical indicator computations.
All functions take prices in newest-first order (index 0 = most recent close).
"""

import math
from typing import Optional


def _ema_series(values_oldest_first: list, period: int) -> list:
    """Returns EMA series same length as input (None for first period-1 positions)."""
    n = len(values_oldest_first)
    if n < period:
        return [None] * n
    k = 2.0 / (period + 1)
    result = [None] * n
    result[period - 1] = sum(values_oldest_first[:period]) / period
    for i in range(period, n):
        result[i] = values_oldest_first[i] * k + result[i - 1] * (1 - k)
    return result


def pct_change(closes: list, n: int) -> Optional[float]:
    if len(closes) <= n:
        return None
    return (closes[0] - closes[n]) / closes[n] * 100


def sma(closes: list, period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    return sum(closes[:period]) / period


def rsi(closes: list, period: int = 14) -> Optional[float]:
    """Wilder's RSI. closes: newest first."""
    if len(closes) < period + 1:
        return None
    prices = list(reversed(closes))  # oldest first
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [abs(min(d, 0.0)) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    return round(100.0 - 100.0 / (1.0 + avg_gain / avg_loss), 2)


def macd(closes: list, fast=12, slow=26, signal=9):
    """Returns (macd_val, signal_val, 'Bullish'|'Bearish'|'N/A')."""
    if len(closes) < slow + signal:
        return None, None, "N/A"

    prices = list(reversed(closes))  # oldest first
    ema_fast = _ema_series(prices, fast)
    ema_slow = _ema_series(prices, slow)

    macd_line = []
    for ef, es in zip(ema_fast, ema_slow):
        if ef is not None and es is not None:
            macd_line.append(ef - es)

    if len(macd_line) < signal:
        return None, None, "N/A"

    sig_series = _ema_series(macd_line, signal)
    m_val = macd_line[-1]
    s_val = sig_series[-1]

    if s_val is None:
        return None, None, "N/A"

    direction = "Bullish" if m_val > s_val else "Bearish"
    return round(m_val, 6), round(s_val, 6), direction


def volume_ratio(volumes: list, period: int = 20) -> Optional[float]:
    if len(volumes) < period + 1:
        return None
    avg = sum(volumes[1 : period + 1]) / period
    if avg == 0:
        return None
    return round(volumes[0] / avg, 2)


# ── Framework scores ─────────────────────────────────────────────────────────

def score_murphy(closes, highs, lows, ma50, ma200):
    """Trend following: price vs MAs + higher-highs/higher-lows over last 10 days."""
    if not closes or ma50 is None or ma200 is None:
        return "Neutral"
    price = closes[0]
    bull, bear = 0, 0
    if price > ma200: bull += 1
    else: bear += 1
    if price > ma50: bull += 1
    else: bear += 1

    # Higher-highs + higher-lows over last 10 candles
    n = min(10, len(highs), len(lows))
    if n >= 4:
        hh = all(highs[i] > highs[i + 1] for i in range(n - 1))
        hl = all(lows[i] > lows[i + 1] for i in range(n - 1))
        if hh and hl: bull += 1
        elif not hh and not hl: bear += 1

    if bull >= 2: return "Bullish"
    if bear >= 2: return "Bearish"
    return "Neutral"


def score_nison(opens, highs, lows, closes):
    """Candlestick pattern detection from last 3 candles."""
    if len(closes) < 3:
        return "Neutral"

    def candle(i):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        body = abs(c - o)
        full = h - l
        upper = h - max(o, c)
        lower = min(o, c) - l
        return o, h, l, c, body, full, upper, lower

    o0, h0, l0, c0, body0, full0, upper0, lower0 = candle(0)
    o1, h1, l1, c1, body1, full1, upper1, lower1 = candle(1)
    o2, h2, l2, c2, body2, full2, upper2, lower2 = candle(2)

    # Doji (very small body)
    if full0 > 0 and body0 / full0 < 0.1:
        return "Neutral"

    # Hammer / Hanging Man (lower shadow ≥ 2× body, small upper shadow)
    if body0 > 0 and lower0 >= 2 * body0 and upper0 < body0:
        return "Bullish"  # assumes downtrend context → hammer

    # Shooting Star (upper shadow ≥ 2× body, small lower shadow)
    if body0 > 0 and upper0 >= 2 * body0 and lower0 < body0:
        return "Bearish"

    # Bullish Engulfing (current bullish, engulfs prior bearish)
    if c0 > o0 and c1 < o1 and c0 > o1 and o0 < c1:
        return "Bullish"

    # Bearish Engulfing
    if c0 < o0 and c1 > o1 and c0 < o1 and o0 > c1:
        return "Bearish"

    # Morning Star (bearish, doji/small, bullish)
    if (c2 < o2 and                          # candle 2 bearish
            body1 < 0.3 * body2 and          # candle 1 small
            c0 > o0 and                       # candle 0 bullish
            c0 > (o2 + c2) / 2):             # closes above midpoint of candle 2
        return "Bullish"

    # Evening Star
    if (c2 > o2 and
            body1 < 0.3 * body2 and
            c0 < o0 and
            c0 < (o2 + c2) / 2):
        return "Bearish"

    return "Neutral"


def score_bulkowski(closes, highs, lows):
    """Chart pattern proxy: 52-week high/low position."""
    n = min(252, len(closes))
    if n < 20:
        return "Neutral"
    high_52w = max(highs[:n])
    low_52w = min(lows[:n])
    price = closes[0]

    dist_from_high = (high_52w - price) / high_52w if high_52w else 1
    dist_from_low = (price - low_52w) / price if price else 1

    if dist_from_high <= 0.15:   # within 15% of 52w high
        return "Bullish"
    if dist_from_low <= 0.10:    # within 10% of 52w low (near bottom)
        return "Bearish"
    return "Neutral"


def score_elder(rsi_val, macd_dir):
    """Triple screen proxy: MACD direction + RSI position."""
    if rsi_val is None or macd_dir == "N/A":
        return "Neutral"
    macd_bull = macd_dir == "Bullish"
    rsi_bull = rsi_val > 50
    if macd_bull and rsi_bull:
        return "Bullish"
    if not macd_bull and not rsi_bull:
        return "Bearish"
    return "Neutral"


def score_oneil(closes, volumes, high_52w, rsi_val, macd_dir):
    """CAN SLIM proxy: volume surge + near 52w high + positive MACD."""
    if not closes or not volumes:
        return "Moderate"
    price = closes[0]
    vol_surge = len(volumes) > 1 and volumes[0] > 1.5 * (sum(volumes[1:21]) / min(20, len(volumes) - 1)) if len(volumes) > 1 else False
    near_high = high_52w and (high_52w - price) / high_52w <= 0.15 if high_52w else False
    macd_bull = macd_dir == "Bullish"
    rsi_ok = rsi_val is not None and rsi_val > 50

    score = sum([vol_surge, near_high, macd_bull, rsi_ok])
    if score >= 3: return "Strong"
    if score >= 1: return "Moderate"
    return "Weak"


# ── Technical Rating ─────────────────────────────────────────────────────────

def technical_rating(price, ma20, ma50, ma200, rsi_val, macd_dir,
                     vol_ratio_val, vol_up, murphy, nison, bulkowski, elder, oneil):
    """Combined score → 'Buy' / 'Hold' / 'Sell' with optional signal count."""
    signals_total = 0
    signals_available = 0

    def add(condition_result):
        nonlocal signals_total, signals_available
        if condition_result is not None:
            signals_available += 1
            signals_total += condition_result

    # Traditional signals
    if price and ma20:
        add(1 if price > ma20 else -1)
    if price and ma50:
        add(1 if price > ma50 else -1)
    if price and ma200:
        add(1 if price > ma200 else -1)

    if rsi_val is not None:
        if 40 <= rsi_val <= 70:   add(1)
        elif rsi_val > 70:         add(-1)
        elif rsi_val < 30:         add(-1)
        else:                      add(0)

    if macd_dir not in (None, "N/A"):
        add(1 if macd_dir == "Bullish" else -1)

    if vol_ratio_val is not None:
        if vol_ratio_val > 1.5:
            add(1 if vol_up else -1)
        else:
            add(0)

    # Framework signals
    fw_map = {"Bullish": 1, "Strong": 1, "Neutral": 0, "Moderate": 0, "Bearish": -1, "Weak": -1}
    for fw in [murphy, nison, bulkowski, elder, oneil]:
        if fw and fw in fw_map:
            add(fw_map[fw])

    if signals_available == 0:
        return "Hold (0/11 signals)"

    score = signals_total
    label = "Buy" if score >= 5 else "Sell" if score <= -5 else "Hold"
    if signals_available < 11:
        return f"{label} ({signals_available}/11 signals)"
    return label


def atr(closes: list, highs: list, lows: list, period: int = 14) -> Optional[float]:
    """Average True Range (Wilder). closes/highs/lows are newest-first."""
    if len(closes) < period + 1:
        return None
    tr_vals = []
    for i in range(period):
        h, l, cp = highs[i], lows[i], closes[i + 1]
        tr_vals.append(max(h - l, abs(h - cp), abs(l - cp)))
    return round(sum(tr_vals) / period, 4)


def compute_all(closes, opens, highs, lows, volumes, currency="USD", usd_rate=1.0):
    """
    Main entry point. All lists are newest-first.
    Returns a dict ready to write as a P&T row.
    """
    if len(closes) < 2:
        return None

    price = closes[0]
    n52 = min(252, len(closes))
    high_52w = max(highs[:n52]) if highs else None

    c1d = pct_change(closes, 1)
    c1w = pct_change(closes, 5)
    c1m = pct_change(closes, 21)
    ma20_v = sma(closes, 20)
    ma50_v = sma(closes, 50)
    ma200_v = sma(closes, 200)
    rsi_v = rsi(closes)
    _, _, macd_dir = macd(closes)
    vol_r = volume_ratio(volumes)
    vol_up = price > closes[1] if len(closes) > 1 else True
    atr14_v = atr(closes, highs, lows, 14)

    murphy_s = score_murphy(closes, highs, lows, ma50_v, ma200_v)
    nison_s = score_nison(opens, highs, lows, closes)
    bulkowski_s = score_bulkowski(closes, highs, lows)
    elder_s = score_elder(rsi_v, macd_dir)
    oneil_s = score_oneil(closes, volumes, high_52w, rsi_v, macd_dir)

    rating = technical_rating(
        price, ma20_v, ma50_v, ma200_v, rsi_v, macd_dir, vol_r, vol_up,
        murphy_s, nison_s, bulkowski_s, elder_s, oneil_s,
    )

    def _fmt(v, dec=2):
        return round(v, dec) if v is not None else "N/A"

    def _fmt_ma(v, count, n):
        if v is None:
            return f"~MA{count}"
        return round(v, 2)

    return {
        "price": round(price, 4),
        "currency": currency,
        "usd_rate": round(usd_rate, 6),
        "pct_1d": c1d,
        "pct_1w": c1w,
        "pct_1m": c1m,
        "ma20": _fmt_ma(ma20_v, min(len(closes), 20), 20),
        "ma50": _fmt_ma(ma50_v, min(len(closes), 50), 50),
        "ma200": _fmt_ma(ma200_v, min(len(closes), 200), 200),
        "rsi14": _fmt(rsi_v),
        "macd": macd_dir,
        "vol_ratio": _fmt(vol_r),
        "murphy": murphy_s,
        "nison": nison_s,
        "bulkowski": bulkowski_s,
        "elder": elder_s,
        "oneil": oneil_s,
        "rating": rating,
        "atr14": _fmt(atr14_v, 4),
    }
