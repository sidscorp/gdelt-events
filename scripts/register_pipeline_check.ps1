# Register `GDELT-PipelineCheck` — a content-asserting health check every 15 minutes.
#
# Replaces GDELT-EmbedWatchdog, which watched the one-off bulk_embed.py backfill and
# disabled itself when that job completed (so "disabled" was its terminal state, not a
# fault). What was actually unmonitored is the incremental pipeline: a running embedder
# that fails every batch, or a dashboard serving HTTP 200 with no article cards, looks
# healthy to any process-alive check.
#
# S4U principal: the check must keep firing after an unattended reboot with no
# interactive login (the same reason Ollama-Serve exists).
#
# Run once, on the Windows host:  powershell -File scripts\register_pipeline_check.ps1

$ErrorActionPreference = 'Stop'
$taskName = 'GDELT-PipelineCheck'
$repo = 'C:\Users\siddh\Code_Library\gdelt-events'
$py = Join-Path $repo '.venv\Scripts\python.exe'
$module = 'pipeline.pipeline_health'
$vbs = 'C:\Users\siddh\bin\gdelt_pipeline_check_hidden.vbs'
$log = Join-Path $repo 'data\logs\pipeline_check.log'

if (-not (Test-Path $py)) { throw "Python not found: $py" }
New-Item -ItemType Directory -Force -Path (Split-Path $vbs) | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

# Task Scheduler does not interpret ">>" in Arguments, so redirect inside a wrapper.
@"
' GDELT pipeline health check. Emits HEALTHY:/PROBLEMS: and exits non-zero on failure.
Q = Chr(34)
cmd = "cmd /c " & Q & "cd /d " & Q & "$repo" & Q & " && " & Q & "$py" & Q & " -u -m $module --quiet >> " & Q & "$log" & Q & " 2>&1 " & Q
CreateObject("WScript.Shell").Run cmd, 0, False
"@ | Set-Content -Path $vbs -Encoding ASCII

$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$vbs`""

# :18 sits after the :07 embed run and the :13 ingest cycle, so the backlog sample is
# taken at a stable phase rather than mid-drain.
# then repeat every 15 minutes (the ingest interval).
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddHours(7).AddMinutes(18) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId (whoami).Trim() -LogonType S4U -RunLevel Limited

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "Content-asserting health check: Ollama serving, feed rendered with article cards, ingest and embed cycles fresh, embeddings keeping up. Exits non-zero on failure." | Out-Null

Write-Host "Registered '$taskName' (every 15 min, log: $log)"

# Prove the check runs and can report both outcomes before trusting the timer.
& $py -u -m $module --quiet
Write-Host "check exit: $LASTEXITCODE"
Start-ScheduledTask -TaskName $taskName
Start-Sleep 12
Get-ScheduledTaskInfo -TaskName $taskName | Select-Object LastRunTime, LastTaskResult | Format-List
