"""
Google Sheets read/write via gspread.
Handles PERCENT format (rows 2–51) vs NUMBER format (rows 52+).
"""

import os
from datetime import datetime
from dotenv import load_dotenv
import gspread
import requests as _requests
from requests.adapters import HTTPAdapter as _HTTPAdapter
import urllib3
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession, Request as _GAuthRequest

# Norton Antivirus SSL/TLS scanning intercepts HTTPS connections and replaces
# server certificates with its own. Norton's CA doesn't mark BasicConstraints as
# critical, which OpenSSL 3.x (Python 3.13) now enforces. There is no Python-level
# flag to disable this check — it is enforced in OpenSSL's C code.
#
# Workaround: a custom HTTPAdapter that forces verify=False at the adapter level.
# We can't use session.verify=False because requests.merge_environment_settings()
# reads CURL_CA_BUNDLE from the environment (set in .env for yfinance) and overrides
# the session-level verify. Mounting this adapter bypasses merge_environment_settings.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class _NoVerifyAdapter(_HTTPAdapter):
    """Forces verify=False regardless of session-level or env-var CA bundle."""
    def send(self, request, *args, **kwargs):
        kwargs["verify"] = False
        return super().send(request, *args, **kwargs)

from config import (
    SPREADSHEET_ID, PT, PT_HEADERS, FUND,
    GROUPS, BUY_OPPS_SHEET_ID, SCORE_STYLE, BURRY_POSITIONS,
    get_display_index, GROUP_PRIORITY, MF_THRESHOLDS,
)

load_dotenv()

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

_gc = None
_sh = None


def _get_sheet():
    global _gc, _sh
    if _sh:
        return _sh
    cred_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not cred_path or not os.path.exists(cred_path):
        raise FileNotFoundError(
            f"Service account JSON not found: {cred_path!r}. "
            "Set GOOGLE_SERVICE_ACCOUNT_JSON in .env"
        )
    creds = Credentials.from_service_account_file(cred_path, scopes=_SCOPES)

    # Build sessions with _NoVerifyAdapter mounted for all HTTPS to bypass
    # Norton SSL interception. Using session.verify=False alone is insufficient
    # because CURL_CA_BUNDLE in the env overrides it in merge_environment_settings.
    _no_verify = _NoVerifyAdapter()
    _token_session = _requests.Session()
    _token_session.mount("https://", _no_verify)
    _authed_session = AuthorizedSession(
        creds, auth_request=_GAuthRequest(session=_token_session)
    )
    _authed_session.mount("https://", _no_verify)

    _gc = gspread.Client(auth=creds, session=_authed_session)
    _sh = _gc.open_by_key(SPREADSHEET_ID)
    return _sh


def _worksheet(name: str):
    sh = _get_sheet()
    try:
        return sh.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        return None


def _safe_float(v):
    try:
        return float(v) if v not in ("", None) else None
    except (TypeError, ValueError):
        return None


def _safe(val):
    if val is None or val == "N/A":
        return ""
    return val


# ── PERCENT vs NUMBER handling ────────────────────────────────────────────────

def _is_pct_row(sheet_row: int) -> bool:
    return 2 <= sheet_row <= 51


def _pct_value(val, sheet_row: int):
    if val is None or val == "N/A":
        return ""
    try:
        f = float(val)
        return f / 100.0 if _is_pct_row(sheet_row) else f
    except (TypeError, ValueError):
        return ""


def _pct_read(raw, sheet_row: int):
    if raw == "":
        return None
    try:
        f = float(raw)
        return f * 100 if _is_pct_row(sheet_row) else f
    except (TypeError, ValueError):
        return None


# ── Sheets API batch formatting ───────────────────────────────────────────────

def _rgb(hex_color: str) -> dict:
    h = hex_color.lstrip("#")
    return {k: int(h[i:i+2], 16) / 255.0 for k, i in zip(("red", "green", "blue"), (0, 2, 4))}


def _cell_format(bg_hex=None, fg_hex=None, bold=False, italic=False,
                 font_size=None, h_align=None, wrap=None) -> dict:
    fmt = {}
    if bg_hex:
        fmt["backgroundColor"] = _rgb(bg_hex)
    text_fmt = {}
    if fg_hex:
        text_fmt["foregroundColor"] = _rgb(fg_hex)
    if bold:
        text_fmt["bold"] = True
    if italic:
        text_fmt["italic"] = True
    if font_size:
        text_fmt["fontSize"] = font_size
    if text_fmt:
        fmt["textFormat"] = text_fmt
    if h_align:
        fmt["horizontalAlignment"] = h_align
    if wrap:
        fmt["wrapStrategy"] = wrap
    return fmt


def _range_spec(sheet_id: int, start_row: int, end_row: int,
                start_col: int = 0, end_col: int = 15) -> dict:
    return {
        "sheetId": sheet_id,
        "startRowIndex": start_row - 1,
        "endRowIndex": end_row,
        "startColumnIndex": start_col,
        "endColumnIndex": end_col,
    }


def _repeat_cell_request(sheet_id, row, col_start, col_end, fmt: dict) -> dict:
    return {
        "repeatCell": {
            "range": _range_spec(sheet_id, row, row, col_start, col_end),
            "cell": {"userEnteredFormat": fmt},
            "fields": "userEnteredFormat(" + ",".join(fmt.keys()) + ")",
        }
    }


def _batch_format(requests: list):
    sh = _get_sheet()
    if requests:
        sh.batch_update({"requests": requests})


# ── Price & Technicals tab ────────────────────────────────────────────────────

def read_pt_row(sheet_row: int) -> list:
    ws = _worksheet("Price & Technicals")
    if not ws:
        return []
    try:
        return ws.row_values(sheet_row)
    except Exception:
        return []


def write_pt_rows_batch(rows: list):
    """
    rows: list of (ticker, index_name, indicators_dict, sheet_row, usd_rate)
    """
    ws = _worksheet("Price & Technicals")
    if not ws:
        raise RuntimeError("Sheet 'Price & Technicals' not found")

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    data = {}

    for ticker, index_name, indicators, sheet_row, usd_rate in rows:
        pct = lambda v, r=sheet_row: _pct_value(v, r)
        data[sheet_row] = [
            ticker, get_display_index(index_name, ticker), now,
            indicators.get("price", ""),
            indicators.get("currency", ""),
            usd_rate,
            pct(indicators.get("pct_1d")),
            pct(indicators.get("pct_1w")),
            pct(indicators.get("pct_1m")),
            _safe(indicators.get("ma20")),
            _safe(indicators.get("ma50")),
            _safe(indicators.get("ma200")),
            _safe(indicators.get("rsi14")),
            indicators.get("macd", ""),
            _safe(indicators.get("vol_ratio")),
            indicators.get("murphy", ""),
            indicators.get("nison", ""),
            indicators.get("bulkowski", ""),
            indicators.get("elder", ""),
            indicators.get("oneil", ""),
            indicators.get("rating", ""),
            BURRY_POSITIONS.get(ticker, ""),
        ]

    if not data:
        return

    sorted_rows = sorted(data.keys())
    blocks, block_start, prev = [], sorted_rows[0], sorted_rows[0]
    for r in sorted_rows[1:]:
        if r == prev + 1:
            prev = r
        else:
            blocks.append((block_start, prev))
            block_start = r
            prev = r
    blocks.append((block_start, prev))

    for (start, end) in blocks:
        values = [data[r] for r in range(start, end + 1)]
        ws.update(f"A{start}:V{end}", values, value_input_option="USER_ENTERED")


# ── Fundamentals tab ─────────────────────────────────────────────────────────

def _fund_source(index_name: str, ticker: str) -> str:
    """Source column (K): Burry stocks get Long/Short suffix, others get group name."""
    if index_name == "Burry":
        pos = BURRY_POSITIONS.get(ticker, "")
        return f"Burry-{pos}" if pos else "Burry"
    return index_name


def write_fundamentals_row(ticker: str, index_name: str, fund: dict, sheet_row: int):
    ws = _worksheet("Fundamentals")
    if not ws:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    row = [
        ticker, get_display_index(index_name, ticker), now,
        _safe(fund.get("pe")),          # D
        _safe(fund.get("eps_growth")),  # E
        _safe(fund.get("rev_growth")),  # F
        _safe(fund.get("div_yield")),   # G
        _safe(fund.get("mkt_cap")),     # H
        _safe(fund.get("sector")),      # I
        _safe(fund.get("valuation")),   # J
        _safe(_fund_source(index_name, ticker)),  # K: Source
        _safe(fund.get("earnings_yield")),  # L: Earnings Yield % (EBIT/EV or 1/PE)
        _safe(fund.get("roic")),        # M: ROIC %
        _safe(fund.get("ev_ebit")),     # N: EV/EBIT ratio
        "",                             # O: MF Rank — computed separately by batch
        _safe(fund.get("fwd_pe")),      # P: Forward P/E
        _safe(fund.get("roe")),         # Q: ROE %
    ]
    ws.update(f"A{sheet_row}:Q{sheet_row}", [row], value_input_option="USER_ENTERED")


def read_fund_all_rows(start_row=2, end_row=172) -> dict:
    """Returns {ticker: fund_dict} for cross-referencing in Buy Opps and MF ranking."""
    ws = _worksheet("Fundamentals")
    if not ws:
        return {}
    try:
        raw = ws.get(f"A{start_row}:Q{end_row}")
    except Exception:
        return {}
    out = {}
    for row in raw:
        row = list(row) + [""] * (17 - len(row))
        ticker = row[FUND["ticker"]]
        if not ticker:
            continue
        out[ticker] = {
            "pe":             _safe_float(row[FUND["pe"]]),
            "eps_growth":     _safe_float(row[FUND["eps_growth"]]),
            "rev_growth":     _safe_float(row[FUND["rev_growth"]]),
            "div_yield":      _safe_float(row[FUND["div_yield"]]),
            "mkt_cap":        row[FUND["mkt_cap"]],
            "sector":         row[FUND["sector"]],
            "valuation":      row[FUND["valuation"]],
            "source":         row[FUND["source"]],
            "earnings_yield": _safe_float(row[FUND["earnings_yield"]]),
            "roic":           _safe_float(row[FUND["roic"]]),
            "ev_ebit":        _safe_float(row[FUND["ev_ebit"]]),
            "mf_rank":        _safe_float(row[FUND["mf_rank"]]),
            "fwd_pe":         _safe_float(row[FUND["fwd_pe"]]),
            "roe":            _safe_float(row[FUND["roe"]]),
        }
    return out


# ── Magic Formula ranking ────────────────────────────────────────────────────

_MF_EXCLUDE_SECTORS = {"Financial Services", "Banks", "Insurance", "Utilities", "Real Estate"}


def compute_and_write_mf_ranks(fund_map: dict) -> dict:
    """
    Compute Greenblatt Magic Formula ranks across all stocks with valid data.
    Ranks by Earnings Yield (EBIT/EV %) + ROIC % — lower combined rank = better pick.
    Excludes: Financials, Banks, Insurance, Utilities, Real Estate (per Greenblatt).
    Writes MF rank to col O in Fundamentals tab.
    Returns {ticker: mf_rank}.
    """
    from config import GROUPS

    # Build ticker → Fundamentals sheet row lookup
    ticker_rows = {}
    for cfg in GROUPS.values():
        for i, t in enumerate(cfg["tickers"]):
            ticker_rows[t] = cfg["start_row"] + i

    # Filter eligible stocks (valid EY and ROIC, excluded sectors filtered out)
    eligible = {}
    for ticker, data in fund_map.items():
        sector = data.get("sector") or ""
        if any(excl.lower() in sector.lower() for excl in _MF_EXCLUDE_SECTORS):
            continue
        ey = data.get("earnings_yield")
        roic = data.get("roic")
        # Fallback: compute EY from PE if EV/EBIT not available
        if ey is None:
            pe = data.get("pe")
            if pe and pe > 0:
                ey = round(100.0 / pe, 2)
        if ey is None or ey <= 0 or roic is None or roic <= 0:
            continue
        eligible[ticker] = {"earnings_yield": ey, "roic": roic}

    if len(eligible) < 2:
        return {}

    # Rank each metric (rank 1 = best)
    ey_sorted = sorted(eligible.items(), key=lambda x: x[1]["earnings_yield"], reverse=True)
    ey_ranks = {t: i + 1 for i, (t, _) in enumerate(ey_sorted)}

    roic_sorted = sorted(eligible.items(), key=lambda x: x[1]["roic"], reverse=True)
    roic_ranks = {t: i + 1 for i, (t, _) in enumerate(roic_sorted)}

    combined = {t: ey_ranks[t] + roic_ranks[t] for t in eligible}
    mf_sorted = sorted(combined.items(), key=lambda x: x[1])
    mf_ranks = {t: i + 1 for i, (t, _) in enumerate(mf_sorted)}

    # Write MF ranks to Fundamentals col O
    ws = _worksheet("Fundamentals")
    if ws:
        updates = []
        for ticker, mf_rank in mf_ranks.items():
            if ticker in ticker_rows:
                row = ticker_rows[ticker]
                updates.append({"range": f"O{row}", "values": [[mf_rank]]})
        if updates:
            try:
                ws.batch_update(updates, value_input_option="USER_ENTERED")
            except Exception as e:
                print(f"  [mf_ranks] write error (non-fatal): {e}")

    return mf_ranks


# ── Read P&T for downstream use ───────────────────────────────────────────────

def _build_row_to_group() -> dict:
    """Derive sheet_row → group_name from GROUPS config (used to add group field to pt_rows)."""
    m = {}
    for gname, cfg in GROUPS.items():
        for i, t in enumerate(cfg["tickers"]):
            m[cfg["start_row"] + i] = gname
    return m

_ROW_TO_GROUP = _build_row_to_group()


def read_pt_all_rows(start_row=2, end_row=172) -> list:
    ws = _worksheet("Price & Technicals")
    if not ws:
        return []
    try:
        raw = ws.get(f"A{start_row}:V{end_row}")
    except Exception:
        return []

    out = []
    for i, row in enumerate(raw):
        sheet_row = start_row + i
        row = list(row) + [""] * (22 - len(row))
        out.append({
            "sheet_row": sheet_row,
            "ticker": row[PT["ticker"]],
            "index": row[PT["index"]],       # display name (e.g. "DJIA", "FTSE 100")
            "group": _ROW_TO_GROUP.get(sheet_row, ""),  # internal group name
            "last_updated": row[PT["last_updated"]],
            "price": _safe_float(row[PT["price"]]),
            "currency": row[PT["currency"]],
            "usd_rate": _safe_float(row[PT["usd_rate"]]),
            "pct_1d": _pct_read(row[PT["pct_1d"]], sheet_row),
            "pct_1w": _pct_read(row[PT["pct_1w"]], sheet_row),
            "pct_1m": _pct_read(row[PT["pct_1m"]], sheet_row),
            "ma20": _safe_float(row[PT["ma20"]]),
            "ma50": _safe_float(row[PT["ma50"]]),
            "ma200": _safe_float(row[PT["ma200"]]),
            "rsi14": _safe_float(row[PT["rsi14"]]),
            "macd": row[PT["macd"]],
            "vol_ratio": _safe_float(row[PT["vol_ratio"]]),
            "murphy": row[PT["murphy"]],
            "nison": row[PT["nison"]],
            "bulkowski": row[PT["bulkowski"]],
            "elder": row[PT["elder"]],
            "oneil": row[PT["oneil"]],
            "rating": row[PT["rating"]],
            "burry": row[PT["burry"]],
        })
    return out


# ── Buy Opportunities tab — full 5-star implementation ────────────────────────

_BUY_OPPS_HEADERS = [
    "#", "Ticker", "Index", "Sector", "Price", "Currency",
    "Valuation", "P/E", "EPS Gr%", "Div Yield%",
    "RSI (14)", "MACD", "1M%",
    "EV/EBIT", "Fwd P/E", "ROIC", "ROE",
    "Score /5", "Conviction Note",
]

_DIVIDER_TEXT = {
    5: "⭐⭐⭐⭐⭐  SCORE 5 / 5  ·  Undervalued + Buy signal + Healthy RSI (40–70) + Bullish MACD",
    4: "⭐⭐⭐⭐  SCORE 4 / 5  ·  Good setup: one signal missing",
    3: "⭐⭐⭐  SCORE 3 / 5  ·  Watch list: fair value + buy signal, but RSI extended or MACD bearish",
    2: "⭐⭐  SCORE 2 / 5  ·  Weak setup: two signals missing",
}

_DIVIDER_STYLE = {
    5: {"bg": "12641f", "fg": "ffffff"},
    4: {"bg": "34a853", "fg": "ffffff"},
    3: {"bg": "fbbc04", "fg": "33302e"},
    2: {"bg": "f9ab00", "fg": "33302e"},
}

_ROW_BG = {5: "d9ead3", 4: "f0f9f0", 3: "fef9e7", 2: "fce8b2"}

_LEGEND = (
    "★ Score: Undervalued=2pts, Fair value=1pt | Technical Buy=1pt | RSI 40–70=1pt | "
    "MACD Bullish=1pt | Max 5pts   ·   — = data not available   ·   "
    "Magic Formula screen (✓ pass / ✗ fail): EV/EBIT≤10x | Fwd P/E≤13x | ROIC≥15% | ROE≥15%   ·   "
    "MF# in Conviction Note = Greenblatt combined EY+ROIC rank (lower = better; excludes Financials/Utilities)"
)


def _mf_pass_fail(value, threshold: float, better_when: str, suffix: str) -> str:
    """Render a Magic Formula screen cell, e.g. '8.4x ✓' or '22.0% ✗'. '—' if no data."""
    if value is None:
        return "—"
    passed = (value <= threshold) if better_when == "low" else (value >= threshold)
    mark = "✓" if passed else "✗"
    return f"{value:.1f}{suffix} {mark}"


def _score_ticker(pt_row: dict, fund: dict) -> int:
    valuation = fund.get("valuation", "")
    rsi = pt_row.get("rsi14")
    macd = pt_row.get("macd", "")
    score = 0
    if valuation == "Undervalued":
        score += 2
    elif valuation == "Fair value":
        score += 1
    score += 1  # Technical Buy (always true by filter)
    if rsi is not None and 40 <= rsi <= 70:
        score += 1
    if macd == "Bullish":
        score += 1
    return score


def _conviction_note(valuation: str, pe, rsi, macd: str, pct_1m, mf_rank=None) -> str:
    parts = []
    if mf_rank is not None:
        parts.append(f"MF#{int(mf_rank)}")
    if valuation == "Undervalued":
        pe_str = f" (P/E {pe:.1f}×)" if pe else ""
        parts.append(f"Undervalued{pe_str}")
    elif valuation == "Fair value":
        pe_str = f" (P/E {pe:.1f}×)" if pe else ""
        parts.append(f"Fair value{pe_str}")
    if macd == "Bullish":
        parts.append("bullish MACD")
    elif macd == "Bearish":
        parts.append("bearish MACD")
    if rsi is not None:
        if 40 <= rsi <= 70:
            parts.append(f"RSI healthy at {rsi:.0f}")
        elif rsi > 70:
            parts.append(f"RSI extended at {rsi:.0f}")
        else:
            parts.append(f"RSI low at {rsi:.0f}")
    if pct_1m is not None:
        direction = "up" if pct_1m >= 0 else "down"
        parts.append(f"{abs(pct_1m):.1f}% {direction} past month")
    return "; ".join(parts[:5]) + "." if parts else "Buy signal confirmed."


def update_buy_opportunities(pt_rows: list, fund_map: dict, scope: str) -> dict:
    """
    Build the 5-star Buy Opportunities tab.
    Returns summary dict: {total, by_score: {5: N, 4: N, 3: N, 2: N}}
    """
    ws = _worksheet("Buy Opportunities")
    if not ws:
        return {}

    from config import EU_SCOPE, US_SCOPE
    all_scope_groups = EU_SCOPE + US_SCOPE

    # 1. Filter: Technical Rating starts with "Buy" AND valuation in {Cheap, Fair}
    candidates = []
    seen_tickers = {}  # ticker → priority for dedup

    for r in pt_rows:
        if not r.get("ticker") or not str(r.get("rating", "")).startswith("Buy"):
            continue
        ticker = r["ticker"]
        index = r.get("index", "")
        fund = fund_map.get(ticker, {})
        valuation = fund.get("valuation", "")
        if valuation not in ("Undervalued", "Fair value"):
            continue
        priority = GROUP_PRIORITY.get(r.get("group", ""), 99)
        if ticker in seen_tickers and seen_tickers[ticker] <= priority:
            continue
        seen_tickers[ticker] = priority
        candidates = [c for c in candidates if c["ticker"] != ticker]
        mf_rank = fund.get("mf_rank")
        score = _score_ticker(r, fund)
        note = _conviction_note(
            valuation, fund.get("pe"), r.get("rsi14"), r.get("macd"),
            r.get("pct_1m"), mf_rank,
        )
        candidates.append({
            "ticker": ticker,
            "index": index,
            "sector": fund.get("sector", "—"),
            "price": r.get("price", ""),
            "currency": r.get("currency", ""),
            "valuation": valuation,
            "pe": fund.get("pe", "—"),
            "eps_growth": fund.get("eps_growth", "—"),
            "div_yield": fund.get("div_yield", "—"),
            "rsi14": r.get("rsi14", "—"),
            "macd": r.get("macd", "—"),
            "pct_1m": r.get("pct_1m"),
            "ev_ebit": fund.get("ev_ebit"),
            "fwd_pe": fund.get("fwd_pe"),
            "roic": fund.get("roic"),
            "roe": fund.get("roe"),
            "score": score,
            "note": note,
            "priority": priority,
        })

    # 2. Sort: score desc → Cheap before Fair → priority → ticker alpha
    def sort_key(c):
        val_order = 0 if c["valuation"] == "Undervalued" else 1
        return (-c["score"], val_order, c["priority"], c["ticker"])

    candidates.sort(key=sort_key)

    # 3. Update date in row 2 (col A), preserve rows 1-4 otherwise
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        ws.update("A2", [[f"Updated: {now_str}"]], value_input_option="USER_ENTERED")
    except Exception:
        pass

    # Ensure headers in row 4
    try:
        ws.update("A4:S4", [_BUY_OPPS_HEADERS], value_input_option="USER_ENTERED")
    except Exception:
        pass

    # 4. Clear from row 5 downward (Magic Formula is now columns N-Q, not a separate
    # row section below the picks — nothing to preserve here anymore).
    try:
        ws.delete_rows(5, max(ws.row_count, 100))
    except Exception:
        pass

    # 5. Build output rows grouped by score tier
    output_rows = []
    format_requests = []
    current_sheet_row = 5
    rank = 0
    summary = {"total": len(candidates), "by_score": {5: 0, 4: 0, 3: 0, 2: 0}}

    for score in [5, 4, 3, 2]:
        tier = [c for c in candidates if c["score"] == score]
        if not tier:
            continue
        summary["by_score"][score] = len(tier)

        # Section divider
        divider_text = _DIVIDER_TEXT[score]
        output_rows.append([divider_text] + [""] * 18)
        style = _DIVIDER_STYLE[score]
        format_requests.append({
            "repeatCell": {
                "range": _range_spec(BUY_OPPS_SHEET_ID, current_sheet_row,
                                     current_sheet_row, 0, 19),
                "cell": {
                    "userEnteredFormat": _cell_format(
                        bg_hex=style["bg"], fg_hex=style["fg"], bold=True
                    )
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }
        })
        current_sheet_row += 1

        # Data rows
        row_bg = _ROW_BG[score]
        for c in tier:
            rank += 1
            pct_1m_display = round(c["pct_1m"], 2) if c["pct_1m"] is not None else "—"
            pe_display = round(c["pe"], 1) if isinstance(c["pe"], float) else "—"
            eps_display = round(c["eps_growth"], 1) if isinstance(c["eps_growth"], float) else "—"
            div_display = round(c["div_yield"], 2) if isinstance(c["div_yield"], float) else "—"
            rsi_display = round(c["rsi14"], 1) if isinstance(c["rsi14"], float) else "—"
            ev_ebit_cell = _mf_pass_fail(c["ev_ebit"], MF_THRESHOLDS["ev_ebit_max"], "low", "x")
            fwd_pe_cell = _mf_pass_fail(c["fwd_pe"], MF_THRESHOLDS["fwd_pe_max"], "low", "x")
            roic_cell = _mf_pass_fail(c["roic"], MF_THRESHOLDS["roic_min"], "high", "%")
            roe_cell = _mf_pass_fail(c["roe"], MF_THRESHOLDS["roe_min"], "high", "%")

            output_rows.append([
                rank, c["ticker"], c["index"], c["sector"],
                c["price"], c["currency"], c["valuation"],
                pe_display, eps_display, div_display,
                rsi_display, c["macd"], pct_1m_display,
                ev_ebit_cell, fwd_pe_cell, roic_cell, roe_cell,
                f"{score}/5", c["note"],
            ])

            # Row background
            format_requests.append({
                "repeatCell": {
                    "range": _range_spec(BUY_OPPS_SHEET_ID, current_sheet_row,
                                         current_sheet_row, 0, 19),
                    "cell": {"userEnteredFormat": _cell_format(bg_hex=row_bg)},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            })

            # Col G (Valuation col index 6): Undervalued=green, Fair value=blue
            if c["valuation"] == "Undervalued":
                val_fmt = _cell_format(bg_hex="6bc185", fg_hex="1e5631", bold=True, h_align="CENTER")
            else:
                val_fmt = _cell_format(bg_hex="c9daf8", fg_hex="1a4a8a", bold=True, h_align="CENTER")
            format_requests.append({
                "repeatCell": {
                    "range": _range_spec(BUY_OPPS_SHEET_ID, current_sheet_row,
                                         current_sheet_row, 6, 7),
                    "cell": {"userEnteredFormat": val_fmt},
                    "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
                }
            })

            # Col L (MACD col index 11): Bullish=green, Bearish=orange
            macd_val = c["macd"]
            if macd_val == "Bullish":
                macd_fmt = _cell_format(fg_hex="1e6b30", bold=True)
            elif macd_val == "Bearish":
                macd_fmt = _cell_format(fg_hex="cc4a00", bold=True)
            else:
                macd_fmt = {}
            if macd_fmt:
                format_requests.append({
                    "repeatCell": {
                        "range": _range_spec(BUY_OPPS_SHEET_ID, current_sheet_row,
                                             current_sheet_row, 11, 12),
                        "cell": {"userEnteredFormat": macd_fmt},
                        "fields": "userEnteredFormat(textFormat)",
                    }
                })

            # Cols N-Q (EV/EBIT, Fwd P/E, ROIC, ROE — indices 13-16): green ✓ / orange ✗
            for col_idx, cell_val in ((13, ev_ebit_cell), (14, fwd_pe_cell),
                                       (15, roic_cell), (16, roe_cell)):
                if "✓" in cell_val:
                    mf_fmt = _cell_format(fg_hex="1a801a", bold=True, h_align="CENTER")
                elif "✗" in cell_val:
                    mf_fmt = _cell_format(fg_hex="cc4a00", bold=True, h_align="CENTER")
                else:
                    mf_fmt = _cell_format(h_align="CENTER")
                format_requests.append({
                    "repeatCell": {
                        "range": _range_spec(BUY_OPPS_SHEET_ID, current_sheet_row,
                                             current_sheet_row, col_idx, col_idx + 1),
                        "cell": {"userEnteredFormat": mf_fmt},
                        "fields": "userEnteredFormat(textFormat,horizontalAlignment)",
                    }
                })

            # Col R (Score col index 17): score-coloured background
            score_fmt = _cell_format(
                bg_hex=style["bg"], fg_hex=style["fg"], bold=True, h_align="CENTER"
            )
            format_requests.append({
                "repeatCell": {
                    "range": _range_spec(BUY_OPPS_SHEET_ID, current_sheet_row,
                                         current_sheet_row, 17, 18),
                    "cell": {"userEnteredFormat": score_fmt},
                    "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
                }
            })

            # Col S (Conviction Note col index 18): wrap, size 9, italic
            note_fmt = _cell_format(italic=True, font_size=9, wrap="WRAP")
            format_requests.append({
                "repeatCell": {
                    "range": _range_spec(BUY_OPPS_SHEET_ID, current_sheet_row,
                                         current_sheet_row, 18, 19),
                    "cell": {"userEnteredFormat": note_fmt},
                    "fields": "userEnteredFormat(textFormat,wrapStrategy)",
                }
            })

            current_sheet_row += 1

    # Blank row then legend
    output_rows.append([""] * 19)
    output_rows.append([_LEGEND] + [""] * 18)
    legend_row = current_sheet_row + 1
    format_requests.append({
        "repeatCell": {
            "range": _range_spec(BUY_OPPS_SHEET_ID, legend_row, legend_row, 0, 19),
            "cell": {
                "userEnteredFormat": _cell_format(
                    fg_hex="757575", italic=True, font_size=8
                )
            },
            "fields": "userEnteredFormat(textFormat)",
        }
    })

    # 6. Write all rows in a single call
    if output_rows:
        ws.append_rows(output_rows, value_input_option="USER_ENTERED")

    # 7. Apply all formatting in a single batch
    if format_requests:
        try:
            _batch_format(format_requests)
        except Exception as e:
            print(f"  [buy_opps] formatting error (non-fatal): {e}")

    return summary


# ── Dashboard tab — full layout ───────────────────────────────────────────────

def update_dashboard(pt_rows: list, buy_summary: dict, scope: str):
    """
    Writes the full Dashboard tab.
    pt_rows: all rows 2-137 from read_pt_all_rows()
    buy_summary: {total, by_score: {5,4,3,2}} from update_buy_opportunities()
    scope: "EU" or "US"
    """
    ws = _worksheet("Dashboard")
    if not ws:
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    valid = [r for r in pt_rows if r.get("ticker") and r.get("price")]

    # Buy/Hold/Sell counts
    buy_c = sum(1 for r in valid if str(r.get("rating", "")).startswith("Buy"))
    hold_c = sum(1 for r in valid if str(r.get("rating", "")).startswith("Hold"))
    sell_c = sum(1 for r in valid if str(r.get("rating", "")).startswith("Sell"))

    # Top 3 gainers / losers by 1D% (pct_1d already normalised in read_pt_all_rows)
    movers = [r for r in valid if r.get("pct_1d") is not None]
    movers_sorted = sorted(movers, key=lambda r: r["pct_1d"], reverse=True)
    gainers = movers_sorted[:3]
    losers = movers_sorted[-3:][::-1]

    # Sanity flags: >5% 1D move or volume >3× avg
    flags = []
    for r in valid:
        pct = r.get("pct_1d")
        vol = r.get("vol_ratio")
        if pct is not None and abs(pct) > 5:
            flags.append(f"{r['ticker']}: 1D {pct:+.1f}%")
        elif vol is not None and vol > 3:
            flags.append(f"{r['ticker']}: vol {vol:.1f}× avg")

    # Buy opps summary line
    bs = buy_summary or {}
    by_score = bs.get("by_score", {})
    buy_opps_line = (
        f"Buy Opportunities: {bs.get('total', 0)} stocks"
        f" | {by_score.get(5, 0)} at score 5/5"
        f" | {by_score.get(4, 0)} at score 4/5"
        f" | {by_score.get(3, 0)} at score 3/5"
    )

    # Group-level breakdown — group by internal group name (not display index string)
    from config import GROUPS, EU_SCOPE, US_SCOPE
    group_rows = []
    for group_name, cfg in GROUPS.items():
        grp = [r for r in valid if r.get("group") == group_name]
        if not grp:
            continue
        b = sum(1 for r in grp if str(r.get("rating", "")).startswith("Buy"))
        h = sum(1 for r in grp if str(r.get("rating", "")).startswith("Hold"))
        s = sum(1 for r in grp if str(r.get("rating", "")).startswith("Sell"))
        rsi_vals = [r["rsi14"] for r in grp if r.get("rsi14") is not None]
        avg_rsi = f"{sum(rsi_vals)/len(rsi_vals):.1f}" if rsi_vals else "—"
        from config import GROUP_DISPLAY_INDEX
        display_name = GROUP_DISPLAY_INDEX.get(group_name, group_name)
        group_rows.append([display_name, len(grp), b, h, s, avg_rsi])

    # Build output
    rows = [
        ["MARKET WATCHLIST DASHBOARD"],
        [f"Last Updated: {now_str}  |  Scope: {scope}"],
        [""],
        ["OVERALL SIGNALS", ""],
        [f"Total tickers tracked: {len(valid)}"],
        [f"Buy: {buy_c}  |  Hold: {hold_c}  |  Sell: {sell_c}"],
        [""],
        ["BY GROUP", "Tickers", "Buy", "Hold", "Sell", "Avg RSI"],
    ]
    rows.extend(group_rows)
    rows.extend([
        [""],
        ["TOP 3 GAINERS (1D %)", "Index", "1D%"],
    ])
    for r in gainers:
        rows.append([r["ticker"], r.get("index", ""), f"{r['pct_1d']:+.2f}%"])
    rows.extend([
        [""],
        ["TOP 3 LOSERS (1D %)", "Index", "1D%"],
    ])
    for r in losers:
        rows.append([r["ticker"], r.get("index", ""), f"{r['pct_1d']:+.2f}%"])
    rows.extend([
        [""],
        ["BUY OPPORTUNITIES"],
        [buy_opps_line],
        [""],
    ])
    if flags:
        rows.append(["DATA SANITY FLAGS (>5% 1D or >3× volume — check before acting)"])
        for f in flags[:20]:
            rows.append([f])

    ws.clear()
    ws.update("A1", rows, value_input_option="USER_ENTERED")
