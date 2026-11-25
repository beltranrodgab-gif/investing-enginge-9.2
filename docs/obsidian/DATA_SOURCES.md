# OBSIDIAN ECONOMICS — Data sources and point-in-time rules

## How data flows

```
official publishers ──► Market Lens collector ──► journal (macro-live.sqlite3, untouched system of record)
                                                     │  bridge (read-only, incremental, automatic in the worker)
FRED/ALFRED ─┐                                       ▼
ECB portal  ─┼──► adapters ──► validation ──► OBSIDIAN store (obsidian.sqlite3, append-only, point in time)
Yahoo       ─┘                                       │
                                                     ▼
                         CLI `data ...`, website /api/data/*, and (next phases) research, risk, brokerage
```

## Sources

| Source | Adapter | Key | Coverage | Point-in-time basis | License (summary) |
|---|---|---|---|---|---|
| Market Lens journal (ONS, StatCan, Eurostat, BIS, OECD, BLS, ECB, BoC, NY Fed, US Treasury) | `marketlens` bridge | none | 154 series, 17 areas | `known_at` = time the collector accepted the snapshot; each snapshot is a vintage | Official statistics; reuse with attribution; OECD/BIS terms vary by dataset |
| FRED / ALFRED | `fred` | `FRED_API_KEY` (free) | 35 US series: growth, inflation, labour, housing, consumer, money, Fed balance sheet, reserves, reverse repo, financial conditions, credit spreads, dollar, oil, copper, fiscal | Monthly/quarterly/annual series: **full ALFRED vintages**, known at the end of each release day. Daily/weekly: current vintage, known from first retrieval | Personal/research use; some series third-party copyrighted (ICE BofA) |
| ECB Data Portal | `ecb` | none | Deposit and MRO rates, Eurosystem balance sheet, euro 2y/10y yields, HICP and core HICP, EUR/USD | Current vintage; known from first retrieval | Reuse permitted with attribution |
| Yahoo Finance | `yahoo` | none | 17 default USD ETFs (equity, bonds, credit, gold, silver, commodities, oil, dollar); any USD ticker via `--symbol` | Raw close: known at the end of its trading day. Adjusted close: known from retrieval only. In-session bars: `provisional` | **Unofficial. Personal, non-commercial use only.** |

The full register, with links to each source's terms, is in `market_lens/obsidian/data/licenses.py`
(also `market_lens.py data sources` and `GET /api/data/sources`). It summarises the terms and is
not legal advice. Read the linked terms before any use beyond personal research.

## Observation contract

Every stored value carries:
- **What:** series, period (ISO end date of the reference period), value, unit, currency and frequency.
- **When:** `known_at`, `published_at`, `retrieved_at` and `vintage`.
- **Status:** `quality` (`ok`, `provisional`, `estimated`, `revised`, `stale`, `suspect`, `missing`), plus `prev_value` when a value replaced an earlier one.

Rules enforced by the store and validation:
- **No look-ahead.** `as_of(series, T)` returns, for each period, the latest row with
  `known_at ≤ T`.
  - Nothing may be stored as known in the future.
  - A source without historical vintages cannot claim to have been known before this system
    fetched it.
- **Nothing fabricated.** Publisher placeholders become `value = None` with `quality = missing`.
  Gaps are never filled.
- **Append-only.** Rows are never updated or deleted (database triggers). A revision is a new
  row with a later `known_at`.
- **Suspicious is flagged, not fixed.** Extreme jumps (more than 10 robust SDs, or a price
  tripling or falling to a third in one step) are stored with `quality = suspect` and an issue
  record. Step series such as policy rates are not flagged on rate decisions.
- **Rejections are recorded.** Every rejected row goes to the `issue` table with its reason.
  Every ingestion run records its outcome, including failures.

## Survivorship and adjustments

- Prices are stored as **raw closes** (the execution reference) and **adjusted closes** (total
  return) in separate series. Adjusted history is rewritten at each distribution, so it is only
  point in time from retrieval. Never execute against it.
- Delisted instruments: the research store (`research.py`) already keeps SEC delisting history.
  It is not yet bridged into this store (Phase 4 work).

## Getting a FRED key

1. Register free at https://fred.stlouisfed.org and request an API key under My Account → API Keys.
2. Set it in your environment before running, for example `export FRED_API_KEY=...`, or in the
   service environment.
3. The key is read from the environment only. It is never written to disk or logs, and error
   messages strip query strings.

## Commands

```sh
python3 market_lens.py data init                 # create the store, register all series
python3 market_lens.py data sync                 # import the Market Lens journal (also automatic in the worker)
python3 market_lens.py data fetch ecb            # euro-area policy, balance sheet, yields, inflation, FX
python3 market_lens.py data fetch fred           # needs FRED_API_KEY
python3 market_lens.py data fetch yahoo --symbol SPY --symbol TLT
python3 market_lens.py data asof fred:CPIAUCSL --at 2024-02-20T00:00:00Z
python3 market_lens.py data quality              # freshness, last value, issue counts
python3 market_lens.py data runs                 # ingestion history, including failures
```

Website (read-only, loopback): `/api/data/sources`, `/api/data/series?source=&category=`,
`/api/data/quality?at=`, `/api/data/asof?id=&at=&start=&end=`, `/api/data/runs`.

## Not covered yet (adapters needed)

| Prompt area | Status | Candidate source (check licence) |
|---|---|---|
| Other central banks: BoE, BoJ, SNB, PBoC, RBA, RBNZ (rates, balance sheets) | missing | BoE IADB; BIS central-bank policy rates (already partly via Market Lens BIS feeds) |
| Central-bank statements, minutes, speeches (text) | missing | Official websites, RSS. Needs a text store and NLP in Phase 3. |
| PMI / ISM | missing | **Proprietary (S&P Global, ISM).** No free licensed source; substitutes are regional Fed surveys via FRED. |
| Swaps, forwards, term premium, FX positioning, options, futures | missing | ACM term premium (NY Fed, free); CFTC COT (free); swaps/options need a licensed vendor. |
| Commodities beyond oil and copper (gas, aluminium, iron ore, agriculture), inventories | partial | EIA (free API key), IMF primary commodity prices, World Bank Pink Sheet. |
| Fiscal beyond US (deficits, issuance) | missing | IMF WEO / IFS, Eurostat government finance. |
| Geopolitics, tariffs, sanctions | missing | GDELT (free), official trade notices. Text analytics in Phase 3. |
| Non-USD instruments | refused by design | Needs FX conversion in the brokerage (Phase 6). |
