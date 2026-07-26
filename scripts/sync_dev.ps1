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

git archive $Ref | tar -x -C $tmp
if ($LASTEXITCODE -ne 0) { throw "git archive failed for $Ref" }

foreach ($sub in @("dashboard", "pipeline", "tests", "scripts", "deploy")) {
    $src = Join-Path $tmp $sub
    if (-not (Test-Path $src)) { continue }
    $dst = Join-Path $dev $sub
    if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
    Copy-Item $src $dst -Recurse -Force
    Write-Host "  synced $sub"
}

foreach ($f in @("gdelt_ingest.py", "config.py", "CLAUDE.md", "requirements.txt")) {
    $src = Join-Path $tmp $f
    if (Test-Path $src) { Copy-Item $src (Join-Path $dev $f) -Force }
}

Remove-Item $tmp -Recurse -Force
Set-Content -Path (Join-Path $dev "DEV_REF.txt") -Value "$Ref $sha $(Get-Date -Format o)" -Encoding ascii
Write-Host "dev tree now at $Ref ($sha)"

if (-not $NoRestart) {
    & (Join-Path $dev "scripts\restart_dev.ps1")
    exit $LASTEXITCODE
}
