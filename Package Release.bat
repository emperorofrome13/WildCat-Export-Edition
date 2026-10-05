@echo off
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 scripts\package_release.py
) else (
  python scripts\package_release.py
)
pause
