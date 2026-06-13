# SQLSwitchCodex V2.4 Guide

This guide is written in ASCII English so it remains readable in normal CMD,
Administrator CMD, and PowerShell consoles with older code pages.

## Main Entry

Run from the tool folder:

```cmd
RUN_SQLSwitchCodex.cmd
```

Or run Python directly:

```cmd
py SQLSwitchCodex.py english-menu
```

The launcher looks for Python in common locations and then opens the English
Safe Menu.

Main menu entries:

- `1. Doctor status`
- `2. Apply Provider Display Patch`
- `3. Undo Provider Display Patch`
- `4. Provider Patch Status`
- `5. Create Patched Desktop Copy`
- `6. Safe Sync: check and create plan`
- `7. Safe Sync: apply latest plan`

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
CREATE_PATCHED_DESKTOP_COPY.cmd
```

Then launch Codex through the generated local launcher:

```cmd
RUN_PATCHED_CODEX_DESKTOP.cmd
```

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
