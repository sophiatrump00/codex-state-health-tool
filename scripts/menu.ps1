param(
    [string]$Profile = ".local\profile.toml"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $PSCommandPath
$Root = Split-Path -Parent $ScriptDir
Set-Location -LiteralPath $Root
$env:PYTHONPATH = Join-Path $Root "src"

function Invoke-V21 {
    param([string[]]$ArgsList)
    python @ArgsList
}

function Get-LatestPlan {
    Get-ChildItem -LiteralPath (Join-Path $Root ".local\plans") -Filter "projection-plan-*.json" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
}

function Get-LatestProjectRestorePlan {
    Get-ChildItem -LiteralPath (Join-Path $Root ".local\plans") -Filter "project-restore-plan-*.json" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
}

while ($true) {
    Write-Host ""
    Write-Host "SQLSwitchCodex V2.1.1"
    Write-Host "1. Doctor"
    Write-Host "2. Plan projection"
    Write-Host "3. Apply latest projection plan"
    Write-Host "4. Plan project restore"
    Write-Host "5. Apply latest project restore plan"
    Write-Host "6. Undo latest snapshot"
    Write-Host "7. Publish check"
    Write-Host "8. Exit"
    $choice = Read-Host "Choose"

    switch ($choice) {
        "1" {
            Invoke-V21 @("-m", "sqlswitchcodex_v21", "doctor", "--profile", $Profile)
        }
        "2" {
            Invoke-V21 @("-m", "sqlswitchcodex_v21", "plan-projection", "--profile", $Profile)
        }
        "3" {
            $plan = Get-LatestPlan
            if (-not $plan) {
                Write-Host "No projection plan found. Run option 2 first."
                continue
            }
            Write-Host "Latest plan: $($plan.FullName)"
            $confirm = Read-Host "Type APPLY to write Codex state"
            if ($confirm -eq "APPLY") {
                Invoke-V21 @("-m", "sqlswitchcodex_v21", "apply-projection", "--profile", $Profile, "--plan", $plan.FullName)
            }
        }
        "4" {
            Invoke-V21 @("-m", "sqlswitchcodex_v21", "plan-project-restore", "--profile", $Profile)
        }
        "5" {
            $plan = Get-LatestProjectRestorePlan
            if (-not $plan) {
                Write-Host "No project restore plan found. Run option 4 first."
                continue
            }
            Write-Host "Latest project restore plan: $($plan.FullName)"
            $confirm = Read-Host "Type RESTORE_PROJECTS to write Codex global-state"
            if ($confirm -eq "RESTORE_PROJECTS") {
                Invoke-V21 @("-m", "sqlswitchcodex_v21", "apply-project-restore", "--profile", $Profile, "--plan", $plan.FullName)
            }
        }
        "6" {
            $confirm = Read-Host "Type UNDO to restore the latest snapshot"
            if ($confirm -eq "UNDO") {
                Invoke-V21 @("-m", "sqlswitchcodex_v21", "undo", "--profile", $Profile)
            }
        }
        "7" {
            Invoke-V21 @("-m", "sqlswitchcodex_v21", "publish-check")
        }
        "8" {
            break
        }
        default {
            Write-Host "Unknown option."
        }
    }
}
