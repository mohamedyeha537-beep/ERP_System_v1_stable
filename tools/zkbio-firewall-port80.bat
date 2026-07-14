@echo off
:: Run as Administrator: right-click → Run as administrator
netsh advfirewall firewall add rule name="ZKBioTime ADMS 80" dir=in action=allow protocol=TCP localport=80
if errorlevel 1 (
    echo.
    echo FAILED — run this file as Administrator.
    echo Right-click the file ^> Run as administrator
    pause
    exit /b 1
)
echo.
echo OK — firewall rule added for TCP port 80 (ZKBioTime ADMS).
echo Device server settings: IP 192.168.1.10  Port 80
pause
