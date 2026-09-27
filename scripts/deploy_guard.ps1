<#
Deploy guard for the shared production tree.

Several agents (claude-code, OpenCode/DeepSeek, ...) deploy into this ONE working
tree by scp, with no coordination. On 2026-08-02 two of them overwrote each other
ten minutes apart: the second reverted a completed renderer fix and left
routes/pages.py importing a briefing._normalize_text that no longer existed, so
gdeltmonitor.com returned 500 until it was restored. Nothing was permanently lost
only because the work had already been committed to a branch.

This cannot make scp safe - scp does not ask permission. What it does is make a
collision LOUD and make the tree's real state visible before you overwrite it:

  status   who holds the lock, what is uncommitted, how far the tree has drifted
  claim    take the lock; refuses if someone else holds one younger than 45 min
  release  give it back

restart_dash.ps1 calls `status` on entry and shouts if a foreign lock is held,
because every deploy ends in a restart - the one choke point every agent uses.

Usage:
  powershell -File scripts\deploy_guard.ps1 status
  powershell -File scripts\deploy_guard.ps1 claim claude-code "briefing renderer"
  powershell -File scripts\deploy_guard.ps1 release claude-code
  ... add -Force to steal a lock you are sure is abandoned.
#>
param(
  [Parameter(Position = 0)][ValidateSet('claim', 'release', 'status')][string]$Action = 'status',
  [Parameter(Position = 1)][string]$Owner = '',
  [Parameter(Position = 2)][string]$Reason = '',
  [switch]$Force
)

$Repo = 'C:\Users\siddh\Code_Library\gdelt-events'
$Lock = Join-Path $Repo 'data\.deploy_lock'
$StaleMinutes = 45

function Read-Lock {
  if (-not (Test-Path $Lock)) { return $null }
  try { return Get-Content $Lock -Raw | ConvertFrom-Json } catch { return $null }
}

function Lock-AgeMin($l) {
  if (-not $l) { return $null }
  return [math]::Round(((Get-Date) - [datetime]$l.taken_at).TotalMinutes, 1)
}

function Show-Status {
  $l = Read-Lock
  if ($l) {
    $age = Lock-AgeMin $l
    $state = if ($age -gt $StaleMinutes) { 'STALE' } else { 'ACTIVE' }
    Write-Output "LOCK $state  owner=$($l.owner)  age=${age}m  reason=$($l.reason)"
  }
  else {
    Write-Output 'LOCK none'
  }

  Push-Location $Repo
  try {
    $dirty = @(git status --short 2>$null)
    if ($dirty.Count) {
      Write-Output "UNCOMMITTED $($dirty.Count) file(s) - commit before deploying, or a"
      Write-Output "            collision will bury work whose rationale nobody recorded:"
      $dirty | Select-Object -First 12 | ForEach-Object { Write-Output "   $_" }
    }
    else {
      Write-Output 'UNCOMMITTED none - tree is clean'
    }
    $branch = git rev-parse --abbrev-ref HEAD 2>$null
    $head = git rev-parse --short HEAD 2>$null
    Write-Output "BRANCH $branch @ $head"
  }
  finally { Pop-Location }
}

switch ($Action) {

  'status' { Show-Status }

  'claim' {
    if (-not $Owner) { Write-Output 'ERROR: claim needs an owner, e.g. claim claude-code "reason"'; exit 2 }
    $l = Read-Lock
    if ($l -and -not $Force) {
      $age = Lock-AgeMin $l
      if ($l.owner -ne $Owner -and $age -le $StaleMinutes) {
        Write-Output "REFUSED: '$($l.owner)' has held the deploy lock for ${age}m"
        Write-Output "         reason: $($l.reason)"
        Write-Output "         Another agent is probably mid-deploy. Wait, coordinate, or"
        Write-Output "         re-run with -Force if you are certain it is abandoned."
        exit 1
      }
      if ($l.owner -ne $Owner) {
        Write-Output "note: taking over a STALE lock from '$($l.owner)' (${age}m old)"
      }
    }
    $payload = @{
      owner = $Owner; reason = $Reason
      taken_at = (Get-Date).ToString('o'); host = $env:COMPUTERNAME
    } | ConvertTo-Json -Compress
    Set-Content -Path $Lock -Value $payload -Encoding UTF8
    Write-Output "CLAIMED by $Owner"
    Show-Status
  }

  'release' {
    $l = Read-Lock
    if (-not $l) { Write-Output 'no lock held'; break }
    if ($l.owner -ne $Owner -and -not $Force) {
      Write-Output "REFUSED: lock belongs to '$($l.owner)', not '$Owner'. Use -Force to override."
      exit 1
    }
    Remove-Item $Lock -Force
    Write-Output "RELEASED (was $($l.owner))"
  }
}
