from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


@dataclass
class Profile:
    path: Path
    codex_home: Path
    target_provider: str
    recovery_report: Path
    allowed_statuses: set[str]
    preserve_existing_partial_threads: bool
    require_integrity_ok: bool
    require_real_rollout: bool
    snapshot_before_apply: bool


@dataclass
class RolloutInfo:
    path: Path
    thread_id: str
    valid: bool
    strict_valid: bool
    reason: str
    title: str
    cwd: str
    created_ms: int
    updated_ms: int
    first_user_message: str
    user_messages: int
    assistant_messages: int
    message_events: int
    source: str
    thread_source: str
    model_provider: str
    model: str
    reasoning_effort: str
    cli_version: str
    sandbox_policy: str
    approval_mode: str
    ephemeral: bool


THREAD_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def eprint(text: str) -> None:
    print(text, file=sys.stderr)


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def utc_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


def resolve_profile_path(value: str | None) -> Path:
    if value:
        p = Path(value)
    else:
        p = PROJECT_ROOT / ".local" / "profile.toml"
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p.resolve()


def resolve_from_project(value: str | os.PathLike[str], base: Path = PROJECT_ROOT) -> Path:
    p = Path(value)
    if not p.is_absolute():
        p = base / p
    return p.resolve()


def load_profile(path_value: str | None) -> Profile:
    path = resolve_profile_path(path_value)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    projection = data.get("projection", {})
    safety = data.get("safety", {})
    return Profile(
        path=path,
        codex_home=resolve_from_project(data["codex_home"], PROJECT_ROOT),
        target_provider=str(data.get("target_provider") or "openai"),
        recovery_report=resolve_from_project(data["recovery_report"], PROJECT_ROOT),
        allowed_statuses=set(str(x) for x in projection.get("allowed_statuses", ["ready_original"])),
        preserve_existing_partial_threads=bool(projection.get("preserve_existing_partial_threads", True)),
        require_integrity_ok=bool(safety.get("require_integrity_ok", True)),
        require_real_rollout=bool(safety.get("require_real_rollout", True)),
        snapshot_before_apply=bool(safety.get("snapshot_before_apply", True)),
    )


def state_db(profile: Profile) -> Path:
    return profile.codex_home / "state_5.sqlite"


def session_index(profile: Profile) -> Path:
    return profile.codex_home / "session_index.jsonl"


def global_state_path(profile: Profile) -> Path:
    return profile.codex_home / ".codex-global-state.json"


def local_dir() -> Path:
    p = PROJECT_ROOT / ".local"
    p.mkdir(parents=True, exist_ok=True)
    return p


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def iso_to_ms(value: str) -> int:
    if not value:
        return 0
    try:
        return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def ms_to_iso(value: int) -> str:
    if value <= 0:
        return utc_iso()
    return dt.datetime.fromtimestamp(value / 1000, tz=dt.UTC).isoformat().replace("+00:00", "Z")


def normalize_text(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def content_text(content: Any) -> str:
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") in {"input_text", "output_text", "text"}:
                parts.append(str(item.get("text") or ""))
    elif isinstance(content, dict):
        parts.append(str(content.get("text") or ""))
    return normalize_text("\n\n".join(p for p in parts if p))


def is_internal_text(text: str) -> bool:
    stripped = text.strip()
    prefixes = (
        "<environment_context>",
        "<turn_aborted>",
        "<developer",
        "<system",
        "<permissions",
        "<app-context",
        "<collaboration_mode>",
        "<apps_instructions>",
        "<skills_instructions>",
        "<plugins_instructions>",
    )
    return not stripped or any(stripped.startswith(prefix) for prefix in prefixes)


def parse_rollout(path: Path, thread_id: str) -> RolloutInfo:
    meta_seen = False
    meta_first = False
    meta_id = ""
    title = ""
    cwd = ""
    created_ms = 0
    updated_ms = 0
    first_user = ""
    user_messages = 0
    assistant_messages = 0
    message_events = 0
    parse_errors = 0
    event_order = 0
    source = ""
    thread_source = ""
    model_provider = ""
    model = ""
    reasoning_effort = ""
    cli_version = ""
    sandbox_policy = ""
    approval_mode = ""
    ephemeral = False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    parse_errors += 1
                    continue
                if not isinstance(obj, dict):
                    continue
                is_first_event = event_order == 0
                event_order += 1
                timestamp = str(obj.get("timestamp") or "")
                ts_ms = iso_to_ms(timestamp)
                if ts_ms:
                    created_ms = min([v for v in (created_ms, ts_ms) if v] or [ts_ms])
                    updated_ms = max(updated_ms, ts_ms)
                if obj.get("type") == "session_meta" and isinstance(obj.get("payload"), dict):
                    if not meta_seen:
                        meta_first = is_first_event
                    meta_seen = True
                    payload = obj["payload"]
                    meta_id = str(payload.get("id") or meta_id)
                    title = normalize_text(str(payload.get("title") or title))
                    cwd = normalize_text(str(payload.get("cwd") or cwd))
                    source = str(payload.get("source") or source)
                    thread_source = str(payload.get("thread_source") or thread_source)
                    model_provider = str(payload.get("model_provider") or model_provider)
                    model = str(payload.get("model") or model)
                    reasoning_effort = str(payload.get("reasoning_effort") or payload.get("model_reasoning_effort") or reasoning_effort)
                    cli_version = str(payload.get("cli_version") or cli_version)
                    ephemeral = bool(payload.get("ephemeral") or ephemeral)
                    created_ms = min([v for v in (created_ms, iso_to_ms(str(payload.get("timestamp") or ""))) if v] or [created_ms])
                    continue
                if obj.get("type") == "turn_context":
                    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
                    if isinstance(payload, dict):
                        if not approval_mode:
                            approval_mode = str(payload.get("approval_policy") or payload.get("approval_mode") or "")
                        if not sandbox_policy:
                            sandbox_policy = sandbox_policy_from_context(payload)
                        if not model:
                            model = str(payload.get("model") or "")
                        if not reasoning_effort:
                            reasoning_effort = str(payload.get("model_reasoning_effort") or payload.get("reasoning_effort") or "")
                    continue
                role = ""
                text = ""
                if obj.get("type") == "response_item" and isinstance(obj.get("payload"), dict):
                    payload = obj["payload"]
                    if payload.get("type") == "message":
                        role = str(payload.get("role") or "")
                        text = content_text(payload.get("content"))
                elif obj.get("type") == "event_msg" and isinstance(obj.get("payload"), dict):
                    payload = obj["payload"]
                    if payload.get("type") == "user_message":
                        role = "user"
                        text = normalize_text(str(payload.get("message") or ""))
                if role in {"user", "assistant"} and text and not is_internal_text(text):
                    message_events += 1
                    if role == "user":
                        user_messages += 1
                        if not first_user:
                            first_user = text
                    else:
                        assistant_messages += 1
    except OSError as exc:
        return RolloutInfo(path, thread_id, False, False, f"read_error:{exc}", "", "", 0, 0, "", 0, 0, 0, "", "", "", "", "", "", "", False)

    if parse_errors:
        reason = "json_parse_errors"
        valid = False
    elif not meta_seen:
        reason = "missing_session_meta"
        valid = False
    elif meta_id != thread_id:
        reason = "thread_id_mismatch"
        valid = False
    elif not meta_first:
        reason = "session_meta_not_first"
        valid = False
    elif user_messages == 0:
        reason = "no_user_history"
        valid = False
    else:
        reason = "ok" if assistant_messages > 0 else "user_only_history"
        valid = True
    return RolloutInfo(
        path=path,
        thread_id=thread_id,
        valid=valid,
        strict_valid=valid and assistant_messages > 0 and message_events > 0,
        reason=reason,
        title=title,
        cwd=cwd,
        created_ms=created_ms,
        updated_ms=updated_ms,
        first_user_message=first_user,
        user_messages=user_messages,
        assistant_messages=assistant_messages,
        message_events=message_events,
        source=source,
        thread_source=thread_source,
        model_provider=model_provider,
        model=model,
        reasoning_effort=reasoning_effort,
        cli_version=cli_version,
        sandbox_policy=sandbox_policy,
        approval_mode=approval_mode,
        ephemeral=ephemeral,
    )


def existing_or(existing: dict[str, Any] | None, key: str, fallback: Any) -> Any:
    if existing and key in existing and existing.get(key) is not None:
        return existing.get(key)
    return fallback


def existing_or_nonempty(existing: dict[str, Any] | None, key: str, fallback: Any) -> Any:
    if existing and key in existing and meaningful(existing.get(key), nonempty=True):
        return existing.get(key)
    return fallback


def connect_state(profile: Profile, readonly: bool) -> sqlite3.Connection:
    db = state_db(profile)
    uri = f"file:{db}?mode={'ro' if readonly else 'rw'}"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def db_integrity(profile: Profile) -> str:
    try:
        conn = connect_state(profile, True)
        try:
            return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return f"ERROR: {exc}"


def load_recovery_report(profile: Profile) -> dict[str, Any]:
    if not profile.recovery_report.exists():
        return {"threads": []}
    return json.loads(profile.recovery_report.read_text(encoding="utf-8"))


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def sandbox_policy_from_context(payload: dict[str, Any]) -> str:
    permission_profile = payload.get("permission_profile")
    if isinstance(permission_profile, dict):
        if permission_profile.get("type") == "disabled":
            return compact_json({"type": "disabled"})
        return compact_json(permission_profile)
    raw_sandbox = payload.get("sandbox_policy")
    if isinstance(raw_sandbox, str):
        return raw_sandbox
    if raw_sandbox is not None:
        return compact_json(raw_sandbox)
    return ""


def current_config_model_provider(profile: Profile) -> str:
    config = profile.codex_home / "config.toml"
    if not config.exists():
        return ""
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    return str(data.get("model_provider") or "")


def current_config_defaults(profile: Profile) -> dict[str, Any]:
    config = profile.codex_home / "config.toml"
    if not config.exists():
        return {}
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    defaults: dict[str, Any] = {}
    for target, keys in {
        "model_provider": ("model_provider",),
        "model": ("model",),
        "reasoning_effort": ("model_reasoning_effort", "reasoning_effort"),
        "approval_mode": ("approval_policy", "approval_mode"),
    }.items():
        for key in keys:
            value = data.get(key)
            if value not in (None, ""):
                defaults[target] = str(value)
                break
    return defaults


def planned_model_provider(
    existing: dict[str, Any] | None,
    info: RolloutInfo,
    config_model_provider: str,
    profile: Profile,
) -> tuple[str, str]:
    if existing and "model_provider" in existing and existing.get("model_provider") is not None:
        return str(existing.get("model_provider") or ""), "existing_db_preserved"
    if info.model_provider:
        return info.model_provider, "rollout_session_meta"
    if config_model_provider:
        return config_model_provider, "current_config_fallback_missing_rollout_metadata"
    return profile.target_provider, "profile_target_fallback_missing_rollout_metadata"


def table_column_info(conn: sqlite3.Connection, table: str) -> dict[str, dict[str, Any]]:
    return {str(row["name"]): dict(row) for row in conn.execute(f"PRAGMA table_info({table})")}


def parse_sqlite_default(raw: Any) -> Any:
    if raw is None:
        raise KeyError("no default")
    text = str(raw).strip()
    upper = text.upper()
    if upper == "NULL":
        return None
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        return text[1:-1].replace("''", "'")
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1].replace('""', '"')
    try:
        return int(text)
    except ValueError:
        return text


CRITICAL_NONEMPTY_INSERT_FIELDS = {
    "rollout_path",
    "source",
    "model_provider",
    "cwd",
    "title",
    "sandbox_policy",
    "approval_mode",
}

PREFERRED_NONEMPTY_METADATA_FIELDS = CRITICAL_NONEMPTY_INSERT_FIELDS | {
    "model",
    "reasoning_effort",
    "cli_version",
    "memory_mode",
}

TEMPLATE_COPY_DENYLIST = {
    "id",
    "rollout_path",
    "created_at",
    "updated_at",
    "created_at_ms",
    "updated_at_ms",
    "archived_at",
    "title",
    "first_user_message",
    "preview",
    "cwd",
}


def meaningful(value: Any, *, nonempty: bool = False) -> bool:
    if value is None:
        return False
    if nonempty and isinstance(value, str) and value == "":
        return False
    return True


def wants_nonempty_metadata(key: str) -> bool:
    return key in PREFERRED_NONEMPTY_METADATA_FIELDS


def choose_template_rows(current: dict[str, dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    rows = list(current.values())

    def score(row: dict[str, Any]) -> tuple[int, int, int]:
        visible = int(row.get("archived") == 0 and row.get("source") == "vscode" and row.get("thread_source") == "user" and row.get("has_user_event") == 1)
        complete = int(bool(row.get("sandbox_policy")) and bool(row.get("approval_mode")) and bool(row.get("model_provider")))
        updated = int(row.get("updated_at_ms") or 0) or int(row.get("updated_at") or 0) * 1000
        return (visible, complete, updated)

    rows.sort(key=score, reverse=True)
    by_provider: dict[str, dict[str, Any]] = {}
    generic: dict[str, Any] | None = None
    for row in rows:
        provider = str(row.get("model_provider") or "")
        if provider and provider not in by_provider and score(row)[1]:
            by_provider[provider] = row
        if generic is None and score(row)[1]:
            generic = row
    return by_provider, generic


def enrich_insert_metadata(
    desired: dict[str, Any],
    columns: dict[str, dict[str, Any]],
    provider_template: dict[str, Any] | None,
    generic_template: dict[str, Any] | None,
    info: RolloutInfo,
    config_defaults: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    enriched = dict(desired)
    sources: dict[str, str] = {}

    rollout_values = {
        "sandbox_policy": info.sandbox_policy,
        "approval_mode": info.approval_mode,
        "model": info.model,
        "reasoning_effort": info.reasoning_effort,
        "cli_version": info.cli_version,
    }
    for key, value in rollout_values.items():
        if key in columns and meaningful(value, nonempty=wants_nonempty_metadata(key)):
            if not meaningful(enriched.get(key), nonempty=wants_nonempty_metadata(key)):
                enriched[key] = value
                sources[key] = "rollout"

    for template, label in ((provider_template, "same_provider_template"), (generic_template, "generic_template")):
        if not template:
            continue
        for key, value in template.items():
            if key not in columns or key in TEMPLATE_COPY_DENYLIST:
                continue
            if meaningful(enriched.get(key), nonempty=wants_nonempty_metadata(key)):
                continue
            if meaningful(value, nonempty=wants_nonempty_metadata(key)):
                enriched[key] = value
                sources.setdefault(key, label)

    current_provider = str(config_defaults.get("model_provider") or "")
    desired_provider = str(enriched.get("model_provider") or "")
    if current_provider and desired_provider == current_provider:
        for key in ("model", "reasoning_effort", "approval_mode"):
            if key in columns and key in config_defaults and not meaningful(enriched.get(key), nonempty=wants_nonempty_metadata(key)):
                enriched[key] = config_defaults[key]
                sources.setdefault(key, "current_config_same_provider")

    missing: list[str] = []
    for key, col in columns.items():
        if key in enriched and meaningful(enriched.get(key), nonempty=key in CRITICAL_NONEMPTY_INSERT_FIELDS):
            continue
        if key in CRITICAL_NONEMPTY_INSERT_FIELDS:
            missing.append(key)
            continue
        if int(col.get("pk") or 0):
            continue
        if int(col.get("notnull") or 0) and col.get("dflt_value") is None:
            missing.append(key)
            continue
        if int(col.get("notnull") or 0) and col.get("dflt_value") is not None:
            try:
                enriched[key] = parse_sqlite_default(col.get("dflt_value"))
                sources.setdefault(key, "schema_default")
            except KeyError:
                missing.append(key)

    return enriched, sources, sorted(set(missing))


def infer_thread_id_from_rollout(path: Path) -> str | None:
    match = THREAD_ID_RE.search(path.name)
    if match:
        return match.group(0)
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, raw in enumerate(handle):
                if index > 200:
                    break
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "session_meta" or not isinstance(obj.get("payload"), dict):
                    continue
                value = str(obj["payload"].get("id") or "")
                if THREAD_ID_RE.fullmatch(value):
                    return value
    except OSError:
        return None
    return None


def discover_live_session_records(profile: Profile) -> list[dict[str, Any]]:
    sessions_dir = profile.codex_home / "sessions"
    if not sessions_dir.exists():
        return []
    records: list[dict[str, Any]] = []
    for rollout in sorted(sessions_dir.rglob("*.jsonl")):
        if "rollout-sqlmaster-" in rollout.name.lower():
            continue
        thread_id = infer_thread_id_from_rollout(rollout)
        if not thread_id:
            continue
        records.append(
            {
                "thread_id": thread_id,
                "title": "",
                "status": "ready_original",
                "chosen_rollout": str(rollout),
                "projection_origin": "live_sessions",
            }
        )
    return records


def projection_records(profile: Profile, report: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in report.get("threads", []):
        if not isinstance(record, dict):
            continue
        thread_id = str(record.get("thread_id") or "")
        if not thread_id or thread_id in seen:
            continue
        record = dict(record)
        record.setdefault("projection_origin", "recovery_report")
        records.append(record)
        seen.add(thread_id)
    for record in discover_live_session_records(profile):
        thread_id = str(record.get("thread_id") or "")
        if not thread_id or thread_id in seen:
            continue
        records.append(record)
        seen.add(thread_id)
    return records


def resolve_rollout_path(record: dict[str, Any], report_path: Path) -> Path:
    raw = str(record.get("chosen_rollout") or "")
    if raw:
        p = Path(raw)
        if p.exists():
            return p.resolve()
    copied = str(record.get("copied_rollout") or "")
    if copied:
        p = Path(copied)
        if p.exists():
            return p.resolve()
    thread_id = str(record["thread_id"])
    matches = list(report_path.parent.rglob(f"canonical-rollout-{thread_id}*.jsonl"))
    if matches:
        return matches[0].resolve()
    return Path(raw or copied)


def read_current_threads(profile: Profile) -> dict[str, dict[str, Any]]:
    conn = connect_state(profile, True)
    try:
        rows = conn.execute("SELECT * FROM threads").fetchall()
        return {str(row["id"]): dict(row) for row in rows}
    finally:
        conn.close()


def active_thread_count(profile: Profile) -> int:
    conn = connect_state(profile, True)
    try:
        return int(
            conn.execute(
                """
                SELECT COUNT(*) FROM threads
                WHERE archived=0 AND source='vscode'
                  AND thread_source='user' AND has_user_event=1
                """
            ).fetchone()[0]
        )
    finally:
        conn.close()


def build_plan(profile: Profile) -> dict[str, Any]:
    report = load_recovery_report(profile)
    records = projection_records(profile, report)
    current = read_current_threads(profile)
    config_model_provider = current_config_model_provider(profile)
    config_defaults = current_config_defaults(profile)
    conn = connect_state(profile, True)
    try:
        columns = table_column_info(conn, "threads")
    finally:
        conn.close()
    templates_by_provider, generic_template = choose_template_rows(current)
    actions: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for record in records:
        thread_id = str(record["thread_id"])
        status = str(record.get("status") or "")
        record_origin = str(record.get("projection_origin") or "recovery_report")
        if status not in profile.allowed_statuses:
            rejected.append({"thread_id": thread_id, "title": record.get("title", ""), "status": status, "reason": "status_not_allowed", "projection_origin": record_origin})
            continue
        rollout = resolve_rollout_path(record, profile.recovery_report)
        if "rollout-sqlmaster-" in rollout.name.lower():
            rejected.append({"thread_id": thread_id, "title": record.get("title", ""), "status": status, "reason": "sqlmaster_not_allowed", "projection_origin": record_origin})
            continue
        if not rollout.exists():
            rejected.append({"thread_id": thread_id, "title": record.get("title", ""), "status": status, "reason": "rollout_missing", "projection_origin": record_origin})
            continue
        info = parse_rollout(rollout, thread_id)
        if not info.strict_valid:
            rejected.append({"thread_id": thread_id, "title": record.get("title", ""), "status": status, "reason": f"rollout_not_strict:{info.reason}", "projection_origin": record_origin})
            continue
        if info.ephemeral:
            rejected.append({"thread_id": thread_id, "title": record.get("title", ""), "status": status, "reason": "ephemeral_thread_not_allowed", "projection_origin": record_origin})
            continue
        if info.thread_source and info.thread_source != "user":
            rejected.append({"thread_id": thread_id, "title": record.get("title", ""), "status": status, "reason": f"thread_source_not_allowed:{info.thread_source}", "projection_origin": record_origin})
            continue
        existing = current.get(thread_id)
        title = str(record.get("title") or info.title or info.first_user_message or thread_id)
        model_provider, model_provider_source = planned_model_provider(existing, info, config_model_provider, profile)
        desired = {
            "id": thread_id,
            "rollout_path": str(rollout),
            "created_at": (info.created_ms or info.updated_ms) // 1000,
            "updated_at": (info.updated_ms or info.created_ms) // 1000,
            "source": existing_or(existing, "source", info.source or "vscode"),
            "model_provider": model_provider,
            "cwd": info.cwd or existing_or(existing, "cwd", str(profile.codex_home)),
            "title": title,
            "sandbox_policy": existing_or_nonempty(existing, "sandbox_policy", info.sandbox_policy),
            "approval_mode": existing_or_nonempty(existing, "approval_mode", info.approval_mode),
            "has_user_event": 1,
            "archived": 0,
            "created_at_ms": info.created_ms or info.updated_ms,
            "updated_at_ms": info.updated_ms or info.created_ms,
            "thread_source": existing_or(existing, "thread_source", info.thread_source or "user"),
            "first_user_message": info.first_user_message[:4000],
            "preview": info.first_user_message[:2000],
        }
        metadata_sources: dict[str, str] = {
            "model_provider": model_provider_source,
        }
        if "model" in columns and info.model:
            desired["model"] = existing_or_nonempty(existing, "model", info.model)
            metadata_sources["model"] = "existing_db_preserved" if existing and meaningful(existing.get("model"), nonempty=True) else "rollout"
        if "reasoning_effort" in columns and info.reasoning_effort:
            desired["reasoning_effort"] = existing_or_nonempty(existing, "reasoning_effort", info.reasoning_effort)
            metadata_sources["reasoning_effort"] = "existing_db_preserved" if existing and meaningful(existing.get("reasoning_effort"), nonempty=True) else "rollout"
        if "cli_version" in columns and info.cli_version:
            desired["cli_version"] = existing_or_nonempty(existing, "cli_version", info.cli_version)
            metadata_sources["cli_version"] = "existing_db_preserved" if existing and meaningful(existing.get("cli_version"), nonempty=True) else "rollout"
        if not existing:
            provider_template = templates_by_provider.get(model_provider)
            desired, inserted_sources, missing = enrich_insert_metadata(
                desired,
                columns,
                provider_template,
                generic_template,
                info,
                config_defaults,
            )
            metadata_sources.update(inserted_sources)
            if missing:
                rejected.append(
                    {
                        "thread_id": thread_id,
                        "title": title,
                        "status": status,
                        "reason": "insert_metadata_incomplete:" + ",".join(missing),
                        "projection_origin": record_origin,
                        "missing_metadata": missing,
                        "rollout_metadata": {
                            "model_provider": info.model_provider,
                            "sandbox_policy": bool(info.sandbox_policy),
                            "approval_mode": bool(info.approval_mode),
                            "model": bool(info.model),
                            "reasoning_effort": bool(info.reasoning_effort),
                            "cli_version": bool(info.cli_version),
                        },
                    }
                )
                continue
        diffs = {}
        if existing:
            for key, value in desired.items():
                if key == "id":
                    continue
                if str(existing.get(key) or "") != str(value or ""):
                    diffs[key] = {"from": existing.get(key), "to": value}
            action = "update" if diffs else "noop"
        else:
            action = "insert"
            diffs = {"new": desired}
        actions.append(
            {
                "action": action,
                "thread_id": thread_id,
                "title": desired["title"],
                "projection_origin": record_origin,
                "desired": desired,
                "diffs": diffs,
                "rollout": {
                    "path": str(rollout),
                    "sha256": sha256_file(rollout),
                    "message_events": info.message_events,
                    "user_messages": info.user_messages,
                    "assistant_messages": info.assistant_messages,
                },
                "metadata_sources": {
                    **metadata_sources,
                },
            }
        )
    created_at = utc_iso()
    return {
        "version": "2.5",
        "created_at": created_at,
        "generated_at": created_at,
        "profile": str(profile.path),
        "codex_home": str(profile.codex_home),
        "target_provider": profile.target_provider,
        "state_db": str(state_db(profile)),
        "session_index": str(session_index(profile)),
        "plan_policy": {
            "model_provider": "preserve_historical_v2",
            "runtime_fields": "complete_insert_metadata_v25",
        },
        "allowed_statuses": sorted(profile.allowed_statuses),
        "thread_metadata_policy": {
            "sandbox_policy": "existing DB row > rollout turn_context > same-provider template > reject insert",
            "approval_mode": "existing DB row > rollout turn_context > same-provider template > current config only for same provider > reject insert",
            "reason": "Safe Sync must not create half-populated thread rows. Missing required insert metadata rejects the row instead of inserting placeholders.",
            "model_provider": "existing DB row is preserved; new rows use rollout session_meta.model_provider; current config/profile are only rare fallbacks when rollout metadata is missing",
            "current_config_model_provider": config_model_provider,
            "warning": "Safe Sync preserves historical model_provider for resume routing; do not rewrite old threads to the current provider to fix UI filtering.",
        },
        "actions": actions,
        "rejected": rejected,
        "summary": {
            "actions_total": len(actions),
            "updates": sum(1 for a in actions if a["action"] == "update"),
            "inserts": sum(1 for a in actions if a["action"] == "insert"),
            "noops": sum(1 for a in actions if a["action"] == "noop"),
            "rejected": len(rejected),
            "recovery_report_threads": len([x for x in report.get("threads", []) if isinstance(x, dict)]),
            "live_session_threads": sum(1 for a in actions if a.get("projection_origin") == "live_sessions")
            + sum(1 for a in rejected if a.get("projection_origin") == "live_sessions"),
            "model_provider_sources": {
                source: sum(
                    1
                    for action in actions
                    if isinstance(action.get("metadata_sources"), dict)
                    and action["metadata_sources"].get("model_provider") == source
                )
                for source in sorted(
                    {
                        str(action.get("metadata_sources", {}).get("model_provider"))
                        for action in actions
                        if isinstance(action.get("metadata_sources"), dict)
                    }
                )
            },
        },
    }


def write_plan(profile: Profile, plan: dict[str, Any]) -> Path:
    plans_dir = local_dir() / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    path = plans_dir / f"projection-plan-{now_stamp()}.json"
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def snapshot(profile: Profile, label: str = "apply") -> Path:
    snap_dir = local_dir() / "snapshots" / f"{now_stamp()}-{label}"
    snap_dir.mkdir(parents=True, exist_ok=False)
    db = state_db(profile)
    backup_db = snap_dir / "state_5.sqlite.sqlite-backup"
    conn = sqlite3.connect(db)
    try:
        out = sqlite3.connect(backup_db)
        try:
            conn.backup(out)
        finally:
            out.close()
    finally:
        conn.close()
    shutil.copy2(db, snap_dir / "state_5.sqlite.raw-copy")
    for suffix in ("-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            shutil.copy2(p, snap_dir / f"state_5.sqlite{suffix}.raw-copy")
    if session_index(profile).exists():
        shutil.copy2(session_index(profile), snap_dir / "session_index.jsonl")
    global_state = profile.codex_home / ".codex-global-state.json"
    if global_state.exists():
        shutil.copy2(global_state, snap_dir / ".codex-global-state.json")
    manifest = {
        "created_at": utc_iso(),
        "codex_home": str(profile.codex_home),
        "state_db": str(db),
        "session_index": str(session_index(profile)),
        "files": {p.name: sha256_file(p) for p in snap_dir.iterdir() if p.is_file()},
    }
    (snap_dir / "snapshot-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return snap_dir


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")]


def qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def strip_unc_prefix(value: str) -> str:
    return value[4:] if value.startswith("\\\\?\\") else value


INSERT_RUNTIME_DENYLIST = {
    "agent_path",
    "agent_nickname",
    "agent_role",
    "tokens_used",
    "git_sha",
    "git_branch",
    "git_origin_url",
}


UPDATE_RUNTIME_DENYLIST = INSERT_RUNTIME_DENYLIST | {
    "sandbox_policy",
    "approval_mode",
    "model_provider",
}


def backfill_required_insert_metadata(
    action: dict[str, Any],
    desired: dict[str, Any],
    columns: dict[str, dict[str, Any]],
) -> None:
    for key, col in columns.items():
        if key in desired:
            continue
        if int(col.get("pk") or 0):
            continue
        if int(col.get("notnull") or 0) and col.get("dflt_value") is not None:
            desired[key] = parse_sqlite_default(col.get("dflt_value"))


def validate_insert_desired(desired: dict[str, Any], columns: dict[str, dict[str, Any]], thread_id: str) -> None:
    missing: list[str] = []
    for key, col in columns.items():
        if key in desired and meaningful(desired.get(key), nonempty=key in CRITICAL_NONEMPTY_INSERT_FIELDS):
            continue
        if int(col.get("pk") or 0):
            continue
        if key in CRITICAL_NONEMPTY_INSERT_FIELDS:
            missing.append(key)
            continue
        if int(col.get("notnull") or 0) and col.get("dflt_value") is None:
            missing.append(key)
    if missing:
        raise RuntimeError(f"Refusing to insert incomplete thread {thread_id}: missing {missing}")


def validate_touched_threads(
    conn: sqlite3.Connection,
    columns: dict[str, dict[str, Any]],
    actions: list[dict[str, Any]],
) -> dict[str, Any]:
    checked = 0
    failures: list[dict[str, Any]] = []
    for action in actions:
        if action.get("action") == "noop":
            continue
        thread_id = str(action.get("thread_id") or "")
        row = conn.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        if row is None:
            failures.append({"thread_id": thread_id, "reason": "missing_after_write"})
            continue
        data = dict(row)
        checked += 1
        missing = []
        for key, col in columns.items():
            if int(col.get("pk") or 0):
                continue
            value = data.get(key)
            if int(col.get("notnull") or 0) and value is None:
                missing.append(key)
            if key in CRITICAL_NONEMPTY_INSERT_FIELDS and not meaningful(value, nonempty=True):
                missing.append(key)
        if missing:
            failures.append({"thread_id": thread_id, "reason": "missing_required_metadata", "fields": sorted(set(missing))})
        rollout = str(data.get("rollout_path") or "")
        if rollout and not Path(strip_unc_prefix(rollout)).exists():
            failures.append({"thread_id": thread_id, "reason": "rollout_path_missing", "rollout_path": rollout})
        desired_provider = str(action.get("desired", {}).get("model_provider") or "")
        if desired_provider and str(data.get("model_provider") or "") != desired_provider:
            failures.append(
                {
                    "thread_id": thread_id,
                    "reason": "model_provider_changed",
                    "expected": desired_provider,
                    "actual": data.get("model_provider"),
                }
            )
    if failures:
        raise RuntimeError("Post-write thread metadata validation failed: " + json.dumps(failures[:5], ensure_ascii=False))
    return {"checked_threads": checked, "failures": 0}


def validate_session_index_alignment(profile: Profile) -> dict[str, Any]:
    visible = set(read_current_threads(profile).keys())
    # Match the same visible predicate used by rebuild_session_index.
    conn = connect_state(profile, True)
    try:
        visible = {
            str(row["id"])
            for row in conn.execute(
                """
                SELECT id FROM threads
                WHERE archived=0 AND source='vscode'
                  AND thread_source='user' AND has_user_event=1
                """
            ).fetchall()
        }
    finally:
        conn.close()
    entries = []
    parse_errors = 0
    target = session_index(profile)
    if target.exists():
        for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            if isinstance(obj, dict) and obj.get("id"):
                entries.append(str(obj["id"]))
    index_ids = set(entries)
    missing = sorted(visible - index_ids)
    unknown = sorted(index_ids - visible)
    if parse_errors or missing or unknown:
        raise RuntimeError(
            "session_index validation failed: "
            + json.dumps(
                {"parse_errors": parse_errors, "missing_visible_refs": missing[:10], "unknown_refs": unknown[:10]},
                ensure_ascii=False,
            )
        )
    return {"session_index_rows": len(entries), "parse_errors": 0, "missing_visible_refs": 0, "unknown_refs": 0}


def apply_plan(profile: Profile, plan_path: Path) -> dict[str, Any]:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    policy = plan.get("plan_policy")
    if not isinstance(policy, dict) or policy.get("model_provider") != "preserve_historical_v2":
        raise RuntimeError("此同步计划由旧版策略生成。请先重新生成 Safe Sync 计划。")
    if Path(plan["state_db"]).resolve() != state_db(profile).resolve():
        raise RuntimeError("Plan state_db does not match profile.")
    if profile.require_integrity_ok and db_integrity(profile) != "ok":
        raise RuntimeError("State DB integrity check is not ok.")
    snap = snapshot(profile, "projection") if profile.snapshot_before_apply else None
    if snap:
        shutil.copy2(plan_path, snap / "applied-plan.json")
    journal = local_dir() / "journal-current.json"
    journal.write_text(json.dumps({"started_at": utc_iso(), "plan": str(plan_path), "snapshot": str(snap) if snap else ""}, ensure_ascii=False, indent=2), encoding="utf-8")
    conn = connect_state(profile, False)
    applied = {"insert": 0, "update": 0, "noop": 0}
    try:
        columns = set(table_columns(conn, "threads"))
        current = {str(row["id"]): dict(row) for row in conn.execute("SELECT * FROM threads").fetchall()}
        conn.execute("BEGIN IMMEDIATE")
        for action in plan["actions"]:
            kind = action["action"]
            if kind == "noop":
                applied["noop"] += 1
                continue
            desired = {key: value for key, value in action["desired"].items() if key in columns}
            if kind == "insert":
                backfill_required_insert_metadata(action, desired, columns)
                desired = {key: value for key, value in desired.items() if key not in INSERT_RUNTIME_DENYLIST}
            if kind == "insert":
                keys = list(desired)
                placeholders = ",".join("?" for _ in keys)
                col_sql = ",".join(qident(key) for key in keys)
                conn.execute(
                    f"INSERT INTO threads ({col_sql}) VALUES ({placeholders})",
                    [desired[key] for key in keys],
                )
            elif kind == "update":
                if action["thread_id"] not in current:
                    raise RuntimeError(f"Planned update target disappeared: {action['thread_id']}")
                updates = {key: value for key, value in desired.items() if key != "id" and key not in UPDATE_RUNTIME_DENYLIST}
                current_row = current[action["thread_id"]]
                for key in ("sandbox_policy", "approval_mode"):
                    if key in desired and key in columns:
                        before = current_row.get(key)
                        after = desired.get(key)
                        if not meaningful(before, nonempty=True) and meaningful(after, nonempty=True):
                            updates[key] = after
                if not updates:
                    applied["noop"] += 1
                    continue
                assignments = ",".join(f"{qident(key)}=?" for key in updates)
                cursor = conn.execute(
                    f"UPDATE threads SET {assignments} WHERE id=?",
                    [*updates.values(), action["thread_id"]],
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"Planned update did not affect exactly one row: {action['thread_id']}")
            else:
                raise RuntimeError(f"Unsupported action kind: {kind}")
            applied[kind] += 1
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    rebuild_session_index(profile)
    if journal.exists():
        journal.unlink()
    return {"snapshot": str(snap) if snap else "", "applied": applied, "active_threads": active_thread_count(profile)}


def rebuild_session_index(profile: Profile) -> None:
    conn = connect_state(profile, True)
    try:
        rows = conn.execute(
            """
            SELECT id, title, COALESCE(updated_at_ms, updated_at * 1000, created_at_ms, created_at * 1000) AS updated_ms
            FROM threads
            WHERE archived=0 AND source='vscode'
              AND thread_source='user' AND has_user_event=1
            ORDER BY updated_ms ASC, id ASC
            """
        ).fetchall()
    finally:
        conn.close()
    lines = []
    for row in rows:
        lines.append(
            json.dumps(
                {
                    "id": row["id"],
                    "thread_name": row["title"],
                    "updated_at": ms_to_iso(int(row["updated_ms"] or 0)),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    target = session_index(profile)
    tmp = target.with_name(target.name + ".tmp-v21")
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    tmp.replace(target)


PROJECT_TOP_LEVEL_KEYS = (
    "electron-saved-workspace-roots",
    "project-order",
    "active-workspace-roots",
    "electron-workspace-root-labels",
)

PROJECT_ATOM_KEYS = ("sidebar-collapsed-groups",)


def load_json_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def norm_path_key(value: str) -> str:
    text = str(value)
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return os.path.normcase(text.rstrip("\\/"))


def unique_paths(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value)
        if not text:
            continue
        key = norm_path_key(text)
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def existing_roots(values: list[str]) -> tuple[list[str], list[str]]:
    existing: list[str] = []
    missing: list[str] = []
    for value in values:
        if Path(value).exists():
            existing.append(value)
        else:
            missing.append(value)
    return existing, missing


def project_roots(state: dict[str, Any]) -> list[str]:
    roots: list[Any] = []
    for key in ("electron-saved-workspace-roots", "project-order", "active-workspace-roots"):
        value = state.get(key)
        if isinstance(value, list):
            roots.extend(value)
    return unique_paths(roots)


def project_labels(state: dict[str, Any]) -> dict[str, str]:
    value = state.get("electron-workspace-root-labels")
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def atom_project_maps(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    atom = state.get("electron-persisted-atom-state")
    if not isinstance(atom, dict):
        return {}
    maps: dict[str, dict[str, Any]] = {}
    for key in PROJECT_ATOM_KEYS:
        value = atom.get(key)
        if isinstance(value, dict):
            maps[key] = value
    return maps


def discover_project_restore_source(profile: Profile) -> Path:
    candidates: list[Path] = []
    candidates.extend(profile.codex_home.glob("..codex-global-state.json.tmp-*"))
    backups = profile.codex_home / "backups_state"
    if backups.exists():
        candidates.extend(backups.rglob(".codex-global-state.json"))
    scored: list[tuple[int, float, Path]] = []
    for path in candidates:
        try:
            state = load_json_file(path)
        except Exception:
            continue
        roots, _missing = existing_roots(project_roots(state))
        if roots:
            scored.append((len(roots), path.stat().st_mtime, path))
    if not scored:
        raise RuntimeError("No project-bearing global-state backup found.")
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return scored[0][2].resolve()


def build_project_restore_plan(profile: Profile, source_path: Path | None) -> dict[str, Any]:
    source = (source_path or discover_project_restore_source(profile)).resolve()
    current_path = global_state_path(profile)
    if not current_path.exists():
        raise RuntimeError(f"Missing current global state: {current_path}")
    source_state = load_json_file(source)
    current_state = load_json_file(current_path)
    source_all_roots = project_roots(source_state)
    source_existing_roots, source_missing_roots = existing_roots(source_all_roots)
    current_roots = project_roots(current_state)
    desired_roots = unique_paths([*source_existing_roots, *current_roots])

    source_order = unique_paths([str(x) for x in source_state.get("project-order", []) if str(x) in source_existing_roots])
    desired_order = unique_paths([*source_order, *desired_roots])
    source_active = unique_paths([str(x) for x in source_state.get("active-workspace-roots", []) if str(x) in source_existing_roots])
    current_active = unique_paths([str(x) for x in current_state.get("active-workspace-roots", []) if str(x) in desired_roots])
    desired_active = unique_paths([*source_active, *current_active]) or desired_roots[:1]

    labels = project_labels(source_state)
    labels.update(project_labels(current_state))
    labels = {key: value for key, value in labels.items() if norm_path_key(key) in {norm_path_key(root) for root in desired_roots}}

    atom_maps: dict[str, dict[str, Any]] = {}
    current_atom_maps = atom_project_maps(current_state)
    for key, source_map in atom_project_maps(source_state).items():
        merged = dict(source_map)
        merged.update(current_atom_maps.get(key, {}))
        atom_maps[key] = merged

    desired: dict[str, Any] = {
        "electron-saved-workspace-roots": desired_roots,
        "project-order": desired_order,
        "active-workspace-roots": desired_active,
    }
    if labels:
        desired["electron-workspace-root-labels"] = labels
    if atom_maps:
        desired["electron-persisted-atom-state"] = atom_maps

    current_keys_present = [key for key in PROJECT_TOP_LEVEL_KEYS if key in current_state]
    added_roots = [root for root in desired_roots if norm_path_key(root) not in {norm_path_key(x) for x in current_roots}]
    created_at = utc_iso()
    return {
        "version": "2.1.1",
        "kind": "project-restore",
        "created_at": created_at,
        "generated_at": created_at,
        "profile": str(profile.path),
        "codex_home": str(profile.codex_home),
        "current_global_state": str(current_path),
        "current_global_state_sha256": sha256_file(current_path),
        "source_global_state": str(source),
        "source_global_state_sha256": sha256_file(source),
        "current_project_keys_present": current_keys_present,
        "source_roots": source_all_roots,
        "source_existing_roots": source_existing_roots,
        "source_missing_roots": source_missing_roots,
        "current_roots": current_roots,
        "added_roots": added_roots,
        "desired": desired,
        "summary": {
            "source_roots": len(source_all_roots),
            "source_existing_roots": len(source_existing_roots),
            "source_missing_roots": len(source_missing_roots),
            "current_roots": len(current_roots),
            "added_roots": len(added_roots),
            "desired_roots": len(desired_roots),
        },
    }


def write_project_restore_plan(plan: dict[str, Any]) -> Path:
    plans_dir = local_dir() / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    path = plans_dir / f"project-restore-plan-{now_stamp()}.json"
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def merge_project_restore_state(current: dict[str, Any], desired: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(current)
    for key in PROJECT_TOP_LEVEL_KEYS:
        if key in desired:
            merged[key] = desired[key]
    desired_atom = desired.get("electron-persisted-atom-state")
    if isinstance(desired_atom, dict):
        atom = merged.setdefault("electron-persisted-atom-state", {})
        if isinstance(atom, dict):
            for key in PROJECT_ATOM_KEYS:
                value = desired_atom.get(key)
                if isinstance(value, dict):
                    existing = atom.get(key)
                    if isinstance(existing, dict):
                        new_value = dict(value)
                        new_value.update(existing)
                        atom[key] = new_value
                    else:
                        atom[key] = value
    return merged


def apply_project_restore(profile: Profile, plan_path: Path) -> dict[str, Any]:
    plan = load_json_file(plan_path)
    if plan.get("kind") != "project-restore":
        raise RuntimeError("Plan is not a project-restore plan.")
    current_path = global_state_path(profile)
    if Path(plan["current_global_state"]).resolve() != current_path.resolve():
        raise RuntimeError("Plan current_global_state does not match profile.")
    snap = snapshot(profile, "project-restore") if profile.snapshot_before_apply else None
    if snap:
        shutil.copy2(plan_path, snap / "applied-project-restore-plan.json")
    current_state = load_json_file(current_path)
    merged = merge_project_restore_state(current_state, plan["desired"])
    tmp = current_path.with_name(current_path.name + ".tmp-v211")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(current_path)
    return {
        "snapshot": str(snap) if snap else "",
        "global_state": str(current_path),
        "restored_roots": merged.get("electron-saved-workspace-roots", []),
        "active_workspace_roots": merged.get("active-workspace-roots", []),
        "project_order": merged.get("project-order", []),
    }


def latest_snapshot() -> Path | None:
    snap_root = local_dir() / "snapshots"
    if not snap_root.exists():
        return None
    snaps = [p for p in snap_root.iterdir() if p.is_dir()]
    return sorted(snaps, key=lambda p: p.name)[-1] if snaps else None


def undo(profile: Profile, snapshot_path: Path | None) -> dict[str, Any]:
    snap = snapshot_path or latest_snapshot()
    if snap is None:
        raise RuntimeError("No snapshot found.")
    backup_db = snap / "state_5.sqlite.sqlite-backup"
    if not backup_db.exists():
        raise RuntimeError(f"Snapshot missing {backup_db.name}")
    db = state_db(profile)
    shutil.copy2(backup_db, db)
    for suffix in ("-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            p.unlink()
    idx = snap / "session_index.jsonl"
    if idx.exists():
        shutil.copy2(idx, session_index(profile))
    global_state = snap / ".codex-global-state.json"
    global_state_restored = False
    if global_state.exists():
        shutil.copy2(global_state, global_state_path(profile))
        global_state_restored = True
    return {"restored_snapshot": str(snap), "integrity": db_integrity(profile), "global_state_restored": global_state_restored}


def apply_plan(profile: Profile, plan_path: Path) -> dict[str, Any]:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    policy = plan.get("plan_policy")
    if (
        not isinstance(policy, dict)
        or policy.get("model_provider") != "preserve_historical_v2"
        or policy.get("runtime_fields") != "complete_insert_metadata_v25"
    ):
        raise RuntimeError("This Safe Sync plan was generated by an older policy. Regenerate the plan with V2.5 first.")
    if Path(plan["state_db"]).resolve() != state_db(profile).resolve():
        raise RuntimeError("Plan state_db does not match profile.")
    if profile.require_integrity_ok and db_integrity(profile) != "ok":
        raise RuntimeError("State DB integrity check is not ok.")
    snap = snapshot(profile, "projection") if profile.snapshot_before_apply else None
    if snap:
        shutil.copy2(plan_path, snap / "applied-plan.json")
    journal = local_dir() / "journal-current.json"
    journal.write_text(
        json.dumps({"started_at": utc_iso(), "plan": str(plan_path), "snapshot": str(snap) if snap else ""}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    conn = connect_state(profile, False)
    applied = {"insert": 0, "update": 0, "noop": 0}
    thread_validation: dict[str, Any] = {"checked_threads": 0, "failures": 0}
    try:
        column_info = table_column_info(conn, "threads")
        columns = set(column_info)
        current = {str(row["id"]): dict(row) for row in conn.execute("SELECT * FROM threads").fetchall()}
        conn.execute("BEGIN IMMEDIATE")
        for action in plan["actions"]:
            kind = action["action"]
            if kind == "noop":
                applied["noop"] += 1
                continue
            desired = {key: value for key, value in action["desired"].items() if key in columns}
            if kind == "insert":
                backfill_required_insert_metadata(action, desired, column_info)
                desired = {key: value for key, value in desired.items() if key not in INSERT_RUNTIME_DENYLIST}
                validate_insert_desired(desired, column_info, str(action["thread_id"]))
                keys = list(desired)
                placeholders = ",".join("?" for _ in keys)
                col_sql = ",".join(qident(key) for key in keys)
                conn.execute(
                    f"INSERT INTO threads ({col_sql}) VALUES ({placeholders})",
                    [desired[key] for key in keys],
                )
            elif kind == "update":
                if action["thread_id"] not in current:
                    raise RuntimeError(f"Planned update target disappeared: {action['thread_id']}")
                updates = {key: value for key, value in desired.items() if key != "id" and key not in UPDATE_RUNTIME_DENYLIST}
                if not updates:
                    applied["noop"] += 1
                    continue
                assignments = ",".join(f"{qident(key)}=?" for key in updates)
                cursor = conn.execute(
                    f"UPDATE threads SET {assignments} WHERE id=?",
                    [*updates.values(), action["thread_id"]],
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"Planned update did not affect exactly one row: {action['thread_id']}")
            else:
                raise RuntimeError(f"Unsupported action kind: {kind}")
            applied[kind] += 1
        thread_validation = validate_touched_threads(conn, column_info, plan["actions"])
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    rebuild_session_index(profile)
    index_validation = validate_session_index_alignment(profile)
    if journal.exists():
        journal.unlink()
    return {
        "snapshot": str(snap) if snap else "",
        "applied": applied,
        "active_threads": active_thread_count(profile),
        "validation": {
            "threads": thread_validation,
            "session_index": index_validation,
        },
    }


def command_doctor(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)
    report = load_recovery_report(profile) if profile.recovery_report.exists() else {}
    global_state = load_json_file(global_state_path(profile)) if global_state_path(profile).exists() else {}
    current_project_roots = project_roots(global_state) if global_state else []
    result = {
        "profile": str(profile.path),
        "codex_home": str(profile.codex_home),
        "state_db_exists": state_db(profile).exists(),
        "session_index_exists": session_index(profile).exists(),
        "global_state_exists": global_state_path(profile).exists(),
        "integrity": db_integrity(profile),
        "active_user_threads": active_thread_count(profile) if state_db(profile).exists() else 0,
        "project_roots": current_project_roots,
        "project_roots_count": len(current_project_roots),
        "target_provider": profile.target_provider,
        "recovery_report": str(profile.recovery_report),
        "recovery_threads": report.get("conversation_count", len(report.get("threads", []))) if report else 0,
        "recovery_summary": report.get("summary", {}) if report else {},
        "latest_snapshot": str(latest_snapshot() or ""),
        "journal_present": (local_dir() / "journal-current.json").exists(),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["integrity"] == "ok" else 2


def command_plan(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)
    plan = build_plan(profile)
    path = write_plan(profile, plan)
    print(json.dumps({"plan": str(path), "summary": plan["summary"], "rejected_sample": plan["rejected"][:5]}, ensure_ascii=False, indent=2))
    return 0


def command_apply(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)
    plan_path = resolve_from_project(args.plan, PROJECT_ROOT)
    result = apply_plan(profile, plan_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_plan_project_restore(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)
    source = resolve_from_project(args.source, PROJECT_ROOT) if args.source else None
    plan = build_project_restore_plan(profile, source)
    path = write_project_restore_plan(plan)
    print(json.dumps({"plan": str(path), "summary": plan["summary"], "source": plan["source_global_state"], "desired_roots": plan["desired"]["electron-saved-workspace-roots"]}, ensure_ascii=False, indent=2))
    return 0


def command_apply_project_restore(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)
    plan_path = resolve_from_project(args.plan, PROJECT_ROOT)
    result = apply_project_restore(profile, plan_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_undo(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)
    snap = resolve_from_project(args.snapshot, PROJECT_ROOT) if args.snapshot else None
    result = undo(profile, snap)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_publish_check(args: argparse.Namespace) -> int:
    risky_exts = {
        ".sqlite",
        ".jsonl",
        ".zip",
        ".asar",
        ".exe",
        ".dll",
        ".pak",
        ".bin",
        ".log",
        ".pyc",
        ".pyo",
    }
    allowed_private = {
        ".local",
        "archive",
        "backups",
        "outputs",
        "patched-desktop",
        "reports",
        "snapshots",
        "__pycache__",
    }
    allowed_private_names = {
        "RUN_PATCHED_CODEX_DESKTOP.cmd",
    }
    risky_names = {
        ".codex-global-state.json",
        ".env",
        "auth.json",
        "config.toml",
        "patch-manifest.json",
    }
    risky_text_patterns = (
        r"C:\\Users\\[^\\]+",
        r"D:\\SQLswitchcodex",
        r"D:\\DEV",
        r"E:\\(?:OneDrive|obsidoan)",
        r"(?i)(api[_-]?key\s*[:=]|authorization\s*[:=]|bearer\s+[a-z0-9._-]+)",
    )
    findings: list[str] = []
    private_present: list[str] = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(PROJECT_ROOT)
        if path.name in allowed_private_names or any(part in allowed_private for part in rel.parts):
            private_present.append(str(rel))
            continue
        if path.suffix.lower() in risky_exts:
            findings.append(str(rel))
        if path.name in risky_names and rel.parts[0] != "profiles":
            findings.append(str(rel))
        if path.suffix.lower() in {".py", ".md", ".toml", ".ps1", ".cmd", ".json"}:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            for pattern in risky_text_patterns:
                if re.search(pattern, text):
                    findings.append(f"{rel} matches {pattern}")
                    break
    print(
        json.dumps(
            {
                "ok": not findings,
                "findings": sorted(set(findings)),
                "ignored_private_files": len(private_present),
                "ignored_private_roots": sorted(
                    set(
                        part
                        for p in private_present
                        for part in Path(p).parts
                        if part in allowed_private or part in allowed_private_names
                    )
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if findings else 0


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sqlswitchcodex-v21")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn in (
        ("doctor", command_doctor),
        ("plan-projection", command_plan),
        ("apply-projection", command_apply),
        ("plan-project-restore", command_plan_project_restore),
        ("apply-project-restore", command_apply_project_restore),
        ("undo", command_undo),
    ):
        p = sub.add_parser(name)
        p.add_argument("--profile", default=None)
        p.set_defaults(func=fn)
        if name == "apply-projection":
            p.add_argument("--plan", required=True)
        if name == "plan-project-restore":
            p.add_argument("--source", default=None)
        if name == "apply-project-restore":
            p.add_argument("--plan", required=True)
        if name == "undo":
            p.add_argument("--snapshot", default=None)
    p = sub.add_parser("publish-check")
    p.set_defaults(func=command_publish_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        eprint(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
