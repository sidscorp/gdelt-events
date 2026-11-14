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

# A second task that also runs `ollama serve` holds :11434 and makes this one exit
# immediately (found 2026-11-14: an out-of-repo "OllamaServe" was doing the serving).
$legacy = 'OllamaServe'
if (Get-ScheduledTask -TaskName $legacy -ErrorAction SilentlyContinue) {
    Write-Warning ("Task '$legacy' also runs ollama serve and will hold :11434. " +
        "Stop and unregister it deliberately: Stop-ScheduledTask -TaskName $legacy; " +
        "Unregister-ScheduledTask -TaskName $legacy -Confirm:`$false")
}

# Bind every interface, not loopback. The LiteLLM gateway reaches this server at its
# Tailscale address (api_base http://100.127.16.82:11434/v1), so a loopback-only bind
# silently breaks every ollama/* alias there - and the failure looks like a gateway
# timeout, not a missing env var. Task Scheduler cannot set environment variables
# directly, so a cmd wrapper does it. There must be NO space before '&&': a space
# makes the value "0.0.0.0 " and Ollama rejects it.
$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c set OLLAMA_HOST=0.0.0.0&& `"$exe`" serve"

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
