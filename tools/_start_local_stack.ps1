$ErrorActionPreference = 'Continue'
Write-Host '=== MySQL services ==='
Get-Service MySQL80, wampstackMySQL -ErrorAction SilentlyContinue | Format-Table Name, Status -AutoSize | Out-String | Write-Host

$mysqld80 = 'C:\Program Files\MySQL\MySQL Server 8.0\bin\mysqld.exe'
$mysqldBit = 'C:\Bitnami\wampstack-8.0.0-1\mysql\bin\mysqld.exe'
Write-Host "MySQL80 binary exists: $(Test-Path $mysqld80)"
Write-Host "Bitnami binary exists: $(Test-Path $mysqldBit)"

# Try start MySQL80 service (needs admin)
try {
  Start-Service MySQL80 -ErrorAction Stop
  Write-Host 'MySQL80 service started'
} catch {
  Write-Host "Cannot start MySQL80 service: $($_.Exception.Message)"
}

Start-Sleep 2
$portOk = $false
try {
  $tn = Test-NetConnection 127.0.0.1 -Port 3306 -WarningAction SilentlyContinue
  $portOk = [bool]$tn.TcpTestSucceeded
} catch {}
Write-Host "Port 3306 open: $portOk"

if (-not $portOk -and (Test-Path $mysqldBit)) {
  Write-Host 'Trying Bitnami mysqld standalone...'
  Start-Process -FilePath $mysqldBit -ArgumentList @(
    '--defaults-file=C:\Bitnami\wampstack-8.0.0-1\mysql\my.ini',
    '--console'
  ) -WindowStyle Minimized
  Start-Sleep 5
  try {
    $tn2 = Test-NetConnection 127.0.0.1 -Port 3306 -WarningAction SilentlyContinue
    $portOk = [bool]$tn2.TcpTestSucceeded
  } catch {}
  Write-Host "Port 3306 after Bitnami: $portOk"
}

if (-not $portOk) {
  Write-Host 'FAIL: MySQL not running. MySQL80 binary missing - reinstall MySQL Server 8.0 or start service as Administrator.'
  exit 2
}

Set-Location 'C:\Users\PIXEL\OneDrive\Desktop\pos2'
Remove-Item Env:PYTHONPATH, Env:PYTHONHOME -ErrorAction SilentlyContinue
$env:Path = ($env:Path -split ';' | Where-Object { $_ -notmatch 'ZKBioTime' }) -join ';'
$env:SYNC_ENABLED = 'false'
$env:POS_RELOAD = '0'
$env:POS_PORT = '8012'
$env:POS_SKIP_STALE_KILL = '1'
Write-Host 'Starting POS on 8012...'
& '.\.venv\Scripts\python.exe' run.py
