# OBSIDIAN ECONOMICS — Phase 1: Audit of the existing project

Audited: `market_lens/` (Market Lens 9.2 plus the strategy-review fixes), git checkpoint
`checkpoint/pre-obsidian`. 38 Python modules, about 11,800 lines, 216 passing tests.

Status words used below: **working** (implemented, wired to a command or API, and tested),
**partial** (implemented but incomplete or not wired), **library only** (functions exist with no
caller), **missing**.

---

## 1. Existing architecture

| Layer | Modules | Notes |
|---|---|---|
| Language / runtime | Python 3.11+, **standard library only** | A deliberate design goal: no packages, no API keys, runs on a personal computer. |
| Source registry and parsers | `sources.py`, `finance_feed.py`, `market_feed.py` | 26 feeds, 154 series, 17 areas, strict per-publisher parser contracts. |
| Network | `net.py` | Bounded, TLS-verified GET/POST requests. No user-supplied URLs, no ambient auth. |
| Point-in-time journal | `store.py` (SQLite, WAL) | Tables `poll`, `got`, `object` (raw bytes, zlib), `snap`, `ready`, `event`. Keeps receipts, vintages, revisions and withdrawals. **This is the strongest part of the project.** |
| Scheduler / worker | `engine.py`, `market_lens.py run`, `autostart.py` | Per-publisher pacing. User-level login service (systemd / launchd / Task Scheduler). |
| Quality gates | `reliability.py` | Source quality, staleness and clock gates; fallbacks between series. |
| Research store | `research_store.py`, `research.py`, `market_data.py`, `sec_edgar.py`, `quote_surface.py`, `analyst_consensus.py`, `short_interest.py`, `delisted.py`, `delist_events.py` | Separate SQLite store, point in time. OHLCV, corporate actions, EDGAR fundamentals, delistings (survivorship), short interest, quotes and options. |
| Signals and regime | `signal_filter.py`, `alpha_engine.py`, `macro_alerts.py`, `asset_classes.py` | Rule-based regime, growth/inflation z-scores and quadrant, diffusion. |
| Portfolio | `strategy_engine.py`, `efficient_frontier.py`, `portfolio_optimizer.py`, `trade_signals.py`, `daily_processor.py` | Correlation-aware vol targeting; exact active-set QP frontier; account ladder; Monte Carlo choice. |
| Paper trading | `paper_trader.py`, `paper_account.py`, `yahoo_prices.py` | Weight-based simulation stored in a JSON file. |
| Supervisor | `jarvis_supervisor.py` | Rule-based "is a change needed" triggers and a briefing. |
| Validation | `backtest.py`, `stock_selector.walk_forward_backtest` | Point-in-time replay (new, minimal). |
| UI / API | `dashboard.py`, `monitor.html`, `plan_api.py` | Read-only `http.server` on 127.0.0.1:8790; one 48-line HTML page; GET endpoints only. |
| Tests | `test_auto.py` | 216 offline tests with replay fixtures, all passing. |

Data flow: publishers → `net` → `store` journal (accepted snapshots only) → `engine` status and
export → `strategy_engine.decide(at)` → plan, paper and supervisor → read-only monitor.

## 2. Feature inventory

| Capability (prompt part) | Status | Where |
|---|---|---|
| Official macro collection: CPI, GDP, unemployment, rates, FX, curve (3) | **working**, limited coverage (75 of 2,560 area × factor slots) | `sources.py` |
| Point-in-time vintages, revisions, receipts (3) | **working**, strong | `store.py` |
| Market prices / OHLCV, corporate actions (3) | **working** (research store) | `market_data.py`, `yahoo_prices.py` |
| SEC fundamentals, delisted universe (3, 15) | **working** | `sec_edgar.py`, `delisted.py` |
| Central bank decisions, minutes, statements, balance sheets (3, 4) | **missing**. Only SOFR, BoC Valet rates and ECB FX are collected. | — |
| Fiscal, liquidity, credit spreads, commodities, geopolitics (3) | **missing** | — |
| Macro research modules with forecasts and citations (4) | **missing**. Only z-scores and a quadrant. | — |
| Regime detection (5) | **partial**: deterministic rules (curve level and history, SOFR); not probabilistic; US only | `alpha_engine.detect_regime` |
| Bayesian / Markov / Kalman helpers (5, 6) | **library only** (`bayesian_regime_update`, `markov_regime_chain`, `kalman_filter_series`, `hurst_exponent`, `granger_causality_test`) | various |
| Econometric / ML forecasting: VAR, ARIMA, GARCH, ML (6) | **missing** | — |
| Strategy library with rationale and validation (7) | **missing**: one macro allocation rule, plus about 25 unwired helpers in `stock_selector.py` | — |
| Paper brokerage: cash, units, orders, fills, ledger, deposits (8) | **partial**: weights and NAV in a JSON file; no orders, fills, units, deposits/withdrawals or trade ledger | `paper_trader.py` |
| Investment groups / mandates (9) | **partial**: the 8-sleeve "operations ladder" with floors and caps; not a mandate model; no experimental cap | `portfolio_optimizer.py` |
| Optimisation: MV, min-var, frontier (10) | **working** (long-only, capped, exact) | `efficient_frontier.py` |
| Risk parity / HRP / Black-Litterman / CVaR (10) | **missing** (simple risk parity is library only) | — |
| Correlation / hidden concentration (11) | **partial**: class correlation priors, `prune_correlated`; no PCA, clustering or stress correlations | `asset_classes.py`, `signal_filter.py` |
| Independent risk engine, limits, kill switch (12) | **missing** (parametric VaR is library only; the drawdown guard sits inside the strategy) | — |
| Autonomous research, model registry, retraining (13) | **missing** | — |
| Stress testing (14) | **partial**: one parametric shock in `black_swan_heat_diffusion` (library only) | — |
| Backtesting (15) | **partial**: point-in-time replay with turnover costs; no event-driven fills, spreads, financing or regime breakdown | `backtest.py` |
| Autonomous portfolio manager loop (16) | **partial**: `decide` → `paper` command; no scheduler loop, decision log or risk gate | — |
| Website (17) | **partial**: read-only monitor and plan tab; no auth, no positions/trades/settings pages; not branded | `dashboard.py`, `monitor.html` |
| Performance accounting, TWR/MWR (18) | **missing** | — |
| Auth, audit trail, live-trading governance (19) | **missing**. No live trading exists, which is correct. | — |

## 3. Missing components (what Obsidian needs that does not exist)

1. A **brokerage ledger**: orders, fills, positions in units, cash, fees, financing, deposits,
   withdrawals and corporate actions, in a transactional database, with a $5,000 default and an
   adjustable capital cap.
2. An **execution simulator** that fills only against real recorded prices, with spread,
   slippage and liquidity limits, and refuses rather than inventing a price.
3. An **independent risk engine**: pre-trade checks, limits, VaR/ES, kill switch. It must be a
   separate module the strategy and AI layers can call but cannot configure.
4. **Investment-group mandates** with dynamic allocation and a hard cap on the experimental
   group.
5. **Performance accounting**: time-weighted and money-weighted returns, P&L attribution, and
   metric windows that are suppressed when there is too little data.
6. A **probabilistic regime model** (HMM / Markov switching) per economy, with confidence.
7. A **forecasting and model library** with baselines, walk-forward evaluation, a model registry
   and versioned datasets.
8. A **strategy framework**: a common interface (rationale, data, signal, entry/exit, sizing,
   costs), with validation gates before any allocation.
9. A **correlation and concentration engine**: PCA, clustering, HRP, stress-period correlation.
10. **Scenario and stress testing** applied per group and for the whole book.
11. **Data coverage**: central banks (FRED/Fed H.4.1, ECB SDW, BoE), credit spreads,
    commodities, fiscal (IMF/World Bank/Treasury), each behind an adapter with licensing notes.
12. An **authenticated web application** with write actions (settings, deposits, kill switch),
    audit log and Obsidian branding.

## 4. Technical debt

- **Dense one-line code** in core files (`store.py`, `engine.py`, `sources.py`,
  `market_lens.py`, `dashboard.py`). This is hard to review safely; reformat when touched.
- **Two parallel strategy pipelines**: CSV-driven (`run_alpha_step` → `paper`) and
  journal-driven (`decide` → `strategy`/`jarvis`). They can disagree. Consolidate on `decide`.
- **About 25 unwired helpers** in `stock_selector.py` and `event_engine.py`, several named
  after famous investors. Treat them as unvalidated until a strategy that uses them passes
  validation.
- **The `_num` helper is duplicated in 11 modules.** No package structure (flat modules), no
  type hints.
- **One 2,500-line test file.** Split it per domain as new work lands.
- **Hand-maintained `file_hashes.json` and `autostart.CORE` list.** A guard test now covers the
  import list.
- **Python 3.12+ `sqlite3` datetime-adapter deprecation warnings** (17 per run).
- **Hard-coded return priors** (`portfolio_optimizer.DEFAULT_ASSETS`). They now shift with
  SOFR but are still assumptions.

## 5. Security assessment

**Good today:**
- The server binds to loopback by default and opens the journal `query_only`.
- Responses carry strict headers (CSP, nosniff, no-referrer).
- Query sizes are bounded and there are no user-supplied paths or URLs.
- Outbound requests are TLS-verified and size-capped.
- No secrets are stored or required.
- The login service runs with least privilege.
- Paper state is written atomically.

**Risks and required changes:**

| # | Finding | Severity | Action |
|---|---|---|---|
| S1 | `run --bind` accepts any address, with **no authentication**. Binding to 0.0.0.0 would publish the whole system on the LAN. | High once write actions exist | Refuse non-loopback binds unless auth is configured; add authentication before any write endpoint. |
| S2 | The web app is read-only today. Settings, deposits and the kill switch will need authenticated, CSRF-protected POSTs with an audit log. | High (new) | Session auth, CSRF token, server-side validation, append-only audit table. |
| S3 | CSP allows `'unsafe-inline'` scripts. | Low on loopback | Move scripts into static files when the frontend is rebuilt. |
| S4 | **Data licensing.** `query1.finance.yahoo.com` and `api.nasdaq.com` are unofficial endpoints whose terms restrict automated and commercial use. FINRA requests carry a placeholder contact (`research@example.invalid`). | Medium (legal / reliability) | Keep them for personal research only; put prices behind an adapter so a licensed feed can replace them; make contact details configurable. |
| S5 | The paper account is a JSON file with no integrity check or history table. | Medium | Move to a transactional ledger with append-only fills. |
| S6 | No live trading exists; `stage_broker_order` is a stub. | — (good) | Keep live trading disabled by default; require a separate config file plus human confirmation. The AI layer gets no code path to it. |
| S7 | No rate limiting. | Low on loopback | Add it with authentication. |

## 6. Development roadmap

Honest sizing: the full specification is many weeks of focused engineering. Each phase below
ends with tests, a status report and a commit. Placeholders are never presented as features.

| Phase | Deliverable | Depends on |
|---|---|---|
| 2 Data | Adapter interface plus metadata contract (source, obs/pub/retrieval/revision time, vintage, unit, quality). New adapters: FRED/ALFRED (US, with vintages), ECB data portal, BoE, Fed H.4.1, IMF. Licensing register. | Phase 1 |
| 3 Macro intelligence | 15 module scaffolds, each producing conditions / history / trend / risks / asset impacts with citations; a central-bank tracker; a probabilistic regime model (Gaussian HMM in numpy) per economy. | 2 |
| 4 Research | Model library (ARIMA, VAR, GARCH, random-walk and AR baselines, gradient boosting), a walk-forward evaluator, and a forecast store with intervals and accuracy. Strategy interface plus 4–6 well-founded strategies (trend, carry, curve, duration, defensive). | 2, 3 |
| 5 Portfolio and risk | Correlation engine (rolling, Spearman, PCA, clustering, HRP, stress windows); optimisers (min-var, risk parity, HRP, Black-Litterman, CVaR); **independent risk engine** with pre-trade checks and a kill switch. | 4 |
| 6 Brokerage | Ledger, execution simulator, $5,000 default and adjustable cap, deposits/withdrawals, TWR/MWR, restart safety. | 5 (risk gate) |
| 7 Autonomous AI | Model registry and versioning, scheduled and event retraining, drift detection, validation gates with multiple-testing control, allocation within limits, decision log. | 4–6 |
| 8 Website | Authenticated app: Overview, Portfolio, Groups, Operations, AI Centre, Macro, Risk, Settings, all on real data. | 6 |
| 9 Validation | End-to-end, stress, backtest and accounting tests. | all |
| 10 Docs / deploy | Install, start, operate, limitations. | all |

## 7. Recommended implementation architecture

Two viable paths. The choice changes installation, operation and everything built afterwards.

**A. Evolve the existing engine (recommended for a personal $5,000 paper account).**
- Keep Python and the SQLite point-in-time journal as the system of record.
- Add an `obsidian/` package: ledger, risk engine, groups, analytics, models.
- Analytics depend on numpy and pandas (+ scipy/statsmodels/scikit-learn as an optional
  extra). The collector stays dependency-free.
- The API runs in the existing server process, extended with authenticated endpoints.
- The frontend is React + TypeScript built with Vite into static files that Python serves, so
  Node is needed only for development.
- **Pros:** one process, restart-safe, works offline on a laptop, reuses the 216 tests and the
  journal.
- **Cons:** not multi-user; SQLite suits one writer.

**B. Full service stack as suggested in the prompt.**
- FastAPI, PostgreSQL, Next.js, Celery + Redis, Docker Compose.
- Analytics tier with numpy / pandas / scipy / statsmodels / scikit-learn / cvxpy / PyTorch.
- **Pros:** standard production architecture; multi-user; scales.
- **Cons:** five services to run and secure; the journal must be migrated or bridged; heavy for a
  single personal account; much longer before anything works end to end.

Under both options:
- Risk limits live in a config table that only the authenticated human (Settings) can change;
  the AI orchestrator uses a read-only interface to it.
- Live trading has no implementation until explicitly requested.
- Every automated action writes a decision record (time, model version, inputs, signal, risk
  result, outcome).
