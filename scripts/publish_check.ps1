Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $PSCommandPath
$Root = Split-Path -Parent $ScriptDir
Set-Location -LiteralPath $Root

$env:PYTHONPATH = Join-Path $Root "src"
python -m sqlswitchcodex_v21 publish-check
