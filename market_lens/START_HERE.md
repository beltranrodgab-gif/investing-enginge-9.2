# Market Lens 6.8 | Automatic macro collector

**Set it up once. After that the worker fetches supported data directly from official publishers, even when the dashboard is closed.** No spreadsheet uploads, API keys, paid feeds, Python packages or manual refreshes. The collector is version 6.8; the optional strategy and dashboard layer that sits on top of it is the 9.2 review build.

## 1 · Turn it on

Use a computer with **Python 3.11 or newer** and an internet connection. On Windows the standard Python installation with the `py` launcher works. **Extract the entire ZIP** before launching anything. Do not run a second foreground worker while enabling automatic start.

| Computer | One-time action in the extracted folder |
|---|---|
| Windows | Double-click **Enable automatic updates.cmd**. Or, in a terminal there: `py -3 market_lens.py enable` |
| macOS | Run `python3 market_lens.py enable` in Terminal from this folder. **Enable automatic updates.command** is also included. |
| Linux with a systemd user session | Run `python3 market_lens.py enable` from this folder, or `sh enable-autostart.sh`. |

`enable` copies the small runtime into your own data folder, registers it for **your account at login**, starts it immediately, and waits for a new worker heartbeat. No administrator access or stored password is requested. The registration is not made just by downloading the file. If your organization blocks scheduled tasks or you do not have a user service manager, use `python3 market_lens.py run` (Windows: `py -3 market_lens.py run`) and keep that terminal open, or run it on an approved server.

## 2 · See what arrived

Open **http://127.0.0.1:8790** on the same computer. Give it a minute to populate the first time. **Scheduler online** and a recent heartbeat mean the loop is running; **Feed schedule** shows whether each source succeeded and when it is due again. A healthy scheduler and a healthy feed are different checks.

- **Series explorer:** choose an area or type a measure, e.g. `diesel` + `Canada`, or `ACT-01` + `Spain`. Select a row to see the period, unit, quality flag and source receipt. A dash is not zero.
- **Release journal:** subsequent new periods, revisions, withdrawals, backfills and status changes. The initial import is a baseline, not thousands of new releases. An unchanged check records a receipt but does not add a release event.
- **Use it in your own engine:** point it at `MarketLens62/latest_live.csv` in your home folder, or the local `/api/series` endpoint. The worker updates the CSV by atomic replacement without opening a browser or spreadsheet. Filter on `state == accepted`; read `period`, `unit`, `current_value`, `exported_utc` and `recheck_due_utc` together. `last_numeric_value` is separate and can be stale. Never treat an old file on a powered-off machine as live. The **Latest values CSV** button downloads a separate point-in-time copy.

## 3 · Leave the host available

You can close the browser and, after enabling auto-start, the setup window. The computer still needs to be **awake and online**, and normally signed into the account that enabled it. It does not collect while powered off, asleep or blocked by a firewall. It checks overdue feeds on resumption, but publisher editions that came and went between checks may be missed. These are scheduled polls, not push alerts or guaranteed first prints. For continuous unattended coverage use an always-on computer or server. An Arena live preview is a demonstration, **not permanent hosting**.

## What is included?

| Normal check interval | Publishers / feeds |
|---|---|
| 30 minutes | ECB reference FX (daily), Bank of Canada Valet, New York Fed SOFR, daily US Treasury yields |
| 1 hour | UK ONS (6 feeds), Statistics Canada (4), Eurostat (3) |
| 2 hours | BIS (3), OECD (3) |
| 6 hours | US BLS (one no-key batch for CPI and unemployment) |

**26 feeds · 154 series · 17 areas.** Areas: US, Canada, UK, euro area, Spain, Germany, France, Italy, Japan, China, Australia, South Korea, Switzerland, Sweden, Norway, Mexico, Brazil. Eleven publisher names appear in the feed table, but **ECB** and **European Central Bank** are the same institution and "US Treasury" is a separate source, so the distinct institutions are ten. Coverage varies. Headline inflation, real GDP growth and broad real effective FX are mapped to every area (not always the national release). The package maps 18 factor codes across **75 of 2,560 area × factor combinations**; the other **2,485** are not on this schedule. Shared euro-area rates/quotes are one observation, not independent national signals. Alternative national/distributor series are never averaged or spliced. Where two feeds cover the same quantity (for example the daily Treasury curve and the monthly OECD yield spread), the group logic keeps exactly one representative per `signal_group`. See **atlas_coverage.csv** for each slot and **series_catalogue.csv** for units and sources.

## Stop, resume or diagnose

- **Turn off the login service:** `python3 market_lens.py disable` (Windows: `py -3 market_lens.py disable`, or double-click **Disable automatic updates.cmd**). macOS also includes a `.command` launcher. Collected data is kept. Run `enable` later to resume. A foreground `run` can be stopped with **Ctrl+C**.- **Inspect:** `python3 market_lens.py status` or open `worker.log` in your home folder's `MarketLens62` directory. Windows default: `%USERPROFILE%\MarketLens62`; macOS/Linux: `~/MarketLens62`. Offline status or repeated source failures are not success. A blocked port does not stop collection; see the log if the dashboard will not open.
- **Keep history:** use the `backup` command in **OPERATIONS.md**. The persistent journal is `macro-live.sqlite3`; do not discard it on restart. Its raw snapshots are retained, so make sure there is disk space.
- **No update to the old archive:** the frozen 3.4 workbook and its November 18 cutoff are unchanged and are not touched by this collector. Your own local collection begins at your first successful check. See **VERIFICATION.md** for what was and was not tested.
