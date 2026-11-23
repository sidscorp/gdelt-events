# Set the GDELT-BriefingPrewarm cadence, and record the live definition in
# deploy/tasks/GDELT-BriefingPrewarm.xml.
#
# Issue #71: the task ran five fixed daily triggers (23/07/11/15/19 local) with an
# 8-hour overnight gap while the 3h briefing's TTL was 3 hours, so every night the
# reader's freshness line promised an update the schedule could not make. It now
# repeats every 3 hours (8/day). Only the TRIGGERS change; the action
# (wscript.exe gdelt_prewarm_hidden.vbs), the S4U principal and the settings are
# preserved.
#
#   powershell -File scripts\set_prewarm_cadence.ps1 -DryRun          # print only
#   powershell -File scripts\set_prewarm_cadence.ps1 -Hours 3         # 8/day
#
# Rollback to the pre-#71 schedule: five daily triggers at 23,07,11,15,19 local.
param(
    [int]$Hours = 3,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
if ($Hours -lt 1 -or $Hours -gt 24) { throw 'Hours must be between 1 and 24' }
$name = 'GDELT-BriefingPrewarm'
$root = Split-Path $PSScriptRoot -Parent
$record = Join-Path $root 'deploy\tasks\GDELT-BriefingPrewarm.xml'

function Show-Schedule($label) {
    $task = Get-ScheduledTask -TaskName $name
    $info = Get-ScheduledTaskInfo -TaskName $name
    Write-Output ($label + ': state=' + $task.State + ' principal=' + $task.Principal.LogonType +
        ' triggers=' + $task.Triggers.Count + ' last=' + $info.LastRunTime + ' next=' + $info.NextRunTime)
    foreach ($tr in $task.Triggers) {
        Write-Output ('  ' + $tr.CimClass.CimClassName + ' start=' + $tr.StartBoundary +
            ' interval=' + $tr.Repetition.Interval + ' duration=[' + $tr.Repetition.Duration + ']' +
            ' days=' + $tr.DaysInterval)
    }
    Write-Output ('  action=' + $task.Actions[0].Execute + ' ' + $task.Actions[0].Arguments)
}

Show-Schedule 'BEFORE'
if ($DryRun) { Write-Output 'DryRun: no change.'; exit 0 }

# Task Scheduler stores local times, so anchor the repetition at local midnight.
# Windows refuses [TimeSpan]::MaxValue here ("P99999999DT23H59M59S ... out of
# range"), so the repetition duration is ten years rather than indefinite.
$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).Date) `
    -RepetitionInterval (New-TimeSpan -Hours $Hours) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
Set-ScheduledTask -TaskName $name -Trigger $trigger | Out-Null

Show-Schedule 'AFTER'

$xml = Export-ScheduledTask -TaskName $name
[IO.File]::WriteAllText($record, $xml, (New-Object System.Text.UTF8Encoding($true)))
Write-Output ('recorded live definition in ' + $record)
