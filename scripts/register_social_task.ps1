# Register the review-first Bluesky social publisher.
#
# The worker reads DuckDB in read-only mode, writes only data\social.db, and
# publishes at most one already-approved event per run.  S4U is load-bearing:
# Interactive scheduled tasks stop firing after unattended Windows reboots.

$ErrorActionPreference = 'Stop'
$taskName = 'GDELT-SocialPublisher'
$repo = 'C:\Users\siddh\Code_Library\gdelt-events'
$py = Join-Path $repo '.venv\Scripts\python.exe'
$script = Join-Path $repo 'pipeline\social_publisher.py'
$vbs = 'C:\Users\siddh\bin\gdelt_social_hidden.vbs'
$log = Join-Path $repo 'data\logs\social_task.log'

if (-not (Test-Path $py)) { throw "Python not found: $py" }
if (-not (Test-Path $script)) { throw "Publisher not found: $script" }
New-Item -ItemType Directory -Force -Path (Split-Path $vbs) | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

@"
' GDELT Monitor social publisher. Python emits a HEALTHY:/PROBLEMS: contract.
Q = Chr(34)
cmd = "cmd /c " & Q & Q & "$py" & Q & " -u " & Q & "$script" & Q & _
      " >> " & Q & "$log" & Q & " 2>&1" & Q
CreateObject("WScript.Shell").Run cmd, 0, False
"@ | Set-Content -Path $vbs -Encoding ASCII

$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$vbs`""
$start = (Get-Date).AddMinutes(2)
# Task Scheduler rejects TimeSpan.MaxValue as an out-of-range ISO duration.
# Ten years is operationally indefinite while remaining valid task XML.
$repeatDuration = New-TimeSpan -Days 3650
$trigger = New-ScheduledTaskTrigger -Once -At $start `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration $repeatDuration
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 12) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 2)
$currentUser = (whoami).Trim()
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType S4U -RunLevel Limited

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description 'Review-first, source-grounded Bluesky event signals for GDELT Monitor.' | Out-Null

$task = Get-ScheduledTask -TaskName $taskName
if ($task.Principal.LogonType -ne 'S4U') {
    throw "Registration did not preserve S4U; actual=$($task.Principal.LogonType)"
}
Write-Output "registered: $taskName state=$($task.State) logon=$($task.Principal.LogonType)"
Write-Output "next run: $((Get-ScheduledTaskInfo -TaskName $taskName).NextRunTime)"

# Populate the review queue now instead of waiting for the first recurrence.
Start-ScheduledTask -TaskName $taskName
