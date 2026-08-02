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
' Runs the SEC financials ingest hidden. Output appended to data/logs/sec_ingest.log.
Q = Chr(34)
py = "$py"
script = "$repo\pipeline\sec_ingest.py"
cmd = "cmd /c " & Q & " " & Q & py & Q & " -u " & Q & script & Q & " --daily --days-back 3" & Q
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
