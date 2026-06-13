param(
    [string]$Profile = ".local\profile.toml",
    [string]$Plan = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $PSCommandPath
$Root = Split-Path -Parent $ScriptDir
Set-Location -LiteralPath $Root

if (-not $Plan) {
    $latest = Get-ChildItem -LiteralPath (Join-Path $Root ".local\plans") -Filter "project-restore-plan-*.json" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $latest) {
        throw "No project restore plan found. Run scripts\plan_project_restore.ps1 first."
    }
    $Plan = $latest.FullName
}

Write-Host "Project restore plan: $Plan"
$confirm = Read-Host "Type RESTORE_PROJECTS to update .codex-global-state.json"
if ($confirm -ne "RESTORE_PROJECTS") {
    Write-Host "Cancelled."
    exit 2
}

$env:PYTHONPATH = Join-Path $Root "src"
python -m sqlswitchcodex_v21 apply-project-restore --profile $Profile --plan $Plan
