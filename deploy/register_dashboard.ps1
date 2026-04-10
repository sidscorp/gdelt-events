# Register the GDELT dashboard as a Windows Scheduled Task that runs at startup
# and stays running. Run as Administrator.

$repoDir = "C:\Users\siddh\Code_Library\gdelt-events"
$python = "$repoDir\.venv\Scripts\python.exe"
$script = "$repoDir\dashboard\serve.py"
$taskName = "GDELT-Dashboard"
$logDir = "$repoDir\data\logs"

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "`"$script`"" `
    -WorkingDirectory "$repoDir\dashboard"

# Run at system startup and when user logs on
$trigger1 = New-ScheduledTaskTrigger -AtStartup
$trigger2 = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger @($trigger1, $trigger2) `
    -Settings $settings `
    -Principal $principal `
    -Description "GDELT News Dashboard — Flask app via waitress on port 8015"

Write-Host "Dashboard scheduled task '$taskName' registered."

# Also start it now
Start-ScheduledTask -TaskName $taskName
Write-Host "Started dashboard task."
