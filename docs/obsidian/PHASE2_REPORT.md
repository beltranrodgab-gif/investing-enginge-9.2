# OBSIDIAN ECONOMICS — Phase 2 report: data infrastructure

Architecture decision (owner, Phase 1): **Option A — evolve the existing engine.** Python and
SQLite; the collector stays dependency-free; the new layers live in `market_lens/obsidian/`.

## Completed

| Item | Where | Verified by |
|---|---|---|
| Observation contract with source, period, published/retrieved/known times, vintage, unit, currency, frequency and quality | `obsidian/data/contract.py` | `Contract` tests |
| Licensing register; every series must reference it | `obsidian/data/licenses.py` | `test_every_series_has_a_registered_license` |
| Append-only point-in-time store with change-only storage, revisions (`prev_value`), issue log, run log, cursor | `obsidian/data/store.py` | `Validation`, `Fred` tests |
| Validation: rejects future knowledge, pre-retrieval claims, bad periods and non-finite values; flags jumps and price-scale errors; staleness | `obsidian/data/validate.py` | `Validation` tests |
| Adapter interface: host allow-list, keys read only from the environment, redacted errors | `obsidian/data/adapters/base.py` | `test_adapter_hosts_are_enforced`, `test_missing_key_is_explained_and_key_never_leaks` |
| FRED/ALFRED adapter (35 series) with genuine historical vintages | `adapters/fred.py` | `Fred` tests (offline payloads) |
| ECB Data Portal adapter (8 series) | `adapters/ecb.py` | `Ecb` tests |
| Yahoo adapter: raw close vs adjusted close vs volume; in-session bars provisional | `adapters/yahoo.py` | `Yahoo` tests |
| Market Lens bridge: journal snapshots become vintages; incremental cursor | `adapters/marketlens.py` | `Bridge` test on a journal built by the collector's own replay, including a revision |
| Automatic sync in the background worker (opt-in after `data init`; failures never stop collection) | `engine.sync_obsidian` | Exercised via `service.sync_journal` |
| CLI `data init/sync/fetch/asof/quality/series/sources/runs` | `market_lens.py` | Manual end-to-end run (below) |
| Read-only website API `/api/data/*` (SQLite `mode=ro`) | `dashboard.py`, `service.web_view` | Live server check plus `WebAndReadOnly` tests |
| Auto-start runtime ships the package | `autostart.CORE` | `test_installed_runtime_contains_the_obsidian_package` |

## Test results

- `python3 -m pytest -q test_auto.py obsidian/tests` → **241 passed**.
  - 25 new tests cover the data layer.
  - One existing test was extended so the installed runtime must include the package.
- Manual end to end, on a journal built from the offline fixtures:
  - `data init` registered 248 series.
  - `data sync` imported 780 observations with 0 rejected; a second sync was incremental.
  - `data asof` and `data quality` returned correct values.
  - `data fetch fred` without a key failed with a clear message.

## Not verified in this environment

- **Live network fetches.** The sandbox's network policy blocks FRED, ECB and Yahoo (proxy
  403). Their parsers are tested against payloads in the documented formats, but the first live
  run on your computer is the real check:
  ```sh
  python3 market_lens.py data fetch ecb
  ```
  ```sh
  python3 market_lens.py data fetch yahoo --symbol SPY
  ```
  Then run with `FRED_API_KEY` set:
  ```sh
  python3 market_lens.py data fetch fred
  ```
  Each failure is recorded in `data runs` with its reason.

## Incomplete (honest list)

- The source gaps listed in `DATA_SOURCES.md` ("Not covered yet"): other central banks, text
  (statements and minutes), PMI (proprietary), swaps/options/futures, more commodities,
  non-US fiscal, geopolitics.
- **No scheduled polling for FRED, ECB or Yahoo yet.** `data fetch` is run on demand. Adding
  them to the worker schedule with per-host pacing is the first task of Phase 3.
- **Non-USD instruments are refused** until the brokerage handles FX (Phase 6).
- **Research store not bridged.** Delisted-issuer history and EDGAR fundamentals in the
  research store are not yet bridged into this store.
- **Strategy layer not switched over.** `strategy_engine.decide` still reads the Market Lens
  journal directly. Moving it to the unified point-in-time store comes with the regime work in
  Phase 3.

## Next: Phase 3 (macroeconomic intelligence)

1. Schedule the network adapters in the worker.
2. Build the macro research modules (growth, inflation, labour, central banks, rates, liquidity,
   credit, FX, commodities, fiscal) on the unified store, with citations from the stored source
   receipts.
3. Build the probabilistic regime model (Gaussian HMM / Markov switching in numpy) per economy,
   with confidence levels and driving indicators.
