import os, sys
sys.path.insert(0, r"C:\Users\Mike\.claude\scheduled-tasks\market-watchlist-tracker\pipeline")
import db, filings, owners_earnings as oe

# GAAP overstatement (%) as published in Burry's "AP SBC: The Tragic Algebra Recurrence" (2026-04-07),
# ~10 fiscal years to FY2025. CRM is the post's dE (54.7%) converted: 1/dE - 1.
BURRY = {"GOOGL": 12.8, "META": 19.98, "ADBE": 13.2, "AAPL": 7.5, "MSFT": 9.8, "AMZN": 21.0, "NFLX": 22.9,
         "NVDA": 28.9, "AMD": 32.6, "INTU": 32.6, "COST": 7.1, "ADP": 1.5, "CSCO": 4.6, "FAST": 1.4,
         "LRCX": 49.1, "CDNS": 26.9, "SNPS": 30.8, "ADI": 26.6, "CRM": (1 / 0.547 - 1) * 100}

c = db.get_client()
print(f"{'tkr':6s} {'burry%':>7s} {'ours%':>7s} {'diff':>6s} {'yrs':>4s} {'Ccov':>5s}  skipped")
for t, b in BURRY.items():
    fin = filings.load_financials(c, t, "US")
    p = oe.pooled(fin["annual"])
    ours = p["overstatement"] * 100 if p["overstatement"] is not None else None
    skipped = "; ".join(f"{y['period_end'][:4]}:{y['reason'][:28]}" for y in p["years"] if not y["included"])
    print(f"{t:6s} {b:7.1f} {('%.1f' % ours) if ours is not None else 'n/a':>7s} "
          f"{('%+.1f' % (ours - b)) if ours is not None else '':>6s} {p['n_included']:>4d} "
          f"{('%.0f%%' % (p['c_coverage'] * 100)) if p['c_coverage'] is not None else '-':>5s}  {skipped[:110]}")
sys.stdout.flush()
os._exit(0)
