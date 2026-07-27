# Register `ollama serve` as a Windows scheduled task.
#
# Why this exists: Ollama was only ever started by a per-user Startup shortcut
# (Ollama.lnk), so it ran only when someone logged in interactively. After a
# reboot with no interactive login it silently stayed down from 2026-07-15 to
# 2026-07-26 - eleven days in which every embed batch failed, the semantic
# search endpoint 503'd, and pill_scorer judged nothing. Nothing alerted,
# because GDELT-EmbedWatchdog watches the embed task, not Ollama.
#
# Run once. Elevation not required (S4U principal, Limited run level).

$taskName = "Ollama-Serve"
$exe = "C:\Users\siddh\AppData\Local\Programs\Ollama\ollama.exe"

if (-not (Test-Path $exe)) { throw "ollama.exe not found at $exe" }

$action = New-ScheduledTaskAction -Execute $exe -Argument "serve"

# At startup AND at logon: startup covers unattended reboots, logon covers the
# case where the machine is already up and the task host has not fired.
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

$currentUser = (whoami).Trim()
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType S4U -RunLevel Limited

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger @($trigger1, $trigger2) `
    -Settings $settings `
    -Principal $principal `
    -Description "Ollama API server on :11434. Feeds article embedding (nomic-embed-text:v1.5), semantic search, and judge-gated pill scoring." | Out-Null

Write-Host "Registered '$taskName' (at startup + at logon)."

Start-ScheduledTask -TaskName $taskName
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 3
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:11434/api/tags" -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) {
            Write-Host "ollama healthy on :11434"
            exit 0
        }
    } catch { }
}
Write-Host "ERROR: ollama did not answer on :11434 within 90s"
exit 1
