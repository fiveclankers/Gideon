<#
.SYNOPSIS
    Control script for Gideon Windows (Status / Silent Start / Verbose Console / Stop All / Restart / Logs).
    Can be run interactively via Desktop\GIDEON.bat or with -Action <status|start|silent|stop|restart|verbose|console|logs|install-startup>.
#>
param(
    [ValidateSet('menu', 'status', 'start', 'silent', 'stop', 'restart', 'verbose', 'console', 'logs', 'install-startup')]
    [string]$Action = 'menu'
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PyW = Join-Path $RepoRoot '.venv\Scripts\pythonw.exe'
$Py = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$MainScript = Join-Path $RepoRoot 'gideon_win.py'
$RuntimeDir = Join-Path $env:LOCALAPPDATA 'gideon-win'
$PidFile = Join-Path $RuntimeDir 'runtime.pid.json'
$MpvPidFile = Join-Path $RuntimeDir 'music\music-mpv.pid'
$StopSignalFile = Join-Path $RuntimeDir 'stop.signal'
$LogFile = Join-Path $RuntimeDir 'logs\gideon_win.log'
$InstallTaskScript = Join-Path $RepoRoot 'install_startup_task.ps1'

function Get-GideonProcesses {
    $all = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
    if (-not $all) { $all = @() }

    $byPid = @{}
    foreach ($p in $all) {
        $byPid[[int]$p.ProcessId] = $p
    }

    $seen = New-Object 'System.Collections.Generic.HashSet[int]'
    $results = @()

    function Add-ResultPid([int]$pidInt, [string]$role, [string]$fallbackName = '') {
        if ($pidInt -le 0 -or $seen.Contains($pidInt)) { return }
        $gp = Get-Process -Id $pidInt -ErrorAction SilentlyContinue
        if (-not $gp) { return }
        [void]$seen.Add($pidInt)
        $pname = if ($gp.ProcessName) { "$($gp.ProcessName).exe" } else { $fallbackName }
        $wsMb = [math]::Round($gp.WorkingSet64 / 1MB, 1)
        $script:__results_acc += [pscustomobject]@{
            Role      = $role
            ProcessId = $pidInt
            Name      = $pname
            RamMB     = $wsMb
        }
    }
    $script:__results_acc = @()

    $rootPids = New-Object 'System.Collections.Generic.HashSet[int]'

    # 1. Explicit CommandLine matches (unelevated or same-privilege processes)
    foreach ($p in $all) {
        $pidInt = [int]$p.ProcessId
        $cmd = [string]($p.CommandLine)
        if ($p.Name -match '^pythonw?\.exe$' -and $cmd -match 'gideon_win\.py') {
            [void]$rootPids.Add($pidInt)
            Add-ResultPid $pidInt 'Gideon Daemon' $p.Name
        } elseif ($p.Name -match '^mpv\.(exe|com)$' -and $cmd -match 'gideon-mpv') {
            Add-ResultPid $pidInt 'mpv Music Player' $p.Name
        } elseif ($p.Name -eq 'agy.exe' -and $cmd -match '--input-format' -and $cmd -match 'stream-json') {
            Add-ResultPid $pidInt 'Antigravity CLI (headless)' $p.Name
        }
    }

    # 2. runtime.pid.json (works even when GideonWindows is elevated and GIDEON.bat is unelevated)
    if (Test-Path $PidFile) {
        try {
            $meta = Get-Content -Path $PidFile -Raw | ConvertFrom-Json
            foreach ($candPid in @($meta.pid, $meta.ppid)) {
                if ($candPid) {
                    $gp = Get-Process -Id ([int]$candPid) -ErrorAction SilentlyContinue
                    if ($gp -and $gp.ProcessName -match '^pythonw?$') {
                        [void]$rootPids.Add([int]$candPid)
                        Add-ResultPid ([int]$candPid) 'Gideon Daemon' "$($gp.ProcessName).exe"
                    }
                }
            }
            if ($meta.codex_pid) {
                $cp = Get-Process -Id ([int]$meta.codex_pid) -ErrorAction SilentlyContinue
                if ($cp -and $cp.ProcessName -match '^(codex|node|cmd)$') {
                    Add-ResultPid ([int]$meta.codex_pid) 'Codex Engine' "$($cp.ProcessName).exe"
                }
            }
            if ($meta.agy_pid) {
                $ap = Get-Process -Id ([int]$meta.agy_pid) -ErrorAction SilentlyContinue
                if ($ap -and $ap.ProcessName -match '^(agy|node)$') {
                    Add-ResultPid ([int]$meta.agy_pid) 'Antigravity CLI (headless)' "$($ap.ProcessName).exe"
                }
            }
        } catch {}
    }

    # 3. Legacy/early-boot fallback: if GideonWindows task is Running and no Gideon Daemon was matched yet
    $task = Get-ScheduledTask -TaskName 'GideonWindows' -ErrorAction SilentlyContinue
    if ($task -and $task.State -eq 'Running' -and $rootPids.Count -eq 0) {
        $earliestStart = $null
        foreach ($p in $all) {
            if ($p.Name -match '^pythonw?\.exe$' -and [string]::IsNullOrEmpty($p.CommandLine)) {
                $pidInt = [int]$p.ProcessId
                $gp = Get-Process -Id $pidInt -ErrorAction SilentlyContinue
                if ($gp) {
                    [void]$rootPids.Add($pidInt)
                    Add-ResultPid $pidInt 'Gideon Daemon' $p.Name
                    try {
                        $st = $gp.StartTime
                        if ($null -eq $earliestStart -or $st -lt $earliestStart) {
                            $earliestStart = $st
                        }
                    } catch {}
                }
            }
        }
        foreach ($p in $all) {
            if ($p.Name -eq 'codex.exe' -and [string]::IsNullOrEmpty($p.CommandLine)) {
                $gp = Get-Process -Id ([int]$p.ProcessId) -ErrorAction SilentlyContinue
                if ($gp) {
                    $include = $true
                    try {
                        if ($null -ne $earliestStart -and $gp.StartTime -lt $earliestStart) {
                            $include = $false
                        }
                    } catch {}
                    if ($include) {
                        Add-ResultPid ([int]$p.ProcessId) 'Codex Engine' $p.Name
                    }
                }
            }
        }
    }

    # 4. Walk descendant tree ONLY for explicit Gideon-owned worker binaries (never GUI/user apps!)
    $descendantPids = New-Object 'System.Collections.Generic.HashSet[int]'
    foreach ($rpid in $rootPids) {
        [void]$descendantPids.Add($rpid)
    }
    $changed = $true
    while ($changed) {
        $changed = $false
        foreach ($p in $all) {
            $pidInt = [int]$p.ProcessId
            $ppidInt = [int]$p.ParentProcessId
            if (-not $descendantPids.Contains($pidInt) -and $descendantPids.Contains($ppidInt)) {
                # Only traverse through cmd/node wrappers or known Gideon engine binaries
                if ($p.Name -match '^(codex\.exe|codex-code-mode-host\.exe|agy\.exe|whisper-cli\.exe|node\.exe|cmd\.exe)$') {
                    [void]$descendantPids.Add($pidInt)
                    $changed = $true
                }
            }
        }
    }
    foreach ($pidInt in $descendantPids) {
        if ($seen.Contains($pidInt) -or -not $byPid.ContainsKey($pidInt)) { continue }
        $p = $byPid[$pidInt]
        $cmd = [string]($p.CommandLine)
        if ($p.Name -match '^(codex\.exe|codex-code-mode-host\.exe)$') {
            Add-ResultPid $pidInt 'Codex Engine' $p.Name
        } elseif ($p.Name -eq 'whisper-cli.exe' -or $cmd -match 'whisper-cli') {
            Add-ResultPid $pidInt 'Whisper STT' $p.Name
        } elseif ($p.Name -eq 'agy.exe' -and ($cmd -match '--input-format' -or [string]::IsNullOrEmpty($cmd))) {
            Add-ResultPid $pidInt 'Antigravity CLI (headless)' $p.Name
        }
    }

    # 5. mpv PID file or gideon-mpv pipe fallback
    if (-not ($script:__results_acc | Where-Object { $_.Role -eq 'mpv Music Player' })) {
        if (Test-Path $MpvPidFile) {
            try {
                $mpid = [int]((Get-Content -Path $MpvPidFile -Raw).Trim())
                if ($mpid -gt 0) {
                    $mp = Get-Process -Id $mpid -ErrorAction SilentlyContinue
                    if ($mp -and $mp.ProcessName -match '^mpv') {
                        Add-ResultPid $mpid 'mpv Music Player' "$($mp.ProcessName).exe"
                    }
                }
            } catch {}
        }
    }
    if (-not ($script:__results_acc | Where-Object { $_.Role -eq 'mpv Music Player' })) {
        if ([System.IO.Directory]::GetFiles('\\.\pipe\') -contains '\\.\pipe\gideon-mpv') {
            foreach ($mp in @(Get-Process -Name 'mpv' -ErrorAction SilentlyContinue)) {
                Add-ResultPid ([int]$mp.Id) 'mpv Music Player' 'mpv.exe'
            }
        }
    }

    $results = $script:__results_acc
    $script:__results_acc = $null
    return $results
}

function Show-Status {
    $procs = @(Get-GideonProcesses)
    $task = Get-ScheduledTask -TaskName 'GideonWindows' -ErrorAction SilentlyContinue
    Write-Host ''
    Write-Host '=== GIDEON WINDOWS STATUS ===' -ForegroundColor Cyan
    if ($procs.Count -eq 0) {
        if ($task -and $task.State -eq 'Running') {
            Write-Host '  State: TASK RUNNING (inspecting elevated process; run Stop or Restart to sync)' -ForegroundColor Yellow
        } else {
            Write-Host '  State: STOPPED (0 processes running)' -ForegroundColor Yellow
        }
    } else {
        $totalMb = [math]::Round(($procs | Measure-Object -Property RamMB -Sum).Sum, 1)
        Write-Host "  State: RUNNING ($($procs.Count) process(es), ${totalMb} MB total WS)" -ForegroundColor Green
        $procs | Format-Table Role, ProcessId, Name, RamMB -AutoSize
    }
    if ($task) {
        Write-Host "  Startup Task 'GideonWindows': REGISTERED ($($task.State), RunLevel=$($task.Principal.RunLevel))" -ForegroundColor DarkCyan
    } else {
        Write-Host "  Startup Task 'GideonWindows': NOT REGISTERED" -ForegroundColor DarkYellow
    }
    Write-Host ''
}

function Stop-Gideon {
    $procs = @(Get-GideonProcesses)
    $task = Get-ScheduledTask -TaskName 'GideonWindows' -ErrorAction SilentlyContinue
    $taskRunning = ($task -and $task.State -eq 'Running')
    if ($procs.Count -eq 0 -and -not $taskRunning) {
        Write-Host 'Gideon is already stopped (no Gideon/mpv/agy/codex processes found).' -ForegroundColor Yellow
        return
    }

    Write-Host "Stopping $($procs.Count) Gideon process(es)..." -ForegroundColor Cyan

    # 1. Clean shutdown signal (works across elevation boundary without UAC on current builds)
    if ($procs | Where-Object { $_.Role -eq 'Gideon Daemon' }) {
        try {
            New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
            Set-Content -Path $StopSignalFile -Value 'stop' -Encoding Ascii -Force
            $deadline = (Get-Date).AddSeconds(2.5)
            while ((Get-Date) -lt $deadline) {
                Start-Sleep -Milliseconds 150
                if (-not (Get-GideonProcesses | Where-Object { $_.Role -eq 'Gideon Daemon' })) {
                    break
                }
            }
        } catch {}
    }

    # 2. Ask mpv to quit cleanly over Named Pipe IPC if still running
    if (Get-GideonProcesses | Where-Object { $_.Role -eq 'mpv Music Player' }) {
        try {
            & $Py -c "import sys; sys.path.insert(0, r'$RepoRoot'); import music_win; music_win.quit_mpv()" 2>&1 | Out-Null
            Start-Sleep -Milliseconds 250
        } catch {}
    }

    # 3. Force-kill any remaining Gideon processes by exact PID (never /T on Gideon Daemon so launched apps stay alive!)
    $remaining = @(Get-GideonProcesses)
    foreach ($p in ($remaining | Sort-Object { if ($_.Role -eq 'Gideon Daemon') { 1 } else { 0 } })) {
        try {
            if ($p.Role -eq 'Gideon Daemon') {
                & taskkill /PID $p.ProcessId /F 2>&1 | Out-Null
            } else {
                & taskkill /PID $p.ProcessId /T /F 2>&1 | Out-Null
            }
        } catch {}
    }

    Start-Sleep -Milliseconds 300
    $stillLeft = @(Get-GideonProcesses)
    if ($stillLeft.Count -gt 0) {
        $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        if (-not $isAdmin) {
            Write-Host 'Elevated Gideon process detected; requesting UAC to stop it cleanly...' -ForegroundColor Yellow
            $cmds = @()
            foreach ($p in $stillLeft) {
                if ($p.Role -eq 'Gideon Daemon') {
                    $cmds += "& taskkill.exe /PID $($p.ProcessId) /F 2>&1 | Out-Null"
                } else {
                    $cmds += "& taskkill.exe /PID $($p.ProcessId) /T /F 2>&1 | Out-Null"
                }
            }
            $killCmd = $cmds -join '; '
            Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList "-NoProfile -ExecutionPolicy Bypass -Command `"$killCmd`""
            Start-Sleep -Milliseconds 400
        }
    }

    Remove-Item -Path $StopSignalFile -Force -ErrorAction SilentlyContinue
    $final = @(Get-GideonProcesses)
    if ($final.Count -eq 0) {
        Remove-Item -Path $PidFile -Force -ErrorAction SilentlyContinue
        Remove-Item -Path $MpvPidFile -Force -ErrorAction SilentlyContinue
        Write-Host 'All Gideon processes (including mpv / Codex / headless agy) stopped cleanly.' -ForegroundColor Green
    } else {
        Write-Host "Warning: some processes could not be stopped: $(($final.ProcessId) -join ', ')" -ForegroundColor Red
    }
}

function Clear-PreviousGideonSetup {
    $procs = @(Get-GideonProcesses)
    $task = Get-ScheduledTask -TaskName 'GideonWindows' -ErrorAction SilentlyContinue
    $taskRunning = ($task -and $task.State -eq 'Running')
    if ($procs.Count -eq 0 -and -not $taskRunning) {
        return
    }
    if ($procs.Count -gt 0) {
        $summary = ($procs | ForEach-Object { "$($_.Role) [PID $($_.ProcessId)]" }) -join ', '
        Write-Host "[WARNING] Previous Gideon session/processes still active: $summary" -ForegroundColor Yellow
    } else {
        Write-Host "[WARNING] Scheduled task 'GideonWindows' is still marked Running." -ForegroundColor Yellow
    }
    Write-Host '[WARNING] Guaranteeing single instance — shutting down all previous Gideon processes first...' -ForegroundColor Yellow
    Stop-Gideon
    Start-Sleep -Milliseconds 350
    $leftover = @(Get-GideonProcesses)
    if ($leftover.Count -gt 0) {
        throw "Refusing to launch a second instance: could not stop PID(s) $(($leftover.ProcessId) -join ', ')"
    }
}

function Start-Gideon {
    Clear-PreviousGideonSetup

    $task = Get-ScheduledTask -TaskName 'GideonWindows' -ErrorAction SilentlyContinue
    if ($task) {
        try {
            Start-ScheduledTask -TaskName 'GideonWindows' -ErrorAction Stop
            $deadline = (Get-Date).AddSeconds(4.0)
            while ((Get-Date) -lt $deadline) {
                Start-Sleep -Milliseconds 200
                $check = @(Get-GideonProcesses | Where-Object { $_.Role -eq 'Gideon Daemon' })
                if ($check.Count -gt 0) {
                    Write-Host "Gideon started silently & elevated via Task Scheduler (PID $($check[0].ProcessId))." -ForegroundColor Green
                    return
                }
            }
            Write-Host 'Warning: GideonWindows task started, but Gideon Daemon PID has not registered yet.' -ForegroundColor Yellow
            return
        } catch {}
    }

    $exe = if (Test-Path $PyW) { $PyW } else { $Py }
    if (-not (Test-Path $exe)) {
        throw "Python executable not found at $exe"
    }
    Start-Process -FilePath $exe -ArgumentList "`"$MainScript`"" -WorkingDirectory $RepoRoot -WindowStyle Hidden
    $deadline2 = (Get-Date).AddSeconds(3.0)
    while ((Get-Date) -lt $deadline2) {
        Start-Sleep -Milliseconds 200
        $check2 = @(Get-GideonProcesses | Where-Object { $_.Role -eq 'Gideon Daemon' })
        if ($check2.Count -gt 0) {
            Write-Host "Gideon started silently in background (PID $($check2[0].ProcessId))." -ForegroundColor Green
            return
        }
    }
    Write-Host 'Gideon launch requested; check logs if it does not appear in the tray.' -ForegroundColor Yellow
}

function Start-GideonVerbose {
    Clear-PreviousGideonSetup
    if (-not (Test-Path $Py)) {
        throw "Python executable not found at $Py"
    }
    try { $Host.UI.RawUI.WindowTitle = 'Gideon Windows — Verbose Console (--cli)' } catch {}
    Write-Host "Launching Gideon in VERBOSE mode: `"$Py`" `"$MainScript`" --cli" -ForegroundColor Cyan
    Write-Host 'Press Ctrl+C or type "quit" to stop.' -ForegroundColor DarkGray
    Set-Location $RepoRoot
    & $Py $MainScript --cli
}

function Show-Logs {
    if (-not (Test-Path $LogFile)) {
        Write-Host "No log file yet at $LogFile" -ForegroundColor Yellow
        return
    }
    Write-Host "=== Last 40 lines of $LogFile ===" -ForegroundColor Cyan
    Get-Content -Path $LogFile -Tail 40
    Write-Host ''
}

function Install-StartupTask {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($isAdmin) {
        & $InstallTaskScript
    } else {
        Write-Host 'Requesting Administrator prompt (UAC) to register elevated logon task...' -ForegroundColor Cyan
        Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$InstallTaskScript`""
    }
}

switch ($Action) {
    'status'          { Show-Status; exit 0 }
    'start'           { Start-Gideon; exit 0 }
    'silent'          { Start-Gideon; exit 0 }
    'stop'            { Stop-Gideon; exit 0 }
    'restart'         { Start-Gideon; exit 0 }
    'verbose'         { Start-GideonVerbose; exit 0 }
    'console'         { Start-GideonVerbose; exit 0 }
    'logs'            { Show-Logs; exit 0 }
    'install-startup' { Install-StartupTask; exit 0 }
}

while ($true) {
    Show-Status
    Write-Host '  [1] Refresh Status'
    Write-Host '  [2] Start Classic Silent Mode (Single-Instance Clean Launch)'
    Write-Host '  [3] Full Restart (Silent Background)'
    Write-Host '  [4] Stop Everything (Gideon + mpv + Codex + agy CLI)'
    Write-Host '  [5] Launch Verbose Mode (Live Console + --cli)'
    Write-Host '  [6] View Recent Logs'
    Write-Host '  [7] Register Silent Startup Task (Admin UAC)'
    Write-Host '  [0] Exit'
    Write-Host ''
    $choice = Read-Host 'Select an option'
    switch ($choice.Trim()) {
        '1' { continue }
        '2' { Start-Gideon; Start-Sleep -Seconds 1 }
        '3' { Start-Gideon; Start-Sleep -Seconds 1 }
        '4' { Stop-Gideon; Start-Sleep -Seconds 1 }
        '5' { Start-GideonVerbose }
        '6' { Show-Logs; Read-Host 'Press Enter to continue' | Out-Null }
        '7' { Install-StartupTask; Start-Sleep -Seconds 1 }
        '0' { break }
        'q' { break }
        default { Write-Host 'Invalid choice.' -ForegroundColor Red; Start-Sleep -Milliseconds 600 }
    }
}
