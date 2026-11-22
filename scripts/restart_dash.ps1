# Restart the production dashboard on :8015 and wait until it answers.
#
# The previous restart_dash.ps1 (renamed to ~\restart_dash.ps1.bak_20260802 and
# never replaced, so the path the gdelt-ops skill documents did not exist) killed
# EVERY python.exe on the box. That takes down the dev instance, any running
# ingest, the embedder and Ollama alongside the thing it means to restart, and it
# is why the skill carries a warning never to point it at dev.
#
# This stops only the prod scheduled task and whatever holds port 8015, mirroring
# scripts/restart_dev.ps1. Dev is left alone.

param(
    [Parameter(Mandatory=$true)][string]$Owner,
    [Parameter(Mandatory=$true)][string]$Token
)
$ErrorActionPreference = "Stop"
$lease = $null
try {
    # Holding a read handle without delete/write sharing prevents release/recovery
    # while a restart is in progress. Verify owner before touching any service.
    $lockPath = Join-Path (Split-Path $PSScriptRoot -Parent) 'data\.deploy_lock'
    $lease = [IO.File]::Open($lockPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    & (Join-Path $PSScriptRoot 'deploy_guard.ps1') assert -Owner $Owner -Token $Token
    if ($LASTEXITCODE -ne 0) { throw 'Restart refused: deployment ownership not verified' }
$taskName = "GDELT-Dashboard"
$port     = 8015

function Get-DashPid {
    $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($c) { return $c.OwningProcess } else { return $null }
}

Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

# The task host does not always reap the python child; clear the port directly.
$dashPid = Get-DashPid
if ($dashPid) {
    Write-Host "stopping stale listener pid=$dashPid"
    Stop-Process -Id $dashPid -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

Start-ScheduledTask -TaskName $taskName

# Poll for health rather than sleeping a fixed amount - the cold DuckDB attach on
# the 40GB prod database is not fast.
$deadline = (Get-Date).AddSeconds(120)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 3
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:$port/api/stats" -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) {
            Write-Host "prod dashboard healthy on :$port (pid=$(Get-DashPid))"
            exit 0
        }
    } catch { }
}

Write-Host "ERROR: prod dashboard did not become healthy within 120s"
Write-Host "--- dashboard.log ---"
Get-Content "$env:USERPROFILE\Code_Library\gdelt-events\data\logs\dashboard.log" -Tail 30 -ErrorAction SilentlyContinue
exit 1

} catch {
    Write-Output "Restart failed: $($_.Exception.Message)"
    exit 1
} finally {
    if ($lease) { $lease.Dispose() }
}
