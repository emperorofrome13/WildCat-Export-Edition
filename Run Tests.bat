@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Run QuickStart.bat once to install dependencies first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m unittest discover -s tests -v
if errorlevel 1 pause
