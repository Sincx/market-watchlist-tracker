# Market Watchlist Tracker — Instructions

**RUN_SCOPE = US.** This run handles US-market tickers and produces the Dashboard, Buy Opportunities, and Morningstar FV tab updates. The EU run (1AM CEST) handles FTSE100 + EU Morningstar tickers and has already written rows 27–51 and 102–115 by the time this run fires at 7AM CEST.

You maintain a Google Sheets-based watchlist tracker. Each run updates price/technicals for all US-scope watchlist tickers, and once a week refreshes fundamentals. You also maintain the Dashboard, Buy Opportunities, and Morningstar FV tabs.

State file: `C:\Users\Mike\.claude\scheduled-tasks\market-watchlist-tracker\sheet-config.json`

## Step 1 — Load or create the spreadsheet

1. Load the gsheets MCP tools if not already loaded (ToolSearch: `"google sheets spreadsheet values"`).
2. Read `sheet-config.json`. If it exists and has a `spreadsheetId`, use that spreadsheet — **do not** recreate tabs or reseed existing data.
3. If `sheet-config.json` does not exist (first run ever), bootstrap:
   - Create a new spreadsheet titled "Market Watchlist Tracker".
   - Create these tabs: `Dashboard`, `Watchlist`, `Price & Technicals`, `Fundamentals`, `Buy Opportunities`, `Morningstar FV`.
   - Seed the `Watchlist` tab with columns: `Index | Ticker | Exchange Suffix | Enabled`, using the lists in **Appendix: Initial Watchlist** below. `Enabled` defaults to `TRUE` for all.
   - Add header rows:
     - `Price & Technicals`: `Ticker | Index | Last Updated | Price | Currency | USD Rate | 1D % | 1W % | 1M % | MA20 | MA50 | MA200 | RSI14 | MACD Signal | Volume vs 20D Avg | Murphy | Nison | Bulkowski | Elder | O'Neil | Technical Rating`
     - `Fundamentals`: `Ticker | Index | Last Updated | P/E | EPS Growth % | Revenue Growth % | Dividend Yield % | Market Cap | Sector | Valuation Band | Source | Earnings Yield % | ROIC % | EV/EBIT | MF Rank | Fwd P/E | ROE %`
     - `Dashboard`: leave blank — Step 5 populates it every run.
   - Write the new spreadsheet's ID to `sheet-config.json` as `{"spreadsheetId": "<id>"}`.

## Step 2 — Determine today's scope

- Read the `Watchlist` tab. Process only rows where `Enabled` is `TRUE` **and** `Index` is one of: `DJI`, `NASDAQ`, `S&P500`, `Morningstar-US`.
- **Fundamentals day**: if today is Monday, refresh fundamentals for all US-scope enabled tickers. Any other day, only refresh fundamentals for US-scope tickers that have **no existing row** in the Fundamentals tab (newly added tickers).

## Step 3 — Update price & technicals (every run)

Run four background `general-purpose` agents in parallel, one per index group. Give each agent the exact row numbers so writes don't collide. Each agent writes directly to the sheet.

| Agent | Index | Row range | Tickers |
|-------|-------|-----------|---------|
| DJI agent | DJI | 2–26 | 25 tickers |
| NASDAQ agent | NASDAQ | 52–76 | 25 tickers |
| S&P 500 agent | S&P500 | 77–101 | 25 tickers |
| Morningstar-US agent | Morningstar-US | 116–137 | 22 tickers |

**Prompt each agent with:**
- The exact row range and ticker list
- The complete fallback chain and indicator methodology below
- "Do not treat rate-limit waits as background tasks — pause inline and retry in the same turn. Write data directly to the sheet; do not report data back through the orchestrator."

**Massive.com rate limits:** free tier = 5 req/min (≥13 s between calls). On HTTP 429: wait 15 s, retry once; if still 429 fall to AV. Use `store_as="{TICKER}_ohlcv"` to cache; run indicator math via a follow-up `query_data` SQL call. Drop tables after use — 50-table cache limit. **`WITH RECURSIVE` and `generate_series()` are blocked — use window functions (`AVG() OVER`, `LAG()`) for SMA/RSI; use Python/Node (Write+Bash) for true EMA/MACD.**

**Fallback chain for US tickers (DJI, NASDAQ, S&P500, Morningstar-US):**

- **Primary**: `mcp__massive__call_api` → `/v2/aggs/ticker/{TICKER}/range/1/day/{from}/{to}` (past 200 trading days). For `BRK.B` use dotted format. If response has only `ticker`+`adjusted` columns (1 row), treat as no data and fall through.
- **On 429**: wait 15 s, retry once. If still 429, fall through.
- **Alpha Vantage**: `mcp__alpha-vantage__TOOL_CALL`, `tool_name="TIME_SERIES_DAILY"`, `symbol="{TICKER}"`, default compact. Parse `"Time Series (Daily)"`, sort descending. On AV 429: wait 1 s, retry once.
- **yfinance MCP**: ToolSearch `mcp__yfinance__` → history tool for `{TICKER}`.
- **WebFetch Yahoo Finance**: `https://finance.yahoo.com/quote/{TICKER}/history`. Parse 50+ rows.
- **stockanalysis.com**: `https://stockanalysis.com/stocks/{TICKER}/history/`. Current price + 1D% only.
- **WebSearch**: last resort.

**Currency for US tickers**: all USD. `Currency = USD`, `USD Rate = 1.0`.

**Compute indicators** (write Python/Node via Write+Bash for MACD — do not rely on SQL EMA):

- 1D% = (close[0] − close[1]) / close[1] × 100
- 1W% = (close[0] − close[4]) / close[4] × 100
- 1M% = (close[0] − close[20]) / close[20] × 100
- MA20 = avg(close[0:20]); MA50 = avg(close[0:50]); MA200 = avg(close[0:200]). If fewer bars: `~MA{n}`.
- RSI14: gains/losses over closes[0:15], RSI = 100 − 100 / (1 + avgGain/avgLoss).
- MACD: true EMA12 and EMA26 of closes, MACD = EMA12 − EMA26, Signal = EMA9(MACD). `Bullish` if MACD > Signal, `Bearish` if MACD < Signal, `N/A` if <26 bars.
- Volume vs 20D avg = volume[0] / avg(volume[0:20]).

**5 Technical Analyst framework scores** (one word each):
- **Murphy** (trend): price vs MA200 = primary trend; price vs MA50 = secondary; 10-day HH/HL pattern. `Bullish` / `Neutral` / `Bearish`.
- **Nison** (candlesticks): last 10 days OHLC — identify single/two/three-candle pattern. `Bullish` / `Neutral` / `Bearish`.
- **Bulkowski** (chart patterns): within 15% of 52w high? Continuation/reversal pattern visible? `Bullish` / `Neutral` / `Bearish`.
- **Elder** (triple screen): MACD histogram trending up + RSI vs 50 on tide direction. `Bullish` / `Neutral` / `Bearish`.
- **O'Neil** (CAN SLIM proxy): volume confirming price direction + near 52w high + positive MACD. `Strong` / `Moderate` / `Weak`.

**Technical Rating** — combined score (range −11 to +11):
- Traditional (+1/−1/0 each): Price vs MA20, Price vs MA50, Price vs MA200, RSI (40–70 = +1; >70 or <30 = −1; 30–40 = 0), MACD (bullish = +1, bearish = −1), Volume (>1.5× avg + price up = +1; >1.5× + price down = −1).
- Framework (+1/0/−1 each): Murphy, Nison, Bulkowski, Elder; O'Neil (Strong = +1, Moderate = 0, Weak = −1).
- Score ≥ 5 → `Buy`; ≤ −5 → `Sell`; otherwise → `Hold`. If any indicator N/A, omit from count: e.g., `Hold (7/11 signals)`. **Always include the signal count caveat, even at 0/11.**

**Write/update each ticker's row** (update in place, never duplicate). Column order: `Ticker | Index | Last Updated | Price | Currency | USD Rate | 1D % | 1W % | 1M % | MA20 | MA50 | MA200 | RSI14 | MACD Signal | Volume vs 20D Avg | Murphy | Nison | Bulkowski | Elder | O'Neil | Technical Rating`.

**Row ownership — US run must ONLY write to these rows:**
- Rows 2–26 (DJI)
- Rows 52–76 (NASDAQ)
- Rows 77–101 (S&P500)
- Rows 116–137 (Morningstar-US)

**Rows 27–51 (FTSE100) and rows 102–115 (EU Morningstar) are owned exclusively by the EU run. Do not write to them under any circumstances — not to fix formatting, not to update a stale value, not as part of any batch write that spans row ranges.**

**Cell formatting notes (for rows this run writes):**
- Rows 2–26 (DJI): PERCENT format for columns G–I (stores fraction, e.g., `0.0014` displays "0.14%"). Write with `valueInputOption: RAW` and store the fractional value.
- Rows 52–137 (NASDAQ/S&P500/Morningstar-US): plain NUMBER 0.00 format for G–I (stores literal %, e.g., `0.14` for 0.14%). If a future run sees G52:I101 re-inherit PERCENT format: reformat that range to plain `0.00`. New rows 116–137 should always use plain NUMBER format.
- When computing Dashboard gainers/losers or Buy Opportunities scoring across all rows: **multiply rows 2–51 raw 1D/1W/1M% values by 100** before comparing against rows 52–137, to account for the PERCENT vs NUMBER convention difference.
- Always fetch with `valueRenderOption: UNFORMATTED_VALUE` when reading percentage cells.

**Always write the full 21-column row** for every ticker — never omit Currency/USD Rate even when they seem "obviously" USD/1.0. Column misalignment from skipped cells has caused bugs in prior runs.

## Step 4 — Update fundamentals (Mondays, or missing rows)

For each US-scope enabled ticker (skip on non-Monday runs unless the ticker has no existing Fundamentals row):

- **Primary**: WebFetch `https://stockanalysis.com/stocks/{TICKER}/`. Extract: P/E ratio, forward P/E, EPS growth %, revenue growth %, dividend yield %, market cap, sector. Also fetch `https://stockanalysis.com/stocks/{TICKER}/financials/ratios/` for ROIC %, ROE %, and EV/EBIT (used to derive Earnings Yield = 100/EV/EBIT).
- **yfinance MCP fallback**: ToolSearch `mcp__yfinance__` → info/fundamentals tool.
- **Yahoo Finance fallback**: `https://finance.yahoo.com/quote/{TICKER}/`.
- **If all fail**: leave existing row untouched; note the skip on Dashboard.

Valuation band: `Cheap` <15× | `Fair` 15–25× | `Elevated` 25–35× | `Expensive` >35×. P/E negative or unavailable → `N/A`.

Write/update the ticker's row in the `Fundamentals` tab. Update `Last Updated` with today's date.

## Step 5 — Update the Dashboard tab

Read back the full `Price & Technicals` tab (all rows 2–137, both EU and US sections — EU was updated by the 1AM run, US just updated). Compute:

- Last run timestamp
- Count of Buy / Hold / Sell across all rows 2–137
- Top 3 gainers and top 3 losers by 1-day % (apply the ×100 multiplier for rows 2–51 when comparing across all rows — see Step 3 formatting notes)
- Buy Opportunities summary line (from Step 6 — write after Step 6 completes)

Flag any ticker with a 1D move > 5% or volume > 3× average on the Dashboard as a "data sanity check" item — note it but don't alter the value.

## Step 6 — Refresh the Buy Opportunities tab

After Steps 3, 4, and 5 are complete, rebuild the **Buy Opportunities** tab (sheetId: `1030352506` — verify via `sheets_get_metadata` if unsure).

### Filter

Cross-reference the now-updated Price & Technicals (rows 2–137) and Fundamentals tabs. A ticker qualifies if:
- `Price & Technicals`.Technical Rating = `Buy`
- `Fundamentals`.Valuation Band ∈ {`Cheap`, `Fair`}

**Deduplication** (same ticker in multiple indices): keep only one instance in priority order: DJI > FTSE100 > S&P500 > NASDAQ > Morningstar-US > Morningstar-EU.

### Score each qualifying ticker (out of 5)

| Signal | Points |
|--------|--------|
| Valuation Band = Cheap | 2 |
| Valuation Band = Fair | 1 |
| Technical Rating = Buy (always true by filter) | 1 |
| RSI(14) in range 40–70 | 1 |
| MACD Signal = Bullish | 1 |

Sort: Score desc → Valuation (Cheap before Fair) → Ticker alphabetical.

### Clear and rewrite data rows

Rows 1–4 of the Buy Opportunities tab are static (title, updated-date, blank, headers) — **do not overwrite them**. Clear all content from row 5 downward first. Update the date in row 2 column A.

For each score tier with qualifying tickers (check 5, 4, 3, 2 — skip empty):

1. Write a **section divider row** (A only, rest blank):
   - Score 5: `⭐⭐⭐⭐⭐  SCORE 5 / 5  ·  Cheap valuation + Buy signal + Healthy RSI (40–70) + Bullish MACD`
   - Score 4: `⭐⭐⭐⭐  SCORE 4 / 5  ·  Good setup: one signal missing`
   - Score 3: `⭐⭐⭐  SCORE 3 / 5  ·  Watch list: fair value + buy signal, but RSI extended or MACD bearish`
   - Score 2: `⭐⭐  SCORE 2 / 5  ·  Weak setup: two signals missing`

2. Write one **data row** per qualifying ticker. Column order (A–S):
   `# | Ticker | Index | Sector | Price | Currency | Valuation | P/E | EPS Gr% | Div Yield% | RSI (14) | MACD | 1M% | EV/EBIT | Fwd P/E | ROIC | ROE | Score /5 | Conviction Note`
   - **# column**: sequential rank across all tiers (1, 2, 3…)
   - **1M% column**: use the value from Price & Technicals (apply ×100 for rows 2–51 where stored as fraction).
   - **EV/EBIT, Fwd P/E, ROIC, ROE (N–Q)**: Magic Formula pass/fail screen, sourced from the Fundamentals tab (columns N, P, M, Q respectively). Each cell renders as `{value}{x or %} {✓ or ✗}` (e.g. `8.4x ✓`, `22.0% ✗`), or `—` if the source value is missing. Thresholds: EV/EBIT ≤10x, Fwd P/E ≤13x, ROIC ≥15%, ROE ≥15%. This is a per-metric pass/fail display, independent of the MF# combined rank noted below — no separate "Magic Formula" row section exists below the picks anymore (retired in favour of these columns).
   - **Conviction Note**: one concise sentence combining key signals; may still be prefixed with `MF#N` (Greenblatt combined Earnings-Yield + ROIC rank across the whole universe, lower = better) when available from the Fundamentals tab.

After all tier sections, write one blank row then the legend row:
`★ Score: Cheap=2pts, Fair=1pt | Technical Buy=1pt | RSI 40–70=1pt | MACD Bullish=1pt | Max 5pts   ·   — = data not available   ·   Magic Formula screen (✓ pass / ✗ fail): EV/EBIT≤10x | Fwd P/E≤13x | ROIC≥15% | ROE≥15%   ·   MF# in Conviction Note = Greenblatt combined EY+ROIC rank (lower = better; excludes Financials/Utilities)`

### Formatting

**Section divider rows:**
- Score 5: background `#12641f`, white bold text
- Score 4: background `#34a853`, white bold text
- Score 3: background `#fbbc04`, dark bold text `#33302e`
- Score 2: background `#f9ab00`, dark bold text

**Data row backgrounds:** Score 5: `#d9ead3` | Score 4: `#f0f9f0` | Score 3: `#fef9e7` | Score 2: `#fce8b2`

**Column G (Valuation):** `Cheap` → bg `#6bc185`, bold dark-green `#1e5631`, centred. `Fair` → bg `#c9daf8`, bold dark-blue `#1a4a8a`, centred.

**Column L (MACD):** `Bullish` → bold green `#1e6b30`. `Bearish` → bold orange `#cc4a00`.

**Columns N–Q (EV/EBIT, Fwd P/E, ROIC, ROE):** centred; cells containing `✓` → bold green `#1a801a`; cells containing `✗` → bold orange `#cc4a00`.

**Column R (Score):** 5/5 → bg `#12641f`, white bold. 4/5 → bg `#34a853`, white bold. 3/5 → bg `#fbbc04`, dark bold. 2/5 → bg `#f9ab00`, dark bold.

**Column S (Conviction Note):** `wrapStrategy: WRAP`, font size 9, italic.

**Legend row:** 8pt, italic, grey `#757575`, no background.

### Update Dashboard

Add a "BUY OPPORTUNITIES" summary line to the Dashboard:
`Buy Opportunities: {N} stocks | {X} at score 5/5 | {Y} at score 4/5 | {Z} at score 3/5`

## Step 7 — Create/Maintain Morningstar FV tab

Check if the `Morningstar FV` tab exists (via `sheets_get_metadata`).

### If tab does NOT exist (first run with this feature)

1. Create it: `sheets_insert_sheet`, name = `Morningstar FV`.
2. Write header row (row 1), columns A–R:
   `Ticker | Name | Exchange | Sector | Moat | Stars | Fair Value | FV Currency | Discount @ Flag | Price @ Flag | Current Price | Price Currency | Current Discount % | Technical Rating | RSI14 | MACD Signal | Date Flagged | Status Notes`
3. Read `C:\Users\Mike\Documents\Fred\Fred\wiki\finance\morningstar\morningstar-watchlist.md`. Extract every stock from all sections (Wide Moat, Narrow Moat, No Moat, Q3 Sector Picks, No FV Data) and write one row per stock with the static data available (columns A–J, Q–R).
   - For tickers with `—` as FV (Q3 sector picks or no-FV group), write `—` in Fair Value and FV Currency columns.
   - For Stars that are not a ★ count but a status note (e.g., "Opportunity", ">20% discount"), write the note in the Stars column.
4. In columns K–P (Current Price through MACD Signal), write VLOOKUP formulas for each data row. Use `valueInputOption: USER_ENTERED` so formulas are evaluated:
   - **K (Current Price):** `=IFERROR(VLOOKUP(A{row},'Price & Technicals'!$A:$U,4,FALSE),"N/A")`
   - **L (Price Currency):** `=IFERROR(VLOOKUP(A{row},'Price & Technicals'!$A:$U,5,FALSE),"N/A")`
   - **M (Current Discount %):** `=IFERROR(IF(AND(G{row}<>"—",G{row}<>"",H{row}=L{row},K{row}<>"N/A"),ROUND((K{row}-G{row})/G{row}*100,1),"N/A"),"N/A")`
     The `H{row}=L{row}` guard ensures discount is only computed when FV Currency and Price Currency match. If they differ (e.g., CPG FV in USD but LSE-listed in GBX), the cell shows `N/A`.
   - **N (Technical Rating):** `=IFERROR(VLOOKUP(A{row},'Price & Technicals'!$A:$U,21,FALSE),"N/A")`
   - **O (RSI14):** `=IFERROR(VLOOKUP(A{row},'Price & Technicals'!$A:$U,13,FALSE),"N/A")`
   - **P (MACD Signal):** `=IFERROR(VLOOKUP(A{row},'Price & Technicals'!$A:$U,14,FALSE),"N/A")`
5. Freeze row 1. Bold header row. Set column widths: A–C narrow (80px), D wide (160px), rest default.

### If tab already exists

- Spot-check 3–4 rows to verify VLOOKUP formulas are still present in columns K–P. If missing/broken, rewrite them.
- Check if all tickers from the morningstar-watchlist.md are present in the tab. If any are missing (new articles ingested since last seed), add them.
- No other action needed — the VLOOKUPs auto-pull from Price & Technicals whenever that tab is updated.

---

## Error handling

If any source is unreachable for a ticker, skip it (leave existing row untouched) and note on Dashboard. Don't fail the whole run over one ticker.

**Do not trust agent self-reported tallies.** After all four background agents complete, read back the actual written data from the `Price & Technicals` tab (via `sheets_batch_get_values` + Python parsing with `valueRenderOption: UNFORMATTED_VALUE`) to compute the true Buy/Hold/Sell count and verify at least one representative row per agent actually updated (check `Last Updated` date). Agents have previously returned `completed` status while having written zero data due to internal "background pacing timer" reasoning — if `Last Updated` still shows yesterday's date for a row that should have been updated, the agent stalled: resume it with an explicit instruction to push the write through inline.

**MACD**: always compute via true EMA in Python/Node, never SQL window functions. An SMA/EMA discrepancy in prior runs caused different agents to produce different ratings for the same ticker (e.g., AAPL appearing in both DJI and NASDAQ rows diverging).

**BRK.B**: use dotted format on Massive — `BRK.B` works directly, no need for `BRK-B` alternate.

---

## Appendix: Initial Watchlist

**DJI** (rows 2–26): AAPL, MSFT, JPM, V, UNH, HD, PG, JNJ, CRM, CAT, MCD, DIS, GS, AXP, IBM, AMGN, HON, CVX, MRK, WMT, KO, NKE, TRV, MMM, CSCO

**FTSE100** (rows 27–51, all `.L` suffix for yfinance/Yahoo; `.LON` for AV): AZN, SHEL, HSBA, ULVR, BP, GSK, DGE, RIO, REL, BATS, GLEN, LSEG, NG, BARC, VOD, PRU, CPG, AAL, TSCO, BA, RR, STAN, LLOY, IMB, SSE

**NASDAQ** (rows 52–76): AAPL, MSFT, GOOGL, AMZN, NVDA, META, TSLA, AVGO, COST, PEP, ADBE, NFLX, AMD, QCOM, INTC, TXN, CSCO, INTU, AMGN, HON, SBUX, GILD, MDLZ, BKNG, ADP

**S&P500** (rows 77–101): BRK.B, LLY, XOM, ABBV, PFE, ORCL, ACN, TMO, DHR, LIN, VZ, T, PM, BMY, LOW, UPS, MS, BLK, SPGI, SCHW, C, ELV, CI, SYK, NEE

**EU Morningstar** (rows 102–115, written by EU run): NWG, EDEN, SAP, ADYEN, COLO-B, PRX, EKTA-B, GIVN, AKZA, KYGA, FCT, ALV, MC, ABI

**Morningstar-US** (rows 116–137): RTX, OTIS, APH, FICO, ANET, NOC, BUD, AMP, TEAM, DVN, RDDT, AS, CMCSA, CLX, GEHC, BAC, EIX, DTE, KMX, AGCO, AMT, CCI

Note: several tickers appear in multiple indices (AAPL in DJI + NASDAQ; LIN, LOW, DHR, SCHW in S&P500 AND appear on Morningstar watchlist — deduplicate in Buy Opportunities as per Step 6 rules). Each occurrence still gets its own row in Price & Technicals.

**FTSE100 bare-ticker collisions (never use Massive for these):** AAL=American Airlines, BA=Boeing, TSCO=Tractor Supply, PRU=Prudential Financial, NG=unrelated US small-cap, RR=unrelated ~$2–3 US stock.

**Morningstar-US ticker notes:** BUD = AB InBev NYSE ADR (tracked separately from ABI.BR in EU group). AS = Amer Sports (NYSE). TEAM = Atlassian (NASDAQ-listed). All 22 are USD-priced via Massive primary.
