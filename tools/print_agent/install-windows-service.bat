@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

REM ============================================================
REM تثبيت وكيل الطباعة كخدمة Windows تعيد التشغيل تلقائياً
REM شغّل هذا الملف كـ Administrator على جهاز المطعم/الكاشير
REM ============================================================

set "SERVICE_NAME=POS-PrintAgent"
set "AGENT_DIR=%~dp0"
set "AGENT_DIR=%AGENT_DIR:~0,-1%"
set "PYTHON_EXE="
set "NSSM_EXE="

echo.
echo === تثبيت خدمة وكيل الطباعة ===
echo المجلد: %AGENT_DIR%
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo [خطأ] Python غير موجود على PATH
  pause
  exit /b 1
)
for /f "delims=" %%i in ('where python') do (
  set "PYTHON_EXE=%%i"
  goto :have_python
)
:have_python
echo Python: %PYTHON_EXE%

if not exist "%AGENT_DIR%\config.json" (
  echo [خطأ] أنشئ config.json من config.json.example أولاً
  pause
  exit /b 1
)

REM ابحث عن nssm
if exist "%AGENT_DIR%\nssm.exe" set "NSSM_EXE=%AGENT_DIR%\nssm.exe"
if not defined NSSM_EXE if exist "%AGENT_DIR%\nssm\nssm.exe" set "NSSM_EXE=%AGENT_DIR%\nssm\nssm.exe"
where nssm >nul 2>&1
if not errorlevel 1 if not defined NSSM_EXE (
  for /f "delims=" %%i in ('where nssm') do (
    set "NSSM_EXE=%%i"
    goto :have_nssm
  )
)
:have_nssm

if defined NSSM_EXE (
  echo استخدام NSSM: %NSSM_EXE%
  "%NSSM_EXE%" stop %SERVICE_NAME% >nul 2>&1
  "%NSSM_EXE%" remove %SERVICE_NAME% confirm >nul 2>&1
  "%NSSM_EXE%" install %SERVICE_NAME% "%PYTHON_EXE%" "%AGENT_DIR%\print_agent.py"
  if errorlevel 1 (
    echo [خطأ] فشل تثبيت NSSM — شغّل كـ Administrator
    pause
    exit /b 1
  )
  "%NSSM_EXE%" set %SERVICE_NAME% AppDirectory "%AGENT_DIR%"
  "%NSSM_EXE%" set %SERVICE_NAME% DisplayName "POS Print Agent"
  "%NSSM_EXE%" set %SERVICE_NAME% Description "وكيل طباعة POS — يستعلم السيرفر ويطبع تلقائياً"
  "%NSSM_EXE%" set %SERVICE_NAME% Start SERVICE_AUTO_START
  "%NSSM_EXE%" set %SERVICE_NAME% AppStdout "%AGENT_DIR%\print_agent.service.log"
  "%NSSM_EXE%" set %SERVICE_NAME% AppStderr "%AGENT_DIR%\print_agent.service.log"
  "%NSSM_EXE%" set %SERVICE_NAME% AppRotateFiles 1
  "%NSSM_EXE%" set %SERVICE_NAME% AppRotateBytes 2000000
  "%NSSM_EXE%" set %SERVICE_NAME% AppExit Default Restart
  "%NSSM_EXE%" set %SERVICE_NAME% AppRestartDelay 5000
  "%NSSM_EXE%" set %SERVICE_NAME% AppThrottle 10000
  net start %SERVICE_NAME%
  echo.
  echo تم التثبيت. الخدمة: %SERVICE_NAME%
  echo ستُعاد التشغيل تلقائياً عند التوقف أو إعادة تشغيل الجهاز.
  echo راقب: %AGENT_DIR%\print_agent.log
  echo.
  pause
  exit /b 0
)

echo NSSM غير موجود — سيتم استخدام Task Scheduler كبديل.
echo يُفضّل تنزيل nssm من https://nssm.cc/download ووضع nssm.exe في هذا المجلد.
echo.

schtasks /Query /TN "%SERVICE_NAME%" >nul 2>&1
if not errorlevel 1 schtasks /Delete /TN "%SERVICE_NAME%" /F >nul 2>&1

schtasks /Create /TN "%SERVICE_NAME%" /TR "\"%PYTHON_EXE%\" \"%AGENT_DIR%\print_agent.py\"" /SC ONSTART /RU SYSTEM /RL HIGHEST /F
if errorlevel 1 (
  echo [خطأ] فشل إنشاء المهمة المجدولة — شغّل كـ Administrator
  pause
  exit /b 1
)
schtasks /Run /TN "%SERVICE_NAME%"
echo.
echo تم إنشاء مهمة مجدولة تعمل عند بدء Windows: %SERVICE_NAME%
echo ملاحظة: Task Scheduler أقل قوة من NSSM في إعادة التشغيل عند التعطل.
echo.
pause
exit /b 0
