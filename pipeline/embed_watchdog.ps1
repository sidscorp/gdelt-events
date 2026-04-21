# Watchdog for bulk_embed.py — restarts if progress stalls.
# Run via Windows Task Scheduler every 10 min.
# Logic: if progress.json hasn't been updated in 5+ minutes and
# embedding isn't complete, the process crashed. Restart it.

$VenvPython = "C:\Users\siddh\Code_Library\gdelt-events\.venv\Scripts\python.exe"
$Script = "C:\Users\siddh\Code_Library\gdelt-events\pipeline\bulk_embed.py"
$LogDir = "C:\Users\siddh\Code_Library\gdelt-events\data\logs"
$ProgressFile = "C:\Users\siddh\Code_Library\gdelt-events\data\embeddings\progress.json"

# No progress file = hasn't started or was cleaned up
if (-not (Test-Path $ProgressFile)) {
    Write-Output "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') No progress file, skipping"
    exit 0
}

$progress = Get-Content $ProgressFile | ConvertFrom-Json

# Already complete
if ($progress.completed -eq $true) {
    Write-Output "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Embedding complete ($($progress.lines_done) articles). Disabling watchdog."
    Disable-ScheduledTask -TaskName 'GDELT-EmbedWatchdog' -ErrorAction SilentlyContinue
    exit 0
}

# Check how recently progress was updated
$lastUpdate = [datetime]::ParseExact($progress.updated_at, 'yyyy-MM-dd HH:mm:ss', $null)
$staleMins = ((Get-Date) - $lastUpdate).TotalMinutes
$done = $progress.lines_done
$total = $progress.total
$pct = [math]::Round($done / $total * 100, 1)

if ($staleMins -lt 5) {
    Write-Output "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Embedding active: $done / $total ($pct%), updated ${staleMins}min ago"
    exit 0
}

# Stale — process likely crashed. Restart.
Write-Output "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Embedding stalled at $done / $total ($pct%), last update ${staleMins}min ago. Restarting..."

Start-Process -FilePath $VenvPython `
    -ArgumentList "-u", $Script, "--embed-only" `
    -RedirectStandardOutput "$LogDir\bulk_embed.log" `
    -RedirectStandardError "$LogDir\bulk_embed_err.log" `
    -WindowStyle Hidden

Write-Output "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Restarted bulk_embed (resuming from $done)"
