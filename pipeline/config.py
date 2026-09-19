"""Central configuration — ticker lists, row mappings, column indices."""

SPREADSHEET_ID = "13AcDZljYJJ8ACc4WI5P-Xyt1YQH41MB56x0OihulT1s"

# ── Price & Technicals column indices (0-based) ──────────────────────────────
PT = {
    "ticker": 0, "index": 1, "last_updated": 2, "price": 3,
    "currency": 4, "usd_rate": 5, "pct_1d": 6, "pct_1w": 7, "pct_1m": 8,
    "ma20": 9, "ma50": 10, "ma200": 11, "rsi14": 12, "macd": 13,
    "vol_ratio": 14, "murphy": 15, "nison": 16, "bulkowski": 17,
    "elder": 18, "oneil": 19, "rating": 20, "burry": 21,
}
PT_HEADERS = [
    "Ticker", "Index", "Last Updated", "Price", "Currency", "USD Rate",
    "1D %", "1W %", "1M %", "MA20", "MA50", "MA200", "RSI14",
    "MACD Signal", "Volume vs 20D Avg", "Murphy", "Nison", "Bulkowski",
    "Elder", "O'Neil", "Technical Rating", "Burry Position",
]

# Michael Burry active positions (sourced from Jul 2026 Substack trading posts + SW50 series).
# Only confirmed holdings are listed — SW50-analysed-but-not-held names are intentionally blank.
BURRY_POSITIONS = {
    # Longs
    "FLUT": "Long",   # Flutter Entertainment — anti-prediction-markets play; made full position Aug 5
    "DKNG": "Long",   # DraftKings — paired with FLUT
    "MOH":  "Long",   # Molina Healthcare — Medicaid managed care, defensive value
    "ADBE": "Long",   # Adobe — "full position" per SW50 Part 2 (Fat Pitch, 0.91x IV15); reaffirmed Jun 12 Trading Post at $199.59; now largest position (with JD)
    "HCA":  "Long",   # HCA Healthcare — "simple rule" buy at 10-12x earnings; added Jun 8
    "BABA": "Long",   # Alibaba — deep-value buyback long; added Jun 12 at $111.90
    "PYPL": "Long",   # PayPal — deep-value buyback long; added Jun 12 at $40.98
    "VEEV": "Long",   # Veeva Systems — deep-value buyback long; added Jun 12 at $159.05
    "ZTS":  "Long",   # Zoetis — full position added Jul 30 at ~$76, avg cost $83; held through Aug 6 earnings
    "FISV": "Long",   # Fiserv — added on dip Aug 6, avg cost $48; first disclosed Aug 6; added again Aug 7 at $51.93
    "MELI": "Long",   # Mercado Libre — avg cost $1,611; first disclosed Aug 6; topped off Aug 7 at $1,812.39
    "JD":   "Long",   # JD.com — one of two largest positions (with ADBE); first disclosed Aug 6, no entry price given
    "LULU": "Long",   # lululemon — full position added Jul 30 (~$118); added again Aug 7 at $127.45; already tracked in Morningstar-US group, flag only
    "FMCC": "Long",   # Freddie Mac — new position added Aug 7 at $5.43; GSE reform/IPO-delay thesis
    "FNMA": "Long",   # Fannie Mae — "Toxic Twins" pairing with FMCC; first explicitly named by ticker Sep 9, 2026 Trading Post
    "BBW":  "Long",   # Build-A-Bear Workshop — pre-existing position, first disclosed Aug 27; BBW Part 1 (Aug 29) covers thesis history; 10-Q still pending as of Aug 29
    "BIRK": "Long",   # Birkenstock — full 5.2% position by mid-Aug ($35s); added to again Aug 26 (mid-$30s); "favorite shoe since high school"
    "SFM":  "Long",   # Sprouts Farmers Market — full 5.2% position added ~Aug 20 (high $70s); PE-takeout candidate thesis
    # Shorts / puts
    "NVDA": "Short",  # Nvidia — AI circular-financing thesis; added Jul 24; tactical Aug 26 call-hedge buy (single digits, mid-high $200s strike) ahead of earnings, thesis unchanged
    "MU":   "Short",  # Micron — memory oversupply; added Jul 24; increased again Aug 12 (~$924) and Aug 13 (~$1000)
    "CAT":  "Short",  # Caterpillar — US capex cycle peak; added Jul 24; trimmed 25% Aug 13 after gains; short again per Aug 26 Trading Post
    "TSLA": "Short",  # Tesla — standing short; covered Aug 13 after a decent gain
    "PLTR": "Short",  # Palantir — rent-seeking gov contractor; DIA/MARS episode; added to Aug 18-20 and Aug 26; Jan 2027 puts (low-mid $100s) re-entered Aug 10, Dec 2026 puts spared in Aug 13 de-gross
    "ORCL": "Short",  # Oracle — outright short added Aug 6 at $144.63, added to Aug 12 (~$152); Jan 2027 puts fully closed Aug 4 (profitable, may re-enter)
    "NBIS": "Short",  # Nebius — added Aug 6 at $211.77; added to Aug 12 (~$247); off-balance-sheet-liability + depreciation-schedule-extension thesis
    "CRWV": "Short",  # CoreWeave — new short added ~Aug 18-20; paired with MU as "public a long time" vs. "not public very long" contrast
    # Closed: AMAT (short disclosed Aug 4, covered Aug 13 after a decent gain)
    # ETF shorts (not tracked as individual rows): SOXX, QQQ
}

# ── Fundamentals column indices (0-based) ────────────────────────────────────
# Columns A-J: existing data. K-Q: Source + Magic Formula screening fields.
FUND = {
    "ticker": 0, "index": 1, "last_updated": 2, "pe": 3,
    "eps_growth": 4, "rev_growth": 5, "div_yield": 6,
    "mkt_cap": 7, "sector": 8, "valuation": 9,
    "source": 10,           # K: tip source (group name)
    "earnings_yield": 11,   # L: Magic Formula — EBIT/EV %
    "roic": 12,             # M: Magic Formula — ROIC %
    "ev_ebit": 13,          # N: Magic Formula — EV/EBIT ratio
    "mf_rank": 14,          # O: Magic Formula combined rank (lower = better)
    "fwd_pe": 15,           # P: Magic Formula screen — Forward P/E
    "roe": 16,              # Q: Magic Formula screen — ROE %
}

# ── Magic Formula pass/fail screening thresholds ──────────────────────────────
# Used to render PASS/FAIL columns on the Buy Opportunities tab (EV/EBIT, Fwd P/E,
# ROIC, ROE). EV/EBIT≤10x and ROIC≥15% mirror the earlier EY≥10% + ROIC≥15% screen;
# Fwd P/E≤13x carried forward from the same screen; ROE≥15% added for symmetry with ROIC.
MF_THRESHOLDS = {
    "ev_ebit_max": 10.0,
    "fwd_pe_max": 13.0,
    "roic_min": 15.0,
    "roe_min": 15.0,
}

# ── Index display names ───────────────────────────────────────────────────────
# Maps the internal group name to the real exchange index shown in sheets.
# For groups where every ticker is in the same index, one entry covers all.
GROUP_DISPLAY_INDEX = {
    "DJI":      "DJIA",
    "FTSE100":  "FTSE 100",
    "NASDAQ":   "NASDAQ 100",
    "S&P500":   "S&P 500",
}

# Per-ticker overrides for Morningstar-EU, Morningstar-US (non-S&P 500), and Burry.
TICKER_DISPLAY_INDEX = {
    # Morningstar-EU — each stock's primary European index
    "NWG":    "FTSE 100",
    "EDEN":   "CAC 40",
    "SAP":    "DAX",
    "ADYEN":  "AEX",
    "COLO-B": "OMXC25",
    "PRX":    "AEX",
    "EKTA-B": "OMX Stockholm",
    "GIVN":   "SMI",
    "AKZA":   "AEX",
    "KYGA":   "ISEQ",
    "FCT":    "FTSE MIB",
    "ALV":    "DAX",
    "MC":     "CAC 40",
    "ABI":    "BEL 20",
    # Morningstar-US — non-S&P 500 or on NASDAQ
    "TEAM":   "NASDAQ 100",  # Atlassian listed on NASDAQ
    "CMCSA":  "NASDAQ 100",  # Comcast on NASDAQ
    "GEHC":   "NASDAQ 100",  # GE Healthcare on NASDAQ
    "KHC":    "NASDAQ 100",  # Kraft Heinz on NASDAQ
    "LULU":   "NASDAQ 100",  # Lululemon on NASDAQ
    "BUD":    "NYSE",        # InBev ADR — not in a US index
    "AS":     "NYSE",        # Amer Sports — not yet in major index
    "POR":    "NYSE",        # Portland General Electric
    "RKT":    "NYSE",        # Rocket Companies
    "ADNT":   "NYSE",        # Adient
    "GIL":    "NYSE",        # Gildan (also TSX)
    "KRC":    "NYSE",        # Kilroy Realty REIT
    "YUMC":   "NYSE",        # Yum China ADR
    "SONY":   "NYSE",        # Sony ADR — primary listing TSE
    "AR":     "NYSE",        # Antero Resources
    "FER":    "NASDAQ",      # Ferrovial — relisted on Nasdaq
    "TSM":    "NYSE",        # Taiwan Semiconductor ADR — not a US index member
    "TW":     "NASDAQ",      # Tradeweb Markets
    "BSY":    "NASDAQ",      # Bentley Systems — dual class, not NASDAQ 100
    "ASML":   "NASDAQ 100",  # ASML — NASDAQ 100 constituent, not S&P 500
    "SNPS":   "NASDAQ 100",  # Synopsys — NASDAQ 100 constituent, not S&P 500
    "MGA":    "NYSE",        # Magna International — Canadian foreign private issuer, not a US index member
    "WMG":    "NASDAQ",      # Warner Music Group — NASDAQ but not NASDAQ 100
    # Burry group — per-ticker
    "FLUT":   "NYSE",        # Flutter Entertainment dual-listed (NYSE + LSE)
    "DKNG":   "NASDAQ 100",  # DraftKings
    "MU":     "NASDAQ 100",  # Micron
    "PLTR":   "NASDAQ 100",  # Palantir (moved to NASDAQ Nov 2024)
    "FRSH":   "NASDAQ",      # Freshworks (NASDAQ but not NASDAQ 100)
    "PCTY":   "NASDAQ",      # Paylocity
    "ADSK":   "NASDAQ 100",  # Autodesk
    "DOCU":   "NASDAQ 100",  # DocuSign
    "ZS":     "NASDAQ 100",  # Zscaler
    "PANW":   "NASDAQ 100",  # Palo Alto Networks
    "CRWD":   "NASDAQ 100",  # CrowdStrike
    "U":      "NYSE",        # Unity Software
    "HCA":    "NYSE",        # HCA Healthcare — added Jun 8 Trading Post
    "BABA":   "NYSE",        # Alibaba ADR — added Jun 12 Trading Post
    "PYPL":   "NASDAQ 100",  # PayPal — added Jun 12 Trading Post
    "VEEV":   "NYSE",        # Veeva Systems — added Jun 12 Trading Post
    "NBIS":   "NASDAQ",      # Nebius Group — NASDAQ but not NASDAQ 100; short added Aug 6 Trading Post
    "FISV":   "NASDAQ",      # Fiserv — NASDAQ but not NASDAQ 100; long first disclosed Aug 6 Trading Post
    "MELI":   "NASDAQ 100",  # Mercado Libre — long first disclosed Aug 6 Trading Post
    "JD":     "NASDAQ 100",  # JD.com — long first disclosed Aug 6 Trading Post
    "FMCC":   "OTC",         # Freddie Mac — OTC Pink, no major index; long added Aug 7 Trading Post
    "FNMA":   "OTC",         # Fannie Mae — OTC Pink, no major index; "Toxic Twins" pairing with FMCC, first named Sep 9 Trading Post
    "BBW":    "NYSE",        # Build-A-Bear Workshop — NYSE, not in a major index; long first disclosed Aug 27 Trading Post
    "BIRK":   "NYSE",        # Birkenstock Holding — NYSE, not in a major index; long, full 5.2% position by mid-Aug
    "SFM":    "NASDAQ 100",  # Sprouts Farmers Market — NASDAQ 100 constituent; long, full 5.2% position ~Aug 20
    "CRWV":   "NASDAQ",      # CoreWeave — NASDAQ, not in NASDAQ 100 (recent IPO); short added ~Aug 18-20
}

# Default display index for Morningstar-US and Burry tickers not in TICKER_DISPLAY_INDEX
_GROUP_FALLBACK = {
    "Morningstar-US": "S&P 500",
    "Burry":          "S&P 500",
    "Morningstar-EU": "EU",
}


def get_display_index(group_name: str, ticker: str) -> str:
    """Return the real exchange/index name for display in sheets."""
    if group_name in GROUP_DISPLAY_INDEX:
        return GROUP_DISPLAY_INDEX[group_name]
    if ticker in TICKER_DISPLAY_INDEX:
        return TICKER_DISPLAY_INDEX[ticker]
    return _GROUP_FALLBACK.get(group_name, group_name)


# ── Ticker groups ─────────────────────────────────────────────────────────────
# start_row: 1-indexed row in Price & Technicals where first ticker lives
# pct_format: "PERCENT" = store fraction (rows 2-51); "NUMBER" = store literal % (rows 52+)
GROUPS = {
    "DJI": {
        "start_row": 2,
        "exchange": "US",
        "currency": "USD",
        "pct_format": "PERCENT",
        "tickers": [
            "AAPL", "MSFT", "JPM", "V", "UNH", "HD", "PG", "JNJ", "CRM", "CAT",
            "MCD", "DIS", "GS", "AXP", "IBM", "AMGN", "HON", "CVX", "MRK", "WMT",
            "KO", "NKE", "TRV", "MMM", "CSCO",
        ],
    },
    "FTSE100": {
        "start_row": 27,
        "exchange": "UK",
        "currency": "GBX",
        "pct_format": "PERCENT",
        "av_suffix": ".LON",
        "yahoo_suffix": ".L",
        # These 6 are dual-listed ADRs — use Polygon (USD prices)
        "dual_listed": {"AZN", "SHEL", "BP", "GSK", "RIO", "VOD"},
        # These bare tickers collide with unrelated US stocks — skip Polygon entirely
        "collision_tickers": {"AAL", "BA", "TSCO", "PRU", "NG", "RR"},
        "tickers": [
            "AZN", "SHEL", "HSBA", "ULVR", "BP", "GSK", "DGE", "RIO", "REL",
            "BATS", "GLEN", "LSEG", "NG", "BARC", "VOD", "PRU", "CPG", "AAL",
            "TSCO", "BA", "RR", "STAN", "LLOY", "IMB", "SSE",
        ],
    },
    "NASDAQ": {
        "start_row": 52,
        "exchange": "US",
        "currency": "USD",
        "pct_format": "NUMBER",
        "tickers": [
            "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AVGO",
            "COST", "PEP", "ADBE", "NFLX", "AMD", "QCOM", "INTC", "TXN", "CSCO",
            "INTU", "AMGN", "HON", "SBUX", "GILD", "MDLZ", "BKNG", "ADP",
        ],
    },
    "S&P500": {
        "start_row": 77,
        "exchange": "US",
        "currency": "USD",
        "pct_format": "NUMBER",
        "tickers": [
            "BRK.B", "LLY", "XOM", "ABBV", "PFE", "ORCL", "ACN", "TMO", "DHR",
            "LIN", "VZ", "T", "PM", "BMY", "LOW", "UPS", "MS", "BLK", "SPGI",
            "SCHW", "C", "ELV", "CI", "SYK", "NEE",
        ],
    },
    "Morningstar-EU": {
        "start_row": 102,
        "exchange": "EU",
        "pct_format": "NUMBER",
        "tickers": [
            "NWG", "EDEN", "SAP", "ADYEN", "COLO-B", "PRX", "EKTA-B",
            "GIVN", "AKZA", "KYGA", "FCT", "ALV", "MC", "ABI",
        ],
        # Yahoo Finance ticker symbols for yfinance
        "yahoo_map": {
            "NWG": "NWG.L", "EDEN": "EDEN.PA", "SAP": "SAP.DE",
            "ADYEN": "ADYEN.AS", "COLO-B": "COLO-B.CO", "PRX": "PRX.AS",
            "EKTA-B": "EKTA-B.ST", "GIVN": "GIVN.SW", "AKZA": "AKZA.AS",
            "KYGA": "KRZ.IR", "FCT": "FCT.MI", "ALV": "ALV.DE",
            "MC": "MC.PA", "ABI": "ABI.BR",
        },
        # Native trading currency per ticker
        "currency_map": {
            "NWG": "GBX",
            "EDEN": "EUR", "SAP": "EUR", "ADYEN": "EUR", "PRX": "EUR",
            "AKZA": "EUR", "KYGA": "EUR", "FCT": "EUR", "ALV": "EUR",
            "MC": "EUR", "ABI": "EUR",
            "COLO-B": "DKK",
            "EKTA-B": "SEK",
            "GIVN": "CHF",
        },
        # stockanalysis.com exchange prefix for history pages
        "sa_prefix_map": {
            "NWG": "lon", "EDEN": "epa", "SAP": "etr", "ADYEN": "ams",
            "COLO-B": "cse", "PRX": "ams", "EKTA-B": "sto", "GIVN": "swx",
            "AKZA": "ams", "KYGA": "ise", "FCT": "bit", "ALV": "etr",
            "MC": "epa", "ABI": "ebr",
        },
    },
    "Morningstar-US": {
        "start_row": 116,
        "exchange": "US",
        "currency": "USD",
        "pct_format": "NUMBER",
        "tickers": [
            # Original 22 (rows 116-137)
            "RTX", "OTIS", "APH", "FICO", "ANET", "NOC", "BUD", "AMP", "TEAM",
            "DVN", "RDDT", "AS", "CMCSA", "CLX", "GEHC", "BAC", "EIX", "DTE",
            "KMX", "AGCO", "AMT", "CCI",
            # Added from Morningstar FV cross-reference (rows 138-158)
            "LAD", "POR", "RKT", "ADNT", "CTVA", "OMC", "KHC", "LULU", "GIL",
            "LEN", "KRC", "MDT", "ABT", "CPB", "ZTS", "YUMC", "BR", "SONY",
            "ICE", "HSY", "AR",
            # Added from Aug 2026 Morningstar "Best Companies to Own" growth/value screens (rows 159-166)
            "FER", "TSM", "TW", "ROL", "BSY", "ECL", "TDG", "ALB",
            # Added from Sep 2026 Morningstar screens: 4-star weekly (ASML/SNPS/CRH/MGA),
            # dividend-raisers (RMD/WMG), dividend aristocrats (AMCR/BF.B/HRL/KMB) (rows 167-176)
            "ASML", "SNPS", "CRH", "MGA", "RMD", "WMG", "AMCR", "BF.B", "HRL", "KMB",
        ],
    },
    # Stocks mentioned by Michael Burry in his 2026 Substack trading posts and SW50 series.
    # Active positions: FLUT(closed Sep 11)/DKNG/MOH/HCA/BABA/PYPL/VEEV/FISV/MELI/JD/FMCC/FNMA/BBW/BIRK/SFM (longs);
    #   MU/PLTR/NBIS/ORCL/CRWV (shorts/puts).
    # SW50 analyses: software & payments valuation series (Parts 1–2).
    # Excludes: NVDA/CAT/TSLA/ORCL/ADBE/INTU/CRM/ZTS/LULU (already tracked in other groups).
    # Not tracked (no pipeline support): 0700.HK (Tencent), 3690.HK (Meituan), TPW.AX/TPLWF (Temple & Webster),
    #   Samsung Electronics (005930.KS).
    # Closed: AMAT (short disclosed Aug 4, covered Aug 13).
    # ETF shorts (not tracked as individual rows): SOXX, QQQ.
    "Burry": {
        "start_row": 177,   # shifted down to make room for 10 new Morningstar-US stocks (Sep 2026 screens)
        "exchange": "US",
        "currency": "USD",
        "pct_format": "NUMBER",
        "tickers": [
            # Active positions (Jul 2026 trading posts)
            "FLUT", "DKNG", "MOH", "MU", "PLTR",
            # SW50 Part 1 — Office SaaS
            "PAYC", "FRSH", "PCTY",
            # SW50 Part 2 — Productivity & Cybersecurity
            "ADSK", "DOCU", "ZS", "PANW", "CRWD", "U",
            # Jun 2026 Trading Posts (filed 2026-08-04)
            "HCA", "BABA", "PYPL", "VEEV",
            # Aug 6, 2026 Trading Post — new short (NBIS) + first-disclosed longs (FISV, MELI, JD)
            "NBIS", "FISV", "MELI", "JD",
            # Aug 7, 2026 Trading Post — new long (FMCC); LULU already tracked in Morningstar-US
            "FMCC",
            # Aug 27, 2026 Trading Post — new long (BBW), first disclosed as a pre-existing position
            "BBW",
            # Aug 18-20, 2026 Trading Post & Short Thoughts — new longs (BIRK, SFM); new short (CRWV)
            "BIRK", "SFM", "CRWV",
            # Sep 9, 2026 Trading Post — FNMA first explicitly named (pre-existing "Toxic Twins" pairing with FMCC)
            "FNMA",
        ],
    },
}

EU_SCOPE = ["FTSE100", "Morningstar-EU"]
US_SCOPE = ["DJI", "NASDAQ", "S&P500", "Morningstar-US", "Burry"]

# Tickers where AV returns GBP prices instead of GBX (if latest close < threshold)
GBP_THRESHOLD = 100  # price < 100 → assume GBP, not GBX

# Buy Opportunities tab sheet ID (verified via sheets_get_metadata)
BUY_OPPS_SHEET_ID = 1030352506

# Priority for Buy Opps deduplication (lower = higher priority group)
GROUP_PRIORITY = {
    "DJI": 1, "FTSE100": 2, "S&P500": 3,
    "NASDAQ": 4, "Morningstar-US": 5, "Morningstar-EU": 6, "Burry": 7,
}

# Hex colours for Buy Opps formatting (converted to Sheets API RGB 0-1 floats)
def _hex(h):
    h = h.lstrip("#")
    return {k: int(h[i:i+2], 16)/255 for k, i in zip("rgb", (0, 2, 4))}

SCORE_STYLE = {
    5: {"divider_bg": _hex("12641f"), "divider_fg": _hex("ffffff"), "row_bg": _hex("d9ead3")},
    4: {"divider_bg": _hex("34a853"), "divider_fg": _hex("ffffff"), "row_bg": _hex("f0f9f0")},
    3: {"divider_bg": _hex("fbbc04"), "divider_fg": _hex("33302e"), "row_bg": _hex("fef9e7")},
    2: {"divider_bg": _hex("f9ab00"), "divider_fg": _hex("33302e"), "row_bg": _hex("fce8b2")},
}
