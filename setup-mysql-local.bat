@echo off
setlocal EnableExtensions
title POS — MySQL محلي
cd /d "%~dp0"

set "PYTHONHOME="
set "PYTHONPATH="

set "PY=%LocalAppData%\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=%LocalAppData%\Programs\Python\Python314\python.exe"
if not exist "%PY%" (
  echo ERROR: Python 3.13/3.14 not found. Run install-requirements.bat first.
  pause
  exit /b 1
)

echo ========================================
echo   POS — نقل SQLite الى MySQL محلي
echo   (Docker + نفس اعداد VPS)
echo ========================================
echo.

docker --version >nul 2>&1
if errorlevel 1 (
  echo ERROR: Docker Desktop غير مثبت أو غير شغال.
  echo ثبّت Docker Desktop ثم أعد المحاولة.
  echo راجع: deploy\mysql-local-windows.md
  pause
  exit /b 1
)

echo [1/3] تثبيت الحزم...
"%PY%" -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo FAILED pip install
  pause
  exit /b 1
)

echo.
echo [2/3] تشغيل MySQL + تحديث .env + نقل pos.db ...
"%PY%" tools\setup_mysql_local.py --all
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
  echo FAILED — راجع الرسائل أعلاه.
  pause
  exit /b %RC%
)

echo [3/3] تم. شغّل السيرفر: start-server.bat
echo.
pause
exit /b 0
