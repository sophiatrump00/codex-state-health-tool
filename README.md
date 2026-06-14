# Codex State Health Tool V2.5

Safety-first local repair tools for Codex Desktop state, left-sidebar visibility,
and provider-switching display issues.

This project is designed for Windows Codex Desktop users who switch between
OpenAI and third-party providers and then see old conversations disappear from
the left sidebar. The default repair path keeps historical thread metadata
intact and avoids touching provider credentials, auth, sandbox settings, model
settings, or rollout content.

## What It Fixes

- Detects common Codex local-state failures with a six-category Doctor.
- Rebuilds the left-sidebar local index from valid rollout files when the index
  is missing entries.
- Automatically decides whether Safe Sync is needed after provider switching.
- Preserves `threads.model_provider` as historical thread metadata.
- Patches Codex Desktop display filtering from `modelProviders:null` to
  `modelProviders:[]  ` so the sidebar can show conversations from all
  providers after provider switching.
- Creates a patched Desktop copy when the Microsoft Store `WindowsApps`
  package cannot be edited directly.
- Keeps Project/Chats UI registry repair separate and cautious.
- Provides backup and undo paths before writes.

## Safety Boundary

Default Safe Sync writes only:

- `state_5.sqlite`
- `session_index.jsonl`

Default Safe Sync does not write:

- `config.toml`
- `auth.json`
- provider settings
- model settings
- sandbox or approval settings
- rollout JSONL content
- `.codex-global-state.json`

Safe Sync V2.5 refuses to insert half-populated thread rows. New inserts must
have complete required metadata from rollout `session_meta` / `turn_context`,
same-provider templates, schema defaults, or the row is rejected.

Provider Display Patch edits only the Codex Desktop application package or a
separate copied Desktop package. It does not edit `.codex` data.

## Quick Start

1. Copy `profiles/profile.example.toml` to `.local/profile.toml`.
2. Edit `codex_home` so it points to your Codex home, usually
   `%USERPROFILE%\.codex`.
3. Run the only daily launcher. It will request Administrator permission:

```cmd
RUN_SQLSwitchCodex.cmd
```

If `py` is available, this also works:

```cmd
py SQLSwitchCodex.py simple-menu
```

For a read-only V2.5 Doctor check without opening the menu:

```cmd
py SQLSwitchCodex.py doctor
```

The simple menu is intentionally ASCII-only to avoid mojibake in classic
Windows CMD and elevated Administrator consoles.

Simple menu entries:

- `1. Provider switch auto repair`
- `2. Manual Safe Sync index rebuild`
- `3. Doctor status`
- `4. Launch patched Desktop copy`
- `5. Advanced menu`

## Recommended Flow

After changing provider/API in CC Switch or `config.toml`:

1. Close Codex.
2. Run `RUN_SQLSwitchCodex.cmd`.
3. Choose `1. Provider switch auto repair`.
4. If the official `WindowsApps` package cannot be patched, the tool creates or
   updates a local patched Desktop copy automatically.
5. The tool automatically checks whether Safe Sync is needed.
6. Launch the patched copy from option `4` when needed.

Use `2. Manual Safe Sync index rebuild` only as an advanced/manual path. Option
`1` already detects `missing_db` / `missing_index` and prompts for Safe Sync only
when the local index is incomplete.

## Why Provider Display Patch Exists

Codex app-server treats provider filters differently depending on the request:

- `modelProviders:null` or omitted: use the current provider from config.
- `modelProviders:[]`: show all providers.

That means switching provider can make old conversations appear missing even
when the database and rollout files are still present. The safer fix is to make
the Desktop client request all providers for display. The tool does not rewrite
historical `threads.model_provider`, because that metadata can matter when
resuming old threads.

## Public Repository Hygiene

Do not publish local Codex state or generated repair artifacts. The `.gitignore`
is configured to exclude private and generated paths such as:

- `.local/`
- `backups/`
- `reports/`
- `patched-desktop/`
- `archive/`
- `__pycache__/`
- `*.sqlite`, `*.jsonl`, `*.asar`, `*.exe`, `*.dll`, `*.pak`, `*.bin`, `*.log`
- `auth.json`, `config.toml`

Before publishing, run:

```cmd
set PYTHONPATH=%CD%\src
python -m sqlswitchcodex_v21 publish-check
```

Also scan for private paths or secrets before pushing:

```cmd
rg -n "C:\\Users\\|api[_-]?key|authorization|bearer|auth.json|config.toml" .
```

## Legacy Scripts

The `scripts/*.ps1` files are legacy V2.1/V2.1.1 helpers. They are kept for
reference and compatibility, but the recommended current entry is the Python
launcher:

```cmd
RUN_SQLSwitchCodex.cmd
```
