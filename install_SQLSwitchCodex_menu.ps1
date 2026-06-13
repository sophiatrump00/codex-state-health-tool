param(
    [string]$TargetRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$SourceRoot = Split-Path -Parent $PSCommandPath
if ([string]::IsNullOrWhiteSpace($TargetRoot)) {
    $TargetRoot = $SourceRoot
}

$SourceRoot = (Resolve-Path -LiteralPath $SourceRoot).Path
if (-not (Test-Path -LiteralPath $TargetRoot -PathType Container)) {
    New-Item -ItemType Directory -Path $TargetRoot | Out-Null
}
$TargetRoot = (Resolve-Path -LiteralPath $TargetRoot).Path

$files = @(
    "SQLSwitchCodex.py",
    "RUN_SQLSwitchCodex.cmd",
    "PATCH_PROVIDER_DISPLAY.cmd",
    "UNPATCH_PROVIDER_DISPLAY.cmd",
    "CREATE_PATCHED_DESKTOP_COPY.cmd",
    "README.md",
    "SQLSwitchCodex_GUIDE.md",
    "pyproject.toml"
)

$dirs = @(
    "src",
    "profiles",
    "scripts"
)

foreach ($name in $files) {
    $source = Join-Path $SourceRoot $name
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Missing installer source file: $source"
    }
}

foreach ($name in $dirs) {
    $source = Join-Path $SourceRoot $name
    if (-not (Test-Path -LiteralPath $source -PathType Container)) {
        throw "Missing installer source folder: $source"
    }
}

foreach ($name in $files) {
    $source = Join-Path $SourceRoot $name
    $destination = Join-Path $TargetRoot $name
    if ([System.IO.Path]::GetFullPath($source) -eq [System.IO.Path]::GetFullPath($destination)) {
        Write-Host "Already in place: $destination"
        continue
    }
    Copy-Item -LiteralPath $source -Destination $destination -Force
    Write-Host "Installed file: $destination"
}

foreach ($name in $dirs) {
    $source = Join-Path $SourceRoot $name
    $destination = Join-Path $TargetRoot $name
    if ([System.IO.Path]::GetFullPath($source) -eq [System.IO.Path]::GetFullPath($destination)) {
        Write-Host "Already in place: $destination"
        continue
    }
    Copy-Item -LiteralPath $source -Destination $TargetRoot -Recurse -Force
    Write-Host "Installed folder: $destination"
}

Write-Host ""
Write-Host "Done."
Write-Host "Run:"
Write-Host "  cd /d `"$TargetRoot`""
Write-Host "  RUN_SQLSwitchCodex.cmd"
Write-Host ""
Write-Host "Before publishing, run:"
Write-Host "  set PYTHONPATH=%CD%\src"
Write-Host "  python -m sqlswitchcodex_v21 publish-check"
