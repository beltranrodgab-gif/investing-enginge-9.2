# Strategy review fixes

Every item below has a regression test in `test_auto.py` (`ReviewFixes`) that fails on the old
behaviour.

## Bugs that made the strategy wrong

| # | Problem | Fix |
|---|---------|-----|
| 1 | Unemployment was averaged into **growth** with a positive sign: a jump in unemployment scored `growth_z = +3.5` (boom). | `FACTOR_SIGN` inverts `LAB-02` (`signal_filter.py`). |
| 2 | Price-index **levels** (Statistics Canada, Jan 2020 = 100) were z-scored as inflation, so a steady 2.4%/yr index read `+1.78`. | Index-unit series are converted to year-on-year % before scoring (`to_yoy`). |
| 3 | `shrink_to_diagonal(m, 0.10)` kept **10%** of every covariance instead of 90% (0.98 correlation became 0.098). | Off-diagonals are multiplied by `1 - shrinkage`. |
| 4 | The paper trader only applied the last two prices: a 100 → 80 → 79 path booked -1.25% instead of -21%. | Marks are stored per asset and the move is measured from the last mark (`last_marks`, `marks`, persisted in `paper_account.json`). |
| 5 | Sizing assumed zero correlation: an equity-only book "at 10% vol" ran ~16%. | `allocate` and `portfolio_vol` use correlation priors (`asset_classes.py`; 0.5 for unknown tickers). |
| 6 | A data outage made the target `{}`, which `apply_turnover_budget` turned into a full liquidation with **0 turnover**: outside the budget, uncosted. | An empty target is budgeted like any move. An outage steps toward half of the current book inside the budget and is labelled `data_outage` (`alpha_engine.outage_target`). Applied in `decide`, `run_alpha_step`, `run_processor`, the paper trader and the CLI. |

## Strategy design

| # | Problem | Fix |
|---|---------|-----|
| 7 | The growth/inflation quadrant was reported but never changed the weights. | `asset_classes.quadrant_tilts` tilts the inverse-vol weights toward the quadrant's buckets (`quadrant_tilt`, default 0.5). Optional trend filter halves assets below their 200-day average (`price_series`). |
| 8 | Four quadrant → allocation tables disagreed. | One table, `asset_classes.ALL_WEATHER_MAP`; `daily_processor` and `dalio_quadrant_weights` derive from it. (`SECTOR_MAP` remains a list of ETF *ideas*, not weights.) |
| 9 | Inverted curve halved risk (mid-2022 to late-2024 was a strong equity market); re-steepening was treated as safe; "tight" was a fixed SOFR > 5.5%. | Inverted → 75% (early warning). Re-steepening after an inversion → `post_inversion_steepening` at 50%. Tightness = SOFR up ≥ 1 pp over ~6 months (level rule only as a fallback without history). |
| 10 | Five euro-area series outvoted one US series. | Euro members collapse into `EA`; regions are GDP-weighted (`REGION_WEIGHT`). |
| 11 | Volatility was hand-typed or a noisy 20-day estimate. | `asset_classes.blended_vol`: max(20-day, EWMA with 30-day half-life over 250 days). Used by the paper trader and the Yahoo auto-vol. |
| 12 | The plan maximised P(hit goal), so an ambitious goal forced maximum risk; priors assumed a fixed 4% cash rate. | Among points within 5 pp of the best goal probability, the highest Sharpe wins, with a warning when the goal is under 50% likely. Priors shift with the live SOFR (`rf`). |

## Safety and measurement

| # | Problem | Fix |
|---|---------|-----|
| 13 | No strategy backtest; fees charged every period regardless of trading; reads used revised data. | `backtest.replay` replays `decide(at=…)` point in time with turnover-based costs and an equal-weight benchmark. `series_history`, `usable_inputs` and `revision_noise_ok` take `at`. `walk_forward_backtest(turnovers=…)` charges fees on turnover. |
| 14 | Drawdown Monte Carlo used 12 steps for the whole horizon and normal tails. | Steps scale with the horizon (52/yr default, 26/yr in the plan) with Student-t (5 d.o.f.) shocks and common random numbers across candidates. |
| 15 | Drawdown guard used the all-time high and could stay on indefinitely. | `drawdown_state`: rolling one-year peak, and re-entry once NAV is above its 50-period average. |
| 16 | Turnover budget semantics undocumented. | Documented as two-sided; `decide` also reports `turnover_one_way`. |

## Also fixed along the way

- `history_zscore` returned ±3.5 for a constant history because of 1e-15 float noise.
- The plan's rounded asset weights could sum to 1.000001; rounding now preserves the total.
- `asset_classes.py` and `backtest.py` are added to the auto-start runtime (`autostart.CORE`), with a test that every local import is in that list (otherwise the installed worker would fail to import).
