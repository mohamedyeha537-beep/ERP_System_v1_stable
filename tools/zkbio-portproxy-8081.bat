@echo off
:: Run as Administrator
:: Forwards port 8081 -> 80 so device still on 8081 can reach ZKBioTime
netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=8081 >nul 2>&1
netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=8081 connectaddress=127.0.0.1 connectport=80
netsh advfirewall firewall add rule name="ZKBioTime ADMS 8081 forward" dir=in action=allow protocol=TCP localport=8081
if errorlevel 1 (
    echo FAILED — run as Administrator
    pause
    exit /b 1
)
echo OK — port 8081 now forwards to ZKBioTime on port 80.
echo You can use EITHER port 80 OR 8081 on the fingerprint device.
netsh interface portproxy show all
pause
