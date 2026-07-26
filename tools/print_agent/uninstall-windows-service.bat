@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "SERVICE_NAME=POS-PrintAgent"
set "NSSM_EXE="

if exist "%~dp0nssm.exe" set "NSSM_EXE=%~dp0nssm.exe"
where nssm >nul 2>&1
if not errorlevel 1 if not defined NSSM_EXE (
  for /f "delims=" %%i in ('where nssm') do set "NSSM_EXE=%%i"
)

if defined NSSM_EXE (
  "%NSSM_EXE%" stop %SERVICE_NAME%
  "%NSSM_EXE%" remove %SERVICE_NAME% confirm
) else (
  net stop %SERVICE_NAME% >nul 2>&1
  sc delete %SERVICE_NAME% >nul 2>&1
)
schtasks /Delete /TN "%SERVICE_NAME%" /F >nul 2>&1
echo تمت إزالة خدمة/مهمة %SERVICE_NAME%
pause
