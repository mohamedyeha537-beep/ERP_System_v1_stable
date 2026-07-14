@echo off
:: Right-click → Run as administrator
setlocal

echo Adding ZKBioTime firewall rules...

netsh advfirewall firewall delete rule name="ZKBioTime Service 9098" >nul 2>&1
netsh advfirewall firewall add rule name="ZKBioTime Service 9098" dir=in action=allow protocol=TCP localport=9098
if errorlevel 1 goto fail

netsh advfirewall firewall delete rule name="ZKBioTime Web 80" >nul 2>&1
netsh advfirewall firewall add rule name="ZKBioTime Web 80" dir=in action=allow protocol=TCP localport=80
if errorlevel 1 goto fail

echo.
echo OK — both rules added:
echo   - ZKBioTime Service 9098  (TCP 9098 inbound)
echo   - ZKBioTime Web 80        (TCP 80 inbound)
echo.
netsh advfirewall firewall show rule name="ZKBioTime Service 9098"
netsh advfirewall firewall show rule name="ZKBioTime Web 80"
pause
exit /b 0

:fail
echo.
echo FAILED — you must Run as administrator.
echo Right-click this file ^> Run as administrator
pause
exit /b 1
