"""Burry-style "IV15": the share price that returns 15% a year over a 15-year hold.

Source for the method is Burry's own prose (Substack: SW50 Part 1 "Intrinsic Value 15",
SW50 Part 2 / LULU rules of thumb, the Fiserv and Palantir three-stage dividend-discount
write-ups). He publishes the recipe in outline, not the model, so this is a reconstruction
and every free choice below is named and documented. The ones he states:

  * the discount rate IS the required return — 15%, not a WACC
  * earnings are "owners' earnings": GAAP earnings after the TRUE cost of stock-based
    compensation (owners_earnings.py: dE x net income)
  * growth is bounded by ROIC ("a company cannot outgrow its ROIC forever"); history and
    Street revenue are a cross-check
  * a multi-stage dividend discount model with net cash added back, and the final IV15
    is a HYBRID of two end points: (A) the full DDM with a terminal value at the 15% rate
    and (B) the same flows with a year-15 exit multiple
  * a company whose owners' earnings are non-positive has no meaningful IV15 ("not
    investible") — except inflecting hypergrowth names, which he handles with a margin-
    development "Stage 0" that is NOT modelled here

What is chosen here (change the constants, not the code):

  growth      min(5-year revenue CAGR, ROIC), clamped to [-5%, 25%] for years 1-5, then a
              linear fade over years 6-15 to TERMINAL_GROWTH
  payout      1 - g/ROIC each year (the part of owners' earnings not needed to fund the
              growth), so fast growth pays little and a slow compounder pays nearly all
  end point A the DDM's own perpetuity, valued at the 15% return
  end point B the year-16 distribution capitalised at MATURE_RETURN, Burry's 6-10% band
              for "actual intrinsic value" — i.e. what a buyer in year 15 would pay
  hybrid      HYBRID_WEIGHT_A / (1 - HYBRID_WEIGHT_A); his weights follow his confidence,
              which is a judgement this module cannot make, so a fixed 50/50
  net cash    cash + short-term investments - total debt, added per share. He pays debt
              down "to the level the company would carry indefinitely" inside the model;
              this just uses today's balance sheet.

Validation (check_iv15_vs_burry.py, 2026-10-28, 8 steady names): NOT calibrated. ADBE 0.91x and
ADSK 1.19x of his figure; LULU, INTU, PAYC, PCTY, DOCU run 1.4-2.0x high; CRM 0.2x low. Back-solving
the growth his figure implies shows why: for ADBE it equals the 11% revenue history, but for PAYC
(3%), DOCU (5%), ADSK (7.5%) and LULU (6%) it is far below history — his AICT tier / turnaround
judgement, which has no numeric recipe in the posts. CRM is the opposite: his figure needs a
payout near 100%, i.e. a much higher ROIC than equity-based capital gives (he takes a "virtual
write-down" of goodwill). Treat the output as a tier-neutral ceiling, not as his number. His
owners' earnings are reproduced more closely (ADBE, CRM, ADSK, PAYC, PCTY within ~5-25%) using
dE over the LAST THREE usable years, as he does for CRM (54.7% pooled, 90.4% recent).

Not modelled (each lowers fidelity to his number): virtual goodwill write-down, tax
normalisation, purchase obligations as debt, float for payment companies, the AICT
tiering that moves his growth and multiple assumptions, and Stage 0.
check_iv15_vs_burry.py compares the output with his published IV15 figures.
"""
from __future__ import annotations

import valuation

REQUIRED_RETURN = 0.15
HORIZON = 15
STAGE1_YEARS = 5
TERMINAL_GROWTH = 0.03
MATURE_RETURN = 0.09
HYBRID_WEIGHT_A = 0.5
GROWTH_FLOOR, GROWTH_CAP = -0.05, 0.25
ROIC_FLOOR, ROIC_CAP = 0.02, 0.60
DELTA_E_CAP = 1.2          # dE above this is treated as 1.2 (Burry's best is 103.5%)
RECENT_YEARS = 3           # usable years behind the run-rate dE
REINVEST_CAP = 0.5         # growth <= this x ROIC, i.e. at most half of owners' earnings plowed back


def recent_delta_e(pooled: dict, recent: int = RECENT_YEARS) -> float | None:
    """Earnings-weighted dE over the last `recent` usable years (sum OE / sum N). Pure.
    The 10-year pool is a verdict on how shareholders have been treated; the run-rate
    needs the current behaviour. Burry does the same: Salesforce's pool is 54.7% but
    he values it on the last three years' 90.4%, and he credits ServiceNow and
    Workday for recent buybacks even though their pools are negative."""
    inc = [y for y in pooled.get("years", []) if y["included"]][-recent:]
    if len(inc) < recent:
        return None
    n = sum(y["net_income"] for y in inc)
    return sum(y["owners_earnings"] for y in inc) / n if n > 0 else None


def owners_earnings_base(annual: dict, pooled: dict) -> tuple[float | None, str | None]:
    """Latest fiscal year's net income x recent dE. Pure. (value, reason-if-None)."""
    ends = sorted(annual)
    if not ends:
        return None, "no annual figures"
    n = annual[ends[-1]].get("net_income")
    if n is None:
        return None, "no net income"
    de = recent_delta_e(pooled)
    if de is None:
        return None, f"no recent owners'-earnings ratio ({pooled.get('reason') or 'last 3 years unusable or loss-making'})"
    return n * min(de, DELTA_E_CAP), None


def roic(oe0: float, balance: dict) -> float | None:
    """Owners' earnings over net operating capital (equity + debt - cash & securities). Pure."""
    eq = balance.get("stockholders_equity")
    if eq is None:
        return None
    cash = (balance.get("cash") or 0.0) + (balance.get("short_term_investments") or 0.0)
    invested = eq + (balance.get("total_debt") or 0.0) - cash
    if invested <= 0:     # buyback-shrunk equity: capital is effectively nil, ROIC is "very high"
        return ROIC_CAP
    return max(ROIC_FLOOR, min(ROIC_CAP, oe0 / invested))


def growth_path(g1: float, gt: float | None = None, stage1: int | None = None, horizon: int = HORIZON) -> list[float]:
    """Year-by-year growth, years 1..horizon: flat g1, then a linear fade that lands on gt in the last year. Pure."""
    gt = TERMINAL_GROWTH if gt is None else gt
    stage1 = STAGE1_YEARS if stage1 is None else stage1
    fade = horizon - stage1
    return [g1 if y <= stage1 else g1 + (gt - g1) * (y - stage1) / fade for y in range(1, horizon + 1)]


def project(oe0: float, g1: float, rate_roic: float, gt: float | None = None, r: float | None = None,
            mature: float | None = None, weight_a: float | None = None) -> dict:
    """Present value (at r) of the 15 years of distributions plus both end points. Pure.
    Returns total equity value (before net cash) for A, B and the hybrid."""
    gt = TERMINAL_GROWTH if gt is None else gt
    r = REQUIRED_RETURN if r is None else r
    mature = MATURE_RETURN if mature is None else mature
    weight_a = HYBRID_WEIGHT_A if weight_a is None else weight_a
    oe, pv_flows, flows = oe0, 0.0, []
    for y, g in enumerate(growth_path(g1, gt), start=1):
        oe *= 1.0 + g
        payout = 1.0 - max(g, 0.0) / rate_roic
        d = oe * max(payout, 0.0)
        flows.append(d)
        pv_flows += d / (1.0 + r) ** y
    d16 = oe * (1.0 + gt) * (1.0 - gt / rate_roic)
    disc = (1.0 + r) ** HORIZON
    a = pv_flows + (d16 / (r - gt)) / disc
    b = pv_flows + (d16 / (mature - gt)) / disc
    return {"a": a, "b": b, "hybrid": weight_a * a + (1.0 - weight_a) * b, "pv_flows": pv_flows,
            "oe15": oe, "d16": d16, "exit_multiple_oe": (d16 / (mature - gt)) / oe if oe else None}


def compute_iv15(fin: dict, pooled: dict, shares_override: float | None = None) -> dict:
    """IV15 per share from one ticker's financials and its pooled SBC figures. Pure.
    {"iv15": float|None, "note": str|None, "inputs": {...}}; None with a note means
    'not investible / not computable' and is not an error."""
    annual, bal = fin["annual"], fin["latest_balance"]
    ends = sorted(annual)[-valuation.HISTORY_YEARS:]
    if not ends:
        return {"iv15": None, "note": "no annual figures", "inputs": {}}
    latest = annual[ends[-1]]
    shares = shares_override or latest.get("shares_outstanding") or latest.get("shares_diluted")
    if not shares or shares <= 0:
        return {"iv15": None, "note": "no share count", "inputs": {}}
    oe0, why = owners_earnings_base(annual, pooled)
    if oe0 is None:
        return {"iv15": None, "note": why, "inputs": {}}
    if oe0 <= 0:
        return {"iv15": None, "note": f"owners' earnings non-positive ({oe0 / 1e6:,.0f}M): no price returns 15% on this model "
                                      "(Burry: 'not investible'; hypergrowth inflection needs his Stage 0)",
                "inputs": {"oe0": oe0}}
    rc = roic(oe0, bal)
    if rc is None:
        return {"iv15": None, "note": "no equity figure for ROIC", "inputs": {"oe0": oe0}}
    hist_g, defaulted = valuation.stage1_growth([annual[e].get("revenue") for e in ends])
    g1 = max(GROWTH_FLOOR, min(GROWTH_CAP, min(hist_g, REINVEST_CAP * rc)))
    pv = project(oe0, g1, rc)
    net_cash = (bal.get("cash") or 0.0) + (bal.get("short_term_investments") or 0.0) - (bal.get("total_debt") or 0.0)
    per_share = {k: (pv[k] + net_cash) / shares for k in ("a", "b", "hybrid")}
    return {
        "iv15": per_share["hybrid"] if per_share["hybrid"] > 0 else None,
        "note": None if per_share["hybrid"] > 0 else "net debt exceeds the present value of owners' earnings",
        "inputs": {
            "oe0": oe0, "pooled_delta_e": pooled["delta_e"], "recent_delta_e": recent_delta_e(pooled), "net_income": annual[ends[-1]].get("net_income"),
            "roic": rc, "revenue_cagr": hist_g, "growth_defaulted": defaulted, "stage1_growth": g1,
            "terminal_growth": TERMINAL_GROWTH, "required_return": REQUIRED_RETURN, "mature_return": MATURE_RETURN,
            "weight_a": HYBRID_WEIGHT_A, "iv15_end_point_a": per_share["a"], "iv15_end_point_b": per_share["b"],
            "exit_multiple_oe": pv["exit_multiple_oe"], "net_cash": net_cash, "shares": shares,
            "fiscal_year_end": ends[-1],
        },
    }
