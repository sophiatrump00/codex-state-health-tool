# SQLSwitchCodex V2.5 Guide

This guide is written in ASCII English so it remains readable in normal CMD,
Administrator CMD, and PowerShell consoles with older code pages.

## Main Entry

Run from the tool folder:

```cmd
RUN_SQLSwitchCodex.cmd
```

Or run Python directly:

```cmd
py SQLSwitchCodex.py simple-menu
```

The launcher requests Administrator permission, looks for Python in common
locations, and then opens the Simple Admin Menu.

Main menu entries:

- `1. Provider switch auto repair`
- `2. Manual Safe Sync index rebuild`
- `3. Doctor status`
- `4. Launch patched Desktop copy`
- `5. Advanced menu`

## Provider Switch Flow

After changing provider/API in CC Switch or `config.toml`:

1. Close Codex.
2. Run `RUN_SQLSwitchCodex.cmd`.
3. Choose `1. Provider switch auto repair`.
4. If direct patching is blocked by `WindowsApps`, the tool creates or updates
   a local patched Desktop copy.
5. The tool automatically decides whether Safe Sync is needed.
6. Use option `4` to launch the patched Desktop copy when needed.

Safe Sync is not required for ordinary provider switching once the display
patch is working. V2.5 runs it only when Doctor detects missing local index
entries, such as `missing_db > 0` or `missing_index > 0`.

## The Core Idea

There are two different problems that can look like "my left sidebar is gone":

1. The local Codex index is incomplete.
2. Codex Desktop is filtering the sidebar to only the current provider.

These need different fixes.

Safe Sync handles the first problem. Provider Display Patch handles the second
problem.

## Safe Sync Boundary

Safe Sync may write:

- `state_5.sqlite`
- `session_index.jsonl`

Safe Sync does not write:

- `config.toml`
- `auth.json`
- provider settings
- model settings
- sandbox or approval settings
- rollout JSONL content
- `.codex-global-state.json`

Safe Sync imports only strict, valid, rollout-backed user threads. Partial or
user-only assets are kept as recovery material and are not bulk-imported into
the Codex database.

Safe Sync V2.5 refuses half-populated inserts. Required thread metadata is
filled from rollout `session_meta` / `turn_context`, same-provider templates,
schema defaults, or the row is rejected before any database write.

## Provider Display Patch

Provider switching can hide old conversations because Codex Desktop may request
the thread list with:

```text
modelProviders:null
```

The app-server interprets that as "filter to the current provider". The display
patch changes the Desktop request marker to:

```text
modelProviders:[]  
```

The two-byte padding keeps the binary marker length unchanged.

This patch does not change `.codex`, rollouts, auth, provider config, models,
or sandbox settings.

## If WindowsApps Blocks Direct Patch

Microsoft Store packages under `WindowsApps` can be non-writable even for an
Administrator or SYSTEM task. In that case, use the copied Desktop fallback:

```cmd
RUN_SQLSwitchCodex.cmd
```

Choose option `1` to create/update the copied Desktop fallback, then option `4`
to launch it.

The copied app lives under:

```text
patched-desktop\app
```

The original Microsoft Store package is not modified.

## Doctor Categories

Doctor reports six categories:

- Local DB
- Left Sidebar
- Rollout
- App State
- Sandbox / Runtime
- Environment

Provider or network failures are classified separately from application-state
failures. Provider/API errors should not automatically trigger database undo.

## Project/Chats UI Registry Repair

Use this only when:

- Doctor shows database and rollout state are healthy.
- Conversations exist.
- Project or Chats grouping is still wrong.

This repair is separate because it touches `.codex-global-state.json`.
It is limited to sidebar UI registry keys and should not touch provider, auth,
sandbox, runtime, cache, or process-manager keys.

## Emergency Levels

Emergency repair is for real application-state failure, not ordinary provider
switching.

- Level 1: state rebuild.
- Level 2: database rebuild.
- Level 3: near-fresh install while preserving sessions.

Moved files go under `backups\emergency_*`; the tool avoids direct deletion.

## Publishing Checklist

Never publish local state:

- `.local/`
- `backups/`
- `reports/`
- `patched-desktop/`
- `archive/`
- `*.sqlite`
- `*.jsonl`
- `*.asar`
- `*.exe`
- `*.dll`
- `*.pak`
- `*.bin`
- `*.log`
- `auth.json`
- `config.toml`

Run before publishing:

```cmd
set PYTHONPATH=%CD%\src
python -m sqlswitchcodex_v21 publish-check
```
