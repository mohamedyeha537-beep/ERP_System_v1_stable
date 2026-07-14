# Stop POS uvicorn/python on ports 8011-8030 — netstat only (no slow WMI).
$ErrorActionPreference = "SilentlyContinue"

[Console]::Out.WriteLine("Stopping POS server...")
[Console]::Out.Flush()

$pids = @{}
try {
    $lines = & netstat -ano -p tcp 2>$null
    if ($lines) {
        foreach ($line in $lines) {
            if ($line -notmatch "LISTENING") { continue }
            if ($line -notmatch ":(80(1[1-9]|2[0-9]|30))\s") { continue }
            if ($line -match "\s+(\d+)\s*$") {
                $id = [int]$Matches[1]
                if ($id -gt 0) { $pids[$id] = $true }
            }
        }
    }
} catch {
    [Console]::Out.WriteLine("  Warning: netstat failed - $_")
}

if ($pids.Count -eq 0) {
    [Console]::Out.WriteLine("  No POS server process found.")
} else {
    foreach ($id in $pids.Keys) {
        [Console]::Out.WriteLine("  Stop PID $id")
        [Console]::Out.Flush()
        & taskkill /PID $id /F /T 2>$null | Out-Null
    }
}

[Console]::Out.WriteLine("Done.")
[Console]::Out.Flush()
