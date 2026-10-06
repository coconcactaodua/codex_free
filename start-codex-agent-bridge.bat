@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo Python 3 is required for the local browser agent bridge.
  pause
  exit /b 1
)

py -3 -c "import websockets" >nul 2>nul
if errorlevel 1 (
  echo Installing the bridge dependency: websockets
  py -3 -m pip install --user websockets
  if errorlevel 1 (
    echo Could not install websockets. Run: py -3 -m pip install --user websockets
    pause
    exit /b 1
  )
)

echo Starting Codex Free batch agent bridge on ws://127.0.0.1:17613
py -3 "%~dp0codex-agent-bridge\bridge.py"
pause
