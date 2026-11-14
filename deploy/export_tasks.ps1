# Re-export the scheduled tasks that run GDELT Monitor into deploy/tasks/.
#
# This directory is GENERATED OUTPUT, not a hand-maintained config. The live
# tasks on the Windows host are the source of truth; this script snapshots them
# so a destroyed host can be rebuilt. Never edit the XML in deploy/tasks/ by
# hand: a hand-edited export silently claims to describe a task that does not
# exist in that form (which is exactly what happened to GDELT-Ingest.xml, whose
# checked-in copy still said "wscript, hourly" months after the live task moved
# to "python, every 15 minutes").
#
# Run on the production host, from anywhere:
#
#   powershell -File deploy\export_tasks.ps1
#   git diff --stat deploy/tasks          # any diff here is real drift
#
# Read-only against the host: Export-ScheduledTask only reads definitions. It
# does not start, stop, register or modify any task.
#
# Note on logon types: the host re-renders StartBoundary offsets in the current
# standard-time offset, so a snapshot taken in winter and one taken in summer
# differ textually (-04:00 vs -05:00) while naming the same instant. Compare
# instants, not strings, before calling that drift.

[CmdletBinding()]
param(
    # Where the XML files go. Defaulted in the body, not here: PowerShell 5.1
    # evaluates a parameter default before $PSScriptRoot is populated when the
    # script is launched with `powershell -File ...`, so JoinPath threw
    # "empty string not allowed" for the documented no-argument invocation.
    [string]$OutDir = '',

    # Task-name regex. Both Ollama tasks are included on purpose: the host has
    # "Ollama-Serve" (registered by scripts\register_ollama_task.ps1) and a
    # second "OllamaServe" that nothing in this repo creates. Snapshotting both
    # keeps that duplication visible instead of hiding it.
    [string]$Filter = '^(GDELT-|Ollama)',

    # Keep XML files whose task no longer exists. Off by default so that a
    # retired task disappears from the snapshot instead of lingering as a file
    # that describes something nobody runs any more.
    [switch]$NoPrune
)

$ErrorActionPreference = 'Stop'

if (-not $OutDir) { $OutDir = Join-Path $PSScriptRoot 'tasks' }

if (-not (Get-Command Export-ScheduledTask -ErrorAction SilentlyContinue)) {
    throw 'Export-ScheduledTask not found - run this on the Windows host, not on Linux/macOS.'
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$selected = @(Get-ScheduledTask | Where-Object { $_.TaskName -match $Filter } | Sort-Object TaskName)
if ($selected.Count -eq 0) { throw "no scheduled tasks match '$Filter'" }

$written = @()
foreach ($task in $selected) {
    $xml = Export-ScheduledTask -TaskName $task.TaskName
    if (-not $xml) { throw "empty export for $($task.TaskName)" }

    # Normalise so the same task produces the same bytes on every export:
    # LF endings and a UTF-8 declaration matching the bytes actually written
    # (the Export cmdlet always declares UTF-16, which is only true if you save
    # it that way - UTF-16 in git shows up as an undiffable binary blob).
    $xml = $xml -replace 'encoding="UTF-16"', 'encoding="UTF-8"'
    $xml = $xml -replace "`r`n", "`n"
    $xml = $xml.TrimEnd("`n") + "`n"

    $path = Join-Path $OutDir ($task.TaskName + '.xml')
    [IO.File]::WriteAllText($path, $xml, (New-Object System.Text.UTF8Encoding $true))
    $written += $task.TaskName
    Write-Host ("  exported {0,-28} {1,5} chars  {2}" -f $task.TaskName, $xml.Length, $task.State)
}

if (-not $NoPrune) {
    foreach ($file in @(Get-ChildItem -Path $OutDir -Filter '*.xml')) {
        $name = [IO.Path]::GetFileNameWithoutExtension($file.Name)
        if ($written -notcontains $name) {
            Remove-Item $file.FullName -Force
            Write-Host "  pruned   $($file.Name)  (no matching task)"
        }
    }
}

Write-Host ""
Write-Host "$($written.Count) task definitions written to $OutDir"
Write-Host "Import one with: Register-ScheduledTask -Xml (Get-Content -Raw <file>) -TaskName <name>"
Write-Host "Do NOT use schtasks /create /xml on these files: it expects the UTF-16 export,"
Write-Host "and these are normalised to UTF-8 so that git can diff them."
