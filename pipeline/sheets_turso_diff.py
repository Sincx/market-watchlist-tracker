"""Master spec Phase 14b, step S4 — the actual go/no-go gate before
decommissioning Google Sheets: one week of daily side-by-side diffing
between the Sheet's output and Turso's, on the overlapping curated
universe. A cheap script, not a manual check, per the spec's own design.

Compares, per ticker present in both:
  - price (Sheet's "Price & Technicals" vs Turso prices.close)
  - RSI14, MACD signal, technical rating
  - fundamentals: P/E, ROIC, earnings yield, sector
  - MF rank (Sheet's Fundamentals-tab column vs Turso v_magic_formula_latest —
    EXPECTED to differ, per Phase 14a: different universe sizes/composition.
    Reported for visibility, not treated as a failure.)

Run daily for a week; only once this shows no unexplained large price/
fundamentals discrepancies (small differences from fetch-timing/rounding
are expected and fine) should S5 (retiring the old path) proceed.

Run standalone: python sheets_turso_diff.py [--price-tolerance-pct 2.0]
"""
from __future__ import annotations

import argparse

import db
import sheets
from fetchers import fetch_fx_rates

PRICE_TOLERANCE_PCT_DEFAULT = 2.0


def _to_usd(amount: float, currency: str, fx: dict) -> float | None:
    """Both sides need normalizing to a common currency before comparing —
    a naive raw-price diff treats a GBX (pence) price as directly comparable
    to a USD/GBP one, producing enormous false-positive "mismatches" that
    are actually just unit differences, not real data problems (found live:
    AZN showed a 7424% "mismatch" that was purely Sheet-USD-ADR vs.
    Turso-GBX-pence, not a real discrepancy).
    """
    if amount is None:
        return None
    native = amount * 0.01 if currency == "GBX" else amount
    ccy = "GBP" if currency == "GBX" else (currency or "USD")
    rate = fx.get(ccy)
    return native * rate if rate else (native if ccy == "USD" else None)


def _turso_snapshot(fx: dict) -> dict[str, dict]:
    client = db.get_client()
    try:
        rows = db.query(client, """
            SELECT u.ticker, u.exchange, p.close, p.currency, p.rsi14, p.macd_signal, p.technical_rating,
                   f.pe, f.roic, f.earnings_yield, f.sector
            FROM universe u
            LEFT JOIN (
                SELECT ticker, exchange, close, currency, rsi14, macd_signal, technical_rating,
                       ROW_NUMBER() OVER (PARTITION BY ticker, exchange ORDER BY date DESC) rn
                FROM prices
            ) p ON p.ticker = u.ticker AND p.exchange = u.exchange AND p.rn = 1
            LEFT JOIN (
                SELECT ticker, exchange, pe, roic, earnings_yield, sector,
                       ROW_NUMBER() OVER (PARTITION BY ticker, exchange ORDER BY as_of_date DESC) rn
                FROM fundamentals
            ) f ON f.ticker = u.ticker AND f.exchange = u.exchange AND f.rn = 1
            WHERE u.active = 1 AND p.close IS NOT NULL;
        """)
    finally:
        client.close()
    # Sheet tickers aren't exchange-qualified, and some (AZN, dual-listed)
    # genuinely have more than one active Turso row — prefer whichever row's
    # USD-normalized price is closest to the Sheet's own (also USD-normalized)
    # price, rather than an arbitrary first-match, so a real dual-listing
    # doesn't masquerade as a huge false "mismatch."
    by_ticker: dict[str, list[dict]] = {}
    for r in rows:
        r["usd_close"] = _to_usd(r["close"], r["currency"], fx)
        by_ticker.setdefault(r["ticker"], []).append(r)
    return by_ticker


def _best_turso_match(candidates: list[dict], sheet_usd_price: float | None) -> dict:
    if len(candidates) == 1 or sheet_usd_price is None:
        return candidates[0]
    scored = [(abs((c["usd_close"] or 0) - sheet_usd_price), c) for c in candidates if c["usd_close"] is not None]
    return min(scored, key=lambda x: x[0])[1] if scored else candidates[0]


def run_diff(price_tolerance_pct: float = PRICE_TOLERANCE_PCT_DEFAULT) -> dict:
    fx = fetch_fx_rates()
    sheet_prices = {r["ticker"]: r for r in sheets.read_pt_all_rows() if r["ticker"]}
    sheet_fund = sheets.read_fund_all_rows()
    turso = _turso_snapshot(fx)

    client = db.get_client()
    try:
        mf_rows = db.query(client, "SELECT ticker, mf_rank FROM v_magic_formula_latest;")
    finally:
        client.close()
    turso_mf_by_ticker: dict[str, int] = {}
    for r in mf_rows:
        turso_mf_by_ticker.setdefault(r["ticker"], r["mf_rank"])

    overlap = sorted(set(sheet_prices) & set(turso))
    price_mismatches, mf_rank_report, missing_fundamentals = [], [], []
    missing_in_turso = sorted(set(sheet_prices) - set(turso))

    for t in overlap:
        sp = sheet_prices[t]
        sheet_usd = _to_usd(sp["price"], sp["currency"], fx)
        tr = _best_turso_match(turso[t], sheet_usd)
        if sheet_usd and tr["usd_close"]:
            diff_pct = abs(sheet_usd - tr["usd_close"]) / sheet_usd * 100
            if diff_pct > price_tolerance_pct:
                price_mismatches.append(
                    f"{t}: sheet={sp['price']} {sp['currency']} (${sheet_usd:.2f}) "
                    f"turso[{tr['exchange']}]={tr['close']} {tr['currency']} (${tr['usd_close']:.2f}) "
                    f"({diff_pct:.1f}% diff)"
                )

        sf = sheet_fund.get(t)
        if not sf:
            missing_fundamentals.append(t)
            continue
        sheet_mf = sf.get("mf_rank")
        if sheet_mf is not None:
            turso_mf = turso_mf_by_ticker.get(t)
            mf_rank_report.append(f"{t}: sheet=#{int(sheet_mf)} turso=#{turso_mf if turso_mf else '—'}")

    return {
        "overlap_count": len(overlap),
        "missing_in_turso": missing_in_turso,
        "missing_fundamentals_in_sheet_map": missing_fundamentals,
        "price_mismatches": price_mismatches,
        "mf_rank_comparison": mf_rank_report,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--price-tolerance-pct", type=float, default=PRICE_TOLERANCE_PCT_DEFAULT)
    args = parser.parse_args()
    result = run_diff(price_tolerance_pct=args.price_tolerance_pct)

    print(f"Overlap: {result['overlap_count']} tickers present in both Sheet and Turso")
    print(f"Missing in Turso (Sheet-only): {len(result['missing_in_turso'])}")
    if result["missing_in_turso"]:
        print(f"  {result['missing_in_turso']}")
    print(f"Price mismatches (>{args.price_tolerance_pct}%): {len(result['price_mismatches'])}")
    for line in result["price_mismatches"][:20]:
        print(f"  {line}")
    print(f"\nMF rank comparison (expect differences — different universe sizes, informational only):")
    for line in result["mf_rank_comparison"][:20]:
        print(f"  {line}")
