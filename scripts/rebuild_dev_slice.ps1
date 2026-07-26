# Rebuild the dev 7-day data slice, end to end.
#
# The dev dashboard MUST be stopped first: DuckDB is single-writer and the
# running dashboard holds the dev database open.
#
# Intended to be run as a one-shot scheduled task (ssh-spawned processes on this
# box survive as unkillable orphans); see scripts/kick_rebuild.ps1.

$ErrorActionPreference = "Continue"
$log = "C:\Users\siddh\Code_Library\gdelt-events-dev\data\logs\rebuild_slice.log"

function Log($m) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m
    Write-Host $line
    Add-Content -Path $log -Value $line
}

Log "=== rebuild start ==="

Log "stopping dev dashboard (releases the dev DuckDB write lock)"
Stop-ScheduledTask -TaskName "GDELT-Dashboard-Dev" -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3
$c = Get-NetTCPConnection -LocalPort 8016 -State Listen -ErrorAction SilentlyContinue
if ($c) { Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 3 }

$py = "C:\Users\siddh\Code_Library\gdelt-events\.venv\Scripts\python.exe"
$script = "C:\Users\siddh\Code_Library\gdelt-events-dev\scripts\build_dev_snapshot.py"

$env:DEV_DAYS = "7"
Log "running build_dev_snapshot.py (DEV_DAYS=7)"
& $py -u $script 2>&1 | ForEach-Object { Add-Content -Path $log -Value $_ }
$rc = $LASTEXITCODE
Log "build_dev_snapshot.py exited rc=$rc"

Log "restarting dev dashboard"
& "C:\Users\siddh\Code_Library\gdelt-events-dev\scripts\restart_dev.ps1" 2>&1 | ForEach-Object { Add-Content -Path $log -Value $_ }

Log "=== rebuild done (rc=$rc) ==="
