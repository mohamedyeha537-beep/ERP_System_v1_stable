@echo off
chcp 65001 >nul
setlocal
set "SCRIPT_DIR=%~dp0"
set "ROOT=%SCRIPT_DIR%..\.."
set "REF=%SCRIPT_DIR%reference"

if not exist "%REF%\receipt_capture.js" (
  echo [خطأ] مجلد المرجع غير موجود: %REF%
  exit /b 1
)

echo استعادة إعدادات الطباعة الحرارية المعتمدة...
copy /Y "%REF%\receipt_capture.js" "%ROOT%\app\static\receipt_capture.js" >nul
copy /Y "%REF%\receipt_print.css" "%ROOT%\app\static\receipt_print.css" >nul
copy /Y "%REF%\escpos_raster.py" "%ROOT%\tools\print_agent\escpos_raster.py" >nul

python "%SCRIPT_DIR%verify_receipt_print.py"
if errorlevel 1 (
  echo.
  echo [تحذير] بعض الفحوصات فشلت — راجع الملفات يدوياً.
  exit /b 1
)

echo.
echo تمت الاستعادة بنجاح.
echo 1. أعد تشغيل السيرفر ^(restart-server.bat^)
echo 2. حدّث المتصفح بقوة ^(Ctrl+F5^) عند طباعة فاتورة
endlocal
