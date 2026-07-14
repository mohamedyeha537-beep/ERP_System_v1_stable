@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM Prevent click-to-freeze (QuickEdit) before any long step
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\disable-console-quickedit.ps1" 2>nul

echo ========================================
echo   POS - restart server
echo ========================================
echo.
echo  Do NOT click inside this window while it runs.
echo  If it looks frozen: press Enter once, or close and run again.
echo.

echo [1/3] Stopping old server...
echo       Scanning ports 8011-8030...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-server.ps1"
if errorlevel 1 (
    echo [WARN] stop-server returned an error — continuing anyway.
)
echo.

echo [2/3] Waiting for port to free...
timeout /t 2 /nobreak >nul
echo.

echo [3/3] Starting server in THIS window...
echo.
echo  IMPORTANT: keep this window open while using the POS.
echo  URL: http://127.0.0.1:8011/
echo  To stop the server: press Ctrl+C in this window.
echo.
echo ========================================
echo.

set POS_SKIP_STALE_KILL=1
set POS_RELOAD=0
call "%~dp0start-server.bat"

echo.
echo Server stopped.
pause
exit /b %ERRORLEVEL%
