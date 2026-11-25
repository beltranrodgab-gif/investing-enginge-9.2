@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if errorlevel 1 goto direct
py -3 market_lens.py disable
goto finish
:direct
python market_lens.py disable
:finish
if errorlevel 1 (echo Setup did not finish. Check the message above.) else (echo Auto-start is off. Saved data was kept.)
pause
