<#
Atomic ownership for a committed production release. Existing lock files are
preserved; no automatic age-based takeover. Windows named mutex serializes
claim/assert/release/recover; exclusive CreateNew also protects initial creation.
SCP itself does not honor this lock: only the authorized release operator writes
production, after claim and version checks. Workers have no deployment authority.

claim <owner> <reason> -Token <unique-guid>
assert <owner> -Token <guid>
release <owner> -Token <guid>
recover -Force -ExpectedHash <SHA256>   # owner-approved abandoned-lock recovery
#>
param(
    [Parameter(Position=0)][ValidateSet('claim','assert','release','status','recover')][string]$Action='status',
    [Parameter(Position=1)][string]$Owner='',
    [Parameter(Position=2)][string]$Reason='',
    [string]$Token='',
    [switch]$Force,
    [string]$ExpectedHash='',
    [string]$RepoRoot=(Split-Path $PSScriptRoot -Parent)
)
$ErrorActionPreference='Stop'
$RepoRoot=[IO.Path]::GetFullPath($RepoRoot)
$Lock=Join-Path $RepoRoot 'data\.deploy_lock'
$sha=[Security.Cryptography.SHA256]::Create()
$identity=[BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($RepoRoot.ToLowerInvariant()))).Replace('-','')
$sha.Dispose()
$mutex=New-Object Threading.Mutex($false, "Global\GDELTDeploy-$identity")
$held=$false
$code=0
try {
    try { $held=$mutex.WaitOne(10000) } catch [Threading.AbandonedMutexException] { $held=$true }
    if (-not $held) { throw 'Deployment guard busy; retry after the active operation' }
    if ($Action -eq 'claim') {
        $parsed=[guid]::Empty
        if (-not $Owner -or -not [guid]::TryParse($Token,[ref]$parsed) -or $parsed -eq [guid]::Empty) {
            throw 'claim requires an owner and a unique nonempty GUID token'
        }
        if ($Force) { throw 'claim cannot force takeover; use explicit recover with the expected lock hash' }
        if (-not (Test-Path (Split-Path $Lock))) { New-Item -ItemType Directory -Path (Split-Path $Lock) | Out-Null }
        # Do not remove a partial lock on failure: interruption requires explicit recovery.
        $stream=[IO.File]::Open($Lock,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        try {
            $payload=@{owner=$Owner;token=$Token;reason=$Reason;taken_at=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json -Compress
            $bytes=[Text.Encoding]::UTF8.GetBytes($payload)
            $stream.Write($bytes,0,$bytes.Length)
            $stream.Flush($true)
        } finally { $stream.Dispose() }
        Write-Output 'CLAIMED'
    } elseif ($Action -eq 'recover') {
        if (-not $Force -or $ExpectedHash -notmatch '^[a-fA-F0-9]{64}$') { throw 'recover requires -Force and the inspected lock SHA256' }
        if (-not (Test-Path $Lock)) { throw 'No lock to recover' }
        $actual=(Get-FileHash $Lock -Algorithm SHA256).Hash
        if ($actual -ne $ExpectedHash) { throw 'Lock changed since inspection; recovery refused' }
        Remove-Item $Lock
        Write-Output 'RECOVERED; acquire a new token before deployment'
    } else {
        if (-not (Test-Path $Lock)) {
            if ($Action -ne 'status') { throw 'No deployment lock held' }
            Write-Output 'LOCK none'
        } else {
            $record=Get-Content $Lock -Raw | ConvertFrom-Json
            if (-not $record.owner -or -not $record.token) { throw 'Invalid or legacy lock; explicit recovery required' }
            if ($Action -eq 'status') {
                Write-Output "LOCK held owner=$($record.owner) taken_at=$($record.taken_at)"
            } else {
                if (-not $Owner -or -not $Token -or $record.owner -cne $Owner -or $record.token -cne $Token) {
                    throw 'Deployment ownership mismatch'
                }
                if ($Action -eq 'release') { Remove-Item $Lock; Write-Output 'RELEASED' }
                else { Write-Output 'OWNERSHIP_OK' }
            }
        }
    }
} catch {
    Write-Output "REFUSED: $($_.Exception.Message)"
    $code=1
} finally {
    if ($held) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
exit $code
