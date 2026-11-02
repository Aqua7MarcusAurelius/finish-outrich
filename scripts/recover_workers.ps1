<#
Restores existing Compose containers and explicitly enabled Telegram workers.
Does not create outreach schedules, claim leads, reauthorize or alter proxies.
#>
[CmdletBinding()]
param(
    [string]$ProjectPath,
    [ValidateRange(30, 1800)][int]$TimeoutSeconds = 600,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $ProjectPath) { $ProjectPath = Split-Path -Parent $PSScriptRoot }

function Get-RecoveryCandidates {
    param([Parameter(Mandatory)]$Snapshot)
    @($Snapshot.workers | Where-Object {
        $_.desired_running -eq $true -and $_.is_active -eq $true -and
        $_.recoverable -eq $true -and $_.task_alive -eq $false -and
        $_.status -ne 'session_expired'
    } | ForEach-Object { [int]$_.account_id })
}

function Invoke-BoundedDocker {
    param([string]$Arguments, [string]$WorkingDirectory, [int]$WaitSeconds = 30)
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = (Get-Command docker.exe -ErrorAction Stop).Source
    $info.Arguments = $Arguments
    $info.WorkingDirectory = $WorkingDirectory
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    try {
        [void]$process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($WaitSeconds * 1000)) {
            $process.Kill()
            return $false
        }
        # Never write command output: it may include local configuration.
        [void]$stdout.GetAwaiter().GetResult()
        [void]$stderr.GetAwaiter().GetResult()
        return $process.ExitCode -eq 0
    } finally { $process.Dispose() }
}

function Invoke-WorkerRecovery {
    param([string]$Root, [int]$Timeout)
    $Root = (Resolve-Path -LiteralPath $Root).Path
    if (-not (Test-Path -LiteralPath (Join-Path $Root 'docker-compose.yml'))) {
        throw 'Project docker-compose.yml is missing.'
    }
    if ($DryRun) {
        $plan = [pscustomobject]@{
            project = $Root; timeout_seconds = $Timeout; dry_run = $true
            actions = @('wait/start Docker Desktop', 'compose start existing containers',
                        'wait health/readiness', 'recover desired workers through native API')
            excluded = @('service accounts', 'manual stops', 'session_expired')
        }
        Write-Host ($plan | ConvertTo-Json -Depth 3)
        return 0
    }

    # ScheduledTask IgnoreNew handles task launches; this also guards manual runs.
    $mutex = New-Object System.Threading.Mutex($false, 'Local\ProjectOutRich.WorkerRecovery')
    $locked = $false
    try {
        try { $locked = $mutex.WaitOne(0) }
        catch [System.Threading.AbandonedMutexException] { $locked = $true }
        if (-not $locked) { return 0 }
        $logDirectory = Join-Path $Root '.claude\operations\worker-recovery'
        [void](New-Item -ItemType Directory -Path $logDirectory -Force)
        $logFile = Join-Path $logDirectory ('boot-{0}.jsonl' -f (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
        function Write-RecoveryLog {
            param([string]$Event, [hashtable]$Fields = @{})
            $Fields.time = (Get-Date).ToUniversalTime().ToString('o')
            $Fields.event = $Event
            ($Fields | ConvertTo-Json -Compress) | Add-Content -LiteralPath $logFile -Encoding UTF8
        }
        Write-RecoveryLog 'started'
        $deadline = (Get-Date).AddSeconds($Timeout)
        $dockerStarted = $false
        $composeStarted = $false
        $requested = @{}
        do {
            if (-not $composeStarted) {
                if (Invoke-BoundedDocker -Arguments 'info' -WorkingDirectory $Root) {
                    $composeStarted = Invoke-BoundedDocker -Arguments 'compose start' -WorkingDirectory $Root
                    if ($composeStarted) { Write-RecoveryLog 'containers_started' }
                } elseif (-not $dockerStarted) {
                    $dockerStarted = $true
                    $desktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
                    if (-not (Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue)) {
                        if (-not (Test-Path -LiteralPath $desktop)) { throw 'Docker Desktop is not installed.' }
                        Start-Process -FilePath $desktop -WindowStyle Hidden
                        Write-RecoveryLog 'docker_desktop_started'
                    }
                }
            }
            if ($composeStarted) {
                try {
                    $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/system/health' -TimeoutSec 10
                    if ($health.status -eq 'ok') {
                        $snapshot = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/system/readiness' -TimeoutSec 10
                        foreach ($accountId in (Get-RecoveryCandidates -Snapshot $snapshot)) {
                            $remaining = [int][Math]::Floor(($deadline - (Get-Date)).TotalSeconds)
                            if ($remaining -lt 1) { break }
                            if ($requested.ContainsKey($accountId)) { continue }
                            # Exactly one attempt per account in this bounded run.
                            $requested[$accountId] = $true
                            try {
                                [void](Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/workers/$accountId/recover" -TimeoutSec ([Math]::Min(15, $remaining)))
                                Write-RecoveryLog 'worker_requested' @{ account_id = $accountId }
                            } catch {
                                Write-RecoveryLog 'worker_request_failed' @{ account_id = $accountId }
                            }
                        }
                        if ($snapshot.ready -eq $true) {
                            Write-RecoveryLog 'ready' @{ desired = $snapshot.desired; connected = $snapshot.desired_connected }
                            return 0
                        }
                    }
                } catch {
                    # No exception/body output: transport failures can contain secrets.
                }
            }
            Start-Sleep -Seconds 5
        } while ((Get-Date) -lt $deadline)
        Write-RecoveryLog 'timeout' @{ requested = @($requested.Keys) }
        return 1
    } finally {
        if ($locked) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    exit (Invoke-WorkerRecovery -Root $ProjectPath -Timeout $TimeoutSeconds)
}
