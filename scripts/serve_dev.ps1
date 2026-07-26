# Launcher for the isolated dev dashboard (port 8016, 7-day data slice).
#
# This exists because a scheduled-task action cannot set environment variables
# directly, and because Start-Process with -RedirectStandardOutput dies
# silently on this box (see CLAUDE.md gotchas). The dev instance therefore runs
# as the GDELT-Dashboard-Dev scheduled task, which execs this script.

$ErrorActionPreference = "Continue"

$devRoot = "C:\Users\siddh\Code_Library\gdelt-events-dev"
$python  = "C:\Users\siddh\Code_Library\gdelt-events\.venv\Scripts\python.exe"

$env:GDELT_DATA_DIR  = "$devRoot\data"
$env:GDELT_DASH_PORT = "8016"

New-Item -ItemType Directory -Force -Path "$devRoot\data\logs" | Out-Null

Set-Location "$devRoot\dashboard"
& $python -u "$devRoot\dashboard\serve.py" --port 8016
