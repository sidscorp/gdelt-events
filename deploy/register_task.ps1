# Register the GDELT ingest as a Windows Scheduled Task (runs every 15 minutes).
# Run as Administrator.

$repoDir = "C:\Users\siddh\Code_Library\gdelt-events"
$python = "$repoDir\.venv\Scripts\python.exe"
$script = "$repoDir\gdelt_ingest.py"
$taskName = "GDELT-Ingest"
$logDir = "$repoDir\data\logs"

# Ensure log dir exists
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# Build the action — run python with the ingest script
$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "`"$script`"" `
    -WorkingDirectory $repoDir

# Trigger: every 15 minutes, starting now, running for 1000 days
# (Task Scheduler rejects TimeSpan.MaxValue as "out of range")
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Days 1000)

# Settings: don't wake computer, don't run on battery restrictions, allow start if missed
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew

# Principal: run as current user, only when logged in
$currentUser = (whoami).Trim()
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited

# Remove old task if it exists
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

# Register the new task
Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "GDELT v2 data ingest every 15 minutes"

Write-Host "Scheduled task '$taskName' registered."
Get-ScheduledTask -TaskName $taskName | Format-List TaskName, State, URI
