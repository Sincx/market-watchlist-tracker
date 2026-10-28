"""Regression harness: our IV15 reconstruction against the IV15 values Burry has published.

Needs the tickers' SEC facts loaded (python filings.py --ticker T1 T2 ...).
His figures are for May-June 2026, ours come from the latest filings on file, so some
drift is expected. His owners'-earnings figures are "this year" (forward) numbers.

  python check_iv15_vs_burry.py
"""
from __future__ import annotations

import os
import sys

import db
import filings
import iv15
import owners_earnings as oe

# ticker: (IV15 $/share, owners' earnings $ as stated or None, source)
BURRY = {
    "ADBE": (262.00, 7.7e9, "SW50 pt2 (2026-05-28)"),
    "LULU": (155.81, None, "LULU (2026-06-25)"),
    "CRM": (69.81, 7.3e9, "SW50 pt1 (2026-05-15)"),
    "INTU": (114.03, 2.8e9, "SW50 pt2"),
    "ADSK": (66.22, 1.1e9, "SW50 pt2"),
    "WDAY": (21.37, 260e6, "SW50 pt1"),
    "NOW": (43.56, 620e6, "SW50 pt1"),
    "PAYC": (91.38, 650e6, "SW50 pt1"),
    "PCTY": (29.47, 315e6, "SW50 pt1"),
    "HUBS": (39.94, 90e6, "SW50 pt1"),
    "FRSH": (5.65, 90e6, "SW50 pt1"),
    "MNDY": (55.21, 90e6, "SW50 pt1"),
    "DOCU": (11.37, 195e6, "SW50 pt2"),
    "PANW": (31.62, None, "SW50 pt2"),
    "CRWD": (27.84, None, "SW50 pt2"),
    "ZS": (21.62, -200e6, "SW50 pt2 (negative OE, still an IV15: Stage 0)"),
    "U": (13.03, 40e6, "SW50 pt2 (inflecting: Stage 0)"),
}


def main(tickers: list[str] | None = None) -> None:
    client = db.get_client()
    print(f"{'tkr':5s} {'his':>8s} {'ours':>8s} {'ratio':>6s} {'(A)':>7s} {'(B)':>7s} | {'OE his':>8s} {'OE ours':>8s} | "
          f"{'dE':>5s} {'ROIC':>5s} {'cagr':>5s} {'g1':>5s}  note")
    rows = []
    for t, (his, his_oe, _src) in BURRY.items():
        if tickers and t not in tickers:
            continue
        fin = filings.load_financials(client, t, "US")
        pooled = oe.pooled(fin["annual"])
        res = iv15.compute_iv15(fin, pooled)
        i = res["inputs"]
        ours = res["iv15"]
        oe0 = i.get("oe0")
        print(f"{t:5s} {his:8.2f} {('%.2f' % ours) if ours else 'n/a':>8s} {('%.2f' % (ours / his)) if ours else '':>6s} "
              f"{('%.1f' % i['iv15_end_point_a']) if 'iv15_end_point_a' in i else '':>7s} "
              f"{('%.1f' % i['iv15_end_point_b']) if 'iv15_end_point_b' in i else '':>7s} | "
              f"{(his_oe / 1e6 if his_oe else float('nan')):8.0f} {(oe0 / 1e6 if oe0 is not None else float('nan')):8.0f} | "
              f"{('%.2f' % i['recent_delta_e']) if i.get('recent_delta_e') is not None else '':>5s} "
              f"{('%.2f' % i['roic']) if i.get('roic') is not None else '':>5s} "
              f"{('%.2f' % i['revenue_cagr']) if i.get('revenue_cagr') is not None else '':>5s} "
              f"{('%.2f' % i['stage1_growth']) if i.get('stage1_growth') is not None else '':>5s}  {res['note'] or ''}"[:230])
        if ours:
            rows.append((t, ours / his))
    if rows:
        ratios = sorted(r for _, r in rows)
        within = sum(1 for r in ratios if 0.75 <= r <= 1.25)
        print(f"\n{len(rows)} computed; median ours/his = {ratios[len(ratios) // 2]:.2f}; within +-25%: {within}")
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main(sys.argv[1:] or None)
