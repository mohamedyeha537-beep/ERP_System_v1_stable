@echo off
setlocal
cd /d "%~dp0"

REM ZKBioTime may set PYTHONHOME/PYTHONPATH globally — breaks POS Python 3.13
set "PYTHONHOME="
set "PYTHONPATH="

echo ========================================
echo   POS - Python diagnostic
echo ========================================
echo.

echo [0] Environment (ZKBioTime conflict check):
if defined PYTHONHOME echo     PYTHONHOME=%PYTHONHOME%  ^<-- remove from Windows env if POS fails
if defined PYTHONPATH echo     PYTHONPATH=%PYTHONPATH%  ^<-- remove from Windows env if POS fails
if not defined PYTHONHOME if not defined PYTHONPATH echo     OK - no PYTHONHOME/PYTHONPATH set
set "PYTHONHOME="
set "PYTHONPATH="
echo.

set "PY=%LocalAppData%\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=%LocalAppData%\Programs\Python\Python314\python.exe"

echo [1] POS Python (use this):
if exist "%PY%" (
    echo     %PY%
    "%PY%" --version
) else (
    echo     NOT FOUND - install Python 3.13
)
echo.

echo [2] Default "python" in PATH (may HANG - Store stub):
where python 2>nul
echo     (if first line is WindowsApps - disable Store aliases)
echo.

echo [3] uvicorn package:
if exist "%PY%" (
    "%PY%" -c "import uvicorn; print('    OK - uvicorn installed')" 2>nul
    if errorlevel 1 echo     MISSING - run install-requirements.bat
)
echo.

echo [4] Quick server test (3 sec):
if exist "%PY%" (
    set POS_SKIP_STALE_KILL=1
    "%PY%" -c "import run; print('    run.py imports OK')" 2>nul
    if errorlevel 1 "%PY%" -c "print('    checking...'); import uvicorn; print('    uvicorn OK')"
)
echo.
echo Next: double-click start-server.bat
echo.
pause
