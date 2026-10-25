"""Master spec Phase 17 — SEC filings calendar and a fetch-once financial-fact
cache for US-listed tickers, filled in gradually: a small batch every day
rather than the whole universe at once.

Why per-company endpoints, not the spec's bulk zips (checked 2026-10-25):
companyfacts.zip is 1.4 GB and the spec's submissions.zip path returns 403,
while data.sec.gov's per-company endpoints work, are free, and cost ~164 KB
(filings list) + ~3.8 MB (facts, full history) per ticker. For a few hundred
tickers filled over a couple of weeks — and thereafter only tickers with a new
filing — that is far lighter than a monthly 1.4 GB pull, and it needs no
unzip/parse of every SEC filer.

What one daily run does (refresh()):
  1. Scope: active US equities that matter — held positions, Magic Formula
     passers, investor-flagged, wiki-mentioned, Burry (scope='core'), or every
     US equity (scope='all').
  2. pick_batch(): today's batch of at most `limit` tickers, in priority order:
       tier 0  never checked
       tier 1  a new filing is known but its facts aren't in yet (retry)
       tier 2  the forward calendar says a filing is due now (re-check every
               2 days for 3 weeks after the expected date, then weekly)
       tier 3  not checked in 30 days (safety net)
     Everything else is skipped, so most days only a handful of tickers cost
     any requests once the initial fill is done.
  3. Per ticker: poll SEC submissions -> record new periodic filings in
     sec_filings -> recompute the forward calendar -> pull companyfacts ONLY if
     there is something new -> cache the curated facts in
     financial_statement_facts -> mirror the latest figures onto fundamentals.

Forward calendar: SEC's fiscalYearEnd plus the last periodic report give the
next period end; adding that company's own median historical filing lag for
that form type (10-K vs 10-Q) gives next_expected_date — stored in
filings_calendar and surfaced by v_filings_calendar_upcoming. An estimate for
scheduling and for showing what's coming, not a promise.

US filers on the us-gaap taxonomy only. IFRS/foreign filers (20-F), ADRs, OTC
names and ETFs have no usable us-gaap facts and are recorded as `degraded` in
data_quality rather than guessed at.

Run standalone: python filings.py [--limit N] [--scope core|all] [--ticker T ...]
                                  [--dry-run] [--upcoming]
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import requests

import data_quality as dq
import db
import resolve_ticker as _rt  # reuses its SEC User-Agent, Norton-safe CA bundle and ticker cache path

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
REQUEST_PAUSE = 0.2          # SEC fair access allows 10 requests/second
DEFAULT_DAILY_LIMIT = 30
FACT_YEARS = 11   # 10 annual SBC-cost periods need 11 year-ends (see owners_earnings.py)
BALANCE_MAX_AGE_DAYS = 400   # see build_financials()
ANNUAL_FORMS = {"10-K", "20-F", "40-F"}
PERIODIC_FORMS = {"10-K", "10-Q", "20-F", "40-F"}
DEFAULT_LAG = {"annual": 60, "quarterly": 38}
CORE_MEMBERSHIP = ("INVESTOR_FLAGGED", "WIKI_MENTIONED", "PORTFOLIO_HOLDING", "BURRY")

# metric -> us-gaap tags in priority order. Anything not listed here is never
# stored; this is a curated set, not a copy of the whole filing.
METRIC_TAGS: dict[str, list[str]] = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "ebit": ["OperatingIncomeLoss"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
    "sbc_expense": ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"],
    "dividends_paid": ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"],
    "buyback_dollars": ["PaymentsForRepurchaseOfCommonStock"],
    "shares_diluted": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    # SBC true-cost inputs (Burry's "Tragic Algebra", owners_earnings.py). Tags
    # are NOT uniform across companies — GOOGL, for one, reports no withholding
    # tag at all — so every one of these may legitimately be absent.
    "shares_outstanding": ["CommonStockSharesOutstanding"],
    "shares_basic": ["WeightedAverageNumberOfSharesOutstandingBasic"],
    "tax_withholding": ["PaymentsRelatedToTaxWithholdingForShareBasedCompensation",
                        "AdjustmentsRelatedToTaxWithholdingForShareBasedCompensation"],   # equity-statement version (LRCX, CDNS)
    # priority order matters: INTU reports both the plan-total tag (180M) and its
    # option-exercise sub-component (14M); taking the first present avoids double counting.
    "option_proceeds": ["ProceedsFromIssuanceOfSharesUnderIncentiveAndShareBasedCompensationPlansIncludingStockOptions",
                        "ProceedsFromStockPlans", "ProceedsFromStockOptionsExercised"],
    # MSFT's ESPP proceeds are only tagged this way. It can also be a genuine equity
    # raise, so owners_earnings only credits it when the share count did not jump.
    "equity_proceeds": ["ProceedsFromIssuanceOfCommonStock"],
    # share-count changes that are NOT employee compensation (Burry: "other reasons
    # for share count changes must be identified and set aside")
    "shares_acquired": ["StockIssuedDuringPeriodSharesAcquisitions"],
    "shares_converted": ["StockIssuedDuringPeriodSharesConversionOfConvertibleSecurities"],
    # same set-asides in dollars, for filers that tag the value but not the share count (INTU, CRM)
    "acq_value": ["StockIssuedDuringPeriodValueAcquisitions"],
    "conv_value": ["StockIssuedDuringPeriodValueConversionOfConvertibleSecurities"],
    # Not SEC tags: written by price_history.py from Yahoo, one pair per fiscal
    # year, so the SBC maths can price shares at the time they were issued.
    "avg_price": ["AvgMarketPrice"],
    "split_ratio": ["SplitRatioInPeriod"],
    # LULU (among others) stopped using the plain tag in 2019 and reports only the
    # restricted-cash-inclusive one; resolution is per period, so the fallback
    # only applies where the plain tag is absent.
    "cash": ["CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "short_term_investments": ["MarketableSecuritiesCurrent", "ShortTermInvestments",
                               "AvailableForSaleSecuritiesCurrent"],
    "stockholders_equity": ["StockholdersEquity",
                            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "goodwill_intangibles_combined": ["IntangibleAssetsNetIncludingGoodwill"],
    "goodwill": ["Goodwill"],
    "intangibles": ["IntangibleAssetsNetExcludingGoodwill"],
    "debt_total": ["LongTermDebt"],
    "debt_noncurrent": ["LongTermDebtNoncurrent"],
    "debt_current": ["LongTermDebtCurrent", "DebtCurrent"],
    "commercial_paper": ["CommercialPaper", "ShortTermBorrowings"],
}
PSEUDO_CONCEPTS = {"AvgMarketPrice", "SplitRatioInPeriod"}   # written by price_history.py, not SEC tags
TAG_TO_METRIC = {tag: m for m, tags in METRIC_TAGS.items() for tag in tags}


# ── pure helpers (no DB, no network) ─────────────────────────────────────────

def add_months(d: date, months: int) -> date:
    """d + N calendar months, clipped to the target month's last day."""
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, monthrange(year, month)[1]))


def parse_submissions(sub: dict) -> dict:
    """Reduce SEC's submissions JSON to what we use: the fiscal year end
    ('MMDD') and the periodic reports (10-K/10-Q/20-F/40-F, originals only —
    amendments don't move the calendar), newest first."""
    rec = (sub.get("filings") or {}).get("recent") or {}
    forms = rec.get("form") or []
    filings = []
    for i, form in enumerate(forms):
        if form not in PERIODIC_FORMS:
            continue
        filings.append({
            "form": form,
            "filed": rec["filingDate"][i],
            "report_date": (rec.get("reportDate") or [None] * len(forms))[i] or None,
            "accession": rec["accessionNumber"][i],
        })
    filings.sort(key=lambda f: (f["report_date"] or f["filed"]), reverse=True)
    return {"fiscal_year_end": sub.get("fiscalYearEnd"), "cik": str(sub.get("cik", "")).zfill(10) or None,
            "filings": filings}


def _median_lag(filings: list[dict], annual: bool, n: int) -> int:
    lags = []
    for f in filings:
        if (f["form"] in ANNUAL_FORMS) != annual or not f["report_date"]:
            continue
        lags.append((date.fromisoformat(f["filed"]) - date.fromisoformat(f["report_date"])).days)
        if len(lags) >= n:
            break
    if not lags:
        return DEFAULT_LAG["annual" if annual else "quarterly"]
    return max(20, min(120, int(statistics.median(lags))))


def compute_calendar(fiscal_year_end: str | None, filings: list[dict]) -> dict | None:
    """The forward calendar: next period end, which form it will be, and the
    expected filing date (period end + this company's own median lag for that
    form). None if there's no periodic report with a period date to project
    from."""
    dated = [f for f in filings if f["report_date"]]
    if not dated:
        return None
    last = dated[0]
    last_end = date.fromisoformat(last["report_date"])
    fye_month = int(fiscal_year_end[:2]) if fiscal_year_end and len(fiscal_year_end) == 4 else None

    if last["form"] in ("20-F", "40-F"):  # annual-only filers
        next_end, next_form = add_months(last_end, 12), last["form"]
    else:
        next_end = add_months(last_end, 3)
        if fye_month is None:
            fye_month = next((date.fromisoformat(f["report_date"]).month for f in dated if f["form"] == "10-K"), None)
        next_form = "10-K" if fye_month is not None and next_end.month == fye_month else "10-Q"

    lag = _median_lag(filings, annual=next_form in ANNUAL_FORMS, n=5 if next_form in ANNUAL_FORMS else 8)
    return {
        "fiscal_year_end": fiscal_year_end,
        "last_report_date": last["report_date"], "last_form": last["form"], "last_filed_date": last["filed"],
        "next_period_end": next_end.isoformat(), "next_expected_form": next_form,
        "next_expected_date": (next_end + timedelta(days=lag)).isoformat(),
    }


def pick_batch(candidates: list[dict], today: date, limit: int) -> list[dict]:
    """Today's work list. Each candidate: {ticker, exchange, priority, cal} where
    cal is its filings_calendar row (or None if never checked). Returns at most
    `limit`, tier order first, then priority (0 = held positions, then core),
    then least-recently-checked."""
    ranked = []
    for c in candidates:
        cal = c.get("cal")
        if cal is None:
            tier = 0
        elif cal.get("facts_pending"):
            tier = 1
        else:
            last_checked = (cal.get("last_checked_at") or "")[:10]
            age = (today - date.fromisoformat(last_checked)).days if last_checked else 9999
            exp = cal.get("next_expected_date")
            tier = None
            if exp and exp <= today.isoformat():
                overdue = (today - date.fromisoformat(exp)).days
                if age >= (2 if overdue <= 21 else 7):
                    tier = 2
            if tier is None and age >= 30:
                tier = 3
        if tier is None:
            continue
        ranked.append((tier, c.get("priority", 2), (cal or {}).get("last_checked_at") or "", c["ticker"], c))
    ranked.sort(key=lambda r: r[:4])
    return [r[4] for r in ranked[:limit]]


def _is_annual_form(form: str | None) -> bool:
    return bool(form) and (form in ANNUAL_FORMS or form.startswith("10-K"))


def extract_facts(cf: dict, ticker: str, exchange: str, today: date | None = None,
                  fetched_at: str | None = None) -> tuple[list[dict], set[str]]:
    """Curated facts from one SEC companyfacts document -> (rows, seen_accessions).

    Keeps only: the METRIC_TAGS concepts, annual-length flows (350-380 days) and
    balance-sheet instants, from the last FACT_YEARS years. SEC tags every figure
    in a filing — including the prior-period comparatives it re-reports — with
    that filing's own fy/fp, so facts are keyed by their own period end and, when
    one period is reported more than once (original, restatement, later
    comparative), the latest-filed value wins.

    seen_accessions is every accession that carried any curated figure, kept
    BEFORE dedupe/windowing: it's how the caller confirms a newly-announced
    filing's data has actually reached companyfacts yet.
    """
    today = today or date.today()
    cutoff = (today - timedelta(days=365 * FACT_YEARS)).isoformat()
    us = (cf.get("facts") or {}).get("us-gaap") or {}
    best: dict[tuple[str, str], dict] = {}
    seen: set[str] = set()
    for tag in TAG_TO_METRIC:
        node = us.get(tag)
        if not node:
            continue
        for unit, items in (node.get("units") or {}).items():
            if unit not in ("USD", "shares"):
                continue
            for it in items:
                if it.get("accn"):
                    seen.add(it["accn"])
                end, start = it.get("end"), it.get("start")
                if not end or end < cutoff or it.get("val") is None:
                    continue
                if start:
                    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
                    if not 350 <= days <= 380:
                        continue
                    # 10-Qs can carry 12-month flows ending at a quarter end (AMZN's
                    # trailing-twelve-month cash flow); those are not fiscal years.
                    if not _is_annual_form(it.get("form")):
                        continue
                key = (tag, end)
                filed = it.get("filed") or ""
                cur = best.get(key)
                if cur is None or filed > cur["filed_date"]:
                    best[key] = {
                        "ticker": ticker, "exchange": exchange, "concept": tag, "period_end": end,
                        "period_start": start, "value": float(it["val"]), "unit": unit,
                        "fiscal_year": int(end[:4]), "form_type": it.get("form"),
                        "filed_date": filed, "accession_number": it.get("accn"),
                        "source": "sec_edgar", "fetched_at": fetched_at,
                    }
    annual_ends = {r["period_end"] for r in best.values() if r["period_start"]}
    for r in best.values():
        r["fiscal_period"] = "FY" if (r["period_start"] or r["period_end"] in annual_ends) else "Q"
    return list(best.values()), seen


def _pick(tags_at_end: dict[str, float], metric: str) -> float | None:
    for tag in METRIC_TAGS[metric]:
        if tag in tags_at_end:
            return tags_at_end[tag]
    return None


def _metrics_at(tags_at_end: dict[str, float]) -> dict[str, float | None]:
    """Collapse the raw tags reported for ONE period end into our metrics."""
    m = {k: _pick(tags_at_end, k) for k in
         ("revenue", "net_income", "ebit", "operating_cash_flow", "capex", "sbc_expense",
          "dividends_paid", "buyback_dollars", "shares_diluted", "cash", "short_term_investments",
          "stockholders_equity", "shares_outstanding", "shares_basic", "tax_withholding",
          "option_proceeds", "equity_proceeds", "shares_acquired", "shares_converted", "acq_value", "conv_value", "avg_price", "split_ratio")}
    total = _pick(tags_at_end, "debt_total")
    if total is None:
        parts = [_pick(tags_at_end, k) for k in ("debt_noncurrent", "debt_current", "commercial_paper")]
        total = sum(p for p in parts if p is not None) if any(p is not None for p in parts) else None
    m["total_debt"] = total
    combined = _pick(tags_at_end, "goodwill_intangibles_combined")
    if combined is not None:
        m["goodwill_and_intangibles"] = combined
    else:
        gw, ia = _pick(tags_at_end, "goodwill"), _pick(tags_at_end, "intangibles")
        m["goodwill_and_intangibles"] = (gw or 0) + (ia or 0) if (gw is not None or ia is not None) else None
    return m


def build_financials(rows: list[dict]) -> dict:
    """financial_statement_facts rows for ONE ticker -> {"annual": {period_end:
    metrics} for each fiscal year with annual flows, "latest_balance": the most
    recent balance-sheet metrics at ANY quarter end (+ its period_end)}. Pure."""
    by_end: dict[str, dict[str, float]] = defaultdict(dict)
    annual_ends: set[str] = set()
    for r in rows:
        by_end[r["period_end"]][r["concept"]] = r["value"]
        # fiscal-year ends come from SEC annual flows only; the yfinance pseudo-facts
        # carry period_start too and must not be able to invent a "year end"
        if r.get("period_start") and r["concept"] not in PSEUDO_CONCEPTS:
            annual_ends.add(r["period_end"])
    annual = {e: _metrics_at(by_end[e]) for e in sorted(annual_ends)}
    balance_keys = ("cash", "short_term_investments", "stockholders_equity", "total_debt", "goodwill_and_intangibles")
    latest_balance: dict = {}
    ends = sorted(by_end, reverse=True)
    # A metric's "latest" value must come from a balance sheet no more than
    # BALANCE_MAX_AGE_DAYS older than the newest one on file. Without this, a tag
    # a company stopped using years ago (LULU's plain cash tag, last reported
    # 2019) would be returned as today's balance.
    floor = (date.fromisoformat(ends[0]) - timedelta(days=BALANCE_MAX_AGE_DAYS)).isoformat() if ends else None
    for end in ends:
        if end < floor:
            break
        m = _metrics_at(by_end[end])
        for k in balance_keys:
            if k not in latest_balance and m.get(k) is not None:
                latest_balance[k] = m[k]
                latest_balance.setdefault("_period_end", {})[k] = end
    return {"annual": annual, "latest_balance": latest_balance}


def load_financials(client, ticker: str, exchange: str = "US") -> dict:
    rows = db.query(client, """
        SELECT concept, period_end, period_start, value FROM financial_statement_facts
        WHERE ticker = :t AND exchange = :e;
    """, {"t": ticker, "e": exchange})
    return build_financials(rows)


# ── SEC access ───────────────────────────────────────────────────────────────

class SecClient:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(_rt._SEC_HEADERS)
        self.s.verify = _rt._SSL_VERIFY

    def get_json(self, url: str) -> dict | None:
        """GET with polite pacing and retry on 429/5xx. None for a 404 (no such
        filer/data); raises after repeated failures."""
        last_err = None
        for attempt, backoff in enumerate((0, 2, 5, 10)):
            if backoff:
                time.sleep(backoff)
            try:
                r = self.s.get(url, timeout=60)
            except requests.RequestException as e:
                last_err = e
                continue
            finally:
                time.sleep(REQUEST_PAUSE)
            if r.status_code == 404:
                return None
            if r.status_code == 200:
                return r.json()
            last_err = RuntimeError(f"HTTP {r.status_code} for {url}")
            if r.status_code not in (429, 500, 502, 503, 504):
                break
        raise last_err or RuntimeError(f"failed: {url}")


def load_cik_map() -> dict[str, str]:
    """ticker -> zero-padded 10-digit CIK, from SEC's company_tickers.json (cached
    on disk by resolve_ticker, refreshed monthly)."""
    path, max_age = _rt._SEC_CACHE_PATH, _rt._SEC_CACHE_MAX_AGE_DAYS
    stale = (not path.exists()) or (datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)).days >= max_age
    if stale:
        r = requests.get(_rt._SEC_URL, headers=_rt._SEC_HEADERS, timeout=30, verify=_rt._SSL_VERIFY)
        r.raise_for_status()
        path.write_text(json.dumps(r.json()))
    raw = json.loads(path.read_text())
    out: dict[str, str] = {}
    for row in raw.values():
        t = (row.get("ticker") or "").upper().replace(".", "-")
        if t and t not in out:
            out[t] = str(row["cik_str"]).zfill(10)
    return out


# ── DB-facing pieces ─────────────────────────────────────────────────────────

def scope_tickers(client, scope: str = "core") -> list[dict]:
    """Active US equities to cover, each with a priority (0 held, 1 other core,
    2 the rest — only returned for scope='all')."""
    universe = db.query(client, """
        SELECT ticker, exchange, index_membership FROM universe
        WHERE active = 1 AND exchange = 'US' AND asset_class = 'equity';
    """)
    held = {r["ticker"] for r in db.query(client, """
        SELECT DISTINCT ticker FROM trades WHERE exchange = 'US' AND status = 'open'
          AND portfolio_id IN ('trading-portfolio', 'equity-pension');
    """)}
    mf_pass = {r["ticker"] for r in db.query(client, """
        SELECT ticker FROM v_magic_formula_latest WHERE exchange = 'US' AND passes_thresholds = 1;
    """)}
    out = []
    for u in universe:
        t, member = u["ticker"], u["index_membership"] or ""
        if t in held:
            prio = 0
        elif t in mf_pass or any(m in member for m in CORE_MEMBERSHIP):
            prio = 1
        else:
            prio = 2
        if prio <= 1 or scope == "all":
            out.append({"ticker": t, "exchange": u["exchange"], "priority": prio})
    return out


def mirror_to_fundamentals(client, ticker: str, exchange: str, fin: dict) -> bool:
    """Copy the latest annual flows + latest balance sheet onto the ticker's
    most recent fundamentals row (never creates a row: a raw-only row dated
    today would become the 'latest' fundamentals row with NULL ratios and could
    knock the ticker out of the Magic Formula screen). Column-preserving."""
    latest_fund = db.query(client, """
        SELECT MAX(as_of_date) d FROM fundamentals WHERE ticker = :t AND exchange = :e;
    """, {"t": ticker, "e": exchange})[0]["d"]
    annual = fin["annual"]
    if not latest_fund or not annual:
        return False
    fy = annual[max(annual)]
    bal = fin["latest_balance"]
    row = {
        "ticker": ticker, "exchange": exchange, "as_of_date": latest_fund,
        "revenue": fy["revenue"], "ebit": fy["ebit"], "net_income": fy["net_income"],
        "operating_cash_flow": fy["operating_cash_flow"], "capex": fy["capex"],
        "sbc_expense": fy["sbc_expense"], "dividends_paid": fy["dividends_paid"],
        "buyback_dollars": fy["buyback_dollars"], "shares_diluted": fy["shares_diluted"],
        "total_debt": bal.get("total_debt"), "cash_and_equivalents": bal.get("cash"),
        "stockholders_equity": bal.get("stockholders_equity"),
        "goodwill_and_intangibles": bal.get("goodwill_and_intangibles"),
    }
    db.upsert(client, "fundamentals", [row])
    return True


def process_ticker(client, sec: SecClient, t: dict, cik: str | None, today: date, now_iso: str,
                   force_facts: bool = False) -> dict:
    """One ticker's full cycle. Returns {"status", "message", "new_filings",
    "facts_rows", "touched"}; never raises for per-ticker data problems."""
    ticker, exchange = t["ticker"], t["exchange"]
    res = {"ticker": ticker, "status": "ok", "message": None, "new_filings": 0, "facts_rows": 0, "touched": False}
    if not cik:
        res.update(status="degraded", message="ticker not in SEC company_tickers (ETF/ADR/OTC/non-filer?)")
        db.upsert(client, "filings_calendar", [{"ticker": ticker, "exchange": exchange, "last_checked_at": now_iso, "facts_pending": 0}])
        return res

    sub = sec.get_json(SUBMISSIONS_URL.format(cik=cik))
    if sub is None:
        res.update(status="degraded", message=f"no SEC submissions for CIK {cik}")
        db.upsert(client, "filings_calendar", [{"ticker": ticker, "exchange": exchange, "cik": cik, "last_checked_at": now_iso, "facts_pending": 0}])
        return res
    parsed = parse_submissions(sub)
    cal = compute_calendar(parsed["fiscal_year_end"], parsed["filings"])

    known = {r["accession_number"] for r in db.query(
        client, "SELECT accession_number FROM sec_filings WHERE ticker = :t AND exchange = :e;", {"t": ticker, "e": exchange})}
    horizon = (today - timedelta(days=365 * FACT_YEARS)).isoformat()
    new = [f for f in parsed["filings"] if f["accession"] not in known and f["filed"] >= horizon]
    if new:
        db.upsert(client, "sec_filings", [{
            "ticker": ticker, "exchange": exchange, "cik": cik, "form_type": f["form"],
            "period_end": f["report_date"], "filed_date": f["filed"], "accession_number": f["accession"],
            "facts_fetched": 0, "discovered_at": now_iso,
        } for f in new])
        res["new_filings"] = len(new)

    unfetched = {r["accession_number"] for r in db.query(
        client, "SELECT accession_number FROM sec_filings WHERE ticker = :t AND exchange = :e AND facts_fetched = 0;",
        {"t": ticker, "e": exchange})}
    prior = db.query(client, "SELECT last_facts_at FROM filings_calendar WHERE ticker = :t AND exchange = :e;",
                     {"t": ticker, "e": exchange})
    never_pulled = not prior or not prior[0]["last_facts_at"]

    facts_pending, last_facts_at = 0, prior[0]["last_facts_at"] if prior else None
    if unfetched or never_pulled or force_facts:
        cf = sec.get_json(FACTS_URL.format(cik=cik))
        rows, seen = extract_facts(cf or {}, ticker, exchange, today, now_iso)
        if not rows:
            res.update(status="degraded", message="no us-gaap facts (IFRS/foreign filer, or no XBRL)")
        else:
            db.upsert(client, "financial_statement_facts", rows)
            res["facts_rows"], res["touched"], last_facts_at = len(rows), True, now_iso
            try:  # yearly average price + split flags for the SBC maths; best-effort, never fails the ticker
                import price_history
                price_history.enrich(client, ticker, exchange)
            except Exception as e:
                res["message"] = f"price history unavailable ({type(e).__name__}): SBC-adjusted valuation will lack prices"
            confirmed = unfetched & seen
            for acc in confirmed:
                db.upsert(client, "sec_filings", [{"ticker": ticker, "exchange": exchange, "accession_number": acc,
                                                   "facts_fetched": 1, "fetched_at": now_iso}])
            missing = unfetched - seen
            if missing:  # announced in submissions but companyfacts hasn't caught up — retry tomorrow
                facts_pending = 1
                res["message"] = f"{len(missing)} new filing(s) not yet in companyfacts"
            mirror_to_fundamentals(client, ticker, exchange, build_financials(rows))

    cal_row = {"ticker": ticker, "exchange": exchange, "cik": cik, "last_checked_at": now_iso,
               "last_facts_at": last_facts_at, "facts_pending": facts_pending}
    if cal:
        cal_row.update(cal)
    db.upsert(client, "filings_calendar", [cal_row])
    db.upsert(client, "universe", [{"ticker": ticker, "exchange": exchange, "cik": cik}])
    return res


def refresh(limit: int = DEFAULT_DAILY_LIMIT, scope: str = "core", tickers: list[str] | None = None,
            dry_run: bool = False, force_facts: bool = False) -> dict:
    today, now_iso = date.today(), datetime.now(timezone.utc).isoformat()
    client = db.get_client()
    try:
        scoped = scope_tickers(client, scope)
        if tickers:
            wanted = [t.upper() for t in tickers]
            in_scope = {s["ticker"]: s for s in scoped}
            scoped = [in_scope.get(t) or {"ticker": t, "exchange": "US", "priority": 0} for t in wanted]
        cals = {(r["ticker"], r["exchange"]): r for r in db.query(client, "SELECT * FROM filings_calendar;")}
        candidates = [{**s, "cal": cals.get((s["ticker"], s["exchange"]))} for s in scoped]
        if tickers or force_facts:  # explicit tickers / a forced re-pull always run, regardless of schedule
            batch = candidates[:limit]
        else:
            batch = pick_batch(candidates, today, limit)

        stats = {"scoped": len(scoped), "picked": len(batch), "checked": 0, "new_filings": 0,
                 "facts_tickers": 0, "facts_rows": 0, "degraded": 0, "errors": 0, "touched": []}
        print(f"[filings] scope={scope}: {len(scoped)} tickers, {len(batch)} due today (limit {limit})")
        if dry_run:
            for b in batch:
                tier = "new" if b["cal"] is None else "due"
                print(f"  would check {b['ticker']} ({tier})")
            return stats

        cik_map = load_cik_map()
        sec = SecClient()
        dq_rows, consecutive_failures = [], 0
        for b in batch:
            t = b["ticker"]
            try:
                res = process_ticker(client, sec, b, cik_map.get(t), today, now_iso, force_facts=force_facts)
                consecutive_failures = 0
            except Exception as e:  # network/SEC trouble for this ticker
                consecutive_failures += 1
                stats["errors"] += 1
                dq_rows.append({"ticker": t, "exchange": b["exchange"], "data_type": "filings",
                                "status": "error", "source": "sec_edgar", "error": f"{type(e).__name__}: {e}"[:300]})
                print(f"  {t}: ERROR {type(e).__name__}: {str(e)[:120]}")
                if consecutive_failures >= 3:
                    print("[filings] 3 consecutive failures — SEC unreachable? stopping this run early")
                    break
                continue
            stats["checked"] += 1
            stats["new_filings"] += res["new_filings"]
            if res["facts_rows"]:
                stats["facts_tickers"] += 1
                stats["facts_rows"] += res["facts_rows"]
            if res["touched"]:
                stats["touched"].append(t)
            if res["status"] == "degraded":
                stats["degraded"] += 1
            dq_rows.append({"ticker": t, "exchange": b["exchange"], "data_type": "filings", "status": res["status"],
                            "source": "sec_edgar", "error": res["message"]})
        if dq_rows:
            dq.record_batch(client, dq_rows)
        return stats
    finally:
        client.close()


def print_upcoming(days: int = 45) -> None:
    client = db.get_client()
    try:
        rows = db.query(client, """
            SELECT ticker, next_expected_form, next_period_end, next_expected_date, days_until
            FROM v_filings_calendar_upcoming WHERE days_until BETWEEN -7 AND :d ORDER BY next_expected_date LIMIT 60;
        """, {"d": days})
    finally:
        client.close()
    print(f"Expected periodic filings in the next {days} days (and any up to 7 days overdue): {len(rows)}")
    for r in rows:
        print(f"  {r['next_expected_date']}  {r['ticker']:<7} {r['next_expected_form']:<5} period ending {r['next_period_end']}  ({r['days_until']:+d}d)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=DEFAULT_DAILY_LIMIT)
    parser.add_argument("--scope", choices=["core", "all"], default="core")
    parser.add_argument("--ticker", nargs="*", help="check these tickers now, ignoring the schedule")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--refetch", action="store_true",
                        help="re-pull companyfacts for the --ticker list (or, without --ticker, the first --limit scoped tickers) even if nothing is new; use after changing METRIC_TAGS")
    parser.add_argument("--upcoming", action="store_true", help="print the forward filing calendar and exit")
    args = parser.parse_args()
    if args.upcoming:
        print_upcoming()
    else:
        s = refresh(limit=args.limit, scope=args.scope, tickers=args.ticker, dry_run=args.dry_run, force_facts=args.refetch)
        print({k: v for k, v in s.items() if k != "touched"}, "touched:", s["touched"])
