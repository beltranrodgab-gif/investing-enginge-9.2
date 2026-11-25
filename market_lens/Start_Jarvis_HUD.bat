@echo off
cd /d "%~dp0"
echo Starting 24/7 Collector + Obsidian Economics // J.A.R.V.I.S. 9.2 on http://127.0.0.1:8790 ...
start "MarketLens Collector" /min cmd /c "python market_lens.py run || py market_lens.py run"
start "" "http://127.0.0.1:8790"
python -c "import dashboard, time; dashboard.start_server('.', '127.0.0.1', 8790); time.sleep(999999)" || py -c "import dashboard, time; dashboard.start_server('.', '127.0.0.1', 8790); time.sleep(999999)"
pause
