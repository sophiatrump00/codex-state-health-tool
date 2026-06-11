param(
    [switch]$Fix,
    [string]$TargetProvider = "openai",
    [string]$CodexHome = (Join-Path $HOME ".codex"),
    [string]$BackupRoot = (Join-Path $PSScriptRoot "codex-health-backups")
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

function Write-Section {
    param([string]$Text)
    Write-Host ""
    Write-Host "=== $Text ===" -ForegroundColor Cyan
}

function Invoke-Sqlite {
    param(
        [string]$Database,
        [string]$Sql,
        [switch]$ReadOnly,
        [switch]$Json
    )

    $args = @()
    if ($ReadOnly) { $args += "-readonly" }
    if ($Json) { $args += "-json" }
    $args += $Database

    $Sql | & sqlite3 @args
}

function Assert-File {
    param([string]$Path, [string]$Name)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "$Name not found: $Path"
    }
}

function New-CodexStateBackup {
    param(
        [string]$CodexHome,
        [string]$BackupRoot
    )

    $db = Join-Path $CodexHome "state_5.sqlite"
    $index = Join-Path $CodexHome "session_index.jsonl"
    $globalState = Join-Path $CodexHome ".codex-global-state.json"

    Assert-File $db "state database"
    Assert-File $index "session index"
    Assert-File $globalState "global state"

    New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null
    $ts = Get-Date -Format "yyyyMMdd_HHmmss"
    $backupDir = Join-Path $BackupRoot "backup_$ts"
    New-Item -ItemType Directory -Force -Path $backupDir | Out-Null

    $sqliteBackup = Join-Path $backupDir "state_5.sqlite.sqlite-backup"
    & sqlite3 $db ".backup '$($sqliteBackup -replace "'", "''")'"

    Copy-Item -LiteralPath $db -Destination (Join-Path $backupDir "state_5.sqlite.raw-copy") -Force
    foreach ($suffix in @("-wal", "-shm")) {
        $p = "$db$suffix"
        if (Test-Path -LiteralPath $p) {
            Copy-Item -LiteralPath $p -Destination (Join-Path $backupDir "state_5.sqlite$suffix.raw-copy") -Force
        }
    }

    Copy-Item -LiteralPath $index -Destination (Join-Path $backupDir "session_index.jsonl.before") -Force
    Copy-Item -LiteralPath $globalState -Destination (Join-Path $backupDir ".codex-global-state.json.before") -Force

    return $backupDir
}

function Get-JsonRows {
    param([string]$Database, [string]$Sql)

    $raw = (Invoke-Sqlite -Database $Database -Sql $Sql -ReadOnly -Json) -join "`n"
    if ([string]::IsNullOrWhiteSpace($raw)) { return @() }
    return @($raw | ConvertFrom-Json)
}

function Rebuild-SessionIndex {
    param([string]$Database, [string]$IndexPath)

    $sql = @"
SELECT id, title, updated_at_ms
FROM threads
WHERE archived=0
  AND source='vscode'
  AND thread_source='user'
ORDER BY updated_at_ms ASC, id ASC;
"@
    $rows = Get-JsonRows -Database $Database -Sql $sql
    $lines = foreach ($r in $rows) {
        $updated = [DateTimeOffset]::FromUnixTimeMilliseconds([int64]$r.updated_at_ms).UtcDateTime.ToString(
            "yyyy-MM-ddTHH:mm:ss.fffffffZ",
            [Globalization.CultureInfo]::InvariantCulture
        )
        [pscustomobject]@{
            id = $r.id
            thread_name = $r.title
            updated_at = $updated
        } | ConvertTo-Json -Compress
    }

    [System.IO.File]::WriteAllLines($IndexPath, [string[]]$lines, [System.Text.UTF8Encoding]::new($false))
}

function Resolve-TargetProvider {
    param([string]$Database, [string]$TargetProvider)

    if ($TargetProvider -ne "auto") {
        return $TargetProvider
    }

    $rows = Get-JsonRows -Database $Database -Sql @"
SELECT model_provider, id, title, updated_at_ms
FROM threads
WHERE archived=0
  AND source='vscode'
  AND thread_source='user'
  AND model_provider IS NOT NULL
  AND model_provider!=''
ORDER BY updated_at_ms DESC, id DESC
LIMIT 1;
"@

    if ($rows.Count -eq 0) {
        throw "Could not auto-detect model_provider from threads."
    }

    return [string]$rows[0].model_provider
}

function Show-Health {
    param([string]$Database, [string]$IndexPath, [string]$TargetProvider)

    $providerSql = $TargetProvider.Replace("'", "''")

    Write-Section "SQLite Integrity"
    Invoke-Sqlite -Database $Database -Sql "PRAGMA integrity_check;" -ReadOnly

    Write-Section "Thread Summary"
    Invoke-Sqlite -Database $Database -ReadOnly -Sql @"
.mode column
.headers on
SELECT COUNT(*) AS total_threads FROM threads;
SELECT archived, COUNT(*) AS n FROM threads GROUP BY archived ORDER BY archived;
SELECT archived, source, COALESCE(NULLIF(thread_source,''),'(blank)') AS thread_source, has_user_event, model_provider, COUNT(*) AS n
FROM threads
GROUP BY archived, source, thread_source, has_user_event, model_provider
ORDER BY archived, source, thread_source, has_user_event, model_provider;
"@

    Write-Section "Known Sidebar Risk Checks"
    Invoke-Sqlite -Database $Database -ReadOnly -Sql @"
.mode column
.headers on
SELECT COUNT(*) AS visible_user_not_target_provider
FROM threads
WHERE archived=0
  AND source='vscode'
  AND thread_source='user'
  AND has_user_event=1
  AND model_provider!='$providerSql';

SELECT COUNT(*) AS blank_thread_source_for_user_threads
FROM threads
WHERE archived=0
  AND source='vscode'
  AND has_user_event=1
  AND (thread_source IS NULL OR thread_source='');

SELECT COUNT(*) AS drive_cwd_without_longpath_prefix
FROM threads
WHERE substr(cwd,1,4)!='\\?\'
  AND length(cwd)>=3
  AND substr(cwd,2,2)=':\'
  AND substr(cwd,1,1) GLOB '[A-Za-z]';
"@

    Write-Section "Project Grouping Preview"
    Invoke-Sqlite -Database $Database -ReadOnly -Sql @"
.mode column
.headers on
SELECT quote(cwd) AS cwd,
       COUNT(*) AS all_threads,
       SUM(CASE WHEN archived=0 AND source='vscode' AND thread_source='user' AND has_user_event=1 THEN 1 ELSE 0 END) AS visible_user_threads,
       SUM(CASE WHEN archived=0 AND source='vscode' AND thread_source='user' AND has_user_event=1 AND model_provider='$providerSql' THEN 1 ELSE 0 END) AS target_provider_visible_user_threads
FROM threads
GROUP BY cwd
ORDER BY target_provider_visible_user_threads DESC, all_threads DESC, cwd;
"@

    Write-Section "Session Index Alignment"
    $dbRows = Get-JsonRows -Database $Database -Sql @"
SELECT id, title
FROM threads
WHERE archived=0
  AND source='vscode'
  AND thread_source='user'
ORDER BY updated_at_ms DESC, id DESC;
"@
    $idxRows = @()
    if (Test-Path -LiteralPath $IndexPath) {
        $idxRows = @(Get-Content -LiteralPath $IndexPath | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json })
    }
    $dbIds = @{}
    foreach ($r in $dbRows) { $dbIds[$r.id] = $true }
    $idxIds = @{}
    foreach ($r in $idxRows) { $idxIds[$r.id] = $true }

    $missing = @($dbRows | Where-Object { -not $idxIds.ContainsKey($_.id) })
    $extra = @($idxRows | Where-Object { -not $dbIds.ContainsKey($_.id) })

    [pscustomobject]@{
        DatabaseUserThreads = $dbRows.Count
        SessionIndexRows = $idxRows.Count
        MissingFromIndex = $missing.Count
        ExtraInIndex = $extra.Count
    } | Format-List

    if ($missing.Count -gt 0) {
        Write-Host "Missing from session_index.jsonl:" -ForegroundColor Yellow
        $missing | Select-Object id, title | Format-Table -AutoSize
    }
    if ($extra.Count -gt 0) {
        Write-Host "Extra in session_index.jsonl:" -ForegroundColor Yellow
        $extra | Select-Object id, thread_name | Format-Table -AutoSize
    }

    Write-Section "Rollout Files"
    $rolloutRows = Get-JsonRows -Database $Database -Sql @"
SELECT id, title, rollout_path
FROM threads
WHERE archived=0
  AND source='vscode'
  AND thread_source='user';
"@
    $missingFiles = @($rolloutRows | Where-Object { -not (Test-Path -LiteralPath $_.rollout_path) })
    [pscustomobject]@{
        UserThreadsChecked = $rolloutRows.Count
        MissingRolloutFiles = $missingFiles.Count
    } | Format-List
    if ($missingFiles.Count -gt 0) {
        $missingFiles | Select-Object id, title, rollout_path | Format-List
    }
}

function Repair-CodexState {
    param([string]$Database, [string]$IndexPath, [string]$TargetProvider)

    $providerSql = $TargetProvider.Replace("'", "''")

    Write-Section "Applying Fixes"
    $sql = @"
.bail on
BEGIN IMMEDIATE;

UPDATE threads
SET thread_source='user'
WHERE archived=0
  AND source='vscode'
  AND has_user_event=1
  AND (thread_source IS NULL OR thread_source='');
SELECT changes() AS fixed_blank_thread_source;

UPDATE threads
SET cwd='\\?\' || cwd
WHERE substr(cwd,1,4)!='\\?\'
  AND length(cwd)>=3
  AND substr(cwd,2,2)=':\'
  AND substr(cwd,1,1) GLOB '[A-Za-z]';
SELECT changes() AS fixed_cwd_longpath_prefix;

UPDATE threads
SET model_provider='$providerSql'
WHERE archived=0
  AND source='vscode'
  AND thread_source='user'
  AND has_user_event=1
  AND model_provider!='$providerSql';
SELECT changes() AS fixed_visible_user_provider;

COMMIT;
PRAGMA wal_checkpoint(PASSIVE);
"@
    Invoke-Sqlite -Database $Database -Sql $sql

    Write-Host "Rebuilding session_index.jsonl from unarchived user threads..."
    Rebuild-SessionIndex -Database $Database -IndexPath $IndexPath
}

if (-not (Get-Command sqlite3 -ErrorAction SilentlyContinue)) {
    throw "sqlite3 was not found in PATH. Install sqlite3 or open a shell where sqlite3 is available."
}

$dbPath = Join-Path $CodexHome "state_5.sqlite"
$indexPath = Join-Path $CodexHome "session_index.jsonl"
$globalStatePath = Join-Path $CodexHome ".codex-global-state.json"

Assert-File $dbPath "state database"
Assert-File $indexPath "session index"
Assert-File $globalStatePath "global state"

if ([string]::IsNullOrWhiteSpace($TargetProvider)) {
    throw "TargetProvider cannot be empty."
}

$TargetProvider = Resolve-TargetProvider -Database $dbPath -TargetProvider $TargetProvider

Write-Section "Backup"
$backupDir = New-CodexStateBackup -CodexHome $CodexHome -BackupRoot $BackupRoot
Write-Host "Backup created: $backupDir" -ForegroundColor Green
Write-Host "Target provider: $TargetProvider" -ForegroundColor Green

Write-Section "Before"
Show-Health -Database $dbPath -IndexPath $indexPath -TargetProvider $TargetProvider

if ($Fix) {
    Repair-CodexState -Database $dbPath -IndexPath $indexPath -TargetProvider $TargetProvider
    Write-Section "After"
    Show-Health -Database $dbPath -IndexPath $indexPath -TargetProvider $TargetProvider
    Write-Host ""
    Write-Host "Fix complete. Restart Codex App if the sidebar has not refreshed." -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "Check-only mode. No repairs were applied. Re-run with -Fix to repair." -ForegroundColor Yellow
}
