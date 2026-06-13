param(
    [string]$Profile = ".local\profile.toml",
    [string]$Source = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $PSCommandPath
$Root = Split-Path -Parent $ScriptDir
Set-Location -LiteralPath $Root

$env:PYTHONPATH = Join-Path $Root "src"
$cmd = @("-m", "sqlswitchcodex_v21", "plan-project-restore", "--profile", $Profile)
if ($Source) {
    $cmd += @("--source", $Source)
}
python @cmd
