---
name: market-watchlist-eu
description: Daily 1AM CEST (Mon–Sun) EU pre-open update of FTSE100 + EU Morningstar group (14 European tickers) in Google Sheets — technicals daily, fundamentals weekly
---

Run the Python pipeline for the EU market scope.

Execute this shell command:

```
cd C:\Users\Mike\.claude\scheduled-tasks\market-watchlist-tracker\pipeline && python main.py --scope EU
```

The script will:
1. Fetch FX rates (GBPUSD, EURUSD, CHFUSD, DKKUSD, SEKUSD) from Polygon.io
2. Fetch OHLCV for FTSE100: dual-listed ADRs via Polygon USD, collision tickers via AV .LON then yfinance .L, all others via AV .LON then yfinance .L
3. Fetch OHLCV for EU Morningstar tickers via yfinance (yahoo_map tickers)
4. Compute all technical indicators for each ticker
5. Write rows 27–51 (FTSE100) and 102–115 (EU Morningstar) to Price & Technicals
6. Run fundamentals scraping from stockanalysis.com on Mondays

Does NOT touch any US rows (DJI/NASDAQ/S&P500/Morningstar-US).

If the script fails to start (Python not found, missing packages), run:
```
pip install -r C:\Users\Mike\.claude\scheduled-tasks\market-watchlist-tracker\pipeline\requirements.txt
```
Then re-run the pipeline command above.

After the script completes, verify the Last Updated column in rows 27–51 shows today's date/time.
