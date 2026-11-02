<# Registers one interactive current-user logon task. Run with -WhatIf to review. #>
[CmdletBinding(SupportsShouldProcess = $true)]
param([string]$ProjectPath)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $ProjectPath) { $ProjectPath = Split-Path -Parent $PSScriptRoot }
$root = (Resolve-Path -LiteralPath $ProjectPath).Path
$script = Join-Path $root 'scripts\recover_workers.ps1'
if (-not (Test-Path -LiteralPath $script)) { throw 'Recovery script is missing.' }
if ($root.Contains('"')) { throw 'Project path must not contain a quote.' }
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskName = 'ProjectOutRich-WorkerRecovery'
$powershell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -ProjectPath "{1}"' -f $script, $root
if ($PSCmdlet.ShouldProcess("$taskName ($identity) at logon, project $root", 'Register or replace current-user recovery task')) {
    $action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
    $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -StartWhenAvailable
    [void](Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force)
    Write-Output "Registered $taskName for $identity. It will run on the next logon."
}
