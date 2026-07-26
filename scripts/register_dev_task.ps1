# Register the dev dashboard (port 8016) as an on-demand scheduled task.
# Run as Administrator, once. Mirrors deploy/register_dashboard.ps1 but with
# NO startup trigger - dev is started explicitly by CI or by hand.

$taskName = "GDELT-Dashboard-Dev"
$launcher = "C:\Users\siddh\Code_Library\gdelt-events-dev\scripts\serve_dev.ps1"

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-ExecutionPolicy Bypass -NonInteractive -File `"$launcher`"" `
    -WorkingDirectory "C:\Users\siddh\Code_Library\gdelt-events-dev"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

$currentUser = (whoami).Trim()
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType S4U `
    -RunLevel Limited

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Settings $settings `
    -Principal $principal `
    -Description "GDELT dev dashboard - waitress on port 8016 against the 7-day data slice. On-demand only."

Write-Host "Registered '$taskName' (on-demand, no trigger)."
Write-Host "Start with: scripts\restart_dev.ps1"
