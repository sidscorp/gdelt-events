# Isolated Windows contract tests. Restart service commands are mocked; no real tasks.
param([string]$Guard=(Join-Path $PSScriptRoot 'deploy_guard.ps1'),
      [string]$Restart=(Join-Path $PSScriptRoot 'restart_dash.ps1'))
$ErrorActionPreference='Stop'
$testRoot=Join-Path ([IO.Path]::GetTempPath()) ('gdelt-lock-test-'+[guid]::NewGuid().ToString())
New-Item -ItemType Directory $testRoot | Out-Null
$token=[guid]::NewGuid().ToString()
$lock=Join-Path $testRoot 'data\.deploy_lock'
function Expect([bool]$Pass,[string]$Message) { if (-not $Pass) { throw $Message } }
try {
    & $Guard claim test 'fixture' -Token $token -RepoRoot $testRoot
    Expect ($LASTEXITCODE -eq 0) 'claim failed'
    & $Guard claim test 'duplicate' -Token $token -RepoRoot $testRoot
    Expect ($LASTEXITCODE -ne 0) 'duplicate claim accepted'
    & $Guard release test -Token 'wrong' -RepoRoot $testRoot
    Expect ($LASTEXITCODE -ne 0 -and (Test-Path $lock)) 'wrong token released lock'
    & $Guard assert test -Token $token -RepoRoot $testRoot
    Expect ($LASTEXITCODE -eq 0) 'correct owner rejected'
    $reader=[IO.File]::Open($lock,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    try {
        & $Guard release test -Token $token -RepoRoot $testRoot
        Expect ($LASTEXITCODE -ne 0 -and (Test-Path $lock)) 'active restart lease could be deleted'
    } finally { $reader.Dispose() }
    & $Guard release test -Token $token -RepoRoot $testRoot
    Expect ($LASTEXITCODE -eq 0 -and -not (Test-Path $lock)) 'release failed'
    Set-Content $lock '{bad fixture'
    & $Guard status -RepoRoot $testRoot
    Expect ($LASTEXITCODE -ne 0) 'malformed lock accepted'
    & $Guard recover -Force -ExpectedHash ('0'*64) -RepoRoot $testRoot
    Expect ($LASTEXITCODE -ne 0 -and (Test-Path $lock)) 'wrong recovery hash accepted'
    $hash=(Get-FileHash $lock -Algorithm SHA256).Hash
    & $Guard recover -Force -ExpectedHash $hash -RepoRoot $testRoot
    Expect ($LASTEXITCODE -eq 0) 'explicit recovery failed'
    # Exercise restart gates with a poisoned service command in this isolated
    # process: even the authorized case cannot touch a real scheduled task.
    $scripts=Join-Path $testRoot 'scripts'
    New-Item -ItemType Directory $scripts | Out-Null
    Copy-Item $Guard (Join-Path $scripts 'deploy_guard.ps1')
    Copy-Item $Restart (Join-Path $scripts 'restart_dash.ps1')
    $global:ServiceCalls=0
    function global:Stop-ScheduledTask { $global:ServiceCalls++; throw 'SYNTHETIC_SERVICE_BOUNDARY' }
    & (Join-Path $scripts 'restart_dash.ps1') -Owner test -Token $token
    Expect ($LASTEXITCODE -ne 0 -and $global:ServiceCalls -eq 0) 'unlocked restart touched service'
    & $Guard claim test fixture -Token $token -RepoRoot $testRoot
    Expect ($LASTEXITCODE -eq 0) 'restart fixture claim failed'
    & (Join-Path $scripts 'restart_dash.ps1') -Owner test -Token 'wrong'
    Expect ($LASTEXITCODE -ne 0 -and $global:ServiceCalls -eq 0) 'wrong-owner restart touched service'
    & (Join-Path $scripts 'restart_dash.ps1') -Owner test -Token $token
    Expect ($LASTEXITCODE -ne 0 -and $global:ServiceCalls -eq 1) 'authorized restart did not reach mocked service'
    & $Guard release test -Token $token -RepoRoot $testRoot
    Expect ($LASTEXITCODE -eq 0) 'restart did not dispose its lease'
    $jobs=@()
    foreach ($i in 1..8) {
        $jobs += Start-Job -ArgumentList $Guard,$testRoot,$i -ScriptBlock {
            param($g,$r,$i)
            & $g claim "worker-$i" fixture -Token ([guid]::NewGuid().ToString()) -RepoRoot $r | Out-Null
            return $LASTEXITCODE
        }
    }
    $jobs | Wait-Job -Timeout 30 | Out-Null
    Expect (@($jobs | Where-Object State -ne 'Completed').Count -eq 0) 'concurrent claim jobs timed out'
    $results=@($jobs | Receive-Job)
    Expect (@($results | Where-Object { $_ -eq 0 }).Count -eq 1) 'concurrent claims did not have exactly one winner'
    $jobs | Remove-Job
    Write-Output 'DEPLOY_GUARD_OK: owner/token, recovery, restart boundaries, lease and 8 concurrent claims'
} finally {
    if ($jobs) { $jobs | Stop-Job -ErrorAction SilentlyContinue; $jobs | Remove-Job -Force -ErrorAction SilentlyContinue }
    Remove-Item $testRoot -Recurse -Force
}
