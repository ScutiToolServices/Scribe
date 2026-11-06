@echo off
REM Starts the Scribe Service on port 8091 (syncs .venv with uv on each run).
REM LABEL: Scribe Service
REM DESCRIPTION: Speech to text MCP tool server for Citadel and Ember (needs WhisperModel running).

cd /d "%~dp0"

uv sync
if errorlevel 1 (
    echo uv sync failed. Install uv: winget install astral-sh.uv
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat

python -m src.run
pause
