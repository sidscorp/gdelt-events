# Sync a git ref from the prod checkout into the dev tree, then restart dev.
#
#   scripts\sync_dev.ps1 -Ref origin/pull/12/head
#   scripts\sync_dev.ps1 -Ref workspace
#
# Only CODE is synced. The dev tree keeps its own data/ (the 7-day slice, its
# own users.db and keys) - that separation is the whole point of the instance,
# and DuckDB is single-writer so the two must never share a database file.

param(
    [Parameter(Mandatory = $true)][string]$Ref,
    [switch]$NoRestart
)

$ErrorActionPreference = "Stop"

$prod = "C:\Users\siddh\Code_Library\gdelt-events"
$dev  = "C:\Users\siddh\Code_Library\gdelt-events-dev"

Set-Location $prod

git rev-parse --verify "$Ref^{commit}" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "ref not found: $Ref" }
$sha = (git rev-parse --short "$Ref^{commit}").Trim()
Write-Host "syncing $Ref ($sha) -> dev tree"

# git archive gives us exactly the tracked files at that ref, with no risk of
# dragging along the prod working tree or its data/ directory.
$tmp = Join-Path $env:TEMP "gdelt-sync-$sha"
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
New-Item -ItemType Directory -Path $tmp | Out-Null

# Write the tarball to disk rather than piping it. PowerShell pipes are not
# binary-safe - `git archive | tar -x` mangles the stream and tar reports
# "Damaged tar archive (bad header checksum)".
$tar = Join-Path $env:TEMP "gdelt-sync-$sha.tar"
if (Test-Path $tar) { Remove-Item $tar -Force }

git archive --format=tar -o $tar $Ref
if ($LASTEXITCODE -ne 0) { throw "git archive failed for $Ref" }

tar -x -f $tar -C $tmp
if ($LASTEXITCODE -ne 0) { throw "tar extract failed for $Ref" }
Remove-Item $tar -Force

# Guard against a silently empty extract - without this the script would
# happily restart dev against nothing and report success.
$extracted = @(Get-ChildItem $tmp -Recurse -File).Count
if ($extracted -lt 50) { throw "extract produced only $extracted files - refusing to sync" }
Write-Host "  extracted $extracted files"

# Stop dev before swapping files: serve_dev.ps1 sets its working directory to
# the dev dashboard/ folder, so Windows locks that directory while it runs and
# the swap fails with a sharing violation.
Write-Host "  stopping dev before file swap"
Stop-ScheduledTask -TaskName "GDELT-Dashboard-Dev" -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
$listener = Get-NetTCPConnection -LocalPort 8016 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    Stop-Process -Id $listener.OwningProcess -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
}

# Mirror in place with robocopy rather than delete-then-copy. serve_dev.ps1
# sets its working directory to the dev dashboard/ folder, and Windows keeps a
# lock on a directory that is any process's cwd even after that process is
# asked to stop - so Remove-Item on the directory fails with a sharing
# violation. Individual files are not locked, so /MIR overwrites them happily.
$synced = 0
foreach ($sub in @("dashboard", "pipeline", "tests", "scripts", "deploy")) {
    $src = Join-Path $tmp $sub
    if (-not (Test-Path $src)) { continue }
    $dst = Join-Path $dev $sub
    # /MIR mirrors (including deletions), /NFL /NDL /NJH /NJS /NP quiet it down.
    robocopy $src $dst /MIR /NFL /NDL /NJH /NJS /NP /R:2 /W:1 | Out-Null
    # robocopy exit codes below 8 are success; 8+ means at least one failure.
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed for $sub (exit $LASTEXITCODE)" }
    $synced++
    Write-Host "  synced $sub"
}

foreach ($f in @("gdelt_ingest.py", "config.py", "CLAUDE.md", "requirements.txt")) {
    $src = Join-Path $tmp $f
    if (Test-Path $src) { Copy-Item $src (Join-Path $dev $f) -Force }
}

Remove-Item $tmp -Recurse -Force
if ($synced -lt 3) { throw "only $synced source dirs synced - refusing to continue" }

Set-Content -Path (Join-Path $dev "DEV_REF.txt") -Value "$Ref $sha $(Get-Date -Format o)" -Encoding ascii
Write-Host "dev tree now at $Ref ($sha)"

if (-not $NoRestart) {
    & (Join-Path $dev "scripts\restart_dev.ps1")
    exit $LASTEXITCODE
}
