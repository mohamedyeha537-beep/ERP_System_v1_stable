@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PY=%LocalAppData%\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=%LocalAppData%\Programs\Python\Python314\python.exe"
if not exist "%PY%" set "PY=python"

echo.
echo === استيراد نسخة السيرفر الفعلي إلى MySQL (XAMPP) ===
echo تأكد أن MySQL يعمل في XAMPP Control Panel
echo.
"%PY%" tools\import_live_mysql_backup.py --all %*
if errorlevel 1 pause
exit /b %errorlevel%
