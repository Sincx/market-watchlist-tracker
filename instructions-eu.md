# Market Watchlist Tracker — EU Run Instructions

**RUN_SCOPE = EU.** This run handles European-market tickers only:
- **FTSE100** (rows 27–51 of the `Price & Technicals` tab)
- **EU Morningstar group** (rows 102–115 of the `Price & Technicals` tab)

**Do NOT update** the Dashboard, Buy Opportunities, or Morningstar FV tabs — the US run at 7AM CEST handles those, reading this run's freshly written data as part of its cross-index pass.

State file: `C:\Users\Mike\.claude\scheduled-tasks\market-watchlist-tracker\sheet-config.json`

---

## Step 1 — Load spreadsheet

1. Load gsheets MCP tools (ToolSearch: `"google sheets spreadsheet values"`).
2. Read `sheet-config.json`. Use the `spreadsheetId` inside. The spreadsheet already exists — do not recreate it or reseed tabs that already exist.

---

## Step 2 — Fetch FX rates (once per run, reuse for all tickers)

Fetch all five pairs in parallel via `mcp__massive__call_api`, endpoint `/v2/last/crypto/{pair}`:

| Pair | Massive symbol |
|------|----------------|
| GBP/USD | `C:GBPUSD` |
| EUR/USD | `C:EURUSD` |
| CHF/USD | `C:CHFUSD` |
| DKK/USD | `C:DKKUSD` |
| SEK/USD | `C:SEKUSD` |

**Fallback for any pair** (Massive 429 or no-data): `mcp__alpha-vantage__TOOL_CALL` with `tool_name="CURRENCY_EXCHANGE_RATE"` and the appropriate `from_currency`/`to_currency`. Store results as `gbp_usd`, `eur_usd`, `chf_usd`, `dkk_usd`, `sek_usd`. Never re-fetch mid-run; reuse cached values.

**USD Rate derivation per currency:**
- GBX (London pence): `USD Rate = gbp_usd / 100`
- GBP: `USD Rate = gbp_usd`
- EUR: `USD Rate = eur_usd`
- CHF: `USD Rate = chf_usd`
- DKK: `USD Rate = dkk_usd`
- SEK: `USD Rate = sek_usd`

---

## Step 3 — Update FTSE100 tickers (rows 27–51)

**Massive.com rate limits:** free tier = 5 req/min. On HTTP 429: wait 15 s, retry once; if still 429 fall through.

**Dual-listed (6 tickers — safe Massive path):** AZN, SHEL, BP, GSK, RIO, VOD.
Use `mcp__massive__call_api` → `/v2/aggs/ticker/{TICKER}/range/1/day/{from}/{to}` (past 200 trading days, `store_as="{TICKER}_ohlcv"`). If the response has only `ticker` + `adjusted` columns (1 row), treat as no data and fall through. Currency = USD, USD Rate = 1.0.

**All other 19 FTSE100 tickers — skip Massive entirely** (confirmed bare-ticker collisions with unrelated US companies). Go straight to Alpha Vantage:

- **Alpha Vantage (primary for 19 non-dual-listed):** `mcp__alpha-vantage__TOOL_CALL`, `tool_name="TIME_SERIES_DAILY"`, `symbol="{TICKER}.LON"`. Parse `"Time Series (Daily)"`, sort descending. Currency = GBX, USD Rate = `gbp_usd / 100`. **GBP/GBX sanity check**: if latest close < 100, AV returned GBP not GBX (CPG recurring quirk) — store Currency = GBP, USD Rate = `gbp_usd`.
- **yfinance MCP (AV failure):** ToolSearch `mcp__yfinance__` → history tool with `{TICKER}.L`.
- **WebFetch Yahoo Finance (yfinance failure):** `https://finance.yahoo.com/quote/{TICKER}.L/history`. Parse 50+ rows.
- **stockanalysis.com (Yahoo failure):** `https://stockanalysis.com/quote/lon/{TICKER}/`. Current price + 1D% only.
- **WebSearch (all fail):** price/1D% only; note on Dashboard (deferred to US run).

**Compute indicators from OHLCV arrays** (applies to all EU tickers — FTSE100 and EU Morningstar alike):

- 1D% = (close[0] − close[1]) / close[1] × 100
- 1W% = (close[0] − close[4]) / close[4] × 100
- 1M% = (close[0] − close[20]) / close[20] × 100
- MA20/50/200 = avg of last N closes; if fewer bars available write `~MA{n}`
- RSI14: gains/losses over closes[0:15], RSI = 100 − 100 / (1 + avgGain/avgLoss)
- MACD: **use true EMA computed in Python/Node (Write+Bash), not SQL window functions** — EMA12 of closes[0..], EMA26 of closes[0..], MACD = EMA12 − EMA26, Signal = EMA9(MACD). Write `Bullish`/`Bearish`; `N/A` if <26 bars.
- Volume vs 20D avg = volume[0] / avg(volume[0:20])
- **Murphy/Nison/Bulkowski/Elder/O'Neil** and **Technical Rating** (combined score ≥5 = Buy, ≤−5 = Sell, otherwise Hold): use the exact same methodology defined in `instructions.md` Step 3.

**Write rows 27–51**: column order `Ticker | Index | Last Updated | Price | Currency | USD Rate | 1D% | 1W% | 1M% | MA20 | MA50 | MA200 | RSI14 | MACD Signal | Volume vs 20D Avg | Murphy | Nison | Bulkowski | Elder | O'Neil | Technical Rating`. Update in place, no duplicates.

**Cell formatting for 1D/1W/1M% (columns G–I, rows 27–51):** these rows have legacy PERCENT cell format (store fraction e.g. `0.0014` to display 0.14%). Use `valueInputOption: RAW` and store the fractional value — this is consistent with how prior runs have always written these rows.

---

## Step 4 — Update EU Morningstar tickers (rows 102–115)

**Ticker map** — these are the assigned rows. If a row does not yet exist in the sheet (first EU run), append it after row 101 in the correct order. If it exists, update it in place.

| Row | Tracker ID | Yahoo Ticker | Exchange | Currency | USD Rate |
|-----|-----------|--------------|----------|----------|----------|
| 102 | NWG | NWG.L | LSE | GBX | gbp_usd/100 |
| 103 | EDEN | EDEN.PA | Euronext Paris | EUR | eur_usd |
| 104 | SAP | SAP.DE | XETRA | EUR | eur_usd |
| 105 | ADYEN | ADYEN.AS | Euronext Amsterdam | EUR | eur_usd |
| 106 | COLO-B | COLO-B.CO | Copenhagen SE | DKK | dkk_usd |
| 107 | PRX | PRX.AS | Euronext Amsterdam | EUR | eur_usd |
| 108 | EKTA-B | EKTA-B.ST | Nasdaq Stockholm | SEK | sek_usd |
| 109 | GIVN | GIVN.SW | SIX Swiss Exchange | CHF | chf_usd |
| 110 | AKZA | AKZA.AS | Euronext Amsterdam | EUR | eur_usd |
| 111 | KYGA | KYGA.IR | Euronext Dublin | EUR | eur_usd |
| 112 | FCT | FCT.MI | Borsa Italiana | EUR | eur_usd |
| 113 | ALV | ALV.DE | XETRA | EUR | eur_usd |
| 114 | MC | MC.PA | Euronext Paris | EUR | eur_usd |
| 115 | ABI | ABI.BR | Euronext Brussels | EUR | eur_usd |

**NWG** (row 102): treat identically to non-dual-listed FTSE100 tickers. AV symbol = `NWG.LON`, yfinance = `NWG.L`. Currency = GBX, USD Rate = `gbp_usd / 100`.

**All other EU Morningstar tickers (rows 103–115) — fallback chain:**

1. **yfinance MCP (primary):** ToolSearch for `mcp__yfinance__` tools. Call the history/OHLCV tool with the Yahoo Ticker from the map above (e.g., `EDEN.PA`). Request 200 trading days. Compute all indicators as in Step 3.
2. **WebFetch Yahoo Finance history (yfinance failure):** `https://finance.yahoo.com/quote/{Yahoo_Ticker}/history`. Parse 50+ rows. Compute indicators.
3. **stockanalysis.com European pages (Yahoo failure):** exchange URL prefix map:

| Exchange | stockanalysis.com prefix | Example |
|----------|--------------------------|---------|
| Euronext Paris | `/quote/epa/{ticker}/history/` | `/quote/epa/eden/history/` |
| XETRA | `/quote/xtra/{ticker}/history/` | `/quote/xtra/sap/history/` |
| Euronext Amsterdam | `/quote/ams/{ticker}/history/` | `/quote/ams/adyen/history/` |
| Copenhagen | `/quote/cse/{ticker}/history/` | `/quote/cse/colo-b/history/` |
| Stockholm | `/quote/sto/{ticker}/history/` | `/quote/sto/ekta-b/history/` |
| SIX Swiss | `/quote/swx/{ticker}/history/` | `/quote/swx/givn/history/` |
| Borsa Italiana | `/quote/bit/{ticker}/history/` | `/quote/bit/fct/history/` |
| Euronext Dublin | `/quote/ise/{ticker}/history/` | `/quote/ise/kyga/history/` |
| Euronext Brussels | `/quote/ebr/{ticker}/history/` | `/quote/ebr/abi/history/` |

   Tickers in stockanalysis URLs are lowercase. If full history is not available, extract current price + 1D% and write indicators as `N/A` with reduced-confidence rating note (e.g., `Hold (0/11 signals)`).

4. **WebSearch (all fail):** current price + 1D% only; note the partial update.

**Write rows 102–115:** same 21-column format. Index column = `Morningstar-EU`. For 1D/1W/1M% in rows 102+, store the literal % number (e.g., `0.14` for 0.14%) using `valueInputOption: RAW` — **do NOT apply PERCENT cell format to rows 102+**, only plain NUMBER format.

---

## Step 5 — Fundamentals (Mondays, or missing rows)

Scope: EU tickers only (Index = FTSE100 or Morningstar-EU). Skip on non-Monday runs unless a ticker has no existing Fundamentals row.

| Ticker type | Primary URL |
|-------------|-------------|
| FTSE100 | `https://stockanalysis.com/quote/lon/{TICKER}/` |
| NWG | `https://stockanalysis.com/quote/lon/NWG/` |
| EU Morningstar (continental) | `https://stockanalysis.com/quote/{sc_prefix}/{ticker}/` (same prefixes as above without `/history/`) |
| Fallback for any | `https://finance.yahoo.com/quote/{Yahoo_Ticker}/` |

Extract: P/E ratio, EPS growth %, revenue growth %, dividend yield %, market cap, sector. Valuation band: Cheap <15× | Fair 15–25× | Elevated 25–35× | Expensive >35×. P/E N/A or negative → write `N/A`.

Write/update the ticker's row in the `Fundamentals` tab. Update `Last Updated` with today's date.

---

## Error handling

If a ticker fails all fallbacks, leave its existing row untouched and note the skip. Do not abort the run.

**Known quirks carried forward:**
- AV daily quota (25 calls/day, resets at midnight UTC) is at ~11PM UTC (1AM CEST). If quota is already exhausted by other prior-day activity, fall through to yfinance MCP for all 19 non-dual-listed FTSE100 tickers.
- CPG (Compass Group, FTSE100): AV consistently returns GBP-priced data, not GBX. Apply the magnitude sanity check (if close < 100, it's GBP).
- Massive's 50-table cache: drop in-memory tables after use (`DROP TABLE IF EXISTS {TICKER}_ohlcv`) to stay within the 50-table limit.
- The 5 tickers that appear in both DJI and NASDAQ (AAPL, MSFT, CSCO, AMGN, HON) are handled by the US run; this EU run does not touch them.
