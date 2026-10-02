@echo off
rem Starts the web UI (if it isn't already running) and opens it in the browser.
setlocal
set "PROJECT_DIR=%~dp0"
set "PYTHON=%PROJECT_DIR%.venv\Scripts\python.exe"
cd /d "%PROJECT_DIR%"

if not exist "%PYTHON%" (
    echo Virtual environment not found. Run the setup steps in README.md first.
    pause
    exit /b 1
)

netstat -ano | findstr ":5000" | findstr "LISTENING" >nul
if errorlevel 1 (
    start "Liquidity Depth Server" /min "%PYTHON%" -m webapp.app
    timeout /t 3 /nobreak >nul
)

start "" "http://127.0.0.1:5000"
