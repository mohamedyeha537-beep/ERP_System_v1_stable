@echo off
setlocal EnableExtensions
title POS — MySQL عبر XAMPP
cd /d "%~dp0"

set "PYTHONHOME="
set "PYTHONPATH="

echo ========================================
echo   POS — MySQL via XAMPP (no Docker)
echo ========================================
echo.
echo Before continuing:
echo   1. Open XAMPP Control Panel
echo   2. Click START next to MySQL
echo.
pause

set "PY=%LocalAppData%\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=%LocalAppData%\Programs\Python\Python314\python.exe"
if not exist "%PY%" (
  echo ERROR: Python not found. Run install-requirements.bat
  pause
  exit /b 1
)

echo Installing Python packages...
"%PY%" -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo pip install failed
  pause
  exit /b 1
)

echo.
echo Setup: create pos_db, update .env, migrate pos.db ...
REM Use root directly on local XAMPP to avoid corrupted MariaDB privilege tables.
"%PY%" tools\setup_mysql_xampp.py --all --use-root
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
  echo FAILED. See deploy\mysql-xampp-windows.md
  pause
  exit /b %RC%
)

echo Done. Run: start-server.bat
pause
exit /b 0
