# Operator notes

## Commands

From the extracted folder. On Windows, use `py -3` in place of `python3`.

```sh
python3 market_lens.py enable                     # account service: install + start now
python3 market_lens.py status                     # heartbeat, successes/failures, next checks
python3 market_lens.py search --geo CA diesel
python3 market_lens.py search --geo ES --factor ACT-01
python3 market_lens.py lookup SC.1324952502        # latest diesel observation and receipt
python3 market_lens.py lookup SC.1324952502 --period 2026-09 --as-of 2026-11-18T23:59:59Z
python3 market_lens.py changes                    # latest 100 post-baseline cell events
python3 market_lens.py export latest-copy.csv      # refuses to overwrite an existing file
python3 market_lens.py backup safe-copy.sqlite3    # consistent online backup, includes committed WAL
python3 market_lens.py disable                    # unregister the account service; keep data
```

`lookup --as-of` fences receipts by local start/index and outcome time, not by reference period or a publisher's edition date. Before the first local acceptance, no value is returned. `--period 2026-09` alone is **not** a vintage query. For a period no longer in the publisher's rolling window, `record` can be an archived observation with its own `indexed` and `h`; `in_snapshot=false` and `current_value` is withheld. The enabled-feed policy applied at startup is also journaled: an explicit historical as-of lookup does not use a future enable/disable decision.

`python3 market_lens.py run` is a foreground alternative to installation. Closing that terminal stops the worker. `--no-dashboard` runs it headless. `run --port 8791` uses a different port in foreground; the default is 8790. The default binds **only to loopback**; this app deliberately has no internet-facing authentication layer. Do not expose a public port on a personal computer. The same origin offers read-only `GET /api/status`, `/api/series?q=diesel&geo=CA`, `/api/series?geo=ES&factor=ACT-01`, `/api/detail?id=SC.1324952502`, `/api/changes` and `/latest.csv`. Viewing these never triggers a publisher request. `once [--feed ecb]` is optional maintenance for an otherwise stopped worker and checks only due feeds. Only one process may write a given data folder.

### Strategy decision (optional)

The `strategy` command turns the collected journal into one explainable portfolio decision. It never contacts a broker and places no orders.

```sh
python3 market_lens.py strategy --vol SPY=0.16 --vol TLT=0.12 --vol GLD=0.14
```

- `--vol ASSET=VOL` (repeatable, required) is the annualised volatility you assign to each asset. The tool does not guess these.
- `--target-vol` (default `0.10`) is the portfolio volatility the sizing aims for; a tighter target produces smaller positions.
- `--max-turnover` (default `0.50`) is a *budget* per step, not a kill switch. A larger required switch is booked in instalments and the rest stays in cash.
- `--history history.json` is an optional JSON list of previous regime names, used to require `confirm_days` agreement before a regime flip.

What it returns: the regime and its risk scale, the macro quadrant with growth/inflation z-scores and which series produced them, macro diffusion, the target and actually booked weights, cash, turnover, an estimated portfolio volatility, the all-weather weights for the quadrant, explicit per-asset orders, any budget warnings, and the list of series that were used or rejected with reasons.

It fails closed. With no scoreable macro input it targets cash rather than sizing a book on no evidence; a missing or implausible SOFR or curve leaves the regime `uncertain` at half risk. The volatility figure is a zero-correlation estimate and therefore understates a correlated book, and observational macro data is not an executable price. Treat the output as decision support, not a forecast or an execution feed.

Add `--jarvis` to embed the supervisor briefing and change triggers, `--plan --account 50000` to add the account spread, and `--state state.json` to remember the previous decision so the next run can tell you whether a change is needed.

### Account spread and efficient frontier (optional)

The `jarvis` command spreads **one account** across a ladder of operations that differ in risk and in how long the money can stay at work: overnight cash, one-month liquidity, one-year income, long duration, core equity, a growth sleeve, real assets and a small tactical arm.

```sh
python3 market_lens.py jarvis --account 50000 --goal 1.3 --years 3 --max-dd 0.20 --vol SPY=0.16
```

How it decides:

- Each operation is sized on the efficient frontier for **its own risk budget** (`efficient_frontier.portfolio_at_vol`). A sleeve that cannot reach its budget with asset weights alone holds part of itself in cash, and that cash is reported.
- The account frontier is then computed **across the operations**, so every point is a genuine spread over several time horizons rather than one bet. Per-operation floors are structural: a plan cannot collapse into a single sleeve.
- Fast Monte Carlo scores each frontier point on the chance of reaching the goal inside the drawdown limit. For terminal wealth it uses the exact log-normal shortcut (one draw per path, closed form); drawdown questions use the pathwise mode.
- `--account` is the only place money enters; everything upstream is weights. `invested + cash` always equals the account value.

Options: `--goal` (growth multiple, default `1.25`), `--years` (default `3`), `--max-dd` (drawdown limit, default `0.25`), `--regime-scale` (shrinks every risk budget together, `0..1`), `--vol ASSET=VOL` (repeatable, optional), and `--state` to persist the previous decision.

**The expected returns are long-run priors, not forecasts.** The frontier shows what follows *if* those assumptions hold. The volatility and correlation structure are also assumptions. Treat the split as disciplined diversification across horizons, not a prediction. The command contacts no broker and places no orders.

The same data is available read-only in the monitor: `GET /api/plan?account=50000` and `GET /api/jarvis`, and the **Account plan** tab. Invalid parameters return 400 rather than a silently wrong answer.

### Paper trading (simulated account)

`python3 market_lens.py paper` marks a **simulated** account to market and rebalances it. It contacts no broker and places no orders.

```sh
python3 market_lens.py paper --nav 100000 --vol SPY=0.16 --vol TLT=0.13 \
  --price SPY=100,101.5,102.3 --price TLT=90,90.6,91.1
```

What it does each run:

- **Marks the book to market first**, using the last two prices per asset. The NAV moves with prices, not only with costs, and cash does not earn the equity return.
- **Sizes the target** from the screened export and the volatilities you supply, then **executes it inside the turnover budget**. A large switch is spread across runs and the remainder stays in cash; `--max-turnover` is a budget, not a target.
- **Charges `--cost-bps` on turnover** (default 10 bps) and reports the cost separately from the market move.
- **Reports unpriced holdings by name** in `unpriced`. They are carried flat and that is stated, because silently marking an unpriced asset as unchanged flatters the result.
- **Labels a fail-closed exit.** With no usable macro input the strategy leaves risk entirely; those exits are marked `emergency: true` and fully costed, rather than being hidden inside the budget.
- **Persists the account** to `<state folder>/paper_account.json` (override with `--account`). The file is written atomically and validated on read, so a corrupt file starts a fresh account instead of trading on bad state. Use `--reset` to start over, and `--history` to include the per-period NAV.

Prices are not collected by the worker; you supply them with `--price`, `--price-file` (a CSV with `asset,price` columns) or `--symbols` (below). `risk_scale_suggestion` reports the risk scale the paper results would justify.

#### Automatic prices from Yahoo Finance

```sh
python3 market_lens.py paper --symbols SPY --symbols TLT --symbols GLD --auto-vol --nav 100000
```

`--symbols` fetches daily history from Yahoo Finance and `--auto-vol` estimates each asset's volatility from that history instead of asking you to type it. `--range` sets the window (default `2y`) and `--max-price-age` (hours, default 12) controls how long a cached series is reused.

Read this before relying on it:

- **It is an unofficial API.** The endpoint is undocumented and can change or rate-limit without notice. It is deliberately kept outside the collected journal: nothing fetched here is written to the SQLite record, no official feed depends on it, and every result is labelled `price_source: Yahoo Finance (unofficial)`.
- **Adjusted close is used, not raw close.** Raw closes here are split-adjusted but not dividend-adjusted, so a dividend would otherwise appear as a price fall. `adjclose` captures both.
- **Failures are loud.** A rejected symbol is named in `prices.failed` with its reason; if no symbol yields usable data the command stops rather than trading on stale marks. Unpriced holdings are carried flat and reported.
- **The cache is reused, and staleness is stated.** `--offline` runs from cache only and never contacts the network. A cached series used past its window is reported as `STALE`, never passed off as current.
- **Tickers are not validated against your needs.** Any Yahoo symbol is accepted, including ones that are not in the plan's asset list; only symbols the planner knows about influence the book.

This is a convenience for **paper trading only**. The collected journal continues to come from official publishers, and nothing here changes that.

### Research data collection (point-in-time)

`python3 market_lens.py research` collects market and filing data for research and backtesting. It writes to a **separate** database, `<state folder>/research.db`, and never to the official collection journal. That separation is deliberate: the journal is replayed cell-by-cell by `audit`, and mixing in a large, unofficial, continuously-revised market dataset would destroy that guarantee.

```sh
# prices, filings and current short interest
python3 market_lens.py research --init --symbols AAPL --symbols SPY --sec --short-interest

# everything: add quotes, the options surface, consensus and the archived filer index
python3 market_lens.py research --symbols AAPL --symbols SPY --sec --short-interest \
    --quotes --options --consensus --delisted --delisted-quarters 4

python3 market_lens.py research --coverage          # what is actually stored, and what is not
```

What it collects:

- **Prices, unadjusted and adjusted.** Unadjusted OHLCV is stored immutably, because a close is a fact about a day. Adjusted prices are *derived*, never stored as fact: `close x factor`, where the cumulative split/dividend factor is versioned by the date it became known. Asking for adjusted history as of an earlier date therefore returns what was known then, not a retroactively rewritten series.
- **Corporate actions.** Dividends and splits with ex-dates. Spin-offs, mergers and ticker changes are **not** published by this source and are not inferred.
- **Point-in-time fundamentals.** SEC XBRL company facts, each carrying its filing date. A restatement adds a row beside the original, so an as-of query returns the vintage the market actually had. A concept-family resolver handles retired tags, because a company can stop using a tag and a naive lookup then returns a years-old figure that still looks authoritative.
- **Filing index.** 10-K, 10-Q, 8-K, Form 4 and 13F filings with EDGAR acceptance timestamps.
- **Short interest.** Twice-monthly shares-short, average daily volume and days-to-cover, with the venue's settlement date. Freshness is checked against the publication cycle: a settlement that is current is labelled current, and a stale one is labelled with a reason. The FINRA archive is used only as a fallback, because its endpoint ignores its own sort and filter parameters and serves its oldest page (2020 data).
- **Bid/ask quotes.** Real bid and ask with sizes, from one exchange, plus the computed spread in basis points. This is **not the consolidated NBBO**: it is a single venue's book, so a measured spread is an upper bound on the true best-quote spread. Every stored quote carries that scope, so no downstream reader can mistake it for the national best bid and offer. A quote whose venue timestamp is missing or older than the staleness limit is stored but reported as not current, and is never used as a live mark. If the venue revises a tick, the original is kept and the difference is reported rather than overwritten.
- **Options surface.** The full chain per expiry with call/put bid, ask, last, volume and open interest, paginated to completion (a single page stops short of the money on a wide chain). Implied volatility is **computed here**, by inverting Black-Scholes on the mid price, and only the **out-of-the-money** leg is used: an in-the-money price is nearly all intrinsic and does not identify a volatility. A one-sided or crossed quote is reported as unpriced, never filled from a neighbouring strike. Skew is call IV minus put IV at matched strikes, which is a simpler quantity than the usual 25-delta skew and is labelled as such. Put/call ratios come from open interest.
- **Analyst consensus, ratings and ownership.** The venue's published consensus EPS per fiscal period with the high/low range and number of estimates, its analyst rating summary, and institutional ownership as a percentage of shares outstanding. Consensus is a **snapshot**: there is no revision history, so consensus as of a past date cannot be reconstructed. Revisions are only visible once this collector has run twice, because each fetch is stored as a dated snapshot. Institutional ownership is **not free float** and is never presented as such.
- **Delisting dates and reasons (Form 25).** With `--delisting-events`, each exchange notice is parsed for the **issuer CIK** — correctly separated from the filing exchange — plus the trading-suspension date, the rule provision and the resulting reason (merger, redemption, expiry, delinquent filing, exchange removal), and the terminal cash price when and only when the delisting was a merger. Readable as-of, so a backtest cannot learn of a delisting before the market did. This is a dated **event**, not a price history: a $10.50 merger consideration is one terminal value, not a series, and a delinquent delisting has no price at all.
- **Public float.** SEC's `dei:EntityPublicFloat`, carrying the filing date, so it is point-in-time. Two limits stated in the report: it is measured on the issuer's fiscal-year cover page rather than daily, and it is a dollar value, so turning it into float shares needs a price for that same date.
- **Archived filer index (survivorship).** SEC quarterly `full-index` archives back to 1993, with issuer deregistration notices (`15-12G`, `15-15D`) as the exit signal. This makes the **issuer** universe survivorship-aware, so historical fundamentals are not limited to companies that still exist. Form `25-NSE` is filed by the *exchange*, not the company, so it is stored separately and excluded from issuer attribution.
- **Cross-asset levels.** VIX, MOVE, credit ETFs, crude, gold, DXY and EUR/USD.

**`--coverage` is the important part.** It prints, dataset by dataset, what is `available`, `partial`, `unavailable` or `not_configured`, with a reason. It verifies rather than assumes: each cross-asset symbol is checked individually, and macro datasets are checked against the official journal when `--journal` is given. Read it before drawing conclusions, because several things your strategy might want are genuinely missing:

| Not available | Why |
|---|---|
| Delisted **price history** | No public source publishes a price *series* for a dead ticker; every endpoint tested returns 404 for Lehman, Enron, Bear Stearns, WorldCom, GM and Twitter. Delisting *dates* are now collected (see below), but a single terminal price is not a series, so **a price backtest is still survivorship-biased** and the report says so. |
| Consolidated NBBO | A single venue's book is not the national best bid and offer, and a consolidated tape needs a licensed feed. Spreads here are measured, but they are an upper bound. |
| Trade ticks | No free tick source; intraday bars are the finest granularity. |
| Stock-loan borrow rates | A commercial product. Short interest is **not** used as a proxy, because the two are not interchangeable. |
| Free float | Institutional ownership is collected, but it measures a different thing: float excludes insider and restricted holdings. The two are kept separate. |
| Consensus **revision history** | The venue publishes today's snapshot only. Revisions accumulate locally as you collect, but no vendor revision series is available, and a historical earnings surprise cannot be rebuilt. |
| NLP / news sentiment | Licensed. No sentiment score is computed. |

Two provider limits are reported honestly rather than papered over:

- **Intraday history is short.** About 7 days at 1-minute, 60 days at 5-minute, 2 years at hourly. Each run reports the real limit per interval.
- **Options coverage is short-dated.** The venue serves only the nearest expiries, so the surface is a few days to a few weeks out, not the full term structure. The report says so rather than implying a complete vol surface.

One collection detail worth knowing: `range=max` on the price source silently returns a *thinned* series (about 169 points across four decades) with no event list. Full history is therefore requested with an explicit epoch window, which returns the real series (about 11,600 daily bars for Apple, with all dividends and splits), and a parse-time check refuses any series that is implausibly sparse.

### Supervisor (J.A.R.V.I.S.)

The supervisor answers one question: *should the book change, and why?* It raises a trigger when the regime changes, when the target book drifts past a tolerance, when estimated volatility exceeds its budget, when drawdown breaches a limit, or when no usable macro input exists. Severity is `halt`, `change`, `watch` or `none`; `action` is `hold` or `rebalance`.

A stopped worker is reported as **stopped**, not as a feed outage: the stored journal is still readable, and stale rows fail closed on their own. The recommended next book is applied in instalments through the same turnover budget as everything else, never as a liquidation.


## Configure (optional)

Nothing needs configuring for the shipped schedule. For a different data path pass the global option *before* the command: `python3 market_lens.py --data /path/to/folder enable`. This is one service per user; enabling with another path changes what that service uses. Do not share one live SQLite folder between machines or use a network filesystem. Choose a local, writable directory. Run later commands with the same `--data` path.

To narrow the schedule, put `settings.json` inside the data directory, **not** beside this guide. Example:

```json
{"enabled":["ecb","ca-cpi","ca-prices"],"interval_minutes":{"ecb":60,"ca-prices":120}}
```

Run `enable` again to restart the service, or stop/restart your foreground process. Detected values remain in SQLite when you disable a feed. Remove the settings file and restart to restore defaults. `status` shows the effective schedule. The first pre-existing due time may still apply on restart. `feed_registry.csv` lists every key, default and minimum interval; the maximum is 1 day. There are at most three in-flight requests, at most one at a time per hostname, with host spacing. Failures back off exponentially (initially 5 minutes; BLS at least 6 hours); publisher `Retry-After` adds a shared host cooldown. There can be additional lag from publisher schedules, network, retries and validation. No scraping of arbitrary URLs or manual data entry.

## Uptime, recovery & uninstall

- **Windows:** Task Scheduler → Task Scheduler Library → `Market Lens Auto 6.8 - …`; runs under the signed-in account, no password, no three-day runtime limit. macOS: per-user LaunchAgent `org.marketlens.auto68`. Linux: systemd user service `market-lens-auto-68.service` (`systemctl --user status market-lens-auto-68.service`). An account policy may block these; confirm the heartbeat. If Python is moved or upgraded to a new path, re-run `enable` with the new interpreter.
- **Always-on Linux:** on a machine you administer, a systemd user service can also run before login/after logout if that account has **lingering** enabled. An administrator can approve `loginctl enable-linger USERNAME` for the collector account. This is optional and not done by this installer. Otherwise use a supervised foreground `run --no-dashboard` in your approved hosting environment, with persistent storage and outbound HTTPS. Closing the web monitor is never required for collection.
- **If it stops:** check your internet connection, system clock, disk space and `worker.log` (rotating, up to 3 backups). Closing Excel if it is holding `latest_live.csv` open can unblock atomic replacement on Windows; search and SQLite data collection are independent of a locked CSV. Restore the connection and leave the worker running; source errors retry automatically. After a process crash, restart the same service. Interrupted candidates are marked as such; existing successes and next-due times survive. A second writer is rejected. Local health reports `overdue` after the accepted receipt's next-due plus 5 minutes. Each series has a separate observation-age guard as well; these are tolerances, not release-calendar guarantees.
- **Backups:** `backup new-name.sqlite3` creates a consistent copy while running. To restore: stop the worker, keep the old folder as a backup, make a **new** data folder, put the backup inside named `macro-live.sqlite3`, restore any `settings.json`, then enable with the new `--data` path. Never mix a restored DB with old `-wal`/`-shm` files, or copy just an open DB file. Raw SHA-256 objects are deduplicated and compressed; SQLite append-only journal records are not automatically purged. Avoid synchronized cloud folders for the live WAL database.
- **Uninstall:** `disable` removes the service, not your evidence. Only delete `MarketLens62` yourself if you no longer want the history; back it up first. The disabled service does not persist its own network activity. Your existing Market Lens workbooks, ZIPs and historical files are never modified.

## Collection semantics

1. Fetch **from the registered public API**, with rolling date windows (or the publisher's complete series/last-N endpoint). Commit the URL, request body when applicable, legal redirects within the same host, raw bytes, SHA-256, retrieval time and available HTTP metadata. A failed request is not a release.
2. Check publisher-specific series identities, frequencies, units where supplied, coordinates, dates, missing markers and overlap. Preserve publisher flags, including provisional/estimated values. New formats, unexplained deletions, backwards tails, date mismatches or broken contracts quarantine the feed, rather than silently substituting stale data. This conservative collector is not a press-wire; it does not promise to accept flash estimates before the end of their reference period.
3. Read back the stored object and check the normalized snapshot count before indexing a searchable receipt. `seen` is local body arrival; `indexed` is a local collector receipt. Neither is the publisher's exact announcement time nor proof an independent strategy saw the value. A publisher's release field is retained where available, not invented where absent. For derived YoY values, use only two available levels exactly 12 months apart; a missing baseline is not interpolated.
4. Track new numeric periods, revisions, values newly available, withdrawals, backfills, quality/edition changes and explicit missing notices. First snapshots (or parser baselines on contract upgrade) are not release events. A raw object may be fetched many times while unchanged. Alternative/sparse series keep their own dates. An empty, unflagged Eurostat grid coordinate is not a published observation; a new month on the common axis alone is not news.
5. `current_value` is withheld if the latest source poll is pending, failed, quarantined, overdue, or its series tail is too old/missing. A retained numeric value from the last accepted snapshot is labeled separately. `latest_live.csv` is refreshed during uptime (normally at least every minute), not a magic live file while the machine is off. The system sees editions available when it checks; revisions outside the requested window, first prints between polls and outages are not reconstructed. Review the source definitions before combining series or using a value in a model.
