---
name: market-watchlist-tracker
description: Daily 7AM CEST (Mon–Sun) US run — updates DJI/NASDAQ/S&P500/Morningstar-US watchlist in Google Sheets, then rebuilds Buy Opportunities tab using all rows including EU data written by the 1AM CEST EU run
---

Run the Python pipeline for the US market scope.

Execute this shell command:

```
cd C:\Users\Mike\.claude\scheduled-tasks\market-watchlist-tracker\pipeline && python main.py --scope US
```

The script will:
1. Fetch OHLCV data from Polygon.io (primary) with yfinance fallback for all US tickers
2. Compute all technical indicators (RSI, MACD, MAs, framework scores, Technical Rating)
3. Write rows 2–26 (DJI), 52–76 (NASDAQ), 77–101 (S&P500), 116–137 (Morningstar-US) to Price & Technicals
4. Update the Buy Opportunities tab across all groups
5. Run fundamentals scraping from stockanalysis.com on Mondays

Do NOT touch rows 27–51 (FTSE100) or 102–115 (EU Morningstar) — those are owned by the EU run.

If the script fails to start (Python not found, missing packages), run:
```
pip install -r C:\Users\Mike\.claude\scheduled-tasks\market-watchlist-tracker\pipeline\requirements.txt
```
Then re-run the pipeline command above.

After the script completes, verify the Last Updated column in rows 2–26 shows today's date/time.
