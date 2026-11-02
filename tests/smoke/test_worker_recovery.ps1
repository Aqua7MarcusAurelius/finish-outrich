Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\..\scripts\recover_workers.ps1')
$snapshot = [pscustomobject]@{ workers = @(
    [pscustomobject]@{ account_id=24; desired_running=$true; is_active=$true; recoverable=$true; task_alive=$false; status='stopped' },
    [pscustomobject]@{ account_id=25; desired_running=$false; is_active=$true; recoverable=$true; task_alive=$false; status='stopped' },
    [pscustomobject]@{ account_id=26; desired_running=$true; is_active=$true; recoverable=$true; task_alive=$true; status='running' },
    [pscustomobject]@{ account_id=71; desired_running=$true; is_active=$true; recoverable=$true; task_alive=$false; status='session_expired' },
    [pscustomobject]@{ account_id=88; desired_running=$true; is_active=$false; recoverable=$true; task_alive=$false; status='stopped' }
) }
$ids = @(Get-RecoveryCandidates -Snapshot $snapshot)
if ($ids.Count -ne 1 -or $ids[0] -ne 24) { throw 'Recovery selection violated explicit intent.' }
Write-Output 'PASS: recovery filters manual stops, live tasks, inactive and expired accounts.'

# Run the real orchestrator twice with Docker/HTTP/sleep replaced. No process,
# network, scheduled task or production workspace is touched by this test.
$script:recoveryPostCount = 0
$script:testConnected = $false
function Invoke-BoundedDocker { return $true }
function Start-Sleep { param($Seconds) }
function Invoke-RestMethod {
    param($Uri, $Method, $TimeoutSec)
    if ($Uri.EndsWith('/health')) { return [pscustomobject]@{ status='ok' } }
    if ($Uri.EndsWith('/readiness')) {
        return [pscustomobject]@{
            ready=$script:testConnected; desired=1
            desired_connected=[int]$script:testConnected
            workers=@([pscustomobject]@{
                account_id=24; desired_running=$true; is_active=$true
                recoverable=$true; task_alive=$script:testConnected; status='stopped'
            })
        }
    }
    if ($Uri.EndsWith('/workers/24/recover') -and $Method -eq 'Post') {
        $script:recoveryPostCount++
        $script:testConnected=$true
        return [pscustomobject]@{ status='starting' }
    }
    throw 'Unexpected recovery API request.'
}
$fixture = Join-Path ([System.IO.Path]::GetTempPath()) ('ProjectOutRich-recovery-test-' + [guid]::NewGuid().ToString('N'))
[void](New-Item -ItemType Directory -Path $fixture)
Set-Content -LiteralPath (Join-Path $fixture 'docker-compose.yml') -Value 'services: {}'
try {
    if ((Invoke-WorkerRecovery -Root $fixture -Timeout 30) -ne 0) { throw 'First recovery failed.' }
    if ((Invoke-WorkerRecovery -Root $fixture -Timeout 30) -ne 0) { throw 'Repeated recovery failed.' }
    if ($script:recoveryPostCount -ne 1) { throw 'Repeated recovery duplicated a worker start.' }
    Write-Output 'PASS: repeated recovery uses native recover only once, without duplicate starts.'
} finally {
    $resolved = (Resolve-Path -LiteralPath $fixture).Path
    $safePrefix = Join-Path ([System.IO.Path]::GetTempPath()) 'ProjectOutRich-recovery-test-'
    if (-not $resolved.StartsWith($safePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Refusing cleanup outside test fixture.'
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
