@echo off
setlocal
cd /d "%~dp0"
set "tracker_python=python"
for %%P in (
  ".venv\Scripts\python.exe"
  ".venv\bin\python.exe"
  ".venv-grpc-probe\Scripts\python.exe"
  ".venv-grpc-probe\bin\python.exe"
  ".venv-build\Scripts\python.exe"
  ".venv-build\bin\python.exe"
) do (
  if exist "%%~P" (
    "%%~P" -c "import flask, waitress, h2.connection, qrcode.image.svg" >nul 2>&1
    if not errorlevel 1 (
      set "tracker_python=%%~P"
      goto run
    )
  )
)
:run
"%tracker_python%" -X utf8 web_app.py %*
if errorlevel 1 (
  echo.
  echo Failed to start. Install dependencies with:
  echo python -m pip install -r requirements.txt
  pause
)
