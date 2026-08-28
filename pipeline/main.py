"""
Entry point: python main.py --scope EU|US

EU scope → FTSE100 + Morningstar-EU  (run at 1AM CEST)
US scope → DJI + NASDAQ + S&P500 + Morningstar-US + Buy Opps  (run at 7AM CEST)
"""

import argparse
import os
import sys

# Add the pipeline directory to sys.path so modules resolve regardless of CWD
_PIPELINE_DIR = os.path.dirname(os.path.abspath(__file__))
if _PIPELINE_DIR not in sys.path:
    sys.path.insert(0, _PIPELINE_DIR)

# Load .env from the pipeline directory (not CWD)
from dotenv import load_dotenv
load_dotenv(os.path.join(_PIPELINE_DIR, ".env"))

# CURL_CA_BUNDLE → windows-ca-bundle.pem, consumed by libcurl inside curl_cffi (yfinance).
# SSL_CERT_FILE and REQUESTS_CA_BUNDLE are intentionally NOT set.
# gspread SSL is handled in sheets.py (verify=False to work around Norton SSL interception).
_curl_ca = os.getenv("CURL_CA_BUNDLE")
if _curl_ca:
    os.environ["CURL_CA_BUNDLE"] = _curl_ca


def main():
    parser = argparse.ArgumentParser(
        description="Market Watchlist Tracker — Python pipeline"
    )
    parser.add_argument(
        "--scope",
        choices=["EU", "US"],
        required=True,
        help="EU = FTSE100 + EU Morningstar; US = DJI + NASDAQ + S&P500 + US Morningstar",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch data and compute indicators but do NOT write to Google Sheets",
    )
    parser.add_argument(
        "--force-funds",
        action="store_true",
        help="Force a fundamentals refresh regardless of day (default: Mondays only)",
    )
    args = parser.parse_args()

    if args.force_funds:
        import pipeline as _pl
        _pl._is_weekly_run = lambda: True

    if args.dry_run:
        print("[DRY RUN] Sheet writes disabled.")
        import sheets as _sh
        import pipeline as _pl
        _sh.write_pt_rows_batch = lambda rows: print(f"  [DRY RUN] would write {len(rows)} rows")
        _sh.write_fundamentals_row = lambda *a, **k: print(f"  [DRY RUN] would write fundamentals")
        _sh.read_pt_all_rows = lambda: (print("  [DRY RUN] skipping read_pt_all_rows"), [])[-1]
        _sh.read_fund_all_rows = lambda: (print("  [DRY RUN] skipping read_fund_all_rows"), {})[-1]
        _sh.update_buy_opportunities = lambda *a, **k: (print(f"  [DRY RUN] would update buy opps"), {})[-1]
        _sh.update_dashboard = lambda *a, **k: print(f"  [DRY RUN] would update dashboard")
        _pl._verify_written = lambda rows: print(f"  [DRY RUN] skipping sheet verify")

    from pipeline import run_eu, run_us

    if args.scope == "EU":
        run_eu()
    else:
        run_us()


if __name__ == "__main__":
    main()
