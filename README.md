# Codex State Health Tool

A small Windows repair tool for Codex Desktop local thread metadata.

It helps when Codex Desktop's left sidebar does not show all expected local chats even though `state_5.sqlite` still contains them.

## What It Does

The tool checks and optionally repairs common local metadata drift:

- Blank `thread_source` on real user threads.
- Mixed `model_provider` values after switching login/API key/provider modes.
- Mixed Windows path formats such as `D:\...` and `\\?\D:\...`.
- `session_index.jsonl` not matching unarchived user threads.

It always creates a backup before checking or fixing.

## What It Does Not Touch

- Archived threads.
- `guardian` / `subagent` internal threads.
- Conversation content.
- Rollout JSONL content.

## Requirements

- Windows.
- Codex Desktop local state under `%USERPROFILE%\.codex`.
- `sqlite3` available in `PATH`.

## Files

Keep these two files in the same folder:

- `Repair-CodexStateHealth.bat`
- `Repair-CodexStateHealth.ps1`

## Quick Start

Double-click:

```text
Repair-CodexStateHealth.bat
```

Main menu:

```text
1. Check only
2. Fix and sync to latest session provider
3. Advanced: choose target provider
0. Exit
```

Recommended normal use:

```text
2. Fix and sync to latest session provider
```

This detects the `model_provider` from the newest user thread and synchronizes visible user threads to that provider.

## Advanced Provider Menu

```text
1. auto detect from newest user thread (Recommended)
2. openai
3. custom
4. anyrouter
5. Type manually
0. Back
```

Use this only if you know which provider Codex Desktop currently expects.

## Backup Location

Each run creates a timestamped backup folder under:

```text
codex-health-backups
```

The backup includes:

- `state_5.sqlite` SQLite backup.
- `state_5.sqlite` raw copy.
- WAL/SHM copies when present.
- `session_index.jsonl`.
- `.codex-global-state.json`.

## Notes

After applying fixes, restart Codex Desktop if the sidebar does not refresh immediately.

This tool is intentionally conservative. It repairs metadata used by the sidebar; it does not modify conversation text.

