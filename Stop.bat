@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 scripts\launcher.py --stop
) else (
  python scripts\launcher.py --stop
)
if errorlevel 1 pause
endlocal
