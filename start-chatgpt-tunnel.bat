@echo off
setlocal
cd /d "%~dp0"

if not exist "openai-runtime-api-key.txt" (
  echo Missing openai-runtime-api-key.txt
  echo Copy openai-runtime-api-key.txt.example to openai-runtime-api-key.txt and paste the runtime key.
  pause
  exit /b 1
)

if not exist "target\release\codex-free.exe" (
  echo Release binary not found. Building it now...
  cargo build --release
  if errorlevel 1 (
    echo Build failed.
    pause
    exit /b 1
  )
)

echo Starting Codex Free with OpenAI Secure MCP Tunnel...
target\release\codex-free.exe --config "%~dp0codex.config.json" --work-dir "%~dp0."
pause
