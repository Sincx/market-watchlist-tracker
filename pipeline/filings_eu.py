"""Raw statement figures for UK / EU equities, from Yahoo (free) — the non-US half of Phase 17.

filings.py fills financial_statement_facts for US tickers from SEC EDGAR. EU/UK companies
file nowhere we can reach cheaply, so this pulls the annual income statement, cash flow and
balance sheet that Yahoo already serves (4-5 fiscal years) and stores them under the SAME
concept names filings.build_financials() reads, so valuation.py runs on them unchanged.

Two things differ from the SEC path and are the reason this is its own module:

  CURRENCY  Yahoo reports in the company's REPORTING currency (`financialCurrency`: Shell and
            AstraZeneca report USD) while the share is quoted in another (`currency`: GBp, i.e.
            pence). Facts are therefore stored as reported, tagged with their currency in `unit`,
            and converted to the ticker's TRADING currency (GBX = pence, EUR, CHF, ...) only when
            loaded for valuation (load_financials), using the latest fx_rates. Stored facts never
            go stale against FX; one constant rate across all years keeps growth rates and the
            SBC ratios in constant currency. Per-share values then come out in the same units as
            prices.close — the GBX/GBP mix-up that has bitten the price pipeline before can't
            happen here because the conversion target is the price's own currency.

  HISTORY   Yahoo serves 4-5 annual periods, not 10. Facts accumulate (a period that has
            dropped off Yahoo's window stays in the table), so history grows by a year every
            year, but for now valuation.py will cap confidence below 'high' (needs 5 periods)
            and Burry's pooled dE rests on 3-4 years.

Mapping notes (concept names are us-gaap tags purely so build_financials can read them):

  LongTermDebt                  <- Yahoo 'Total Debt', which INCLUDES lease liabilities. Right for
                                   IFRS filers: IFRS 16 puts lease principal in financing cash
                                   flow, so operating cash flow is flattered and the leases must
                                   come off enterprise value as debt instead.
  NetIncomeLoss                 <- net income attributable to common holders
  tax withholding / option proceeds are not on Yahoo, so the SBC-adjusted DCF reports its cost as
  a lower bound (c_coverage 0%) and is capped to low/medium confidence by valuation.py.

Every row carries form_type 'AR' and source 'yfinance'. The forward calendar written to
filings_calendar is an ESTIMATE: last year end + 1 year + 120 days.

Usage:
    python filings_eu.py                      # today's batch (default 40, never-checked first)
    python filings_eu.py --limit 900          # one-off backfill of the whole UK/EU universe
    python filings_eu.py --ticker SHEL AZN    # these now (UK and EU rows both match)
    python filings_eu.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import data_quality as dq
import db
import filings

EXCHANGES = ("UK", "EU")
DEFAULT_DAILY_LIMIT = 40
REQUEST_PAUSE = 0.5
EXPECTED_LAG_DAYS = 120          # year end -> annual report, UK/EU (estimate)
ROLL_FORWARD_AFTER_DAYS = 200    # an expected date this stale is a dead/changed filer, not "late"
SOURCE = "yfinance"
FORM = "AR"

# (concept, statement, Yahoo row labels in priority order, transform)
_MONEY = "money"
_SHARES = "shares"


def _outflow(v: float) -> float:       # payments are negative on Yahoo; we store them positive, like SEC
    return abs(v)


def _buyback(v: float) -> float:       # a positive 'Repurchase Of Capital Stock' is not a repurchase
    return -v if v < 0 else 0.0


FLOWS = [
    ("Revenues", "income", ("Total Revenue", "Operating Revenue"), _MONEY, None),
    ("NetIncomeLoss", "income", ("Net Income Common Stockholders", "Net Income"), _MONEY, None),
    ("OperatingIncomeLoss", "income", ("Operating Income", "EBIT"), _MONEY, None),
    ("WeightedAverageNumberOfDilutedSharesOutstanding", "income", ("Diluted Average Shares",), _SHARES, None),
    ("WeightedAverageNumberOfSharesOutstandingBasic", "income", ("Basic Average Shares",), _SHARES, None),
    ("NetCashProvidedByUsedInOperatingActivities", "cash",
     ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities"), _MONEY, None),
    ("PaymentsToAcquirePropertyPlantAndEquipment", "cash", ("Capital Expenditure", "Purchase Of PPE"), _MONEY, _outflow),
    ("ShareBasedCompensation", "cash", ("Stock Based Compensation",), _MONEY, None),
    ("PaymentsOfDividends", "cash", ("Cash Dividends Paid", "Common Stock Dividend Paid"), _MONEY, _outflow),
    ("PaymentsForRepurchaseOfCommonStock", "cash", ("Repurchase Of Capital Stock", "Common Stock Payments"), _MONEY, _buyback),
]
INSTANTS = [
    ("StockholdersEquity", "balance", ("Stockholders Equity", "Common Stock Equity"), _MONEY, None),
    ("LongTermDebt", "balance", ("Total Debt", "Long Term Debt And Capital Lease Obligation"), _MONEY, None),
    ("CashAndCashEquivalentsAtCarryingValue", "balance", ("Cash And Cash Equivalents",), _MONEY, None),
    ("ShortTermInvestments", "balance", ("Other Short Term Investments",), _MONEY, None),
    ("IntangibleAssetsNetIncludingGoodwill", "balance", ("Goodwill And Other Intangible Assets",), _MONEY, None),
    ("CommonStockSharesOutstanding", "balance", ("Ordinary Shares Number", "Share Issued"), _SHARES, None),
]
NON_MONEY_UNITS = {"shares", "ratio"}


# ── pure helpers ─────────────────────────────────────────────────────────────

def norm_ccy(c: str | None) -> str | None:
    """Currency code as used in fx_rates; pence (Yahoo 'GBp', our 'GBX') is its own unit."""
    if not c:
        return None
    if c in ("GBp", "GBX", "GBx"):
        return "GBX"
    return c.upper()


def fx_factor(frm: str | None, to: str | None, rates: dict[str, float]) -> float | None:
    """Multiply an amount in `frm` by this to get `to`. rates: USD per 1 unit of each currency
    (GBX is derived from GBP). None when either currency has no rate. Pure."""
    frm, to = norm_ccy(frm), norm_ccy(to)
    if not frm or not to:
        return None
    r = dict(rates)
    if "GBP" in r:
        r.setdefault("GBX", r["GBP"] / 100.0)
    if frm not in r or to not in r or not r[to]:
        return None
    return r[frm] / r[to]


def frame_to_dict(df) -> dict[str, dict[str, float]]:
    """Yahoo statement DataFrame (labels x period-end Timestamps) -> {label: {YYYY-MM-DD: value}}, NaNs dropped."""
    out: dict[str, dict[str, float]] = {}
    if df is None or getattr(df, "empty", True):
        return out
    for label, ser in df.iterrows():
        row = {}
        for col, v in ser.items():
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f == f:
                row[str(col)[:10]] = f
        if row:
            out[str(label)] = row
    return out


def extract_rows(frames: dict[str, dict], ticker: str, exchange: str, fin_ccy: str | None,
                 now_iso: str) -> list[dict]:
    """financial_statement_facts rows from {'income'|'cash'|'balance': {label: {date: value}}}. Pure.
    Annual periods only (Yahoo's statements are annual); empty when there is no usable income/cash data."""
    ccy = norm_ccy(fin_ccy)
    rows: list[dict] = []
    for concept, stmt, labels, kind, fn in FLOWS + INSTANTS:
        data = frames.get(stmt) or {}
        label = next((l for l in labels if data.get(l)), None)
        if label is None:
            continue
        for end, v in data[label].items():
            if kind == _MONEY and not ccy:
                continue    # a money figure with no known currency can't be converted: don't store it
            value = fn(v) if fn else v
            start = (date.fromisoformat(end) - timedelta(days=364)).isoformat() if stmt != "balance" else None
            rows.append({
                "ticker": ticker, "exchange": exchange, "concept": concept, "period_end": end,
                "period_start": start, "value": value, "unit": ccy if kind == _MONEY else "shares",
                "fiscal_year": int(end[:4]), "fiscal_period": "FY", "form_type": FORM,
                "filed_date": None, "accession_number": None, "source": SOURCE, "fetched_at": now_iso,
            })
    if not any(r["concept"] in ("NetIncomeLoss", "NetCashProvidedByUsedInOperatingActivities", "Revenues") for r in rows):
        return []
    return rows


def calendar_for(rows: list[dict], today: date) -> dict | None:
    """Estimated forward calendar from the newest annual period on file. Pure."""
    ends = sorted({r["period_end"] for r in rows if r["period_start"]})
    if not ends:
        return None
    last = date.fromisoformat(ends[-1])
    nxt = filings.add_months(last, 12)
    while nxt + timedelta(days=EXPECTED_LAG_DAYS) < today - timedelta(days=ROLL_FORWARD_AFTER_DAYS):
        nxt = filings.add_months(nxt, 12)
    return {"fiscal_year_end": f"{last.month:02d}{last.day:02d}", "last_report_date": last.isoformat(), "last_form": FORM,
            "next_period_end": nxt.isoformat(), "next_expected_form": FORM,
            "next_expected_date": (nxt + timedelta(days=EXPECTED_LAG_DAYS)).isoformat()}


def convert_rows(rows: list[dict], to_ccy: str | None, rates: dict[str, float]) -> tuple[list[dict], dict]:
    """Money rows -> the trading currency; share counts and the price pseudo-facts (already in trading
    units) are untouched. Returns (rows, fx_meta). fx_meta['factor'] is None if a needed rate is missing,
    in which case money rows are returned unconverted and the caller must not value them. Pure."""
    from_ccys = {r["unit"] for r in rows if r["unit"] not in NON_MONEY_UNITS and r["concept"] not in filings.PSEUDO_CONCEPTS}
    if len(from_ccys) > 1:
        return rows, {"from": sorted(from_ccys), "to": to_ccy, "factor": None, "error": "mixed reporting currencies"}
    frm = next(iter(from_ccys), None)
    if frm is None:
        return rows, {"from": None, "to": to_ccy, "factor": 1.0}
    f = fx_factor(frm, to_ccy, rates)
    meta = {"from": frm, "to": norm_ccy(to_ccy), "factor": f}
    if f is None:
        meta["error"] = f"no FX rate for {frm}->{to_ccy}"
        return rows, meta
    out = []
    for r in rows:
        if r["unit"] in NON_MONEY_UNITS or r["concept"] in filings.PSEUDO_CONCEPTS or r["value"] is None:
            out.append(r)
        else:
            out.append({**r, "value": r["value"] * f})
    return out, meta


# ── I/O ──────────────────────────────────────────────────────────────────────

def load_rates(client) -> dict[str, float]:
    """Latest USD-per-unit rate for each currency in fx_rates."""
    rows = db.query(client, """
        SELECT currency, usd_rate FROM fx_rates
        WHERE date = (SELECT MAX(f2.date) FROM fx_rates f2 WHERE f2.currency = fx_rates.currency);
    """)
    return {r["currency"]: r["usd_rate"] for r in rows if r["usd_rate"]}


def trading_currency(client, ticker: str, exchange: str) -> str | None:
    """The currency the ticker's latest stored price is in (what fair values are compared against),
    falling back to the universe's own label."""
    p = db.query(client, "SELECT currency FROM prices WHERE ticker = :t AND exchange = :e ORDER BY date DESC LIMIT 1;",
                 {"t": ticker, "e": exchange})
    if p and p[0]["currency"]:
        return p[0]["currency"]
    u = db.query(client, "SELECT currency FROM universe WHERE ticker = :t AND exchange = :e;", {"t": ticker, "e": exchange})
    return u[0]["currency"] if u else None


def load_financials(client, ticker: str, exchange: str, rates: dict[str, float] | None = None,
                    to_ccy: str | None = None) -> dict:
    """filings.load_financials for a UK/EU ticker, with every money figure in the trading currency.
    Adds 'fx' (from, to, factor[, error]) so the audit trail can show the conversion."""
    rows = db.query(client, """
        SELECT concept, period_end, period_start, value, unit FROM financial_statement_facts
        WHERE ticker = :t AND exchange = :e;
    """, {"t": ticker, "e": exchange})
    to_ccy = to_ccy or trading_currency(client, ticker, exchange)
    rows, meta = convert_rows(rows, to_ccy, rates if rates is not None else load_rates(client))
    fin = filings.build_financials(rows)
    fin["fx"] = meta
    return fin


def fetch_yahoo(yahoo_ticker: str) -> tuple[str | None, dict]:
    """(financialCurrency, {'income','cash','balance': {label: {date: value}}}) — raises on network trouble."""
    import fetchers  # noqa: F401  (side effect: loads .env + CA-bundle settings for yfinance)
    import yfinance as yf
    tk = yf.Ticker(yahoo_ticker)
    info = tk.info or {}
    frames = {"income": frame_to_dict(tk.financials), "cash": frame_to_dict(tk.cashflow),
              "balance": frame_to_dict(tk.balance_sheet)}
    return info.get("financialCurrency"), frames


def scope_tickers(client) -> list[dict]:
    """Active UK/EU equities with a Yahoo symbol, with a priority (0 held, 1 core/MF pass, 2 the rest)."""
    universe = db.query(client, """
        SELECT ticker, exchange, yahoo_ticker, index_membership FROM universe
        WHERE active = 1 AND exchange IN ('UK', 'EU') AND asset_class = 'equity'
          AND yahoo_ticker IS NOT NULL AND yahoo_ticker != '';
    """)
    held = {(r["ticker"], r["exchange"]) for r in db.query(client, """
        SELECT DISTINCT ticker, exchange FROM trades WHERE exchange IN ('UK', 'EU') AND status = 'open'
          AND portfolio_id IN ('trading-portfolio', 'equity-pension');
    """)}
    mf_pass = {(r["ticker"], r["exchange"]) for r in db.query(client, """
        SELECT ticker, exchange FROM v_magic_formula_latest WHERE exchange IN ('UK', 'EU') AND passes_thresholds = 1;
    """)}
    out = []
    for u in universe:
        key, member = (u["ticker"], u["exchange"]), u["index_membership"] or ""
        prio = 0 if key in held else 1 if key in mf_pass or any(m in member for m in filings.CORE_MEMBERSHIP) else 2
        out.append({"ticker": u["ticker"], "exchange": u["exchange"], "yahoo_ticker": u["yahoo_ticker"], "priority": prio})
    return out


def process_ticker(client, t: dict, today: date, now_iso: str, fetch=fetch_yahoo, enrich=True) -> dict:
    """One ticker's full cycle. Returns {'status','message','facts_rows'}; never raises for data problems
    (network errors from `fetch` propagate so the caller can count consecutive failures)."""
    ticker, exchange, yahoo = t["ticker"], t["exchange"], t["yahoo_ticker"]
    res = {"ticker": ticker, "status": "ok", "message": None, "facts_rows": 0}
    fin_ccy, frames = fetch(yahoo)
    rows = extract_rows(frames, ticker, exchange, fin_ccy, now_iso)
    base_cal = {"ticker": ticker, "exchange": exchange, "last_checked_at": now_iso, "facts_pending": 0}
    if not rows:
        res["message"] = ("n/a: no annual statements on Yahoo (fund / trust / thin coverage)" if not any(frames.values())
                          else "n/a: statements have no revenue, net income or operating cash flow"
                          + ("" if fin_ccy else " (and no reporting currency)"))
        db.upsert(client, "filings_calendar", [base_cal])
        return res
    if not fin_ccy:
        res.update(status="degraded", message="statements found but Yahoo gives no financialCurrency: money figures not stored")
    db.upsert(client, "financial_statement_facts", rows)
    res["facts_rows"] = len(rows)
    if enrich:
        try:   # yearly average price + split flags for the SBC maths; best-effort, never fails the ticker
            import price_history
            price_history.enrich(client, ticker, exchange, yahoo_ticker=yahoo)
        except Exception as e:
            res["message"] = f"price history unavailable ({type(e).__name__}): SBC-adjusted valuation will lack prices"
    cal = calendar_for(rows, today)
    db.upsert(client, "filings_calendar", [{**base_cal, **(cal or {}), "last_facts_at": now_iso}])
    return res


def refresh(limit: int = DEFAULT_DAILY_LIMIT, tickers: list[str] | None = None, dry_run: bool = False,
            force: bool = False) -> dict:
    today, now_iso = date.today(), datetime.now(timezone.utc).isoformat()
    client = db.get_client()
    try:
        scoped = scope_tickers(client)
        cals = {(r["ticker"], r["exchange"]): r for r in db.query(
            client, "SELECT * FROM filings_calendar WHERE exchange IN ('UK', 'EU');")}
        candidates = [{**s, "cal": cals.get((s["ticker"], s["exchange"]))} for s in scoped]
        if tickers or force:
            want = {x.upper() for x in tickers} if tickers else None
            batch = [c for c in candidates if want is None or c["ticker"] in want or c["yahoo_ticker"].upper() in want][:limit]
        else:
            batch = filings.pick_batch(candidates, today, limit)
        stats = {"scoped": len(scoped), "picked": len(batch), "checked": 0, "facts_tickers": 0, "facts_rows": 0,
                 "n_a": 0, "degraded": 0, "errors": 0}
        print(f"[filings_eu] {len(scoped)} UK/EU tickers, {len(batch)} due (limit {limit})")
        if dry_run:
            for b in batch:
                print(f"  would check {b['ticker']} ({b['exchange']}, {b['yahoo_ticker']}) — {'new' if b['cal'] is None else 'due'}")
            return stats
        dq_rows, consecutive = [], 0
        for i, b in enumerate(batch):
            try:
                res = process_ticker(client, b, today, now_iso)
                consecutive = 0
            except Exception as e:
                consecutive += 1
                stats["errors"] += 1
                dq_rows.append({"ticker": b["ticker"], "exchange": b["exchange"], "data_type": "filings", "status": "error",
                                "source": SOURCE, "error": f"{type(e).__name__}: {e}"[:300]})
                print(f"  {b['ticker']} ({b['yahoo_ticker']}): ERROR {type(e).__name__}: {str(e)[:120]}")
                if consecutive >= 5:
                    print("[filings_eu] 5 consecutive failures — Yahoo throttling or unreachable? stopping this run early")
                    break
                time.sleep(REQUEST_PAUSE * 4)
                continue
            stats["checked"] += 1
            if res["facts_rows"]:
                stats["facts_tickers"] += 1
                stats["facts_rows"] += res["facts_rows"]
            elif res["status"] == "ok":
                stats["n_a"] += 1
            if res["status"] == "degraded":
                stats["degraded"] += 1
            dq_rows.append({"ticker": b["ticker"], "exchange": b["exchange"], "data_type": "filings", "status": res["status"],
                            "source": SOURCE, "error": res["message"]})
            if (i + 1) % 50 == 0:
                print(f"  ...{i + 1}/{len(batch)} done ({stats['facts_tickers']} with statements)", flush=True)
                dq.record_batch(client, dq_rows)
                dq_rows = []
            time.sleep(REQUEST_PAUSE)
        if dq_rows:
            dq.record_batch(client, dq_rows)
        return stats
    finally:
        client.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=DEFAULT_DAILY_LIMIT)
    ap.add_argument("--ticker", nargs="*", help="check these tickers (or Yahoo symbols) now, ignoring the schedule")
    ap.add_argument("--force", action="store_true", help="ignore the schedule: take the first --limit tickers in scope")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    print(refresh(limit=a.limit, tickers=a.ticker, dry_run=a.dry_run, force=a.force))
    sys.stdout.flush()
    os._exit(0)
