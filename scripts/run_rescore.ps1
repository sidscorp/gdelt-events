# Judged pill backfill over the 2026-07-15..26 embedding-gap window.
#
# Writes to <cat>__v2 shadow categories only - nothing goes live until
# flip_pills.py promotes them, so a bad run is discarded rather than shipped.
#
# GDELT-EmbedNewArticles is disabled for the duration: it chains pill_scorer,
# which writes article_tags, and DuckDB is single-writer. The embedding
# backfill died this way once already. RE-ENABLING IT AFTERWARDS IS REQUIRED.

$ErrorActionPreference = "Continue"
$repo = "C:\Users\siddh\Code_Library\gdelt-events"
$py   = "$repo\.venv\Scripts\python.exe"
$log  = "$repo\data\logs\rescore_backfill.log"

Set-Location $repo
if (Test-Path $log) { Remove-Item $log -Force }

function Log($m) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m
    Write-Output $line
    Add-Content -Path $log -Value $line
}

Log "=== rescore start ==="

Log "disabling GDELT-EmbedNewArticles (avoids a second article_tags writer)"
Disable-ScheduledTask -TaskName "GDELT-EmbedNewArticles" -ErrorAction SilentlyContinue | Out-Null
Log ("  state = " + (Get-ScheduledTask -TaskName 'GDELT-EmbedNewArticles').State)

# Let any in-flight embed cycle finish before we start writing.
Start-Sleep -Seconds 20

Log "running: python -m pipeline.rescore_pills --days 14"
# *>&1 merges every PowerShell stream; Tee-Object gives us a log AND keeps the
# scheduled task's discarded stdout from being the only sink.
& $py -u -m pipeline.rescore_pills --days 14 *>&1 |
    Tee-Object -FilePath $log -Append
$rc = $LASTEXITCODE
Log "rescore_pills exited rc=$rc"

Log "re-enabling GDELT-EmbedNewArticles"
Enable-ScheduledTask -TaskName "GDELT-EmbedNewArticles" -ErrorAction SilentlyContinue | Out-Null
Log ("  state = " + (Get-ScheduledTask -TaskName 'GDELT-EmbedNewArticles').State)

Log "=== rescore done (rc=$rc) - shadow cats written, NOT yet flipped ==="
