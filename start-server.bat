@echo off

setlocal EnableExtensions

title POS Server

cd /d "%~dp0"



REM ZKBioTime may set PYTHONHOME/PYTHONPATH globally — breaks POS Python 3.13

if defined PYTHONHOME echo [WARN] PYTHONHOME=%PYTHONHOME% — will be cleared for this session

if defined PYTHONPATH echo [WARN] PYTHONPATH set — will be cleared for this session

set "PYTHONHOME="

set "PYTHONPATH="



if not defined POS_SKIP_STALE_KILL set POS_SKIP_STALE_KILL=0

if not defined POS_RELOAD set POS_RELOAD=0



powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\disable-console-quickedit.ps1" 2>nul



set "PY=%LocalAppData%\Programs\Python\Python313\python.exe"

if not exist "%PY%" set "PY=%LocalAppData%\Programs\Python\Python314\python.exe"



if not exist "%PY%" (

    echo.

    echo ERROR: Python 3.13/3.14 not found.

    echo Install from https://www.python.org/downloads/

    echo Or run: install-requirements.bat

    echo.

    pause

    exit /b 1

)



echo ========================================

echo   POS Server

echo ========================================

echo Python: %PY%

echo Folder: %CD%

echo.



"%PY%" -c "import uvicorn" 2>nul

if errorlevel 1 (

    echo Packages missing — installing requirements...

    "%PY%" -m pip install -r requirements.txt

    if errorlevel 1 (

        echo.

        echo FAILED to install packages. Run install-requirements.bat

        pause

        exit /b 1

    )

)



echo Starting on http://127.0.0.1:8011/ ...

echo.

echo  WAIT for: Application startup complete

echo  Then open: http://127.0.0.1:8011/shop

echo.

echo  Do NOT click inside this black window (freezes server)

echo  Stop only with Ctrl+C

echo.

echo ----------------------------------------



"%PY%" run.py 2>&1

set "RC=%ERRORLEVEL%"



echo.

echo ----------------------------------------

if not "%RC%"=="0" (

    echo Server stopped with error code %RC%

) else (

    echo Server stopped.

)

pause

exit /b %RC%

