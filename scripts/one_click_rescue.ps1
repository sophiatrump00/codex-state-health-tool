param(
    [string]$Profile = ".local\profile.toml",
    [string]$Snapshot = "",
    [switch]$StopCodex
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $PSCommandPath
$Root = Split-Path -Parent $ScriptDir
Set-Location -LiteralPath $Root

if ($StopCodex) {
    Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessName -like "*Codex*" -or $_.ProcessName -like "*OpenAI*" } |
        Stop-Process -Force
}

$env:PYTHONPATH = Join-Path $Root "src"
$cmd = @("-m", "sqlswitchcodex_v21", "undo", "--profile", $Profile)
if ($Snapshot) {
    $cmd += @("--snapshot", $Snapshot)
}

python @cmd
