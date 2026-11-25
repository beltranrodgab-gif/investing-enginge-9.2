@echo off
cd /d "%~dp0"
echo Starting Obsidian Economics // J.A.R.V.I.S. on http://127.0.0.1:8790 ...
start "" "http://127.0.0.1:8790"
python -c "import dashboard, time; dashboard.start_server('.', '127.0.0.1', 8790); time.sleep(999999)" || py -c "import dashboard, time; dashboard.start_server('.', '127.0.0.1', 8790); time.sleep(999999)"
pause
