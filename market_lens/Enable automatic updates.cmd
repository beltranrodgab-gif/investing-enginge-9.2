@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if errorlevel 1 goto direct
py -3 market_lens.py enable
goto finish
:direct
python market_lens.py enable
:finish
if errorlevel 1 (echo Setup did not finish. Check the message above.) else (echo Open http://127.0.0.1:8790 to view the monitor.)
pause
