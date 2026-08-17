@echo off
setlocal
cd /d "%~dp0"
python web_app.py
if errorlevel 1 (
  echo.
  echo Failed to start. Install dependencies with:
  echo python -m pip install -r requirements.txt
  pause
)
