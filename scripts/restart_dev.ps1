# Restart the dev dashboard on :8016 and wait until it answers.
#
# Deliberately does NOT use the prod restart_dash.ps1 pattern of killing every
# python.exe - that would take production down with it. This stops only the
# dev scheduled task and the process listening on 8016.

$ErrorActionPreference = "Continue"
$taskName = "GDELT-Dashboard-Dev"
$port     = 8016

function Get-DevPid {
    $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($c) { return $c.OwningProcess } else { return $null }
}

Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

# The task host does not always reap the python child; clear the port directly.
$devPid = Get-DevPid
if ($devPid) {
    Write-Host "stopping stale listener pid=$devPid"
    Stop-Process -Id $devPid -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

Start-ScheduledTask -TaskName $taskName

# Poll for health rather than sleeping a fixed amount - cold DuckDB attach on
# the 7GB slice can take a while.
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 3
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:$port/api/stats" -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) {
            Write-Host "dev dashboard healthy on :$port (pid=$(Get-DevPid))"
            exit 0
        }
    } catch { }
}

Write-Host "ERROR: dev dashboard did not become healthy within 90s"
Write-Host "--- dev_dashboard.err.log ---"
Get-Content "C:\Users\siddh\Code_Library\gdelt-events-dev\data\logs\dev_dashboard.err.log" -Tail 30 -ErrorAction SilentlyContinue
exit 1
