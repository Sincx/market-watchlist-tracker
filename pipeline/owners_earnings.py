"""Owners' earnings adjusted for the true cost of stock-based compensation.

Implements Michael Burry's "Tragic Algebra of Stock-Based Compensation"
(Substack, 2025-12-01 "Foundations", 2026-04-07 "AP SBC"; the closed-form
dilution derivation is by Phil Clifton, wiki: raw/Mathematical_Derivation.pdf).

The idea: GAAP expenses SBC at its grant-date value (G) and Wall Street adds it
back entirely. The real cost to an owner is the cash the company spends, over
time, to deliver those shares and offset their dilution:

    V  = cost of shares issued to employees  = I x P
       = T + P x dS          (I = W + dS shares issued, P = T / W = average
                              repurchase price; substituting gives this form,
                              which needs neither W nor I)
    C  = net cash paid on RSU vesting for employees' withheld tax
       = C_w (tax withholding payments) - C_e (option / ESPP proceeds)
    Omega = V + C            the whole SBC "paycheck"
    owners' earnings = N + G - Omega        (G is added back only to be replaced)
    dE = owners' earnings / N               share of GAAP earnings that is the owners'

  N = GAAP net income, T = buyback dollars, dS = change in shares outstanding
  over the year (negative when the count fell), P = average price of the stock.
  Pure-dilution years (no buyback) fall out of the same formula: T = 0 so
  V = P x dS, the market value of the shares handed out.

Burry pools dE across ~10 years, earnings-weighted (sum of owners' earnings /
sum of N) rather than averaging yearly ratios, because single years are noisy.
GAAP "overstates" owners' earnings by 1/dE - 1 (his Alphabet figure: dE 88.7%
-> 12.8%).

Where P comes from: Burry uses the true average repurchase price (T / W) and,
for companies with no buyback, Bloomberg's average price. XBRL almost never
gives W cleanly (his post explains why the aggregators fail at this), so we use
the average market price over the fiscal year throughout (price_history.py).

Years are skipped — "excluded with neutral treatment", as his gate check does —
when the inputs can't be trusted: a split inside the year, a share-count change
over 15% (split / merger / conversion), a gap between year ends that isn't about a year, or a
missing price/net income/SBC. Missing C_w is NOT a skip but is counted: where
a company reports no withholding tag (Alphabet) the result is an upper bound on
dE, and `c_coverage` says how much of the window it affects.

Pure functions; no DB, no network.

Sources of known divergence from Burry's figures, so a mismatch isn't a surprise:
  * his C includes withholding for companies whose XBRL tag is custom or absent
  * his S counts come from the shareholders' equity statement; ours from the
    balance-sheet cover tag
  * his window is the 10-K fiscal years ending 2016-2025; ours is whatever
    the facts table holds, which can differ by a year at either end
"""
from __future__ import annotations

from datetime import date

EQUITY_RAISE_SHARE_JUMP = 0.05   # share count up more than this => ProceedsFromIssuanceOfCommonStock is a raise, not ESPP
MAX_SHARE_CHANGE = 0.15      # |dS| / S0 above this (after setting aside tagged acquisition/conversion shares) is a
                             # split, merger, conversion or offering rather than SBC; the heaviest real diluters
                             # Burry names run 3-5% a year. AMD's Xilinx year (+35%) and SNPS's Ansys year (+20%) are caught.
MIN_YEARS = 3                # fewer included years than this -> no pooled result
WINDOW_YEARS = 10
RECENT_YEARS = 3             # years behind the run-rate cost used by the DCF


def dilution_multiple(d: float, g: float, y: float) -> float:
    """Clifton's closed form for the value of a growing cash flow that is also
    diluted at rate y a year, as a multiple of the FIRST-year cash flow:

        PV = CF / ((1 + d)(1 + y) - (1 + g))

    (y = 0 recovers Gordon growth, CF / (d - g).) Dilution multiplies with the
    discount rate rather than adding to it, so d=10%, g=5%: y=0 -> 20x,
    y=1% -> 16.4x, y=5% -> 9.5x. Not used in the DCF itself (that deducts the
    measured cash cost instead, which would double count) — kept as the
    reference formula and for the IV15 work. Pure."""
    denom = (1 + d) * (1 + y) - (1 + g)
    if denom <= 0:
        raise ValueError("growth must be below the dilution-adjusted discount rate")
    return 1.0 / denom


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _shares(m: dict) -> tuple[float | None, bool]:
    """(share count, from_weighted_average_fallback)"""
    if m.get("shares_outstanding"):
        return m["shares_outstanding"], False
    if m.get("shares_basic"):
        return m["shares_basic"], True
    return None, False


def year_cost(prev_end: str, prev: dict, cur_end: str, cur: dict) -> dict:
    """One fiscal year's SBC cost. `prev`/`cur` are annual metric dicts from
    filings.build_financials. Returns {"included": bool, "reason": str|None, ...}."""
    out: dict = {"period_end": cur_end, "included": False, "reason": None}
    gap = _days(prev_end, cur_end)
    if not 350 <= gap <= 380:
        out["reason"] = f"year ends {gap} days apart"
        return out
    if (cur.get("split_ratio") or 1.0) != 1.0:
        out["reason"] = f"stock split in year (x{cur['split_ratio']:g})"
        return out
    n, g = cur.get("net_income"), cur.get("sbc_expense")
    if n is None or g is None:
        out["reason"] = "net income or SBC expense missing"
        return out
    price = cur.get("avg_price")
    if not price:
        out["reason"] = "no average price for the year"
        return out
    s1, fb1 = _shares(cur)
    s0, fb0 = _shares(prev)
    if not s1 or not s0:
        out["reason"] = "share counts missing"
        return out
    ds_raw = s1 - s0
    # shares issued for acquisitions / convertible conversions are not employee pay
    other = (cur.get("shares_acquired") or 0.0) + (cur.get("shares_converted") or 0.0)
    other_value = 0.0   # the same set-asides, in dollars, where only the value is tagged
    if not cur.get("shares_acquired"):
        other_value += cur.get("acq_value") or 0.0
    if not cur.get("shares_converted"):
        other_value += cur.get("conv_value") or 0.0
    ds = ds_raw - other - other_value / price
    if abs(ds_raw) / s0 > MAX_SHARE_CHANGE and abs(ds) / s0 > MAX_SHARE_CHANGE:
        out["reason"] = f"share count moved {ds_raw / s0:+.0%} (split/merger/offering?)"
        return out
    t = cur.get("buyback_dollars") or 0.0
    cw = cur.get("tax_withholding")
    ce = cur.get("option_proceeds")
    if ce is None and ds_raw / s0 < EQUITY_RAISE_SHARE_JUMP:
        ce = cur.get("equity_proceeds")      # MSFT-style ESPP line; not credited if it looks like a raise
    ce = ce or 0.0
    v = t + price * ds     # ds already excludes the set-asides
    c = (cw or 0.0) - ce
    omega = v + c
    out.update(
        included=True, net_income=n, sbc_gaap=g, buyback=t, shares_change=ds, avg_price=price,
        shares_set_aside=other, v=v, c=c, omega=omega, owners_earnings=n + g - omega,
        c_known=cw is not None, shares_from_weighted_avg=fb1 or fb0,
        delta_e=((n + g - omega) / n) if n > 0 else None,
    )
    return out


def pooled(annual: dict[str, dict], window: int = WINDOW_YEARS) -> dict:
    """Pooled SBC-adjusted earnings over the latest `window` fiscal years.

    `annual` is filings.build_financials()["annual"] ({period_end: metrics}).
    Returns {"years": [...year_cost dicts, oldest first], "n_included", "delta_e",
    "overstatement", "recent_omega", "c_coverage", "ok", "reason"}."""
    ends = sorted(annual)
    years = [year_cost(ends[i - 1], annual[ends[i - 1]], ends[i], annual[ends[i]]) for i in range(1, len(ends))]
    years = years[-window:]
    inc = [y for y in years if y["included"]]
    res: dict = {"years": years, "n_included": len(inc), "delta_e": None, "overstatement": None,
                 "recent_omega": None, "recent_fcf_cost_ratio": None, "c_coverage": None,
                 "ok": False, "reason": None}
    if len(inc) < MIN_YEARS:
        res["reason"] = f"only {len(inc)} usable fiscal year(s) of {len(years)} (need {MIN_YEARS})"
        return res
    sum_n = sum(y["net_income"] for y in inc)
    sum_oe = sum(y["owners_earnings"] for y in inc)
    res["c_coverage"] = sum(1 for y in inc if y["c_known"]) / len(inc)
    res["sum_net_income"], res["sum_owners_earnings"] = sum_n, sum_oe
    recent = inc[-RECENT_YEARS:]
    res["recent_omega"] = sum(y["omega"] for y in recent) / len(recent)
    res["ok"] = True
    if sum_n > 0:
        res["delta_e"] = sum_oe / sum_n
        if sum_oe > 0:
            res["overstatement"] = sum_n / sum_oe - 1.0
        else:
            res["reason"] = "owners' earnings are negative across the window (Burry's 'Tragic Tier')"
    else:
        res["reason"] = "cumulative GAAP net income is not positive"
    return res


def fcf_cost_ratio(annual: dict[str, dict], included_years: list[dict], recent: int = RECENT_YEARS) -> float | None:
    """Share of recent free cash flow consumed by the true SBC cost: sum(Omega) /
    sum(OCF - capex) over the same last `recent` included years. Floored at 0
    (a company retiring more stock than it issues is returning capital, not
    paying a cost). None when cash-flow data is missing. Pure."""
    yrs = included_years[-recent:]
    omega = fcf = 0.0
    for y in yrs:
        m = annual.get(y["period_end"]) or {}
        if m.get("operating_cash_flow") is None:
            return None
        omega += y["omega"]
        fcf += m["operating_cash_flow"] - abs(m.get("capex") or 0.0)
    if fcf <= 0:
        return None
    return max(0.0, omega / fcf)
