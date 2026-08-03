# Registers GDELT-SecIngest: the daily incremental refresh of SEC financials.
#
# Runs at 06:40 local. SEC posts the previous day's filing index overnight, and
# this sits after the other morning jobs rather than on top of them. It reads the
# ~1.1 MB daily index, takes the CIKs that filed a 10-K/10-Q, and refetches only
# those - typically ~180 filers, about 90 seconds. It never re-downloads the bulk
# archive; --backfill is a manual, one-off operation.
#
# --days-back 3 so a missed run (or a weekend, when SEC publishes no index)
# self-heals on the next successful one instead of leaving a permanent hole.

$name = 'GDELT-SecIngest'
$py   = 'C:\Users\siddh\Code_Library\gdelt-events\.venv\Scripts\python.exe'
$repo = 'C:\Users\siddh\Code_Library\gdelt-events'
$vbs  = 'C:\Users\siddh\bin\gdelt_sec_ingest_hidden.vbs'

# VBS shim so the task never flashes a console window (house convention here).
@"
' Runs the SEC financials ingest hidden. Both stages log to data/logs/sec_ingest.log
' themselves; sec_task.log catches whatever escapes Python logging entirely.
Q = Chr(34)
py = "$py"
script = "$repo\pipeline\sec_ingest.py"
derive = "$repo\pipeline\sec_derive.py"
logf = "$repo\data\logs\sec_task.log"
' Ingest THEN derive: the observations, growth rates and sector percentiles are
' computed from the snapshots, so a refresh that skips the derive step leaves the
' page showing new numbers with stale context.
'
' Derive is called by ABSOLUTE PATH, never "-m pipeline.sec_derive". A scheduled task
' inherits C:\Windows\System32 as its working directory, so -m cannot find the package
' and the stage died on import with ModuleNotFoundError - silently, because nothing
' redirected its output and ingest_log only recorded the ingest. Every other wrapper in
' this directory calls its script by absolute path; sec_derive.py puts the repo root on
' sys.path itself. The parentheses put BOTH stages inside the redirect, so a failure
' that happens before logging is configured still lands somewhere readable.
cmd = "cmd /c " & Q & " ( " & Q & py & Q & " -u " & Q & script & Q & " --daily --days-back 3" & _
      " && " & Q & py & Q & " -u " & Q & derive & Q & " ) >> " & Q & logf & Q & " 2>&1 " & Q
CreateObject("WScript.Shell").Run cmd, 0, False
"@ | Set-Content -Path $vbs -Encoding ASCII

Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue

$action  = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$vbs`""
$trigger = New-ScheduledTaskTrigger -Daily -At 06:40
$set     = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
    -Settings $set -Description 'Daily SEC financials refresh (targeted by filing index)' | Out-Null

$t = Get-ScheduledTask -TaskName $name
Write-Output "registered: $name  state=$($t.State)"
Write-Output "next run  : $((Get-ScheduledTaskInfo -TaskName $name).NextRunTime)"
