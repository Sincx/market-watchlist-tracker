-- Fred Finance System — Turso schema (v2.2 + cash ledger addition)
-- Source of truth: claude/fred-finance-system-spec.md §4, extended per the
-- 2026-09-10 review decision to track portfolio cash/margin in Turso rather
-- than leaving it dashboard-display-only.

-- ── Universe & market data ──────────────────────────────────────────────────

CREATE TABLE universe (
    ticker TEXT NOT NULL, exchange TEXT NOT NULL,
    index_membership TEXT NOT NULL,   -- 'SP500' | 'FTSE350' | 'STOXX600' | 'MORNINGSTAR' | 'INVESTOR_FLAGGED' | 'CRYPTO_CORE' | 'CRYPTO_DEFI' | 'WIKI_MENTIONED' | comma-list
    yahoo_ticker TEXT, currency TEXT, sector TEXT,
    added_date TEXT, active INTEGER DEFAULT 1,
    sa_prefix TEXT,   -- stockanalysis.com's exchange-prefix segment (quote/<prefix>/<ticker>), non-US only. Added 2026-09-10 — Phase 4 prep.
    notes TEXT,   -- manual, single current free-text note per ticker. Added 2026-09-11 (Phase 2 P2.3) via live ALTER TABLE.
    asset_class TEXT NOT NULL DEFAULT 'equity',   -- 'equity' | 'crypto'. Added 2026-09-11 (Phase 2 P2.1) via live ALTER TABLE.
    PRIMARY KEY (ticker, exchange)
);

-- Crypto-only metadata with no equity equivalent (chain, contract, protocol
-- facts previously hand-maintained in wiki/crypto/crypto-portfolio.md's
-- prose tables). universe/prices are reused as-is for crypto (asset_class
-- discriminator + exchange='CRYPTO') rather than forking parallel tables —
-- OHLCV + technical_rating already generalize fine to a crypto daily bar.
CREATE TABLE crypto_meta (
    ticker TEXT PRIMARY KEY,
    chain TEXT,                 -- 'Bitcoin' | 'Ethereum' | 'BNB Smart Chain' | 'Ethereum/Arbitrum/BSC' ...
    contract_address TEXT,
    category TEXT,              -- 'core' | 'defi' | 'meme' | ...
    protocol_notes TEXT,        -- staking APR, TVL source, security-rating notes — free text
    updated_at TEXT
);

CREATE TABLE prices (
    ticker TEXT, exchange TEXT, date TEXT,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    currency TEXT, usd_rate REAL,
    ma20 REAL, ma50 REAL, ma200 REAL, rsi14 REAL, macd_signal TEXT,
    vol_ratio REAL, technical_rating TEXT, fetched_at TEXT,
    atr14 REAL,   -- Wilder's ATR14 (indicators.py already computed this; was never
                  -- persisted until 2026-09-14). Used for stop-loss sizing (entry − 1.5×ATR14).
    PRIMARY KEY (ticker, exchange, date)
);

CREATE TABLE fundamentals (
    ticker TEXT, exchange TEXT, as_of_date TEXT,
    pe REAL, fwd_pe REAL, eps_growth REAL, rev_growth REAL, div_yield REAL,
    mkt_cap REAL, sector TEXT, earnings_yield REAL, roic REAL, roe REAL, ev_ebit REAL,
    source TEXT, fetched_at TEXT,
    PRIMARY KEY (ticker, exchange, as_of_date)
);

CREATE TABLE screen_results (
    run_date TEXT, ticker TEXT, exchange TEXT,
    earnings_yield REAL, roic REAL, ey_rank INTEGER, roic_rank INTEGER,
    mf_rank INTEGER, passes_thresholds INTEGER,
    PRIMARY KEY (run_date, ticker, exchange)
);

-- ── Signals — generalizes per-group "Buy Opportunities" flagging ───────────

CREATE TABLE signals (
    signal_id TEXT PRIMARY KEY, ticker TEXT, exchange TEXT,
    source TEXT,          -- 'morningstar-undervalued' | 'morningstar-dividend' | 'magic-formula-pass'
                           -- | 'investor:<investor_id>' | 'manual' | 'llm-research' (Phase 2 P2.0b —
                           -- a judgment task's own WebSearch/WebFetch finding, persisted before the
                           -- narrative is written, so it becomes a queryable fact like every other source)
                           -- | 'briefing-recommendation' (Phase 3 spec, 2026-09-19 — the briefing's
                           -- own structured new-position idea, shared write path with the
                           -- Recommended Trades spec's mechanical shadow portfolio)
    detail TEXT,           -- JSON: fair_value, discount_pct, stars, moat, direction, etc.
    flagged_date TEXT, source_ref TEXT,   -- link to the wiki page this came from
    -- Pending-idea lifecycle — applies only to source='briefing-recommendation'
    -- rows; every other source's rows just keep the 'new' default forever,
    -- meaningless but harmless (Pending Trade Ideas only ever queries on
    -- source AND status together, so old rows never leak in regardless of
    -- their status value). Added via live ALTER TABLE 2026-09-19.
    status TEXT DEFAULT 'new',   -- 'new' | 'approved' | 'rejected' | 'snoozed' | 'expired'
    status_updated_at TEXT
);

-- ── Strategies, generalized across instrument types ─────────────────────────

CREATE TABLE strategies (
    strategy_id TEXT PRIMARY KEY, name TEXT,
    instrument_type TEXT,   -- 'equity_long' | 'equity_short' | 'option'
    description TEXT, rules_ref TEXT,   -- section link into model-portfolio-management.md
    active INTEGER DEFAULT 1
);

-- ── Portfolios (real, paper, or shadow-tracking another investor) ──────────

CREATE TABLE portfolios (
    portfolio_id TEXT PRIMARY KEY, name TEXT,
    kind TEXT,              -- 'real' | 'paper' | 'shadow' | 'recommended' (Recommended Trades
                             -- spec, 2026-09-19 — mechanically mirrors the briefing's own
                             -- recommendations, no discretion, vs. 'shadow' mirroring a
                             -- disclosed investor's positions)
    mirrors_investor_id TEXT,   -- NULL unless kind='shadow'
    mirrors_portfolio_id TEXT,  -- NULL unless kind='recommended' — added via live ALTER TABLE 2026-09-19
    base_currency TEXT, created_date TEXT, active INTEGER DEFAULT 1
);

-- ── Trades — shorts, options, strategy/portfolio linkage, signal attribution ─

CREATE TABLE trades (
    trade_id TEXT PRIMARY KEY,
    portfolio_id TEXT,       -- FK → portfolios
    strategy_id TEXT,        -- FK → strategies
    ticker TEXT, exchange TEXT,
    instrument_type TEXT,    -- 'equity' | 'option'
    -- 'long' | 'short' — the directional bet, evaluated against entry_price/
    -- exit_price (the UNDERLYING's price for options, not the premium): a
    -- bought call or a bought/sold-to-open position that profits when price
    -- rises is 'long'; a bought put, or anything profiting when price falls,
    -- is 'short' — regardless of whether the option itself was bought or
    -- sold to open. (Corrected 2026-09-10 — an earlier comment here said
    -- "short = sold/written to open", which is wrong: a long put is bought
    -- to open but is a 'short' bet in this sense.)
    direction TEXT,
    entry_date TEXT, entry_price REAL, shares REAL,
    currency TEXT,            -- native trade currency of entry_price/exit_price (e.g. 'GBX','EUR','USD') — added 2026-09-10 for multi-currency-native portfolios (Trading Portfolio); NULL/assume USD for portfolios sized in USD-equivalent (paper-trading)
    -- Options-specific (NULL for equity trades). Manual entry only — no live
    -- options data feed for entry terms (strike/expiry/premium paid). Daily
    -- mark-to-market IS now computed (see option_marks below, added
    -- 2026-09-14) via Black-Scholes off the underlying's own tracked price —
    -- this comment previously said P&L wasn't modeled at all, which is now
    -- only true for the entry side, not the ongoing mark.
    option_type TEXT,        -- 'call' | 'put' | NULL
    strike REAL, expiry_date TEXT, premium REAL, contracts INTEGER,
    -- 'paid' (bought to open — a long premium position, profits when the
    -- option's value rises) | 'received' (sold/written to open — a short
    -- premium position, profits when it falls). Distinct from `direction`
    -- above, which is the bet on the UNDERLYING, not the premium cash flow —
    -- a bought put is direction='short' (bearish bet) AND premium_flow='paid'
    -- (long the put itself); a written covered call would be direction='long'
    -- but premium_flow='received'. Defaults to 'paid' since every option
    -- position recorded so far has been a bought put (Phase 7c backfill).
    premium_flow TEXT DEFAULT 'paid',
    stop_loss REAL, target1 REAL, target2 REAL,
    exit_date TEXT, exit_price REAL, status TEXT,   -- 'open' | 'closed'
    thesis TEXT,
    source_signal_id TEXT,   -- FK → signals: why this trade was made
    -- Cumulative $ already realized from T1/T2 partial exits on a still-
    -- OPEN position — schema has no per-tranche history (one entry + one
    -- exit per row, documented Phase 7 limitation), so `shares` gets
    -- manually reduced to the remaining size when a tranche sells and the
    -- proceeds have nowhere else to live. Added 2026-09-12 after finding
    -- fred-dashboard's Net P&L completely omitted this for 7 P1 positions
    -- ($5,872 banked, invisible everywhere) — NULL/0 for positions with no
    -- partial exits yet. daily-paper-trader's T1/T2 trigger steps
    -- (instructions.md Steps B/C) must increment this alongside the
    -- existing `shares` correction, or it drifts stale again.
    realized_pnl_partial REAL
);

-- Daily option mark-to-market, added 2026-09-14 (options_pricing.py). One row
-- per (trade_id, date) — mirrors `prices`' date-keyed shape rather than
-- overwriting a single "current" value, so a history of marks accumulates
-- the same way equity prices do. Computed via Black-Scholes using the
-- underlying's own tracked `prices.close` and a HISTORICAL volatility proxy
-- (annualized stdev of the underlying's trailing daily log returns) — there
-- is no live options-chain/IV data source in this pipeline, so this is a
-- realized-vol approximation of IV, not a true market-implied one. Written
-- by `refresh-technicals` right after prices update for the day, so the
-- underlying close it joins against is always same-day.
CREATE TABLE option_marks (
    trade_id TEXT,               -- FK → trades
    date TEXT,                   -- valuation date
    underlying_price REAL, underlying_price_date TEXT,   -- the prices.close/date actually used
    volatility REAL,             -- annualized, from trailing ~90 daily closes (see historical_volatility())
    time_to_expiry_years REAL, risk_free_rate REAL,   -- risk_free_rate is a documented constant (RISK_FREE_RATE in options_pricing.py), not fetched live
    premium_estimate REAL,       -- Black-Scholes fair value, per underlying share
    mkt_value REAL,              -- premium_estimate * shares, position's native currency (no FX conversion here)
    unrealized_pnl REAL,         -- (premium_estimate - trades.premium) * shares, signed by trades.premium_flow
    method TEXT,                 -- provenance tag, e.g. 'black-scholes-hv90'
    PRIMARY KEY (trade_id, date)
);

-- ── Cash ledger — portfolio-level cash/margin accounting ───────────────────
-- Append-only. Short-cash convention: opening a short writes NO ledger row
-- (margin never touches cash); closing a short writes one 'short_close_pnl'
-- row for the realized P&L only. Long buy/sell and option premium flows are
-- ordinary cash movements. deposit/withdrawal/fee/dividend cover the rest.
-- Running balance per portfolio = SUM(amount) via v_portfolio_cash_balance.

CREATE TABLE cash_ledger (
    entry_id TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL,   -- FK → portfolios
    entry_date TEXT NOT NULL,
    amount REAL NOT NULL,          -- positive = credit, negative = debit
    entry_type TEXT NOT NULL,      -- 'deposit' | 'withdrawal' | 'buy' | 'sell' |
                                    -- 'short_close_pnl' | 'option_premium' | 'dividend' | 'fee'
    trade_id TEXT,                 -- FK → trades, nullable (deposits/withdrawals have none)
    note TEXT
);

-- ── Tracked investors + their disclosed positions over time ────────────────

CREATE TABLE tracked_investors (
    investor_id TEXT PRIMARY KEY, name TEXT,
    source_type TEXT,        -- '13F' | 'substack' | 'letter' | 'manual'
    source_ref TEXT          -- wiki page(s) this is sourced from
);

CREATE TABLE investor_positions (
    investor_id TEXT, ticker TEXT, exchange TEXT,
    direction TEXT,           -- 'long' | 'short'
    disclosed_date TEXT, entry_price_hint REAL,
    status TEXT,              -- 'open' | 'trimmed' | 'closed'
    source_ref TEXT,          -- wiki page / trading-post citation
    PRIMARY KEY (investor_id, ticker, exchange, disclosed_date)
);

-- ── Task registry ────────────────────────────────────────────────────────────

CREATE TABLE task_registry (
    task_id TEXT PRIMARY KEY, kind TEXT,
    schedule_cron TEXT, description TEXT, entry_point TEXT,
    last_run_at TEXT, last_run_status TEXT
);

-- ── Row-level pipeline health (Master spec Phase 13) ──────────────────────────
-- task_registry answers "did the job run"; this answers "which specific
-- tickers failed" — today that requires reading logs by hand. Every
-- deterministic job upserts one row per ticker per run.

CREATE TABLE data_quality (
    ticker TEXT, exchange TEXT,
    data_type TEXT,             -- 'technicals' | 'fundamentals' | 'screen' | 'option_mark'
    last_attempt_at TEXT, last_success_at TEXT, last_source TEXT,
    status TEXT,                 -- 'ok' | 'degraded' (fell back) | 'error' (all sources failed)
    error_message TEXT, consecutive_failures INTEGER DEFAULT 0,
    PRIMARY KEY (ticker, exchange, data_type)
);

-- ── Views for the queryable layer ────────────────────────────────────────────

-- sector here is f.sector (fundamentals, FMP/scrape-sourced) — the SAME
-- field screen.py's _MF_EXCLUDE_SECTORS filter actually reads — not
-- u.sector (universe, Wikipedia-sourced). The two can genuinely disagree
-- per ticker (e.g. WISE: universe='Financial Services', fundamentals=
-- 'Technology'); showing universe.sector here made correctly-included rows
-- look like exclusion-filter leaks. Fixed 2026-09-10.
CREATE VIEW v_magic_formula_latest AS
SELECT sr.*, f.sector, u.index_membership, f.pe, f.div_yield
FROM screen_results sr
JOIN universe u ON u.ticker = sr.ticker AND u.exchange = sr.exchange
JOIN fundamentals f ON f.ticker = sr.ticker AND f.exchange = sr.exchange
WHERE sr.run_date = (SELECT MAX(run_date) FROM screen_results)
  AND f.as_of_date = (SELECT MAX(as_of_date) FROM fundamentals f2
                      WHERE f2.ticker = sr.ticker AND f2.exchange = sr.exchange);

CREATE VIEW v_strategy_performance AS
SELECT strategy_id, portfolio_id,
       COUNT(*) AS trades_total,
       SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) AS trades_closed,
       SUM(CASE WHEN status = 'closed' AND
                ((direction = 'long' AND exit_price > entry_price) OR
                 (direction = 'short' AND exit_price < entry_price))
           THEN 1 ELSE 0 END) AS wins,
       AVG(CASE WHEN status = 'closed' THEN
                (CASE WHEN direction = 'short' THEN -1 ELSE 1 END)
                * (exit_price - entry_price) / entry_price
           END) AS avg_return_pct,
       AVG(CASE WHEN status = 'closed' THEN julianday(exit_date) - julianday(entry_date) END) AS avg_holding_days
FROM trades
GROUP BY strategy_id, portfolio_id;

-- Fixed 2026-09-10 (Phase 8): the original version did shares*entry_price
-- with no currency awareness. Fine for single-currency portfolios (paper-
-- trading, the Burry shadow — both USD-only), but Trading Portfolio is
-- genuinely multi-currency (GBX/EUR/USD native prices, see the `currency`
-- column added this same day) — GBX (pence) entry_price is ~100x its GBP
-- value, so open_cost_basis came out at $194,404 for 18 positions sized
-- ~$1,000-1,500 each. This fix corrects the GBX order-of-magnitude only
-- (÷100 to GBP); it does NOT convert GBP/EUR/USD to one common currency —
-- those still get summed as if they were equal, which is still wrong, just
-- far less wrong than the 100x GBX error. A real fix needs a live FX join
-- per position, not attempted here — flagged as a follow-up.
CREATE VIEW v_portfolio_performance AS
SELECT portfolio_id,
       SUM(CASE WHEN status='open' THEN
             (CASE WHEN direction='short' THEN -1 ELSE 1 END) * shares * entry_price
             * (CASE WHEN currency = 'GBX' THEN 0.01 ELSE 1.0 END)
           ELSE 0 END) AS open_cost_basis,
       COUNT(CASE WHEN status='open' THEN 1 END) AS open_positions,
       COUNT(CASE WHEN status='closed' THEN 1 END) AS closed_positions
FROM trades GROUP BY portfolio_id;

CREATE VIEW v_portfolio_cash_balance AS
SELECT portfolio_id, SUM(amount) AS cash_balance
FROM cash_ledger
GROUP BY portfolio_id;

-- Recommended Trades spec (2026-09-19) §1a — daily USD-per-1-unit rate per
-- currency, needed to convert a trade's P&L to EUR at ITS OWN entry/exit
-- date, not just today's rate (Trading Portfolio positions span months,
-- and FX moves meaningfully over that window even if less than equities
-- typically do). Backfilled via fx_rates_backfill.py (yfinance historical
-- daily closes for GBPUSD=X/EURUSD=X/etc.); kept current going forward by
-- technicals.py's existing daily fetch_fx_rates() call, added there
-- alongside the historical backfill rather than as a separate daily job.
CREATE TABLE fx_rates (
    date TEXT NOT NULL, currency TEXT NOT NULL, usd_rate REAL NOT NULL,
    PRIMARY KEY (date, currency)
);

-- Phase 3 spec (2026-09-19) §3.2 — signal-strengthened candidate ranking:
-- a Magic Formula pass corroborated by one or more independent signals
-- (morningstar-undervalued, investor:burry, a wiki-mention, etc.) should be
-- presented and ranked as higher-conviction than a screen-only pass. Uses
-- correlated subqueries with COUNT(DISTINCT source)/GROUP_CONCAT(DISTINCT
-- source) rather than the spec's own literal GROUP BY mf.*/GROUP_CONCAT —
-- that form is ambiguous SQL (GROUP BY on ticker/exchange while selecting
-- mf.*'s other columns) and the spec's own "Open risks" flagged the exact
-- double-counting bug this form would have if a ticker got the same source
-- more than once (e.g. two separate Morningstar mentions) — DISTINCT here
-- avoids both problems from the start rather than fixing them later.
CREATE VIEW v_investment_opportunities AS
SELECT mf.*,
       (SELECT GROUP_CONCAT(DISTINCT s.source) FROM signals s
        WHERE s.ticker = mf.ticker AND s.exchange = mf.exchange) AS corroborating_signals,
       (SELECT COUNT(DISTINCT s.source) FROM signals s
        WHERE s.ticker = mf.ticker AND s.exchange = mf.exchange) AS signal_count
FROM v_magic_formula_latest mf;

-- Phase 2 (P2.0c): most recent signal per ticker, used by both the universe
-- and screener API routes so neither has to re-derive this with a correlated
-- subquery. signal_id is a random UUID (not sortable), so the tiebreaker for
-- two signals landing on the same flagged_date is SQLite's own `rowid`
-- (monotonically increasing with insert order) rather than signal_id DESC.
CREATE VIEW v_latest_signal AS
SELECT s.ticker, s.exchange, s.source, s.detail, s.flagged_date, s.source_ref
FROM signals s
WHERE s.rowid = (
    SELECT s2.rowid FROM signals s2
    WHERE s2.ticker = s.ticker AND s2.exchange = s.exchange
    ORDER BY s2.flagged_date DESC, s2.rowid DESC
    LIMIT 1
);
