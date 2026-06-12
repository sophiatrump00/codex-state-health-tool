#!/usr/bin/env python3
"""
SQLSwitchCodex

Keep Codex App conversations visible when switching between OpenAI login mode
and third-party API/provider mode.

Principle: never infer the repair direction from state_5.sqlite. The Codex
state database is the thing being repaired, so it cannot be the source of truth.
Provider direction comes from an explicit CLI target, a supplied CC Switch DB/SQL
current provider, or config.toml. If config.toml has no top-level model_provider,
OpenAI login mode is assumed and the target provider is "openai".
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


APP_NAME = "SQLSwitchCodex"
CODEX_SOURCE = "vscode"
DEFAULT_OFFICIAL_PROVIDER = "openai"
TOOL_DIR = Path(__file__).resolve().parent
DEFAULT_MASTER_DB = TOOL_DIR / "codex_conversations.sqlite"
THREAD_ID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


@dataclass(frozen=True)
class ProviderDecision:
    provider: str
    source: str
    detail: str


@dataclass(frozen=True)
class Paths:
    codex_home: Path
    state_db: Path
    session_index: Path
    global_state: Path
    config_toml: Path


class StopRepair(RuntimeError):
    """Raised for deliberate safety stops."""


def utc_timestamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%d_%H%M%S")


def print_section(title: str) -> None:
    print()
    print(f"=== {title} ===")


def default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def build_paths(codex_home: Path) -> Paths:
    return Paths(
        codex_home=codex_home,
        state_db=codex_home / "state_5.sqlite",
        session_index=codex_home / "session_index.jsonl",
        global_state=codex_home / ".codex-global-state.json",
        config_toml=codex_home / "config.toml",
    )


def require_file(path: Path, label: str) -> None:
    if not path.exists():
        raise StopRepair(f"{label} not found: {path}")


def sql_quote(value: str) -> str:
    return value.replace("'", "''")


def normalize_path_text(value: str | None) -> str:
    if not value:
        return ""
    path = value.strip().replace("/", "\\")
    low = path.lower()
    if low.startswith("\\\\?\\unc\\"):
        path = "\\\\" + path[8:]
    elif low.startswith("\\\\?\\"):
        path = path[4:]
    if len(path) > 3 and path.endswith("\\"):
        path = path.rstrip("\\")
    return path


def is_under_path(path: str, root: str) -> bool:
    p = normalize_path_text(path).lower()
    r = normalize_path_text(root).lower()
    return p == r or p.startswith(r + "\\")


def unique_by_normalized(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value:
            continue
        key = normalize_path_text(value).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(normalize_path_text(value))
    return result


def parse_top_level_model_provider(config_text: str) -> str | None:
    for raw_line in config_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            break
        if line.startswith("model_provider"):
            left, sep, right = line.partition("=")
            if sep and left.strip() == "model_provider":
                value = right.strip()
                if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
                    return value[1:-1].strip()
                return value.strip()
    return None


def parse_provider_from_config_file(config_path: Path) -> ProviderDecision | None:
    if not config_path.exists():
        return None
    text = config_path.read_text(encoding="utf-8", errors="replace")
    provider = parse_top_level_model_provider(text)
    if provider:
        return ProviderDecision(provider=provider, source="config.toml", detail=str(config_path))
    return None


def is_official_cc_provider(row: sqlite3.Row) -> bool:
    id_value = str(row["id"] or "").lower()
    name = str(row["name"] or "").lower()
    category = str(row["category"] or "").lower()
    provider_type = str(row["provider_type"] or "").lower()
    return (
        id_value in {"codex-official", "openai", "official"}
        or category == "official"
        or provider_type == "official"
        or "openai official" in name
    )


def load_sql_export_to_temp_db(sql_path: Path) -> Path:
    require_file(sql_path, "CC Switch SQL export")
    temp_dir = TOOL_DIR / ".tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    db_path = temp_dir / "cc-switch-export.sqlite"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        sql_text = sql_path.read_text(encoding="utf-8", errors="replace")
        conn.executescript(sql_text)
        conn.commit()
    finally:
        conn.close()
    return db_path


def decision_from_cc_switch_db(db_path: Path) -> ProviderDecision:
    require_file(db_path, "CC Switch SQLite DB")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, app_type, name, settings_config, category, provider_type, is_current
            FROM providers
            WHERE app_type='codex' AND is_current=1
            ORDER BY id
            """
        ).fetchall()
    except sqlite3.Error as exc:
        raise StopRepair(f"Could not read CC Switch providers table: {exc}") from exc
    finally:
        conn.close()

    if not rows:
        raise StopRepair("CC Switch DB has no current codex provider. Choose --target manually.")
    if len(rows) > 1:
        names = ", ".join(f"{r['id']}:{r['name']}" for r in rows)
        raise StopRepair(f"CC Switch DB has multiple current codex providers: {names}")

    row = rows[0]
    if is_official_cc_provider(row):
        return ProviderDecision(
            provider=DEFAULT_OFFICIAL_PROVIDER,
            source="cc-switch",
            detail=f"current provider {row['id']} ({row['name']}) is official",
        )

    try:
        settings = json.loads(row["settings_config"] or "{}")
    except json.JSONDecodeError as exc:
        raise StopRepair(f"Current CC Switch provider settings_config is not valid JSON: {exc}") from exc

    config_text = settings.get("config")
    if not isinstance(config_text, str) or not config_text.strip():
        raise StopRepair(
            f"Current third-party provider {row['id']} ({row['name']}) has no config text. "
            "Choose --target manually."
        )

    provider = parse_top_level_model_provider(config_text)
    if not provider:
        raise StopRepair(
            f"Current third-party provider {row['id']} ({row['name']}) has no top-level "
            "model_provider. Choose --target manually."
        )

    return ProviderDecision(
        provider=provider,
        source="cc-switch",
        detail=f"current provider {row['id']} ({row['name']}) config model_provider",
    )


def resolve_target_provider(args: argparse.Namespace, paths: Paths) -> ProviderDecision:
    if args.target != "auto":
        return ProviderDecision(provider=args.target, source="manual", detail="--target")

    temp_cc_db: Path | None = None
    if args.cc_switch_sql:
        temp_cc_db = load_sql_export_to_temp_db(Path(args.cc_switch_sql))
        return decision_from_cc_switch_db(temp_cc_db)
    if args.cc_switch_db:
        return decision_from_cc_switch_db(Path(args.cc_switch_db))

    config_decision = parse_provider_from_config_file(paths.config_toml)
    if config_decision:
        return config_decision

    return ProviderDecision(
        provider=DEFAULT_OFFICIAL_PROVIDER,
        source="default",
        detail="no top-level model_provider found; treating as OpenAI login mode",
    )


def connect_state_db(path: Path, readonly: bool) -> sqlite3.Connection:
    require_file(path, "Codex state database")
    if readonly:
        uri = f"file:{path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_active_threads(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, title, cwd, model_provider, updated_at_ms, updated_at, rollout_path
        FROM threads
        WHERE archived=0
          AND source=?
          AND thread_source='user'
          AND has_user_event=1
        ORDER BY COALESCE(updated_at_ms, updated_at) ASC, id ASC
        """,
        (CODEX_SOURCE,),
    ).fetchall()


def backup_codex_state(paths: Paths, backup_root: Path) -> Path:
    backup_dir = backup_root / f"backup_{utc_timestamp()}"
    backup_dir.mkdir(parents=True, exist_ok=False)

    conn = sqlite3.connect(paths.state_db)
    try:
        out = sqlite3.connect(backup_dir / "state_5.sqlite.sqlite-backup")
        try:
            conn.backup(out)
        finally:
            out.close()
    finally:
        conn.close()

    shutil.copy2(paths.state_db, backup_dir / "state_5.sqlite.raw-copy")
    for suffix in ("-wal", "-shm"):
        p = Path(str(paths.state_db) + suffix)
        if p.exists():
            shutil.copy2(p, backup_dir / f"state_5.sqlite{suffix}.raw-copy")

    shutil.copy2(paths.session_index, backup_dir / "session_index.jsonl.before")
    shutil.copy2(paths.global_state, backup_dir / ".codex-global-state.json.before")
    if paths.config_toml.exists():
        shutil.copy2(paths.config_toml, backup_dir / "config.toml.before")

    return backup_dir


def repair_sqlite(conn: sqlite3.Connection, provider: str) -> dict[str, int]:
    stats: dict[str, int] = {}
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")

    cur.execute(
        """
        UPDATE threads
        SET cwd = CASE
          WHEN substr(cwd,1,8)='\\\\?\\UNC\\' THEN '\\\\' || substr(cwd,9)
          WHEN substr(cwd,1,4)='\\\\?\\' THEN substr(cwd,5)
          ELSE cwd
        END
        WHERE cwd IS NOT NULL AND cwd!=''
        """
    )
    stats["normalized_cwd_to_plain"] = cur.rowcount

    cur.execute(
        """
        UPDATE threads
        SET has_user_event=1
        WHERE archived=0
          AND source=?
          AND has_user_event=0
          AND (COALESCE(first_user_message,'')!='' OR COALESCE(preview,'')!='')
        """,
        (CODEX_SOURCE,),
    )
    stats["fixed_has_user_event"] = cur.rowcount

    cur.execute(
        """
        UPDATE threads
        SET thread_source='user'
        WHERE archived=0
          AND source=?
          AND has_user_event=1
          AND COALESCE(thread_source,'')=''
        """,
        (CODEX_SOURCE,),
    )
    stats["fixed_blank_thread_source"] = cur.rowcount

    cur.execute(
        """
        UPDATE threads
        SET model_provider=?
        WHERE archived=0
          AND source=?
          AND thread_source='user'
          AND has_user_event=1
          AND model_provider!=?
        """,
        (provider, CODEX_SOURCE, provider),
    )
    stats["fixed_provider_to_target"] = cur.rowcount

    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return stats


def iso_from_ms(ms_value: Any) -> str:
    ms = int(ms_value or 0)
    if ms <= 0:
        return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
    value = dt.datetime.fromtimestamp(ms / 1000, tz=dt.UTC)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def rebuild_session_index(conn: sqlite3.Connection, path: Path) -> None:
    rows = fetch_active_threads(conn)
    lines = []
    for row in rows:
        lines.append(
            json.dumps(
                {
                    "id": row["id"],
                    "thread_name": row["title"],
                    "updated_at": iso_from_ms(row["updated_at_ms"] or row["updated_at"]),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def projectless_roots() -> list[str]:
    roots = [str(Path.home() / "Documents" / "Codex")]
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        roots.append(str(Path(userprofile) / "Documents" / "Codex"))
    return unique_by_normalized(roots)


def repair_global_state(conn: sqlite3.Connection, path: Path) -> None:
    state = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    threads = fetch_active_threads(conn)
    roots = projectless_roots()

    project_roots: list[str] = []
    hints: dict[str, str] = {}
    outputs: dict[str, str] = {}
    projectless_ids: list[str] = []

    for row in threads:
        cwd = normalize_path_text(row["cwd"])
        matched_projectless = next((root for root in roots if is_under_path(cwd, root)), None)
        if matched_projectless:
            projectless_ids.append(row["id"])
            hints[row["id"]] = normalize_path_text(matched_projectless)
            outputs[row["id"]] = normalize_path_text(str(Path(cwd) / "outputs"))
        else:
            project_roots.append(cwd)
            hints[row["id"]] = cwd

    state["project-order"] = unique_by_normalized(project_roots)
    state["electron-saved-workspace-roots"] = unique_by_normalized(project_roots)
    state["active-workspace-roots"] = unique_by_normalized(
        normalize_path_text(v) for v in state.get("active-workspace-roots", [])
    )
    state["projectless-thread-ids"] = unique_by_normalized(projectless_ids)
    state["thread-workspace-root-hints"] = hints
    state["thread-projectless-output-directories"] = outputs

    labels = state.get("electron-workspace-root-labels", {})
    if isinstance(labels, dict):
        state["electron-workspace-root-labels"] = {
            normalize_path_text(k): v for k, v in labels.items()
        }

    path.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def read_session_index_ids(path: Path) -> list[str]:
    ids: list[str] = []
    if not path.exists():
        return ids
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("id"):
            ids.append(str(obj["id"]))
    return ids


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_rollout_path(path_value: str | None) -> str:
    return normalize_path_text(path_value)


def default_master_path(value: str | None = None) -> Path:
    return Path(value) if value else DEFAULT_MASTER_DB


def ensure_master_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            conversation_key TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            cwd TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'vscode',
            preferred_rollout_path TEXT NOT NULL DEFAULT '',
            archived INTEGER NOT NULL DEFAULT 0,
            archived_at INTEGER,
            has_user_event INTEGER NOT NULL DEFAULT 0,
            thread_source TEXT,
            first_user_message TEXT NOT NULL DEFAULT '',
            preview TEXT NOT NULL DEFAULT '',
            created_at INTEGER,
            updated_at INTEGER,
            created_at_ms INTEGER,
            updated_at_ms INTEGER,
            last_model_provider TEXT NOT NULL DEFAULT '',
            providers_json TEXT NOT NULL DEFAULT '[]',
            thread_json TEXT NOT NULL DEFAULT '{}',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS provider_observations (
            thread_id TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT '',
            observed_at TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            cwd TEXT NOT NULL DEFAULT '',
            thread_source TEXT,
            archived INTEGER NOT NULL DEFAULT 0,
            has_user_event INTEGER NOT NULL DEFAULT 0,
            rollout_path TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(thread_id, provider, observed_at)
        );

        CREATE TABLE IF NOT EXISTS rollout_events (
            thread_id TEXT NOT NULL,
            event_key TEXT NOT NULL,
            event_order INTEGER NOT NULL DEFAULT 0,
            timestamp TEXT,
            event_type TEXT,
            payload_type TEXT,
            role TEXT,
            raw_json TEXT NOT NULL,
            raw_hash TEXT NOT NULL,
            source_rollout_path TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY(thread_id, event_key)
        );

        CREATE TABLE IF NOT EXISTS state_threads_snapshot (
            id TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            model_provider TEXT NOT NULL DEFAULT '',
            thread_source TEXT,
            has_user_event INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0,
            title TEXT NOT NULL DEFAULT '',
            cwd TEXT NOT NULL DEFAULT '',
            rollout_path TEXT NOT NULL DEFAULT '',
            row_json TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            PRIMARY KEY(id, source)
        );

        CREATE INDEX IF NOT EXISTS idx_rollout_events_thread_order
            ON rollout_events(thread_id, timestamp, event_order);
        CREATE INDEX IF NOT EXISTS idx_provider_observations_thread
            ON provider_observations(thread_id);
        CREATE INDEX IF NOT EXISTS idx_state_threads_snapshot_source
            ON state_threads_snapshot(source, has_user_event, archived);
        """
    )
    conn.commit()


def connect_master_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_master_schema(conn)
    return conn


def conversation_key_from_thread(data: dict[str, Any]) -> str:
    parts = [
        normalize_path_text(str(data.get("cwd") or "")).lower(),
        str(data.get("title") or "").strip(),
        str(data.get("first_user_message") or "").strip()[:1000],
    ]
    return sha256_text("\n".join(parts))


def provider_list(existing_json: str | None, provider: str | None) -> list[str]:
    providers: set[str] = set()
    if existing_json:
        try:
            parsed = json.loads(existing_json)
            if isinstance(parsed, list):
                providers.update(str(item) for item in parsed if item)
        except json.JSONDecodeError:
            pass
    if provider:
        providers.add(str(provider))
    return sorted(providers)


def extract_text_from_payload(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    message = payload.get("message")
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        texts: list[str] = []
        for item in message:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    texts.append(text)
        if texts:
            return "\n".join(texts)
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    texts.append(text)
        return "\n".join(texts)
    return ""


def guess_thread_id_from_path(path: Path) -> str | None:
    match = THREAD_ID_RE.search(path.name)
    return match.group(1) if match else None


def rollout_metadata(path: Path) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "thread_id": guess_thread_id_from_path(path),
        "source": "",
        "model_provider": "",
        "cwd": "",
        "title": "",
        "first_user_message": "",
        "preview": "",
        "has_user_event": 0,
        "created_at_ms": 0,
        "updated_at_ms": 0,
    }
    if not path.exists():
        return meta

    try:
        stat = path.stat()
        meta["created_at_ms"] = int(stat.st_ctime * 1000)
        meta["updated_at_ms"] = int(stat.st_mtime * 1000)
    except OSError:
        pass

    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                payload = obj.get("payload")
                if obj.get("type") == "session_meta" and isinstance(payload, dict):
                    meta["thread_id"] = str(payload.get("id") or meta["thread_id"] or "")
                    meta["source"] = payload.get("source") or meta["source"]
                    meta["model_provider"] = payload.get("model_provider") or meta["model_provider"]
                    meta["cwd"] = normalize_path_text(payload.get("cwd") or meta["cwd"])
                    continue
                if isinstance(payload, dict) and payload.get("type") == "user_message":
                    text = extract_text_from_payload(payload)
                    if text and not meta["first_user_message"]:
                        meta["first_user_message"] = text
                        meta["preview"] = text[:240]
                        meta["title"] = text.splitlines()[0][:160]
                    meta["has_user_event"] = 1
    except OSError:
        return meta

    return meta


def synthetic_thread_from_rollout(path: Path, meta: dict[str, Any], paths: Paths) -> dict[str, Any]:
    now_ms = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    created_ms = safe_int(meta.get("created_at_ms"), now_ms)
    updated_ms = safe_int(meta.get("updated_at_ms"), created_ms)
    thread_id = str(meta.get("thread_id") or guess_thread_id_from_path(path) or sha256_text(str(path))[:36])
    title = str(meta.get("title") or meta.get("first_user_message") or thread_id).strip()
    return {
        "id": thread_id,
        "rollout_path": normalize_rollout_path(str(path)),
        "created_at": created_ms,
        "updated_at": updated_ms,
        "source": CODEX_SOURCE,
        "model_provider": str(meta.get("model_provider") or ""),
        "cwd": normalize_path_text(str(meta.get("cwd") or paths.codex_home)),
        "title": title[:240],
        "sandbox_policy": "",
        "approval_mode": "",
        "tokens_used": 0,
        "has_user_event": 1,
        "archived": 0,
        "archived_at": None,
        "git_sha": None,
        "git_branch": None,
        "git_origin_url": None,
        "cli_version": "",
        "first_user_message": str(meta.get("first_user_message") or ""),
        "agent_nickname": None,
        "agent_role": None,
        "memory_mode": "enabled",
        "model": None,
        "reasoning_effort": None,
        "agent_path": None,
        "created_at_ms": created_ms,
        "updated_at_ms": updated_ms,
        "thread_source": "user",
        "preview": str(meta.get("preview") or meta.get("first_user_message") or "")[:400],
    }


def upsert_conversation(master: sqlite3.Connection, data: dict[str, Any], observed_at: str) -> None:
    thread_id = str(data["id"])
    provider = str(data.get("model_provider") or "")
    normalized = dict(data)
    normalized["cwd"] = normalize_path_text(str(data.get("cwd") or ""))
    normalized["rollout_path"] = normalize_rollout_path(str(data.get("rollout_path") or ""))
    existing = master.execute(
        "SELECT * FROM conversations WHERE id=?",
        (thread_id,),
    ).fetchone()

    providers = provider_list(existing["providers_json"] if existing else None, provider)
    archived = safe_int(normalized.get("archived"))
    has_user_event = 1 if safe_int(normalized.get("has_user_event")) else 0
    thread_source = str(normalized.get("thread_source") or "")
    updated_ms = safe_int(normalized.get("updated_at_ms"), safe_int(normalized.get("updated_at")))

    if existing:
        old_updated_ms = safe_int(existing["updated_at_ms"], safe_int(existing["updated_at"]))
        merged_archived = 1 if safe_int(existing["archived"]) and archived else 0
        merged_has_user_event = 1 if safe_int(existing["has_user_event"]) or has_user_event else 0
        merged_thread_source = "user" if thread_source == "user" or existing["thread_source"] == "user" else thread_source
        if updated_ms < old_updated_ms:
            master.execute(
                """
                UPDATE conversations
                SET archived=?, has_user_event=?, thread_source=?, last_model_provider=?,
                    providers_json=?, last_seen_at=?
                WHERE id=?
                """,
                (
                    merged_archived,
                    merged_has_user_event,
                    merged_thread_source,
                    provider or existing["last_model_provider"],
                    json.dumps(providers, ensure_ascii=False, separators=(",", ":")),
                    observed_at,
                    thread_id,
                ),
            )
            return
    else:
        merged_archived = archived
        merged_has_user_event = has_user_event
        merged_thread_source = thread_source

    master.execute(
        """
        INSERT INTO conversations (
            id, conversation_key, title, cwd, source, preferred_rollout_path,
            archived, archived_at, has_user_event, thread_source,
            first_user_message, preview, created_at, updated_at, created_at_ms, updated_at_ms,
            last_model_provider, providers_json, thread_json, first_seen_at, last_seen_at
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            conversation_key=excluded.conversation_key,
            title=excluded.title,
            cwd=excluded.cwd,
            source=excluded.source,
            preferred_rollout_path=excluded.preferred_rollout_path,
            archived=excluded.archived,
            archived_at=excluded.archived_at,
            has_user_event=excluded.has_user_event,
            thread_source=excluded.thread_source,
            first_user_message=excluded.first_user_message,
            preview=excluded.preview,
            created_at=excluded.created_at,
            updated_at=excluded.updated_at,
            created_at_ms=excluded.created_at_ms,
            updated_at_ms=excluded.updated_at_ms,
            last_model_provider=excluded.last_model_provider,
            providers_json=excluded.providers_json,
            thread_json=excluded.thread_json,
            last_seen_at=excluded.last_seen_at
        """,
        (
            thread_id,
            conversation_key_from_thread(normalized),
            str(normalized.get("title") or ""),
            str(normalized.get("cwd") or ""),
            str(normalized.get("source") or CODEX_SOURCE),
            str(normalized.get("rollout_path") or ""),
            merged_archived,
            normalized.get("archived_at"),
            merged_has_user_event,
            merged_thread_source,
            str(normalized.get("first_user_message") or ""),
            str(normalized.get("preview") or ""),
            normalized.get("created_at"),
            normalized.get("updated_at"),
            normalized.get("created_at_ms"),
            normalized.get("updated_at_ms"),
            provider,
            json.dumps(providers, ensure_ascii=False, separators=(",", ":")),
            json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
            observed_at,
            observed_at,
        ),
    )


def insert_provider_observation(master: sqlite3.Connection, data: dict[str, Any], observed_at: str) -> None:
    master.execute(
        """
        INSERT OR IGNORE INTO provider_observations (
            thread_id, provider, observed_at, title, cwd, thread_source,
            archived, has_user_event, rollout_path
        )
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            str(data.get("id") or ""),
            str(data.get("model_provider") or ""),
            observed_at,
            str(data.get("title") or ""),
            normalize_path_text(str(data.get("cwd") or "")),
            data.get("thread_source"),
            safe_int(data.get("archived")),
            safe_int(data.get("has_user_event")),
            normalize_rollout_path(str(data.get("rollout_path") or "")),
        ),
    )


def upsert_thread_snapshot(master: sqlite3.Connection, data: dict[str, Any], observed_at: str) -> None:
    master.execute(
        """
        INSERT INTO state_threads_snapshot (
            id, source, model_provider, thread_source, has_user_event,
            archived, title, cwd, rollout_path, row_json, observed_at
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id, source) DO UPDATE SET
            model_provider=excluded.model_provider,
            thread_source=excluded.thread_source,
            has_user_event=excluded.has_user_event,
            archived=excluded.archived,
            title=excluded.title,
            cwd=excluded.cwd,
            rollout_path=excluded.rollout_path,
            row_json=excluded.row_json,
            observed_at=excluded.observed_at
        """,
        (
            str(data.get("id") or ""),
            str(data.get("source") or ""),
            str(data.get("model_provider") or ""),
            data.get("thread_source"),
            safe_int(data.get("has_user_event")),
            safe_int(data.get("archived")),
            str(data.get("title") or ""),
            normalize_path_text(str(data.get("cwd") or "")),
            normalize_rollout_path(str(data.get("rollout_path") or "")),
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            observed_at,
        ),
    )


def event_identity(thread_id: str, obj: Any, raw: str) -> str:
    if isinstance(obj, dict) and obj.get("type") == "session_meta":
        return "session_meta"
    payload = obj.get("payload") if isinstance(obj, dict) else None
    candidates: list[str] = []
    if isinstance(payload, dict):
        for key in ("client_id", "id", "event_id", "call_id", "response_id", "turn_id"):
            value = payload.get(key)
            if value:
                candidates.append(f"{key}:{value}")
        item = payload.get("item")
        if isinstance(item, dict):
            for key in ("id", "call_id"):
                value = item.get(key)
                if value:
                    candidates.append(f"item.{key}:{value}")
        response = payload.get("response")
        if isinstance(response, dict) and response.get("id"):
            candidates.append(f"response.id:{response['id']}")
    if candidates:
        event_type = obj.get("type") if isinstance(obj, dict) else ""
        payload_type = payload.get("type") if isinstance(payload, dict) else ""
        return sha256_text("|".join([thread_id, str(event_type), str(payload_type), *candidates]))
    try:
        normalized = stable_json(obj)
    except (TypeError, ValueError):
        normalized = raw
    return "hash:" + sha256_text(normalized)


def rollout_event_fields(obj: Any) -> tuple[str | None, str | None, str | None, str | None]:
    if not isinstance(obj, dict):
        return None, None, None, None
    event_type = obj.get("type")
    timestamp = obj.get("timestamp")
    payload = obj.get("payload")
    payload_type = None
    role = None
    if isinstance(payload, dict):
        payload_type = payload.get("type")
        role = payload.get("role")
        item = payload.get("item")
        if isinstance(item, dict):
            role = role or item.get("role")
            payload_type = payload_type or item.get("type")
    return (
        str(timestamp) if timestamp is not None else None,
        str(event_type) if event_type is not None else None,
        str(payload_type) if payload_type is not None else None,
        str(role) if role is not None else None,
    )


def import_rollout_events(
    master: sqlite3.Connection,
    thread_id: str,
    rollout_path: str,
    observed_at: str,
) -> dict[str, int]:
    stats = {"files_seen": 0, "files_missing": 0, "events_added": 0, "events_duplicate": 0}
    if not rollout_path:
        stats["files_missing"] += 1
        return stats
    path = Path(normalize_rollout_path(rollout_path))
    if not path.exists():
        stats["files_missing"] += 1
        return stats

    stats["files_seen"] += 1
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                raw = line.rstrip("\r\n")
                if not raw.strip():
                    continue
                try:
                    obj: Any = json.loads(raw)
                except json.JSONDecodeError:
                    obj = {"type": "raw_line", "payload": {"line": raw}}
                event_key = event_identity(thread_id, obj, raw)
                timestamp, event_type, payload_type, role = rollout_event_fields(obj)
                raw_hash = sha256_text(raw)
                cur = master.execute(
                    """
                    INSERT OR IGNORE INTO rollout_events (
                        thread_id, event_key, event_order, timestamp, event_type,
                        payload_type, role, raw_json, raw_hash, source_rollout_path,
                        first_seen_at, last_seen_at
                    )
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        thread_id,
                        event_key,
                        index,
                        timestamp,
                        event_type,
                        payload_type,
                        role,
                        raw,
                        raw_hash,
                        normalize_rollout_path(str(path)),
                        observed_at,
                        observed_at,
                    ),
                )
                if cur.rowcount:
                    stats["events_added"] += 1
                else:
                    stats["events_duplicate"] += 1
                    master.execute(
                        """
                        UPDATE rollout_events
                        SET last_seen_at=?
                        WHERE thread_id=? AND event_key=?
                        """,
                        (observed_at, thread_id, event_key),
                    )
    except OSError:
        stats["files_missing"] += 1
    return stats


def merge_stats(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + int(value)


def fetch_all_user_threads(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM threads
        WHERE source=?
          AND has_user_event=1
        ORDER BY COALESCE(updated_at_ms, updated_at, created_at_ms, created_at) ASC, id ASC
        """,
        (CODEX_SOURCE,),
    ).fetchall()


def store_all_conversations(paths: Paths, master_db: Path, include_orphans: bool = True) -> dict[str, Any]:
    require_file(paths.state_db, "Codex state database")
    observed_at = dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    stats: dict[str, Any] = {
        "raw_threads_seen": 0,
        "state_threads_seen": 0,
        "orphan_rollouts_seen": 0,
        "rollout_files_seen": 0,
        "rollout_files_missing": 0,
        "events_added": 0,
        "events_duplicate": 0,
    }

    state_conn = connect_state_db(paths.state_db, readonly=True)
    master = connect_master_db(master_db)
    state_rollouts: set[str] = set()
    try:
        master.execute("BEGIN")
        all_thread_rows = state_conn.execute("SELECT * FROM threads ORDER BY id ASC").fetchall()
        stats["raw_threads_seen"] = len(all_thread_rows)
        for row in all_thread_rows:
            data = row_to_dict(row)
            data["cwd"] = normalize_path_text(str(data.get("cwd") or ""))
            data["rollout_path"] = normalize_rollout_path(str(data.get("rollout_path") or ""))
            upsert_thread_snapshot(master, data, observed_at)

        rows = fetch_all_user_threads(state_conn)
        stats["state_threads_seen"] = len(rows)
        for row in rows:
            data = row_to_dict(row)
            data["cwd"] = normalize_path_text(str(data.get("cwd") or ""))
            data["rollout_path"] = normalize_rollout_path(str(data.get("rollout_path") or ""))
            state_rollouts.add(data["rollout_path"].lower())
            upsert_conversation(master, data, observed_at)
            insert_provider_observation(master, data, observed_at)
            event_stats = import_rollout_events(master, str(data["id"]), data["rollout_path"], observed_at)
            merge_stats(stats, {
                "rollout_files_seen": event_stats["files_seen"],
                "rollout_files_missing": event_stats["files_missing"],
                "events_added": event_stats["events_added"],
                "events_duplicate": event_stats["events_duplicate"],
            })

        if include_orphans:
            sessions_dir = paths.codex_home / "sessions"
            if sessions_dir.exists():
                for rollout in sessions_dir.rglob("*.jsonl"):
                    normalized = normalize_rollout_path(str(rollout)).lower()
                    if normalized in state_rollouts:
                        continue
                    meta = rollout_metadata(rollout)
                    if meta.get("source") != CODEX_SOURCE or not safe_int(meta.get("has_user_event")):
                        continue
                    data = synthetic_thread_from_rollout(rollout, meta, paths)
                    stats["orphan_rollouts_seen"] += 1
                    upsert_conversation(master, data, observed_at)
                    insert_provider_observation(master, data, observed_at)
                    event_stats = import_rollout_events(master, str(data["id"]), data["rollout_path"], observed_at)
                    merge_stats(stats, {
                        "rollout_files_seen": event_stats["files_seen"],
                        "rollout_files_missing": event_stats["files_missing"],
                        "events_added": event_stats["events_added"],
                        "events_duplicate": event_stats["events_duplicate"],
                    })

        master.commit()
        summary = master_summary(master)
        stats.update({f"master_{key}": value for key, value in summary.items()})
    finally:
        state_conn.close()
        master.close()
    return stats


def master_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    conversation_count = int(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0])
    event_count = int(conn.execute("SELECT COUNT(*) FROM rollout_events").fetchone()[0])
    raw_thread_count = int(conn.execute("SELECT COUNT(*) FROM state_threads_snapshot").fetchone()[0])
    raw_thread_sources = {
        str(row["source"]): int(row["n"])
        for row in conn.execute(
            "SELECT source, COUNT(*) AS n FROM state_threads_snapshot GROUP BY source ORDER BY source"
        ).fetchall()
    }
    providers: dict[str, int] = {}
    for row in conn.execute("SELECT providers_json FROM conversations").fetchall():
        try:
            items = json.loads(row["providers_json"] or "[]")
        except json.JSONDecodeError:
            items = []
        if not isinstance(items, list):
            continue
        for item in items:
            provider = str(item or "")
            if provider:
                providers[provider] = providers.get(provider, 0) + 1
    return {
        "conversations": conversation_count,
        "events": event_count,
        "raw_threads": raw_thread_count,
        "raw_thread_sources": raw_thread_sources,
        "source_modes": providers,
    }


def current_codex_summary(paths: Paths) -> dict[str, Any]:
    conn = connect_state_db(paths.state_db, readonly=True)
    try:
        total = int(
            conn.execute(
                "SELECT COUNT(*) FROM threads WHERE source=? AND has_user_event=1",
                (CODEX_SOURCE,),
            ).fetchone()[0]
        )
        visible = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM threads
                WHERE source=? AND has_user_event=1 AND archived=0 AND thread_source='user'
                """,
                (CODEX_SOURCE,),
            ).fetchone()[0]
        )
        rows = conn.execute(
            """
            SELECT model_provider, COUNT(*) AS n
            FROM threads
            WHERE source=? AND has_user_event=1
            GROUP BY model_provider
            ORDER BY model_provider
            """,
            (CODEX_SOURCE,),
        ).fetchall()
        source_modes = {str(row["model_provider"]): int(row["n"]) for row in rows}
        return {"total": total, "visible": visible, "source_modes": source_modes}
    finally:
        conn.close()


def restore_rollout_path_for_thread(
    thread_id: str,
    preferred: str,
    paths: Paths,
    created_ms: int,
) -> str:
    when = dt.datetime.fromtimestamp(created_ms / 1000, tz=dt.UTC) if created_ms > 0 else dt.datetime.now(dt.UTC)
    folder = paths.codex_home / "sessions" / when.strftime("%Y") / when.strftime("%m")
    filename = f"rollout-sqlmaster-{when.strftime('%Y-%m-%dT%H-%M-%S')}-{thread_id}.jsonl"
    return normalize_rollout_path(str(folder / filename))


def default_thread_value(column: str, data: dict[str, Any], conv: sqlite3.Row, provider: str, paths: Paths) -> Any:
    now_ms = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    created_ms = safe_int(data.get("created_at_ms"), safe_int(conv["created_at_ms"], now_ms))
    updated_ms = safe_int(data.get("updated_at_ms"), safe_int(conv["updated_at_ms"], created_ms))
    defaults: dict[str, Any] = {
        "id": conv["id"],
        "rollout_path": restore_rollout_path_for_thread(conv["id"], conv["preferred_rollout_path"], paths, created_ms),
        "created_at": safe_int(data.get("created_at"), created_ms),
        "updated_at": safe_int(data.get("updated_at"), updated_ms),
        "source": CODEX_SOURCE,
        "model_provider": provider,
        "cwd": normalize_path_text(str(data.get("cwd") or conv["cwd"] or paths.codex_home)),
        "title": str(data.get("title") or conv["title"] or conv["id"]),
        "sandbox_policy": str(data.get("sandbox_policy") or ""),
        "approval_mode": str(data.get("approval_mode") or ""),
        "tokens_used": safe_int(data.get("tokens_used")),
        "has_user_event": 1,
        "archived": safe_int(conv["archived"]),
        "archived_at": data.get("archived_at") or conv["archived_at"],
        "git_sha": data.get("git_sha"),
        "git_branch": data.get("git_branch"),
        "git_origin_url": data.get("git_origin_url"),
        "cli_version": str(data.get("cli_version") or ""),
        "first_user_message": str(data.get("first_user_message") or conv["first_user_message"] or ""),
        "agent_nickname": data.get("agent_nickname"),
        "agent_role": data.get("agent_role"),
        "memory_mode": str(data.get("memory_mode") or "enabled"),
        "model": data.get("model"),
        "reasoning_effort": data.get("reasoning_effort"),
        "agent_path": data.get("agent_path"),
        "created_at_ms": created_ms,
        "updated_at_ms": updated_ms,
        "thread_source": "user",
        "preview": str(data.get("preview") or conv["preview"] or ""),
    }
    return defaults.get(column, data.get(column))


def restore_sqlite_from_master(
    state_conn: sqlite3.Connection,
    master: sqlite3.Connection,
    provider: str,
    paths: Paths,
) -> dict[str, int]:
    stats = {"threads_upserted": 0, "threads_normalized": 0}
    table_info = state_conn.execute("PRAGMA table_info(threads)").fetchall()
    columns = [row["name"] for row in table_info]
    rows = master.execute(
        """
        SELECT *
        FROM conversations
        WHERE source=? AND has_user_event=1
        ORDER BY COALESCE(updated_at_ms, updated_at, created_at_ms, created_at) ASC, id ASC
        """,
        (CODEX_SOURCE,),
    ).fetchall()

    state_conn.execute("BEGIN IMMEDIATE")
    for conv in rows:
        try:
            data = json.loads(conv["thread_json"] or "{}")
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        values = [default_thread_value(col, data, conv, provider, paths) for col in columns]
        placeholders = ",".join("?" for _ in columns)
        column_sql = ",".join(columns)
        update_sql = ",".join(f"{col}=excluded.{col}" for col in columns if col != "id")
        state_conn.execute(
            f"""
            INSERT INTO threads ({column_sql})
            VALUES ({placeholders})
            ON CONFLICT(id) DO UPDATE SET {update_sql}
            """,
            values,
        )
        stats["threads_upserted"] += 1

    cur = state_conn.execute(
        """
        UPDATE threads
        SET model_provider=?,
            thread_source='user',
            has_user_event=1,
            cwd = CASE
              WHEN substr(cwd,1,8)='\\\\?\\UNC\\' THEN '\\\\' || substr(cwd,9)
              WHEN substr(cwd,1,4)='\\\\?\\' THEN substr(cwd,5)
              ELSE cwd
            END
        WHERE source=? AND has_user_event=1
        """,
        (provider, CODEX_SOURCE),
    )
    stats["threads_normalized"] = cur.rowcount
    state_conn.commit()
    state_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return stats


def backup_one_file(source: Path, backup_dir: Path) -> None:
    if not source.exists():
        return
    normalized = normalize_path_text(str(source)).replace(":", "")
    parts = [part for part in normalized.split("\\") if part]
    target = backup_dir / "rollout-files" / Path(*parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def adjusted_rollout_raw(raw: str, provider: str) -> str:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(obj, dict) or obj.get("type") != "session_meta":
        return raw
    payload = obj.get("payload")
    if isinstance(payload, dict):
        payload["model_provider"] = provider
        obj["payload"] = payload
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return raw


def write_rollouts_from_master(
    master: sqlite3.Connection,
    paths: Paths,
    provider: str,
    backup_dir: Path,
) -> dict[str, int]:
    stats = {"rollouts_written": 0, "rollouts_skipped_empty": 0, "events_written": 0}
    conversations = master.execute(
        """
        SELECT id, preferred_rollout_path, created_at_ms
        FROM conversations
        WHERE source=? AND has_user_event=1
        ORDER BY COALESCE(updated_at_ms, created_at_ms) ASC, id ASC
        """,
        (CODEX_SOURCE,),
    ).fetchall()
    for conv in conversations:
        events = master.execute(
            """
            SELECT raw_json, event_type, timestamp, event_order
            FROM rollout_events
            WHERE thread_id=?
            ORDER BY
              CASE WHEN event_type='session_meta' THEN 0 ELSE 1 END,
              COALESCE(timestamp, ''),
              event_order
            """,
            (conv["id"],),
        ).fetchall()
        if not events:
            stats["rollouts_skipped_empty"] += 1
            continue
        rollout_path = restore_rollout_path_for_thread(
            conv["id"],
            conv["preferred_rollout_path"],
            paths,
            safe_int(conv["created_at_ms"]),
        )
        path = Path(rollout_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        backup_one_file(path, backup_dir)
        tmp_path = path.with_name(path.name + ".tmp-sqlswitch")
        with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
            for event in events:
                handle.write(adjusted_rollout_raw(str(event["raw_json"]), provider))
                handle.write("\n")
        tmp_path.replace(path)
        stats["rollouts_written"] += 1
        stats["events_written"] += len(events)
    return stats


def concise_status(paths: Paths, master_db: Path) -> dict[str, Any]:
    current = current_codex_summary(paths)
    master_data: dict[str, Any]
    if master_db.exists():
        master = connect_master_db(master_db)
        try:
            master_data = master_summary(master)
        finally:
            master.close()
    else:
        master_data = {"conversations": 0, "events": 0, "source_modes": {}}
    return {"current": current, "master": master_data, "master_db": str(master_db)}


def print_store_result(master_db: Path, stats: dict[str, Any]) -> None:
    print_section("SQL Master Saved")
    print(f"master_db              = {master_db}")
    print(f"raw_threads_seen       = {stats['raw_threads_seen']}")
    print(f"state_threads_seen     = {stats['state_threads_seen']}")
    print(f"orphan_rollouts_seen   = {stats['orphan_rollouts_seen']}")
    print(f"rollout_files_seen     = {stats['rollout_files_seen']}")
    print(f"events_added           = {stats['events_added']}")
    print(f"events_duplicate       = {stats['events_duplicate']}")
    print(f"master_raw_threads     = {stats['master_raw_threads']}")
    print(f"master_conversations   = {stats['master_conversations']}")
    print(f"master_events          = {stats['master_events']}")
    print(f"raw_thread_sources     = {stats['master_raw_thread_sources']}")
    print(f"source_modes_saved     = {stats['master_source_modes']}")


def print_concise_status(status: dict[str, Any]) -> None:
    print_section("Current Codex")
    print(f"total_conversations    = {status['current']['total']}")
    print(f"visible_conversations  = {status['current']['visible']}")
    print(f"source_modes_present   = {status['current']['source_modes']}")
    print_section("SQL Master")
    print(f"master_db              = {status['master_db']}")
    print(f"raw_threads            = {status['master']['raw_threads']}")
    print(f"conversations          = {status['master']['conversations']}")
    print(f"events                 = {status['master']['events']}")
    print(f"raw_thread_sources     = {status['master']['raw_thread_sources']}")
    print(f"source_modes_saved     = {status['master']['source_modes']}")


def command_store(args: argparse.Namespace) -> int:
    paths = build_paths(Path(args.codex_home))
    master_db = default_master_path(args.master_db)
    stats = store_all_conversations(paths, master_db, include_orphans=not args.no_orphans)
    print_store_result(master_db, stats)
    return 0


def command_status(args: argparse.Namespace) -> int:
    paths = build_paths(Path(args.codex_home))
    master_db = default_master_path(args.master_db)
    print_concise_status(concise_status(paths, master_db))
    return 0


def command_restore(args: argparse.Namespace) -> int:
    paths = build_paths(Path(args.codex_home))
    master_db = default_master_path(args.master_db)
    for path, label in (
        (paths.state_db, "Codex state database"),
        (paths.session_index, "session index"),
        (paths.global_state, "global state"),
    ):
        require_file(path, label)

    decision = resolve_target_provider(args, paths)
    print_section("Target Mode")
    print(f"target_provider = {decision.provider}")
    print(f"source          = {decision.source}")
    print(f"detail          = {decision.detail}")

    if not args.no_store_first:
        print_section("Preflight Store")
        pre_stats = store_all_conversations(paths, master_db, include_orphans=not args.no_orphans)
        print(f"stored_conversations = {pre_stats['master_conversations']}")
        print(f"stored_events        = {pre_stats['master_events']}")

    if args.dry_run:
        print_section("Dry Run")
        print("No Codex files were changed.")
        print_concise_status(concise_status(paths, master_db))
        return 0

    require_file(master_db, "SQL master database")
    backup_dir = backup_codex_state(paths, Path(args.backup_root))
    print_section("Backup")
    print(backup_dir)

    master = connect_master_db(master_db)
    state_conn = connect_state_db(paths.state_db, readonly=False)
    try:
        sqlite_stats = restore_sqlite_from_master(state_conn, master, decision.provider, paths)
    finally:
        state_conn.close()

    rollout_stats = write_rollouts_from_master(master, paths, decision.provider, backup_dir)
    master.close()

    state_conn = connect_state_db(paths.state_db, readonly=False)
    try:
        rebuild_session_index(state_conn, paths.session_index)
        repair_global_state(state_conn, paths.global_state)
    finally:
        state_conn.close()

    print_section("Applied")
    for key, value in {**sqlite_stats, **rollout_stats}.items():
        print(f"{key} = {value}")
    print_concise_status(concise_status(paths, master_db))
    return 0


def verify(paths: Paths, provider: str) -> dict[str, Any]:
    conn = connect_state_db(paths.state_db, readonly=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        active_threads = fetch_active_threads(conn)
        active_ids = [r["id"] for r in active_threads]
        provider_rows = conn.execute(
            """
            SELECT model_provider, COUNT(*) AS n
            FROM threads
            WHERE archived=0 AND source=? AND thread_source='user' AND has_user_event=1
            GROUP BY model_provider
            ORDER BY model_provider
            """,
            (CODEX_SOURCE,),
        ).fetchall()
        risks = conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM threads
               WHERE archived=0 AND source=? AND has_user_event=1 AND COALESCE(thread_source,'')='') AS blank_thread_source,
              (SELECT COUNT(*) FROM threads
               WHERE archived=0 AND source=? AND thread_source='user' AND has_user_event=1 AND model_provider!=?) AS not_target_provider,
              (SELECT COUNT(*) FROM threads
               WHERE archived=0 AND source=? AND substr(cwd,1,4)='\\\\?\\') AS long_cwd
            """,
            (CODEX_SOURCE, CODEX_SOURCE, provider, CODEX_SOURCE),
        ).fetchone()

        project_matches: list[dict[str, Any]] = []
        if paths.global_state.exists():
            state = json.loads(paths.global_state.read_text(encoding="utf-8", errors="replace"))
            for root in state.get("project-order", []) or []:
                count = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM threads
                    WHERE archived=0
                      AND source=?
                      AND thread_source='user'
                      AND has_user_event=1
                      AND cwd=?
                    """,
                    (CODEX_SOURCE, str(root)),
                ).fetchone()[0]
                project_matches.append({"root": str(root), "exact_thread_count": int(count)})

        index_ids = read_session_index_ids(paths.session_index)
        active_set = set(active_ids)
        index_set = set(index_ids)

        return {
            "integrity": integrity,
            "active_user_threads": len(active_threads),
            "provider_counts": {str(r["model_provider"]): int(r["n"]) for r in provider_rows},
            "risk_counts": {
                "blank_thread_source": int(risks["blank_thread_source"]),
                "not_target_provider": int(risks["not_target_provider"]),
                "long_cwd": int(risks["long_cwd"]),
            },
            "session_index": {
                "rows": len(index_ids),
                "missing_from_index": len(active_set - index_set),
                "extra_in_index": len(index_set - active_set),
                "match": active_set == index_set,
            },
            "project_matches": project_matches,
        }
    finally:
        conn.close()


def print_report(decision: ProviderDecision, report: dict[str, Any]) -> None:
    print_section("Provider Decision")
    print(f"target_provider = {decision.provider}")
    print(f"source          = {decision.source}")
    print(f"detail          = {decision.detail}")

    print_section("Verification")
    print(f"integrity              = {report['integrity']}")
    print(f"active_user_threads    = {report['active_user_threads']}")
    print(f"provider_counts        = {report['provider_counts']}")
    print(f"risk_counts            = {report['risk_counts']}")
    print(f"session_index          = {report['session_index']}")

    print_section("Project Exact Matches")
    if not report["project_matches"]:
        print("(no project roots found)")
    for item in report["project_matches"]:
        print(f"{item['exact_thread_count']:>3}  {item['root']}")


def command_verify(args: argparse.Namespace) -> int:
    paths = build_paths(Path(args.codex_home))
    for path, label in (
        (paths.state_db, "Codex state database"),
        (paths.session_index, "session index"),
        (paths.global_state, "global state"),
    ):
        require_file(path, label)
    decision = resolve_target_provider(args, paths)
    report = verify(paths, decision.provider)
    print_report(decision, report)
    return 0


def command_sync(args: argparse.Namespace) -> int:
    paths = build_paths(Path(args.codex_home))
    for path, label in (
        (paths.state_db, "Codex state database"),
        (paths.session_index, "session index"),
        (paths.global_state, "global state"),
    ):
        require_file(path, label)

    decision = resolve_target_provider(args, paths)
    before = verify(paths, decision.provider)
    print_report(decision, before)

    if args.dry_run:
        print_section("Dry Run")
        print("No files were changed.")
        return 0

    backup_dir = backup_codex_state(paths, Path(args.backup_root))
    print_section("Backup")
    print(backup_dir)

    conn = connect_state_db(paths.state_db, readonly=False)
    try:
        stats = repair_sqlite(conn, decision.provider)
        rebuild_session_index(conn, paths.session_index)
        repair_global_state(conn, paths.global_state)
    finally:
        conn.close()

    print_section("Applied")
    for key, value in stats.items():
        print(f"{key} = {value}")

    after = verify(paths, decision.provider)
    print_report(decision, after)
    return 0


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Synchronize Codex conversation visibility across OpenAI login and third-party API modes.",
    )
    parser.add_argument(
        "--codex-home",
        default=str(default_codex_home()),
        help="Codex home directory. Default: %%CODEX_HOME%% or ~/.codex",
    )
    parser.add_argument(
        "--target",
        default="auto",
        help="Provider target: auto, openai, custom, or any explicit provider id. Default: auto",
    )
    parser.add_argument(
        "--cc-switch-db",
        help="Optional CC Switch SQLite database. If supplied in auto mode, its current codex provider decides the target.",
    )
    parser.add_argument(
        "--cc-switch-sql",
        help="Optional CC Switch SQL export. If supplied in auto mode, it is imported to a temporary SQLite DB for provider detection.",
    )
    parser.add_argument(
        "--master-db",
        default=str(DEFAULT_MASTER_DB),
        help="SQL master database for complete Codex conversations. Default: ./codex_conversations.sqlite",
    )

    sub = parser.add_subparsers(dest="command", required=True)
    store = sub.add_parser("store", help="Save every Codex user conversation into the SQL master.")
    store.add_argument(
        "--master-db",
        default=argparse.SUPPRESS,
        help="SQL master database for complete Codex conversations.",
    )
    store.add_argument(
        "--no-orphans",
        action="store_true",
        help="Do not scan ~/.codex/sessions for rollout files missing from state_5.sqlite.",
    )

    restore = sub.add_parser("restore", help="Synchronize the SQL master into the current/target provider.")
    restore.add_argument(
        "--master-db",
        default=argparse.SUPPRESS,
        help="SQL master database for complete Codex conversations.",
    )
    restore.add_argument(
        "--backup-root",
        default=str(TOOL_DIR / "backups"),
        help="Backup directory root. Default: ./backups beside this tool.",
    )
    restore.add_argument("--dry-run", action="store_true", help="Show the target and counts, but do not change Codex files.")
    restore.add_argument(
        "--no-store-first",
        action="store_true",
        help="Skip the protective preflight store from current Codex state into the SQL master.",
    )
    restore.add_argument(
        "--no-orphans",
        action="store_true",
        help="During the protective preflight store, do not scan orphan rollout files.",
    )

    status = sub.add_parser("status", help="Show a concise current Codex and SQL master summary.")
    status.add_argument(
        "--master-db",
        default=argparse.SUPPRESS,
        help="SQL master database for complete Codex conversations.",
    )
    sub.add_parser("verify", help="Check provider direction, Codex state, session index, and project matches.")

    sync = sub.add_parser("sync", help="Backup and synchronize Codex state to the target provider.")
    sync.add_argument(
        "--backup-root",
        default=str(Path.cwd() / "codex-state-backups"),
        help="Backup directory root. Default: ./codex-state-backups",
    )
    sync.add_argument("--dry-run", action="store_true", help="Show what would be used, but do not change files.")
    return parser


def interactive_main_menu() -> int:
    while True:
        print(f"{APP_NAME}")
        print()
        print("1. 保存全部 Codex 对话到 SQL 主库")
        print("2. 从 SQL 主库同步到当前模式")
        print("3. 检查状态")
        print("0. 退出")
        print()
        choice = input("Choose [1/2/3/0]: ").replace("\ufeff", "").replace("Ã¯Â»Â¿", "").strip() or "3"

        if choice == "1":
            rc = main(["store"])
            input("\n按 Enter 返回上级...")
            if rc:
                return rc
            print()
            continue
        if choice == "2":
            rc = interactive_restore()
            if rc:
                return rc
            print()
            continue
        if choice == "3":
            rc = main(["status"])
            input("\n按 Enter 返回上级...")
            if rc:
                return rc
            print()
            continue
        if choice in {"0", "q", "Q", "exit", "退出"}:
            return 0

        print(f"Unknown choice: {choice}", file=sys.stderr)
        print()


def interactive_restore() -> int:
    paths = build_paths(default_codex_home())
    args = argparse.Namespace(target="auto", cc_switch_db=None, cc_switch_sql=None)
    try:
        decision = resolve_target_provider(args, paths)
        detected = decision.provider
    except StopRepair:
        detected = "custom"

    while True:
        print()
        print(f"检测到当前模式: {detected}")
        print("1. 确认同步到当前模式")
        print("2. 改为 OpenAI official")
        print("3. 改为 third-party custom")
        print("0. 返回上级")
        print()
        choice = input("Choose [1/2/3/0]: ").replace("\ufeff", "").replace("Ã¯Â»Â¿", "").strip() or "1"

        if choice == "0":
            return 0
        if choice == "1":
            return main(["--target", detected, "restore", "--backup-root", str(TOOL_DIR / "backups")])
        if choice == "2":
            return main(["--target", "openai", "restore", "--backup-root", str(TOOL_DIR / "backups")])
        if choice == "3":
            return main(["--target", "custom", "restore", "--backup-root", str(TOOL_DIR / "backups")])

        print(f"Unknown choice: {choice}", file=sys.stderr)


def interactive_main() -> int:
    return interactive_main_menu()

    print(f"{APP_NAME} interactive mode")
    print()
    print("1. Verify current auto mode")
    print("2. Sync to OpenAI official mode")
    print("3. Sync to third-party API mode (custom)")
    print("4. Verify third-party API mode (custom)")
    print("5. Sync using CC Switch SQL export")
    print()
    choice = input("Choose [1/2/3/4/5]: ").replace("\ufeff", "").replace("ï»¿", "").strip() or "1"

    if choice == "1":
        return main(["verify"])
    if choice == "2":
        return main(["--target", "openai", "sync", "--backup-root", str(TOOL_DIR / "backups")])
    if choice == "3":
        return main(["--target", "custom", "sync", "--backup-root", str(TOOL_DIR / "backups")])
    if choice == "4":
        return main(["--target", "custom", "verify"])
    if choice == "5":
        sql_path = input("CC Switch SQL export path: ").replace("\ufeff", "").replace("ï»¿", "").strip().strip('"')
        if not sql_path:
            print("No SQL export path provided.", file=sys.stderr)
            return 2
        return main(["--cc-switch-sql", sql_path, "sync", "--backup-root", str(TOOL_DIR / "backups")])

    print(f"Unknown choice: {choice}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    if argv is None and len(sys.argv) == 1:
        return interactive_main()

    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "store":
            return command_store(args)
        if args.command == "restore":
            return command_restore(args)
        if args.command == "status":
            return command_status(args)
        if args.command == "verify":
            return command_verify(args)
        if args.command == "sync":
            return command_sync(args)
        parser.error("unknown command")
        return 2
    except StopRepair as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 3
    except sqlite3.Error as exc:
        print(f"SQLite error: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
