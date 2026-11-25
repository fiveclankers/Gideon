param(
    [switch]$Console
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PyW = Join-Path $RepoRoot '.venv\Scripts\pythonw.exe'
$Py = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$Script = Join-Path $RepoRoot 'gideon_win.py'

if (-not (Test-Path $Py)) {
    throw "Virtual environment python not found at $Py"
}

if ($Console) {
    Set-Location $RepoRoot
    & $Py $Script --cli
} else {
    $exe = if (Test-Path $PyW) { $PyW } else { $Py }
    Start-Process -FilePath $exe -ArgumentList "`"$Script`"" -WorkingDirectory $RepoRoot -WindowStyle Hidden
}
