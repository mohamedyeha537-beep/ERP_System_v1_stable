@echo off
:: Usually NOT needed: ZKBioTime already listens on 9098 (attsite.ini Port=9098).
:: Only use if you deliberately moved iclock to port 80 and need external 9098.
:: Run as Administrator — forwards device port 9098 to ZKBioTime (port 80)
netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=9098 >nul 2>&1
netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=9098 connectaddress=127.0.0.1 connectport=80
netsh advfirewall firewall delete rule name="ZKBioTime ADMS 9098" >nul 2>&1
netsh advfirewall firewall add rule name="ZKBioTime ADMS 9098" dir=in action=allow protocol=TCP localport=9098
if errorlevel 1 (
    echo FAILED — run as Administrator
    pause
    exit /b 1
)
echo OK — port 9098 forwards to ZKBioTime on port 80.
netsh interface portproxy show all
pause
