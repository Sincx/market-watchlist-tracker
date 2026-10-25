"""Fair-value estimates from the SEC figures that filings.py collects (Phase 17).

Per US ticker with facts on file this writes `valuations` rows:

  dcf_fair_value       a two-stage discounted-cash-flow value per share
  book_value           stockholders' equity per share
  tangible_book_value  (equity - goodwill & intangibles) per share

  dcf_sbc_adjusted     the same DCF after deducting the TRUE cost of stock-based
                       compensation (owners_earnings.py — Burry's "Tragic
                       Algebra"), because operating cash flow adds SBC back as
                       if it cost nothing

(`burry_iv15` is deliberately NOT here: it needs a validation pass against
Burry's published figures before it is trusted — see the Phase 17 notes.)

DCF, deliberately plain so every number is auditable:

  FCF0     latest fiscal year's operating cash flow - capex (SBC is NOT deducted
           here; the dcf_sbc_adjusted variant deducts it, as FCF0 x (1 - rho))
  growth   revenue CAGR over the available annual history, clamped to
           [-5%, 20%]; with fewer than 3 annual points a 5% default is used and
           confidence is 'low'
  years 1-5   FCF grows at that rate
  years 6-10  growth fades linearly to the terminal rate (2.5%)
  after 10    Gordon-growth perpetuity
  discount rate  by market cap: >= $50B 8%, $5-50B 10%, < $5B 12%; +1% if the
           stage-1 growth is >= 15% (a faster grower is a less certain one)
  equity   EV - debt + cash & short-term investments, divided by diluted shares

Foreign private issuers (latest annual report is a 20-F / 40-F) are skipped: their
per-share figures are per ORDINARY share, not per ADS, and may be in another
currency, so they would not line up with the ADR price they get compared to.

Not applicable (recorded in data_quality as status 'ok' with the reason — an
intentional exclusion, not a failure): Financials / Financial Services / Real
Estate / Utilities (cash flow is not the right lens; book value is still
written), and non-positive FCF. Missing inputs (no OCF, no shares, no price)
are 'degraded', because those are data gaps worth looking at.

This module writes ONLY to `valuations` and `data_quality`. It does not emit
`signals`: doing so would change live recommendations (and the reject-
suppression 'conviction rises' escape hatch) before anyone has looked at the
numbers it produces.

Usage:
    python valuation.py                    # compute whatever is missing/stale
    python valuation.py --all              # recompute everything with facts
    python valuation.py --ticker AAPL LULU --dry-run
"""
from __future__ import annotations

import argparse
import json
import statistics
from datetime import date, datetime, timezone

import libsql_client

import data_quality as dq
import db
import filings
import owners_earnings as oe

TERMINAL_GROWTH = 0.025
STAGE1_YEARS = 5
FADE_YEARS = 5
GROWTH_FLOOR, GROWTH_CAP = -0.05, 0.20
DEFAULT_GROWTH = 0.05
MIN_GROWTH_POINTS = 3          # annual revenue points needed to trust a CAGR
HISTORY_YEARS = 5
STALE_AFTER_DAYS = 30          # recompute at least this often (market-cap tier can drift)
FACTS_FRESH_DAYS = 460         # latest annual period end older than this -> not 'high' confidence
FAR_FROM_PRICE = (0.25, 4.0)   # DCF/price outside this => not 'high' confidence: usually an input problem
OWN_METHODS = ("dcf_fair_value", "dcf_sbc_adjusted", "book_value", "tangible_book_value")
FOREIGN_FORMS = {"20-F", "40-F"}
EXTREME_OVERSTATEMENT = 0.75    # see _sbc_adjusted_dcf()
DCF_EXCLUDED_SECTORS = {"Financials", "Financial Services", "Real Estate", "Utilities"}


# ── pure maths ───────────────────────────────────────────────────────────────

def discount_rate(market_cap: float | None, stage1_growth: float) -> float:
    if market_cap is None or market_cap < 5e9:
        r = 0.12      # unknown cap is treated as small: the conservative tier
    elif market_cap < 50e9:
        r = 0.10
    else:
        r = 0.08
    return r + (0.01 if stage1_growth >= 0.15 else 0.0)


def stage1_growth(revenues: list[float]) -> tuple[float, bool]:
    """Revenue CAGR (oldest -> newest of the given annual series), clamped.
    Returns (growth, defaulted). Needs MIN_GROWTH_POINTS positive points."""
    pts = [r for r in revenues if r is not None]
    if len(pts) < MIN_GROWTH_POINTS or pts[0] <= 0 or pts[-1] <= 0:
        return DEFAULT_GROWTH, True
    cagr = (pts[-1] / pts[0]) ** (1.0 / (len(pts) - 1)) - 1.0
    return max(GROWTH_FLOOR, min(GROWTH_CAP, cagr)), False


def project_dcf(fcf0: float, g1: float, r: float, gt: float = TERMINAL_GROWTH,
                stage1: int = STAGE1_YEARS, fade: int = FADE_YEARS) -> dict:
    """Present value of the projected cash flows + terminal value. Pure.
    Requires r > gt + 1% (a perpetuity that close to r is a rounding error
    away from infinite)."""
    if r <= gt + 0.01:
        raise ValueError(f"discount rate {r:.3f} must exceed terminal growth {gt:.3f} by more than 1%")
    flows, fcf = [], fcf0
    for yr in range(1, stage1 + fade + 1):
        if yr <= stage1:
            g = g1
        else:  # linear fade: the last fade year lands exactly on the terminal rate
            g = g1 + (gt - g1) * (yr - stage1) / fade
        fcf *= 1.0 + g
        flows.append(fcf)
    pv_flows = sum(f / (1.0 + r) ** (i + 1) for i, f in enumerate(flows))
    tv = flows[-1] * (1.0 + gt) / (r - gt)
    pv_tv = tv / (1.0 + r) ** len(flows)
    ev = pv_flows + pv_tv
    return {"ev": ev, "pv_flows": pv_flows, "pv_terminal": pv_tv, "tv_share": pv_tv / ev if ev else None,
            "final_year_fcf": flows[-1]}


def rate_confidence(n_annual: int, fcfs: list[float], defaulted_growth: bool,
                    market_cap_known: bool, latest_period_end: str | None,
                    today: date, tv_share: float | None) -> tuple[str, list[str]]:
    """high / medium / low plus the reasons that held it down. Pure."""
    why = []
    if defaulted_growth:
        why.append("growth defaulted (thin revenue history)")
    if n_annual < MIN_GROWTH_POINTS:
        why.append(f"only {n_annual} annual period(s)")
    positive = [f for f in fcfs if f is not None and f > 0]
    if len(fcfs) >= 3 and len(positive) < len(fcfs):
        why.append("FCF not positive every year")
    if any(w.startswith(("growth defaulted", "only")) for w in why):
        return "low", why
    if len(positive) < 3:
        return "low", why + ["fewer than 3 positive-FCF years"]
    mean = statistics.fmean(positive)
    cv = statistics.pstdev(positive) / mean if mean else None
    if cv is not None and cv > 0.5:
        why.append(f"volatile FCF (cv {cv:.2f})")
    if not market_cap_known:
        why.append("market cap unknown (12% rate assumed)")
    if latest_period_end and (today - date.fromisoformat(latest_period_end)).days > FACTS_FRESH_DAYS:
        why.append("latest annual figures are stale")
    if tv_share is not None and tv_share > 0.8:
        why.append(f"{tv_share:.0%} of value is terminal")
    if n_annual >= HISTORY_YEARS and not why:
        return "high", why
    return "medium", why


def compute_valuations(fin: dict, market_cap: float | None, sector: str | None, today: date,
                       price: float | None = None) -> dict:
    """Everything derivable from one ticker's financials. Pure. Returns
    {"dcf": {...}|None, "dcf_note": str|None, "dcf_ok": bool, "book": ..., "tangible": ..., "problems": [...]}.
    dcf_ok False means a DATA gap (-> degraded); True with dcf None means an
    intentional exclusion (-> ok)."""
    annual = fin["annual"]
    bal = fin["latest_balance"]
    ends = sorted(annual)[-HISTORY_YEARS:]
    out: dict = {"dcf": None, "dcf_note": None, "dcf_ok": True, "book": None, "tangible": None, "problems": [],
                 "dcf_sbc": None, "sbc_note": None, "sbc": None}
    if not ends:
        out.update(dcf_ok=False, dcf_note="no annual figures on file")
        return out
    latest = annual[ends[-1]]
    shares = latest.get("shares_diluted")
    if not shares or shares <= 0:
        out["problems"].append("no diluted share count")
        out.update(dcf_ok=False, dcf_note="no diluted share count")
        return out

    eq = bal.get("stockholders_equity")
    if eq is not None and eq > 0:
        out["book"] = {"fair_value": eq / shares, "inputs": {"stockholders_equity": eq, "shares_diluted": shares,
                                                          "balance_period_end": bal.get("_period_end", {}).get("stockholders_equity")}}
        gi = bal.get("goodwill_and_intangibles") or 0.0
        if eq - gi > 0:
            out["tangible"] = {"fair_value": (eq - gi) / shares,
                               "inputs": {"stockholders_equity": eq, "goodwill_and_intangibles": gi, "shares_diluted": shares}}

    if sector in DCF_EXCLUDED_SECTORS:
        out["dcf_note"] = f"dcf n/a: {sector} (cash-flow DCF is the wrong lens; book value written)"
        return out

    ocf, capex = latest.get("operating_cash_flow"), latest.get("capex")
    if ocf is None:
        out.update(dcf_ok=False, dcf_note="no operating cash flow reported")
        return out
    fcf0 = ocf - abs(capex or 0.0)
    if fcf0 <= 0:
        out["dcf_note"] = f"dcf n/a: non-positive free cash flow ({fcf0 / 1e6:,.0f}M)"
        return out

    g1, defaulted = stage1_growth([annual[e].get("revenue") for e in ends])
    r = discount_rate(market_cap, g1)
    pv = project_dcf(fcf0, g1, r)
    debt = bal.get("total_debt")
    cash = (bal.get("cash") or 0.0) + (bal.get("short_term_investments") or 0.0)
    equity_value = pv["ev"] - (debt or 0.0) + cash
    if equity_value <= 0:
        out["dcf_note"] = (f"dcf n/a: debt ({(debt or 0.0) / 1e6:,.0f}M) exceeds the value of the cash flows "
                           f"({pv['ev'] / 1e6:,.0f}M) — equity worth nothing on this model")
        return out
    fcfs = [(annual[e].get("operating_cash_flow") - abs(annual[e].get("capex") or 0.0))
            if annual[e].get("operating_cash_flow") is not None else None for e in ends]
    conf, why = rate_confidence(len(ends), fcfs, defaulted, market_cap is not None, ends[-1], today, pv["tv_share"])
    if debt is None:
        why.append("no debt reported (treated as 0)")
    far = _far_from_price(equity_value / shares, price)
    if far:
        why.append(far)
        conf = "low"
    out["sbc"] = sbc = oe.pooled(annual)
    out["dcf_sbc"], out["sbc_note"] = _sbc_adjusted_dcf(annual, sbc, fcf0, g1, r, debt, cash, shares, conf, why, market_cap,
                                                         defaulted, price)
    out["dcf"] = {
        "fair_value": equity_value / shares, "discount_rate": r, "confidence": conf,
        "inputs": {
            "fcf0": fcf0, "operating_cash_flow": ocf, "capex": capex, "sbc_expense_not_deducted": latest.get("sbc_expense"),
            "fiscal_year_end": ends[-1], "annual_periods_used": len(ends),
            "stage1_growth": g1, "growth_defaulted": defaulted, "terminal_growth": TERMINAL_GROWTH,
            "stage1_years": STAGE1_YEARS, "fade_years": FADE_YEARS, "discount_rate": r, "market_cap": market_cap,
            "revenue_series": {e: annual[e].get("revenue") for e in ends}, "fcf_series": dict(zip(ends, fcfs)),
            "enterprise_value": pv["ev"], "terminal_value_share": pv["tv_share"],
            "total_debt": debt, "cash_and_short_term_investments": cash, "shares_diluted": shares,
            "balance_period_end": bal.get("_period_end"), "confidence_notes": why,
        },
    }
    return out


def _far_from_price(fair_value: float, price: float | None) -> str | None:
    """A DCF several times (or a fraction of) the market price is much more often a
    bad input (vehicle-fleet capex outside PP&E, payment-settlement cash, a preferred
    share on a common's CIK) than a 4x mispricing, so say so. Pure."""
    if not price or price <= 0:
        return None
    ratio = fair_value / price
    if ratio > FAR_FROM_PRICE[1] or ratio < FAR_FROM_PRICE[0]:
        return f"DCF is {ratio:.2f}x the market price: check inputs before relying on it"
    return None


def _sbc_adjusted_dcf(annual: dict, sbc: dict, fcf0: float, g1: float, r: float, debt: float | None,
                      cash: float, shares: float, base_conf: str, base_why: list[str], market_cap: float | None,
                      defaulted: bool, price: float | None = None) -> tuple[dict | None, str | None]:
    """The DCF again with the true SBC cost taken out of free cash flow. Pure.

    rho = recent sum(Omega) / recent sum(OCF - capex): the share of free cash
    flow the company really spends on SBC (buybacks that merely offset issuance,
    RSU tax withholding, and the market value of net new shares). The projected
    FCF is scaled by (1 - rho) every year, so the cost grows with the business
    rather than staying a fixed dollar amount. Share count is the CURRENT one:
    the dilution is already paid for in cash, so applying it twice would double
    count."""
    if not sbc["ok"]:
        return None, f"sbc-adjusted dcf n/a: {sbc['reason']}"
    included = [y for y in sbc["years"] if y["included"]]
    rho = oe.fcf_cost_ratio(annual, included)
    if rho is None:
        return None, "sbc-adjusted dcf n/a: cash-flow figures missing for the cost window"
    if rho >= 1.0:
        return None, f"sbc-adjusted dcf n/a: true SBC cost ({rho:.0%} of FCF) consumes all free cash flow (Burry's 'Tragic Tier')"
    pv = project_dcf(fcf0 * (1.0 - rho), g1, r)
    equity_value = pv["ev"] - (debt or 0.0) + cash
    if equity_value <= 0:
        return None, "sbc-adjusted dcf n/a: debt exceeds the value of the owners' cash flows"
    notes = list(base_why)
    conf = base_conf
    weak = []
    if sbc["c_coverage"] is not None and sbc["c_coverage"] < 0.5:
        weak.append(f"RSU tax-withholding tag reported in only {sbc['c_coverage']:.0%} of years (cost is a lower bound)")
    if sbc["n_included"] < 5:
        weak.append(f"only {sbc['n_included']} usable SBC years")
    if weak:
        notes += weak
        conf = {"high": "medium", "medium": "low"}.get(conf, conf)
    # An overstatement this large is either a genuinely abusive SBC programme or an input
    # problem (untagged acquisition / conversion shares, erratic earnings); either way a
    # person should look at the 10-Ks before leaning on the number. Burry's own range for
    # the NASDAQ-100 tops out around 50% outside his nine "Tragic Tier" names.
    if sbc["overstatement"] is not None and sbc["overstatement"] > EXTREME_OVERSTATEMENT:
        notes.append(f"GAAP overstatement {sbc['overstatement']:.0%} is extreme: verify against the 10-Ks")
        conf = "low"
    return {
        "fair_value": equity_value / shares, "discount_rate": r, "confidence": conf,
        "inputs": {
            "method": "fcf0 x (1 - rho), rho = recent sum(Omega) / sum(OCF - capex)", "rho": rho,
            "fcf0": fcf0, "owners_fcf0": fcf0 * (1.0 - rho), "stage1_growth": g1, "growth_defaulted": defaulted,
            "terminal_growth": TERMINAL_GROWTH, "discount_rate": r, "market_cap": market_cap,
            "pooled_delta_e": sbc["delta_e"], "gaap_overstatement": sbc["overstatement"],
            "sbc_years_used": sbc["n_included"], "sbc_years_skipped": {
                y["period_end"]: y["reason"] for y in sbc["years"] if not y["included"]},
            "c_coverage": sbc["c_coverage"], "recent_omega": sbc["recent_omega"],
            "yearly": {y["period_end"]: {k: y[k] for k in ("net_income", "sbc_gaap", "v", "c", "omega", "delta_e")}
                       for y in included},
            "enterprise_value": pv["ev"], "total_debt": debt, "cash_and_short_term_investments": cash,
            "shares_diluted": shares, "confidence_notes": notes,
        },
    }, None


# ── I/O ──────────────────────────────────────────────────────────────────────

def _latest_context(client, ticker: str, exchange: str) -> dict:
    f = db.query(client, "SELECT mkt_cap, sector FROM fundamentals WHERE ticker = :t AND exchange = :e "
                         "ORDER BY as_of_date DESC LIMIT 1;", {"t": ticker, "e": exchange})
    p = db.query(client, "SELECT close, date FROM prices WHERE ticker = :t AND exchange = :e "
                         "ORDER BY date DESC LIMIT 1;", {"t": ticker, "e": exchange})
    return {"mkt_cap": (f[0]["mkt_cap"] if f else None), "sector": (f[0]["sector"] if f else None),
            "price": (p[0]["close"] if p else None), "price_date": (p[0]["date"] if p else None)}


def _due(client, all_: bool, tickers: list[str] | None, today: date) -> list[dict]:
    cands = db.query(client, """
        SELECT c.ticker, c.exchange, c.last_facts_at, c.last_form,
               (SELECT MAX(v.computed_at) FROM valuations v WHERE v.ticker = c.ticker AND v.exchange = c.exchange) AS last_valued
        FROM filings_calendar c WHERE c.last_facts_at IS NOT NULL AND c.exchange = 'US';
    """)
    if tickers:
        want = {t.upper() for t in tickers}
        return [c for c in cands if c["ticker"] in want]
    if all_:
        return cands
    due = []
    for c in cands:
        lv = c["last_valued"]
        if lv is None or lv < c["last_facts_at"] or (today - date.fromisoformat(lv[:10])).days >= STALE_AFTER_DAYS:
            due.append(c)
    return due


def run(all_: bool = False, tickers: list[str] | None = None, dry_run: bool = False) -> dict:
    today, now_iso = date.today(), datetime.now(timezone.utc).isoformat()
    client = db.get_client()
    stats = {"considered": 0, "valued": 0, "dcf": 0, "dcf_sbc": 0, "excluded": 0, "degraded": 0}
    try:
        val_rows, dq_rows, stale = [], [], []   # stale: (ticker, exchange, method) this run no longer produces
        for c in _due(client, all_, tickers, today):
            t, ex = c["ticker"], c["exchange"]
            stats["considered"] += 1
            if c.get("last_form") in FOREIGN_FORMS:
                stats["excluded"] += 1
                if not dry_run:
                    client.execute("DELETE FROM valuations WHERE ticker = :t AND exchange = :e;", {"t": t, "e": ex})
                dq_rows.append({"ticker": t, "exchange": ex, "data_type": "valuation", "status": "ok", "source": "sec_edgar",
                                "error": f"valuation n/a: foreign private issuer ({c['last_form']}) — per-share figures would not match the ADR price"})
                print(f"  {t:6s} skipped: foreign private issuer ({c['last_form']})")
                continue
            ctx = _latest_context(client, t, ex)
            res = compute_valuations(filings.load_financials(client, t, ex), ctx["mkt_cap"], ctx["sector"], today, ctx["price"])

            def add(method, d, rate=None, conf=None):
                val_rows.append({
                    "run_date": today.isoformat(), "ticker": t, "exchange": ex, "method": method,
                    "fair_value": d["fair_value"], "discount_rate": rate,
                    "terminal_growth_rate": TERMINAL_GROWTH if method == "dcf_fair_value" else None,
                    "horizon_years": STAGE1_YEARS + FADE_YEARS if method == "dcf_fair_value" else None,
                    "inputs_json": json.dumps(d["inputs"], default=str), "confidence": conf, "computed_at": now_iso})

            if res["dcf"]:
                add("dcf_fair_value", res["dcf"], res["dcf"]["discount_rate"], res["dcf"]["confidence"])
                stats["dcf"] += 1
            if res["dcf_sbc"]:
                add("dcf_sbc_adjusted", res["dcf_sbc"], res["dcf_sbc"]["discount_rate"], res["dcf_sbc"]["confidence"])
                stats["dcf_sbc"] += 1
            if res["book"]:
                add("book_value", res["book"], conf="medium")   # shares are the weighted diluted count, not period-end
            if res["tangible"]:
                add("tangible_book_value", res["tangible"], conf="medium")
            if res["dcf"] or res["book"]:
                stats["valued"] += 1
            if res["dcf_ok"]:
                if res["dcf_note"]:
                    stats["excluded"] += 1
                dq_rows.append({"ticker": t, "exchange": ex, "data_type": "valuation", "status": "ok",
                                "source": "sec_edgar", "error": res["dcf_note"]})
            else:
                stats["degraded"] += 1
                dq_rows.append({"ticker": t, "exchange": ex, "data_type": "valuation", "status": "degraded",
                                "source": "sec_edgar", "error": res["dcf_note"]})
            made = {r["method"] for r in val_rows if r["ticker"] == t and r["exchange"] == ex}
            stale += [(t, ex, m) for m in OWN_METHODS if m not in made]
            fv = res["dcf"]["fair_value"] if res["dcf"] else None
            sv = res["dcf_sbc"]["fair_value"] if res["dcf_sbc"] else None
            px = ctx["price"]
            de = (res["sbc"] or {}).get("delta_e")
            print(f"  {t:6s} dcf={'%.2f' % fv if fv is not None else 'n/a':>9}  sbc-adj={'%.2f' % sv if sv is not None else 'n/a':>9}"
                  f"  dE={'%.1f%%' % (de * 100) if de is not None else 'n/a':>7}  price={'%.2f' % px if px else 'n/a':>8}"
                  f"  {res['dcf']['confidence'] if res['dcf'] else '-':>6}  {res['dcf_note'] or res['sbc_note'] or ''}")
        if not dry_run:
            # a method that stopped applying (or went negative) must not linger as the "latest" value
            if stale:
                client.batch([libsql_client.Statement(
                    "DELETE FROM valuations WHERE ticker = :t AND exchange = :e AND method = :m;", {"t": t, "e": e, "m": m})
                    for t, e, m in stale])
            db.upsert(client, "valuations", val_rows)
            dq.record_batch(client, dq_rows)
        return stats
    finally:
        client.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="recompute every ticker with facts, not just stale ones")
    ap.add_argument("--ticker", nargs="+")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    print(run(all_=a.all, tickers=a.ticker, dry_run=a.dry_run))
    import os, sys
    sys.stdout.flush()
    os._exit(0)
