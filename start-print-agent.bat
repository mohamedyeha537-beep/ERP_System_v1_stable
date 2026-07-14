@echo off
setlocal
if not exist "%~dp0tools\print_agent\print_agent.py" (
    echo ERROR: tools\print_agent folder not found.
    echo Put start-print-agent.bat next to the tools folder, or unzip the full start-print-agent package.
    echo Expected: %~dp0tools\print_agent\print_agent.py
    pause
    exit /b 1
)
cd /d "%~dp0tools\print_agent"

REM ZKBioTime may set PYTHONHOME/PYTHONPATH globally — breaks POS Python 3.13
set "PYTHONHOME="
set "PYTHONPATH="

set "PY=%LocalAppData%\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=%LocalAppData%\Programs\Python\Python314\python.exe"
if not exist "%PY%" set "PY=python"

if not exist "config.json" (
    echo ERROR: config.json not found.
    echo Copy config.json.example and set server_url + agent_token.
    pause
    exit /b 1
)

echo ========================================
echo   Print Agent (Local)
echo ========================================
echo Python: %PY%
echo Folder: %CD%
echo.
echo Leave this window open while POS is running.
echo server_url must match POS port (default 8011).
echo.

"%PY%" print_agent.py

if errorlevel 1 (
    echo.
    echo Print agent exited with error.
    pause
    exit /b 1
)

pause
