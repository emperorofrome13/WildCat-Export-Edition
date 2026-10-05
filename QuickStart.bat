@echo off
setlocal
cd /d "%~dp0"
title WildCat Export Edition
where py >nul 2>nul
if not errorlevel 1 (
  py -3 scripts\launcher.py %*
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python was not found. Install Python 3.10 or newer from python.org.
    echo Enable "Add Python to PATH" during installation, then try again.
    pause
    exit /b 1
  )
  python scripts\launcher.py %*
)
if errorlevel 1 (
  echo.
  echo WildCat could not start. Read the message above, fix it, and try again.
  pause
)
endlocal
