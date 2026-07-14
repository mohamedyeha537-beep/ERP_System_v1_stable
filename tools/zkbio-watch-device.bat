@echo off
:: Watch for fingerprint device contacting ZKBioTime (run while rebooting device)
echo Waiting for connections from 192.168.1.201 to port 9098...
echo ZKBioTime listens on 9098 (see attsite.ini). Do NOT run portproxy-9098.bat.
echo Press Ctrl+C to stop.
:loop
netstat -ano | findstr "192.168.1.201" | findstr ":9098"
if not errorlevel 1 echo [%date% %time%] DEVICE CONNECTED!
timeout /t 3 /nobreak >nul
goto loop
