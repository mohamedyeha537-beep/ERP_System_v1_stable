@echo off
setlocal
cd /d "%~dp0"

REM ZKBioTime may set PYTHONHOME/PYTHONPATH globally — breaks POS Python 3.13
set "PYTHONHOME="
set "PYTHONPATH="

set "PY=%LocalAppData%\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=%LocalAppData%\Programs\Python\Python314\python.exe"

if not exist "%PY%" (
    echo ERROR: Python 3.13/3.14 not found.
    echo Install from https://www.python.org/downloads/
    pause
    exit /b 1
)

echo Installing packages for POS...
echo Using: %PY%
echo.

"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -r requirements.txt

if errorlevel 1 (
    echo.
    echo INSTALL FAILED.
    pause
    exit /b 1
)

echo.
echo Done. Now run: start-server.bat
pause
