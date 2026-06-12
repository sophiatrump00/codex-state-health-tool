# Codex State Health Tool

A small Windows-friendly utility for backing up, checking, and restoring local Codex App conversation state when switching between OpenAI login mode and a custom OpenAI-compatible API provider.

The tool keeps a local SQL master database, stores Codex user conversations into it, and can restore missing conversations/events back into the current provider mode.

## Safety Notice

Do not publish your generated data files. They can contain private prompts, file paths, project names, provider details, and local configuration.

This repository is intended to publish only source code and documentation. The included `.gitignore` excludes generated/private files such as:

- `codex_conversations.sqlite`
- `backups/`
- `state_5.sqlite`
- `session_index.jsonl`
- `.codex-global-state.json`
- `config.toml`
- `.env`

## Requirements

- Windows
- Python 3.11 or newer
- Codex App local state in `%USERPROFILE%\.codex`

The script uses only Python standard library modules.

## Quick Start

From the project directory:

```powershell
py .\SQLSwitchCodex.py
```

Interactive mode:

```text
1. Save all Codex conversations to SQL master
2. Sync SQL master to current mode
3. Check status
0. Exit
```

Recommended daily flow:

```powershell
py .\SQLSwitchCodex.py store
py .\SQLSwitchCodex.py restore
```

`restore` runs a protective `store` first unless `--no-store-first` is supplied.

## Common Commands

Save all current Codex user conversations into the SQL master:

```powershell
py .\SQLSwitchCodex.py store
```

Restore the SQL master into the current detected mode:

```powershell
py .\SQLSwitchCodex.py restore
```

Force restore into OpenAI login mode:

```powershell
py .\SQLSwitchCodex.py --target openai restore
```

Force restore into custom provider mode:

```powershell
py .\SQLSwitchCodex.py --target custom restore
```

Show a concise status report:

```powershell
py .\SQLSwitchCodex.py status
```

Preview restore without writing files:

```powershell
py .\SQLSwitchCodex.py --target openai restore --dry-run
```

Use a different Codex home directory:

```powershell
py .\SQLSwitchCodex.py --codex-home "C:\Users\you\.codex" status
```

## What The Tool Touches

By default, the tool reads/writes local Codex state under:

```text
%USERPROFILE%\.codex
```

It may read or update:

- `state_5.sqlite`
- `session_index.jsonl`
- `.codex-global-state.json`
- rollout JSONL files under `sessions/`

It creates generated files beside the script by default:

- `codex_conversations.sqlite`
- `backups/`

These files are private and should not be committed.

## Provider Detection

The tool can detect the target provider from:

- explicit `--target`
- optional CC Switch DB/SQL export
- top-level `model_provider` in `config.toml`

If no top-level provider is found, OpenAI login mode is assumed.

## Legacy Commands

Older commands are still available:

```powershell
py .\SQLSwitchCodex.py verify
py .\SQLSwitchCodex.py sync
```

For normal use, prefer `store`, `restore`, and `status`.

## Before Publishing

Run this check before pushing:

```powershell
git status --short
```

Only source/documentation files should appear. Do not commit databases, backups, state files, session logs, or local configuration.
