<#
.SYNOPSIS
    Registers a Windows Task Scheduler logon job that starts Gideon elevated
    (RunLevel Highest) without a UAC prompt, so PTT hotkeys work even inside
    Administrator-elevated games.
#>
param(
    [string]$TaskName = 'GideonWindows'
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PyW = Join-Path $RepoRoot '.venv\Scripts\pythonw.exe'
$Script = Join-Path $RepoRoot 'gideon_win.py'

if (-not (Test-Path $PyW)) {
    throw "Missing $PyW"
}
if (-not (Test-Path $Script)) {
    throw "Missing $Script"
}

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Warning "Registering a Highest-privilege logon task requires Administrator rights."
    Write-Output "Run this one-liner in an elevated PowerShell window:"
    Write-Output "  powershell -NoProfile -ExecutionPolicy Bypass -File `"$($MyInvocation.MyCommand.Path)`""
    exit 2
}

$action = New-ScheduledTaskAction `
    -Execute $PyW `
    -Argument "`"$Script`"" `
    -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description 'Starts Gideon Windows Voice Companion elevated at user logon without UAC prompts.' `
    -Force | Out-Null

Write-Output "Scheduled task '$TaskName' registered successfully (RunLevel=Highest, AtLogOn)."
