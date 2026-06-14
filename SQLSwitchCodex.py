# -*- coding: utf-8 -*-
from __future__ import annotations

import csv
import copy
import datetime as dt
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
from pathlib import Path

try:
    stdio_encoding = os.environ.get("SQLSWITCH_STDIO_ENCODING", "utf-8")
    sys.stdout.reconfigure(encoding=stdio_encoding, errors="replace")
    sys.stderr.reconfigure(encoding=stdio_encoding, errors="replace")
except AttributeError:
    pass


ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
DEFAULT_PROFILE = ROOT / ".local" / "profile.toml"
PLANS_DIR = ROOT / ".local" / "plans"
SNAPSHOTS_DIR = ROOT / ".local" / "snapshots"
BACKUPS_DIR = ROOT / "backups"
REPORTS_DIR = ROOT / "reports"

SIDEBAR_UI_KEYS = (
    "electron-saved-workspace-roots",
    "project-order",
    "active-workspace-roots",
    "electron-workspace-root-labels",
    "projectless-thread-ids",
    "thread-workspace-root-hints",
    "thread-project-assignments",
    "thread-projectless-output-directories",
)

PROVIDER_FILTER_PATCH_FROM = b"modelProviders:null"
PROVIDER_FILTER_PATCH_TO = b"modelProviders:[]  "
if len(PROVIDER_FILTER_PATCH_FROM) != len(PROVIDER_FILTER_PATCH_TO):
    raise RuntimeError("provider display patch marker length mismatch")

DISALLOWED_GLOBAL_KEY_FRAGMENTS = (
    "sandbox",
    "approval",
    "provider",
    "auth",
    "heartbeat",
    "permission",
    "process_manager",
    "computer-use",
    "cache",
)

APP_ERROR_KEYWORDS = (
    "local database inaccessible",
    "sqlite state runtime failed",
    "failed to initialize sqlite state runtime",
    "state db backfill timeout",
    "state db backfill",
    "no such table",
    "database disk image is malformed",
    "couldn't set up agent sandbox",
    "couldn't set up non-admin sandbox",
    "sandbox setup failed",
    "agent sandbox failed",
    "non-admin sandbox failed",
    "write ace grant failed",
    "setnamedsecurityinfow failed",
    "setup refresh completed with errors",
    "setup refresh had errors",
    "app-server startup error",
    "process manager failed",
)

PROVIDER_KEYWORDS = (
    "401",
    "403",
    "429",
    "500",
    "502",
    "503",
    "model not found",
    "invalid_api_key",
    "connection timeout",
    "dns",
    "proxy",
    "base_url",
    "upstream",
    "rate limit",
)

STATUS_ORDER = ("PASS", "PROVIDER", "WARN", "APP_ERROR", "FATAL")
THREAD_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def pause() -> None:
    try:
        input("\n按 Enter 返回菜单...")
    except EOFError:
        pass


def pause_en() -> None:
    try:
        input("\nPress Enter to return...")
    except EOFError:
        pass


def clear() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def hr() -> None:
    print("-" * 72)


def latest_file(folder: Path, pattern: str) -> Path | None:
    if not folder.exists():
        return None
    matches = [p for p in folder.glob(pattern) if p.is_file()]
    return max(matches, key=lambda p: p.stat().st_mtime) if matches else None


def latest_snapshot() -> Path | None:
    if not SNAPSHOTS_DIR.exists():
        return None
    matches = [p for p in SNAPSHOTS_DIR.iterdir() if p.is_dir()]
    return max(matches, key=lambda p: p.name) if matches else None


def latest_backup() -> Path | None:
    if not BACKUPS_DIR.exists():
        return None
    matches = [p for p in BACKUPS_DIR.iterdir() if p.is_dir()]
    return max(matches, key=lambda p: p.name) if matches else None


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def strip_unc_prefix(value: str) -> str:
    return value[4:] if value.startswith("\\\\?\\") else value


def norm_path_key(value: str) -> str:
    return os.path.normcase(strip_unc_prefix(str(value)).rstrip("\\/"))


def unique_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value:
            continue
        key = norm_path_key(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_tool_report(prefix: str, data: dict[str, object]) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"{prefix}_{now_stamp()}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def status_rank(status: str) -> int:
    try:
        return STATUS_ORDER.index(status)
    except ValueError:
        return STATUS_ORDER.index("WARN")


def aggregate_status(statuses: list[str]) -> str:
    if not statuses:
        return "PASS"
    return max(statuses, key=status_rank)


def add_check(
    report: dict[str, object],
    category: str,
    status: str,
    item: str,
    detail: str = "",
    data: dict[str, object] | None = None,
) -> None:
    checks = report.setdefault("checks", [])
    if not isinstance(checks, list):
        raise RuntimeError("doctor report checks container is invalid")
    check: dict[str, object] = {
        "category": category,
        "status": status,
        "item": item,
        "detail": detail,
    }
    if data:
        check["data"] = data
    checks.append(check)


def finalize_doctor_report(report: dict[str, object]) -> dict[str, object]:
    checks = report.get("checks", [])
    if not isinstance(checks, list):
        checks = []
    categories: dict[str, dict[str, object]] = {}
    for check in checks:
        if not isinstance(check, dict):
            continue
        category = str(check.get("category") or "Unknown")
        status = str(check.get("status") or "WARN")
        item = categories.setdefault(
            category,
            {"status": "PASS", "counts": {name: 0 for name in STATUS_ORDER}},
        )
        counts = item.get("counts")
        if isinstance(counts, dict):
            counts[status] = int(counts.get(status, 0)) + 1
        item["status"] = aggregate_status([str(item.get("status") or "PASS"), status])
    statuses = [str(value.get("status")) for value in categories.values()]
    overall = aggregate_status(statuses)
    report["categories"] = categories
    report["overall_status"] = overall
    report["should_offer_undo"] = any(
        isinstance(check, dict) and str(check.get("status")) in {"APP_ERROR", "FATAL"}
        for check in checks
    )
    return report


def print_doctor_report(report: dict[str, object]) -> None:
    print("6 类 Doctor 自动检测")
    hr()
    print(f"总体状态       : {report.get('overall_status')}")
    print(f"是否建议撤销   : {'是' if report.get('should_offer_undo') else '否'}")
    print(f"Codex home    : {report.get('codex_home')}")
    processes = report.get("codex_processes")
    print(f"Codex 是否运行 : {'是' if processes else '否'}")
    print()
    categories = report.get("categories")
    if isinstance(categories, dict):
        for name in (
            "Local DB",
            "Left Sidebar",
            "Rollout",
            "App State",
            "Sandbox / Runtime",
            "Environment",
        ):
            info = categories.get(name, {})
            status = info.get("status", "PASS") if isinstance(info, dict) else "PASS"
            print(f"{name:<18} {status}")
    print()
    checks = report.get("checks")
    if isinstance(checks, list):
        important = [
            check for check in checks
            if isinstance(check, dict) and str(check.get("status")) != "PASS"
        ]
        if important:
            print("需要关注：")
            for check in important[:30]:
                print(f"  [{check.get('status')}] {check.get('category')} - {check.get('item')}: {check.get('detail')}")
            if len(important) > 30:
                print(f"  ... 还有 {len(important) - 30} 条，详见报告 JSON。")
        else:
            print("未发现需要关注的问题。")


def run_capture(args: list[str], timeout: int = 8) -> tuple[int, str]:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 999, str(exc)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def read_text_tail(path: Path, limit: int = 128 * 1024) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit:
                handle.seek(max(0, size - limit))
            data = handle.read(limit)
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def keyword_hits(text: str, keywords: tuple[str, ...]) -> list[str]:
    lower = text.lower()
    return [keyword for keyword in keywords if keyword in lower]


def is_inside_path(parent: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def codex_process_records() -> list[tuple[str, int]]:
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return []
    processes: list[tuple[str, int]] = []
    for row in csv.reader(io.StringIO(result.stdout)):
        if len(row) < 2:
            continue
        name = row[0].strip()
        try:
            pid = int(row[1])
        except ValueError:
            continue
        lower = name.lower()
        if lower in {"codex.exe", "codex"} or lower.startswith("codex"):
            if "command-runner" not in lower:
                processes.append((name, pid))
    return sorted(set(processes), key=lambda item: item[1])


def codex_processes() -> list[str]:
    return [f"{name}({pid})" for name, pid in codex_process_records()]


def require_codex_closed(operation: str) -> bool:
    processes = codex_process_records()
    if not processes:
        return True
    print()
    print(f"检测到 Codex 仍在运行：{', '.join(f'{name}({pid})' for name, pid in processes)}")
    print(f"{operation} 会写入 Codex 本地状态。为避免被内存旧状态覆盖，现在自动强制关闭 Codex。")
    for name, pid in processes:
        print(f"正在关闭 {name}({pid}) ...")
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    for _ in range(40):
        remaining = codex_process_records()
        if not remaining:
            print("Codex 已关闭，可以继续。")
            return True
        time.sleep(0.5)

    print("仍检测到 Codex 进程未退出：")
    for name, pid in codex_process_records():
        print(f"  {name}({pid})")
    print("已取消写入。请手动关闭 Codex 后重试。")
    return False


def import_cli_main():
    if not SRC.exists():
        raise RuntimeError(f"找不到 src 目录：{SRC}")
    sys.path.insert(0, str(SRC))
    from sqlswitchcodex_v21.cli import main as cli_main

    return cli_main


def run_cli(args: list[str]) -> int:
    cli_main = import_cli_main()
    old_cwd = Path.cwd()
    try:
        os.chdir(ROOT)
        return int(cli_main(args))
    finally:
        os.chdir(old_cwd)


def run_profile_command(command: str, extra: list[str] | None = None) -> int:
    args = [command, "--profile", str(DEFAULT_PROFILE)]
    if extra:
        args.extend(extra)
    return run_cli(args)


def load_profile_codex_home() -> Path | None:
    try:
        import tomllib

        data = tomllib.loads(DEFAULT_PROFILE.read_text(encoding="utf-8"))
        raw = str(data.get("codex_home", "")).strip()
        if not raw:
            return None
        path = Path(raw)
        if not path.is_absolute():
            path = ROOT / path
        return path.resolve()
    except Exception:
        return None


def create_backup(codex_home: Path, label: str) -> Path:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    backup_dir = BACKUPS_DIR / f"backup_{now_stamp()}_{label}"
    backup_dir.mkdir(parents=True, exist_ok=False)

    db = codex_home / "state_5.sqlite"
    session_index = codex_home / "session_index.jsonl"
    global_state = codex_home / ".codex-global-state.json"
    profile = DEFAULT_PROFILE

    copied: list[Path] = []
    if db.exists():
        sqlite_backup = backup_dir / "state_5.sqlite.sqlite-backup"
        source = sqlite3.connect(str(db))
        try:
            target = sqlite3.connect(str(sqlite_backup))
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        copied.append(sqlite_backup)

        raw_copy = backup_dir / "state_5.sqlite.raw-copy"
        shutil.copy2(db, raw_copy)
        copied.append(raw_copy)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db) + suffix)
            if sidecar.exists():
                out = backup_dir / f"state_5.sqlite{suffix}.raw-copy"
                shutil.copy2(sidecar, out)
                copied.append(out)

    for source_path, name in (
        (session_index, "session_index.jsonl.before"),
        (global_state, ".codex-global-state.json.before"),
        (profile, "profile.toml.before"),
    ):
        if source_path.exists():
            out = backup_dir / name
            shutil.copy2(source_path, out)
            copied.append(out)

    manifest = {
        "created_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "label": label,
        "codex_home": str(codex_home),
        "note": "V2.5 backup before left navigation sync. Restore manually or use .local snapshots.",
        "files": {path.name: sha256_file(path) for path in copied if path.exists()},
    }
    (backup_dir / "backup-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return backup_dir


def discover_codex_app_asar_candidates() -> list[Path]:
    candidates: list[Path] = []
    where_exe = shutil.which("where.exe")
    if not where_exe and os.name == "nt":
        system_root = Path(os.environ.get("SystemRoot", "C:\\Windows"))
        fallback = system_root / "System32" / "where.exe"
        if fallback.exists():
            where_exe = str(fallback)
    rc, output = run_capture([where_exe or "where.exe", "codex"], timeout=4)
    if rc == 0:
        for line in output.splitlines():
            raw = line.strip().strip('"')
            if not raw:
                continue
            path = Path(raw)
            if path.name.lower() in {"codex.exe", "codex"} and path.parent.name.lower() == "resources":
                candidates.append(path.parent / "app.asar")

    native_host = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "chrome-native-hosts.json"
    if native_host.exists():
        try:
            data = json.loads(native_host.read_text(encoding="utf-8", errors="replace"))
            resources_values: list[str] = []
            resources = data.get("resourcesPath")
            if isinstance(resources, str):
                resources_values.append(resources)
            hosts = data.get("chromeNativeHosts")
            if isinstance(hosts, list):
                for host in hosts:
                    if isinstance(host, dict) and isinstance(host.get("resourcesPath"), str):
                        resources_values.append(str(host["resourcesPath"]))
            for resources in resources_values:
                candidates.append(Path(resources) / "app.asar")
        except Exception:
            pass

    windows_apps = Path(os.environ.get("ProgramFiles", "C:\\Program Files")) / "WindowsApps"
    try:
        candidates.extend(windows_apps.glob("OpenAI.Codex_*__2p2nqsd0c76g0/app/resources/app.asar"))
        candidates.extend(windows_apps.glob("OpenAI.Codex_*/app/resources/app.asar"))
    except OSError:
        pass

    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        try:
            if not path.exists() or not path.is_file():
                continue
            key = str(path.resolve()).lower()
        except OSError:
            continue
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return sorted(unique, key=lambda p: p.stat().st_mtime, reverse=True)


def inspect_provider_display_patch(path: Path | None = None) -> dict[str, object]:
    candidates = [path] if path else discover_codex_app_asar_candidates()
    if not candidates:
        return {"status": "missing", "candidates": []}
    target = candidates[0]
    data = target.read_bytes()
    unpatched = data.count(PROVIDER_FILTER_PATCH_FROM)
    patched = data.count(PROVIDER_FILTER_PATCH_TO)
    if unpatched:
        status = "needs_patch"
    elif patched:
        status = "patched"
    else:
        status = "unknown_no_marker"
    writable = False
    write_error = ""
    try:
        with target.open("r+b"):
            writable = True
    except OSError as exc:
        write_error = f"{type(exc).__name__}: {exc}"
    return {
        "status": status,
        "app_asar": str(target),
        "sha256": sha256_file(target),
        "unpatched_occurrences": unpatched,
        "patched_occurrences": patched,
        "writable": writable,
        "write_error": write_error,
        "all_candidates": [str(item) for item in candidates],
    }


def current_config_model_provider(codex_home: Path) -> str | None:
    config = codex_home / "config.toml"
    if not config.exists():
        return None
    try:
        import tomllib

        data = tomllib.loads(config.read_text(encoding="utf-8", errors="replace"))
        provider = data.get("model_provider")
        return str(provider) if provider else None
    except Exception:
        text = config.read_text(encoding="utf-8", errors="replace")
        match = re.search(r'(?m)^\s*model_provider\s*=\s*["\']?([^"\'\s#]+)', text)
        return match.group(1) if match else None


def provider_visibility_summary(codex_home: Path) -> dict[str, object]:
    db = codex_home / "state_5.sqlite"
    summary: dict[str, object] = {
        "current_provider": current_config_model_provider(codex_home),
        "visible_all_providers": 0,
        "visible_current_provider": 0,
        "provider_counts": {},
    }
    if not db.exists():
        return summary
    conn = connect_sqlite_readonly(db)
    try:
        columns = set(sqlite_table_columns_local(conn, "threads"))
        if "model_provider" not in columns:
            return summary
        rows = conn.execute(
            """
            SELECT model_provider, COUNT(*) AS c
            FROM threads
            WHERE archived=0 AND source='vscode'
              AND thread_source='user' AND has_user_event=1
            GROUP BY model_provider
            """
        ).fetchall()
    finally:
        conn.close()
    counts = {str(row["model_provider"] or "<NULL>"): int(row["c"]) for row in rows}
    current = summary.get("current_provider")
    summary["provider_counts"] = counts
    summary["visible_all_providers"] = sum(counts.values())
    summary["visible_current_provider"] = counts.get(str(current), 0) if current else 0
    return summary


def read_persistent_env(name: str, scope: str) -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg

        if scope == "User":
            root = winreg.HKEY_CURRENT_USER
            subkey = "Environment"
        elif scope == "Machine":
            root = winreg.HKEY_LOCAL_MACHINE
            subkey = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
        else:
            return ""
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ) as key:
            value, _kind = winreg.QueryValueEx(key, name)
        return str(value or "")
    except Exception:
        return ""


def auth_env_status(codex_home: Path | None) -> dict[str, object]:
    provider = current_config_model_provider(codex_home) if codex_home else None
    auth_path = codex_home / "auth.json" if codex_home else None
    auth_exists = bool(auth_path and auth_path.exists())
    auth_has_key = False
    auth_parse_error = ""
    if auth_path and auth_path.exists():
        try:
            data = json.loads(auth_path.read_text(encoding="utf-8", errors="replace"))
            auth_has_key = isinstance(data, dict) and bool(data.get("OPENAI_API_KEY"))
        except Exception as exc:
            auth_parse_error = f"{type(exc).__name__}: {exc}"
    process_has_key = bool(os.environ.get("OPENAI_API_KEY"))
    user_has_key = bool(read_persistent_env("OPENAI_API_KEY", "User"))
    machine_has_key = bool(read_persistent_env("OPENAI_API_KEY", "Machine"))
    third_party = bool(provider and provider != "openai")
    return {
        "current_provider": provider or "",
        "third_party_mode": third_party,
        "auth_json": str(auth_path) if auth_path else "",
        "auth_json_exists": auth_exists,
        "auth_has_openai_api_key": auth_has_key,
        "auth_parse_error": auth_parse_error,
        "process_env_has_openai_api_key": process_has_key,
        "user_env_has_openai_api_key": user_has_key,
        "machine_env_has_openai_api_key": machine_has_key,
        "conflict": third_party and (auth_has_key or process_has_key or user_has_key or machine_has_key),
    }


def print_auth_env_status_en(status: dict[str, object]) -> None:
    print("Provider Auth/Env Status")
    hr()
    print(f"Current provider : {status.get('current_provider') or '(unknown)'}")
    print(f"auth.json exists : {'yes' if status.get('auth_json_exists') else 'no'}")
    print(f"auth has key     : {'yes' if status.get('auth_has_openai_api_key') else 'no'}")
    print(f"Process env key  : {'yes' if status.get('process_env_has_openai_api_key') else 'no'}")
    print(f"User env key     : {'yes' if status.get('user_env_has_openai_api_key') else 'no'}")
    print(f"Machine env key  : {'yes' if status.get('machine_env_has_openai_api_key') else 'no'}")
    if status.get("auth_parse_error"):
        print(f"auth parse       : {status.get('auth_parse_error')}")
    if status.get("conflict"):
        print("Overlap          : yes - third-party provider mode is using OPENAI_API_KEY/auth.json.")
        print("Note             : this can be intentional for OpenAI-compatible providers,")
        print("                   but historical openai threads may fail until matching auth is restored.")
    else:
        print("Overlap          : no")


def safe_sync_needed_from_report(report: dict[str, object]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    checks = report.get("checks")
    if not isinstance(checks, list):
        return False, reasons
    for check in checks:
        if not isinstance(check, dict):
            continue
        detail = str(check.get("detail") or "")
        item = str(check.get("item") or "")
        status = str(check.get("status") or "")
        if status == "PASS":
            continue
        for key in ("missing_db", "missing_index", "parse_errors", "missing_visible_refs"):
            match = re.search(rf"{key}=([0-9]+)", detail)
            if match and int(match.group(1)) > 0:
                reasons.append(f"{item}: {key}={match.group(1)}")
        if "session_index.jsonl" in item and status in {"WARN", "APP_ERROR", "FATAL"}:
            reasons.append(f"{item}: {detail}")
    return bool(reasons), reasons


def create_app_file_backup(path: Path, label: str) -> Path:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    backup_dir = BACKUPS_DIR / f"backup_{now_stamp()}_{label}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    copied = backup_dir / path.name
    shutil.copy2(path, copied)
    manifest = {
        "created_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "label": label,
        "source": str(path),
        "copy": str(copied),
        "source_sha256": sha256_file(path),
        "copy_sha256": sha256_file(copied),
        "note": "Backup before Codex Desktop provider sidebar display patch.",
    }
    (backup_dir / "backup-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return backup_dir


def replace_bytes_in_place(path: Path, needle: bytes, replacement: bytes) -> int:
    if len(needle) != len(replacement):
        raise RuntimeError("同长补丁长度不一致，拒绝写入。")
    data = path.read_bytes()
    offsets: list[int] = []
    start = 0
    while True:
        index = data.find(needle, start)
        if index < 0:
            break
        offsets.append(index)
        start = index + len(needle)
    if not offsets:
        return 0
    with path.open("r+b") as handle:
        for offset in offsets:
            handle.seek(offset)
            handle.write(replacement)
    return len(offsets)


def apply_provider_display_patch_to_asar(path: Path) -> dict[str, object]:
    before = inspect_provider_display_patch(path)
    changed = replace_bytes_in_place(path, PROVIDER_FILTER_PATCH_FROM, PROVIDER_FILTER_PATCH_TO)
    after = inspect_provider_display_patch(path)
    if changed and int(after.get("unpatched_occurrences") or 0) != 0:
        raise RuntimeError("补丁后仍发现 modelProviders:null，请从 backup 恢复后再检查。")
    return {"before": before, "after": after, "replacements": changed}


def undo_provider_display_patch_on_asar(path: Path) -> dict[str, object]:
    before = inspect_provider_display_patch(path)
    changed = replace_bytes_in_place(path, PROVIDER_FILTER_PATCH_TO, PROVIDER_FILTER_PATCH_FROM)
    after = inspect_provider_display_patch(path)
    return {"before": before, "after": after, "replacements": changed}


def is_windows_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def internal_provider_patch_child(mode: str, app_asar: Path, result_path: Path) -> int:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object]
    try:
        if mode == "apply":
            result = apply_provider_display_patch_to_asar(app_asar)
        elif mode == "undo":
            result = undo_provider_display_patch_on_asar(app_asar)
        else:
            raise RuntimeError(f"unknown provider patch mode: {mode}")
        payload = {"ok": True, "mode": mode, "result": result}
        rc = 0
    except Exception as exc:
        payload = {
            "ok": False,
            "mode": mode,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
        rc = 1
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return rc


def run_provider_patch_via_system_task_legacy_long_tr(app_asar: Path, mode: str) -> dict[str, object]:
    if os.name != "nt":
        raise RuntimeError("SYSTEM task fallback is only available on Windows.")
    if not is_windows_admin():
        raise PermissionError("当前进程不是管理员，无法创建 SYSTEM 计划任务。")

    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    result_path = PLANS_DIR / f"provider-patch-system-{mode}-{now_stamp()}.json"
    task_name = f"SQLSwitchCodexProviderPatch_{mode}_{now_stamp()}"
    start_time = (dt.datetime.now() + dt.timedelta(minutes=5)).strftime("%H:%M")
    command = subprocess.list2cmdline(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--internal-provider-patch",
            mode,
            str(app_asar),
            str(result_path),
        ]
    )
    create_args = [
        "schtasks",
        "/Create",
        "/TN",
        task_name,
        "/SC",
        "ONCE",
        "/ST",
        start_time,
        "/TR",
        command,
        "/RU",
        "SYSTEM",
        "/RL",
        "HIGHEST",
        "/F",
    ]
    rc_create, out_create = run_capture(create_args, timeout=20)
    if rc_create != 0:
        raise RuntimeError(f"创建 SYSTEM 计划任务失败：{out_create.strip()}")
    try:
        rc_run, out_run = run_capture(["schtasks", "/Run", "/TN", task_name], timeout=20)
        if rc_run != 0:
            raise RuntimeError(f"启动 SYSTEM 计划任务失败：{out_run.strip()}")
        deadline = time.time() + 90
        while time.time() < deadline:
            if result_path.exists():
                payload = json.loads(result_path.read_text(encoding="utf-8"))
                if not payload.get("ok"):
                    raise RuntimeError(str(payload.get("error") or "SYSTEM patch failed"))
                result = payload.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError("SYSTEM 补丁结果格式异常。")
                return {
                    "via": "SYSTEM scheduled task",
                    "task_name": task_name,
                    "result_path": str(result_path),
                    "result": result,
                }
            time.sleep(0.5)
        rc_query, out_query = run_capture(["schtasks", "/Query", "/TN", task_name, "/V", "/FO", "LIST"], timeout=20)
        raise RuntimeError(
            "SYSTEM 计划任务未产生结果文件。"
            f" query_rc={rc_query}; query={out_query.strip()}"
        )
    finally:
        run_capture(["schtasks", "/Delete", "/TN", task_name, "/F"], timeout=20)


def run_provider_patch_via_system_task(app_asar: Path, mode: str) -> dict[str, object]:
    if os.name != "nt":
        raise RuntimeError("SYSTEM task fallback is only available on Windows.")
    if not is_windows_admin():
        raise PermissionError("Current process is not elevated; cannot create SYSTEM scheduled task.")

    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now_stamp()
    result_path = PLANS_DIR / f"provider-patch-system-{mode}-{stamp}.json"
    wrapper_path = PLANS_DIR / f"provider_patch_system_{mode}_{stamp}.cmd"
    log_path = PLANS_DIR / f"provider-patch-system-{mode}-{stamp}.log"
    task_name = f"SQLSwitchCodexProviderPatch_{mode}_{stamp}"
    start_time = (dt.datetime.now() + dt.timedelta(minutes=5)).strftime("%H:%M")

    child_args = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--internal-provider-patch",
        mode,
        str(app_asar),
        str(result_path),
    ]
    wrapper_path.write_text(
        "\n".join(
            [
                "@echo off",
                "setlocal",
                'set "SQLSWITCH_STDIO_ENCODING=utf-8"',
                'set "PYTHONIOENCODING=utf-8"',
                f"{subprocess.list2cmdline(child_args)} > {subprocess.list2cmdline([str(log_path)])} 2>&1",
                "exit /b %ERRORLEVEL%",
                "",
            ]
        ),
        encoding="ascii",
    )
    task_command = f'cmd.exe /c "{wrapper_path}"'
    create_args = [
        "schtasks",
        "/Create",
        "/TN",
        task_name,
        "/SC",
        "ONCE",
        "/ST",
        start_time,
        "/TR",
        task_command,
        "/RU",
        "SYSTEM",
        "/RL",
        "HIGHEST",
        "/F",
    ]
    rc_create, out_create = run_capture(create_args, timeout=20)
    if rc_create != 0:
        raise RuntimeError(
            "Failed to create SYSTEM scheduled task: "
            f"{out_create.strip()} wrapper={wrapper_path} tr_len={len(task_command)}"
        )
    try:
        rc_run, out_run = run_capture(["schtasks", "/Run", "/TN", task_name], timeout=20)
        if rc_run != 0:
            raise RuntimeError(f"Failed to start SYSTEM scheduled task: {out_run.strip()}")
        deadline = time.time() + 90
        while time.time() < deadline:
            if result_path.exists():
                payload = json.loads(result_path.read_text(encoding="utf-8"))
                if not payload.get("ok"):
                    extra = read_text_tail(log_path, 16 * 1024) if log_path.exists() else ""
                    raise RuntimeError(str(payload.get("error") or "SYSTEM patch failed") + f"; log={extra}")
                result = payload.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError("SYSTEM patch result has an invalid format.")
                return {
                    "via": "SYSTEM scheduled task",
                    "task_name": task_name,
                    "result_path": str(result_path),
                    "wrapper_path": str(wrapper_path),
                    "log_path": str(log_path),
                    "result": result,
                }
            time.sleep(0.5)
        rc_query, out_query = run_capture(["schtasks", "/Query", "/TN", task_name, "/V", "/FO", "LIST"], timeout=20)
        extra = read_text_tail(log_path, 16 * 1024) if log_path.exists() else ""
        raise RuntimeError(
            "SYSTEM scheduled task did not create the result file. "
            f"query_rc={rc_query}; query={out_query.strip()}; wrapper={wrapper_path}; log={extra}"
        )
    finally:
        run_capture(["schtasks", "/Delete", "/TN", task_name, "/F"], timeout=20)


def apply_provider_display_patch_resilient(app_asar: Path) -> dict[str, object]:
    try:
        return {"via": "direct", "result": apply_provider_display_patch_to_asar(app_asar)}
    except PermissionError:
        return run_provider_patch_via_system_task(app_asar, "apply")


def undo_provider_display_patch_resilient(app_asar: Path) -> dict[str, object]:
    try:
        return {"via": "direct", "result": undo_provider_display_patch_on_asar(app_asar)}
    except PermissionError:
        return run_provider_patch_via_system_task(app_asar, "undo")


def create_patched_desktop_copy() -> dict[str, object]:
    candidates = discover_codex_app_asar_candidates()
    if not candidates:
        raise RuntimeError("Codex Desktop app.asar was not found.")
    source_asar = candidates[0]
    source_resources = source_asar.parent
    source_app = source_resources.parent
    if not (source_app / "Codex.exe").exists():
        raise RuntimeError(f"Source app directory does not look like Codex Desktop: {source_app}")

    dest_root = ROOT / "patched-desktop"
    dest_app = dest_root / "app"
    dest_resources = dest_app / "resources"
    dest_asar = dest_resources / "app.asar"
    dest_root.mkdir(parents=True, exist_ok=True)

    print(f"Source app      : {source_app}")
    print(f"Destination     : {dest_app}")
    print("Copying desktop files. This can take a few minutes...")
    robocopy = shutil.which("robocopy.exe") or shutil.which("robocopy")
    if robocopy:
        rc, output = run_capture(
            [
                robocopy,
                str(source_app),
                str(dest_app),
                "/E",
                "/COPY:DAT",
                "/DCOPY:DAT",
                "/R:1",
                "/W:1",
                "/NFL",
                "/NDL",
                "/NP",
            ],
            timeout=15 * 60,
        )
        if rc >= 8:
            raise RuntimeError(f"robocopy failed with rc={rc}: {output[-4000:]}")
    else:
        shutil.copytree(source_app, dest_app, dirs_exist_ok=True)

    if not dest_asar.exists():
        raise RuntimeError(f"Copied app.asar was not found: {dest_asar}")
    before = inspect_provider_display_patch(dest_asar)
    changed = replace_bytes_in_place(dest_asar, PROVIDER_FILTER_PATCH_FROM, PROVIDER_FILTER_PATCH_TO)
    after = inspect_provider_display_patch(dest_asar)
    if int(after.get("unpatched_occurrences") or 0) != 0:
        raise RuntimeError("Patched desktop copy still contains modelProviders:null.")

    old_launcher = ROOT / ("RUN_PATCHED_CODEX" + "_DESKTOP.cmd")
    if old_launcher.exists():
        old_launcher.unlink()
    manifest = {
        "created_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "source_app": str(source_app),
        "source_asar": str(source_asar),
        "dest_app": str(dest_app),
        "dest_asar": str(dest_asar),
        "launcher": "",
        "before": before,
        "after": after,
        "replacements": changed,
        "note": "Patched desktop copy. Original WindowsApps package is untouched. Launch it from RUN_SQLSwitchCodex.cmd.",
    }
    (dest_root / "patch-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def visible_thread_rows(codex_home: Path) -> list[sqlite3.Row]:
    db = codex_home / "state_5.sqlite"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return list(
            conn.execute(
                """
                SELECT id, title, cwd, updated_at_ms, updated_at
                FROM threads
                WHERE archived=0 AND source='vscode'
                  AND thread_source='user' AND has_user_event=1
                ORDER BY COALESCE(updated_at_ms, updated_at * 1000, created_at_ms, created_at * 1000) ASC, id ASC
                """
            )
        )
    finally:
        conn.close()


def session_index_ids(codex_home: Path) -> list[str]:
    path = codex_home / "session_index.jsonl"
    if not path.exists():
        return []
    ids: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        ids.append(str(obj.get("id") or ""))
    return unique_keep_order(ids)


def is_generated_codex_workspace(path_text: str) -> bool:
    path = Path(strip_unc_prefix(path_text))
    try:
        rel = path.resolve().relative_to((Path.home() / "Documents" / "Codex").resolve())
    except (OSError, ValueError):
        return False
    return len(rel.parts) >= 2 and rel.parts[0].startswith("20")


def is_real_project_workspace(path_text: str) -> bool:
    if not path_text:
        return False
    path = Path(strip_unc_prefix(path_text))
    if is_generated_codex_workspace(str(path)):
        return False
    try:
        if not path.exists() or not path.is_dir():
            return False
    except OSError:
        return False
    return True


def global_state_candidates(codex_home: Path) -> list[Path]:
    candidates: list[Path] = []
    candidates.extend(codex_home.glob("..codex-global-state.json.tmp-*"))
    backups_state = codex_home / "backups_state"
    if backups_state.exists():
        candidates.extend(backups_state.rglob(".codex-global-state.json"))
    candidates.extend((ROOT / ".local" / "snapshots").glob("*/.codex-global-state.json"))
    legacy = ROOT / "archive" / "legacy-20260613_080612" / "backups"
    if legacy.exists():
        candidates.extend(legacy.rglob(".codex-global-state.json.before"))
    result: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        try:
            key = str(path.resolve()).lower()
        except OSError:
            continue
        if key not in seen and path.exists():
            seen.add(key)
            result.append(path)
    return result


def backup_project_roots(codex_home: Path) -> tuple[list[str], dict[str, str], list[str]]:
    roots: list[str] = []
    labels: dict[str, str] = {}
    active: list[str] = []
    scored: list[tuple[float, Path, dict[str, object]]] = []
    for path in global_state_candidates(codex_home):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        source_roots = data.get("electron-saved-workspace-roots")
        if not isinstance(source_roots, list):
            continue
        existing_roots = [str(root) for root in source_roots if is_real_project_workspace(str(root))]
        if existing_roots:
            scored.append((path.stat().st_mtime, path, data))
    for _mtime, _path, data in sorted(scored, key=lambda item: item[0], reverse=True):
        roots.extend(str(root) for root in data.get("electron-saved-workspace-roots", []) if is_real_project_workspace(str(root)))
        active.extend(str(root) for root in data.get("active-workspace-roots", []) if is_real_project_workspace(str(root)))
        source_labels = data.get("electron-workspace-root-labels")
        if isinstance(source_labels, dict):
            for key, value in source_labels.items():
                if is_real_project_workspace(str(key)):
                    labels[str(key)] = str(value)
    return unique_keep_order(roots), labels, unique_keep_order(active)


def inferred_project_roots_from_db(codex_home: Path) -> list[str]:
    roots: list[str] = []
    for row in visible_thread_rows(codex_home):
        cwd = strip_unc_prefix(str(row["cwd"] or ""))
        if is_real_project_workspace(cwd):
            roots.append(cwd)
    if ROOT.exists():
        roots.append(str(ROOT))
    return unique_keep_order(roots)


def matching_project_root_for_cwd(cwd_text: str, project_roots: list[str]) -> str | None:
    cwd_key = norm_path_key(cwd_text)
    matches = [
        root for root in project_roots
        if cwd_key == norm_path_key(root) or cwd_key.startswith(norm_path_key(root) + os.sep)
    ]
    return max(matches, key=len) if matches else None


def projectless_thread_maps(
    codex_home: Path,
    state: dict[str, object],
    project_roots: list[str],
) -> tuple[list[str], dict[str, str], dict[str, str]]:
    db = codex_home / "state_5.sqlite"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = list(
            conn.execute(
                """
                SELECT id, cwd FROM threads
                WHERE archived=0 AND source='vscode'
                """
            )
        )
    finally:
        conn.close()

    rows_by_id = {str(row["id"]): row for row in rows}
    existing_projectless = state.get("projectless-thread-ids")
    if not isinstance(existing_projectless, list):
        existing_projectless = []

    projectless_ids: list[str] = []
    for thread_id in (str(x) for x in existing_projectless):
        row = rows_by_id.get(thread_id)
        if row is None or is_generated_codex_workspace(str(row["cwd"] or "")):
            projectless_ids.append(thread_id)
    for row in rows:
        if is_generated_codex_workspace(str(row["cwd"] or "")):
            projectless_ids.append(str(row["id"]))
    projectless_ids = unique_keep_order(projectless_ids)

    old_hints = state.get("thread-workspace-root-hints")
    old_outputs = state.get("thread-projectless-output-directories")
    if not isinstance(old_hints, dict):
        old_hints = {}
    if not isinstance(old_outputs, dict):
        old_outputs = {}

    default_workspace = str(Path.home() / "Documents" / "Codex")
    recovered_outputs = Path(default_workspace) / "sqlswitchcodex-restored-outputs"
    hints: dict[str, str] = {}
    outputs: dict[str, str] = {}

    for row in rows:
        thread_id = str(row["id"])
        cwd = strip_unc_prefix(str(row["cwd"] or ""))
        root = matching_project_root_for_cwd(cwd, project_roots)
        if root:
            hints[thread_id] = root

    for thread_id in projectless_ids:
        row = rows_by_id.get(thread_id)
        hints[thread_id] = str(old_hints.get(thread_id) or hints.get(thread_id) or default_workspace)
        if thread_id in old_outputs:
            outputs[thread_id] = str(old_outputs[thread_id])
        elif row and row["cwd"]:
            outputs[thread_id] = str(Path(strip_unc_prefix(str(row["cwd"]))) / "outputs")
        else:
            outputs[thread_id] = str(recovered_outputs / thread_id / "outputs")
    return projectless_ids, hints, outputs


def thread_project_assignments(
    codex_home: Path,
    state: dict[str, object],
    project_roots: list[str],
) -> tuple[dict[str, object], dict[str, int], int]:
    existing = state.get("thread-project-assignments")
    assignments: dict[str, object] = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    counts: dict[str, int] = {}
    changed = 0

    for row in visible_thread_dicts(codex_home):
        thread_id = str(row.get("id") or "")
        cwd = strip_unc_prefix(str(row.get("cwd") or ""))
        if not thread_id or not cwd:
            continue
        root = matching_project_root_for_cwd(cwd, project_roots)
        if not root:
            continue
        assignment = {
            "projectKind": "local",
            "projectId": root,
            "path": root,
            "cwd": cwd,
            "pendingCoreUpdate": False,
        }
        if stable_json(assignments.get(thread_id)) != stable_json(assignment):
            changed += 1
        assignments[thread_id] = assignment
        counts[root] = counts.get(root, 0) + 1

    return assignments, counts, changed


def build_sidebar_registry_projection(codex_home: Path) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    global_state_path = codex_home / ".codex-global-state.json"
    if not global_state_path.exists():
        raise RuntimeError(f"找不到 .codex-global-state.json：{global_state_path}")

    state = json.loads(global_state_path.read_text(encoding="utf-8"))
    desired_state = copy.deepcopy(state)

    backup_roots, backup_labels, backup_active = backup_project_roots(codex_home)
    current_roots = state.get("electron-saved-workspace-roots")
    if not isinstance(current_roots, list):
        current_roots = []
    inferred_roots = inferred_project_roots_from_db(codex_home)
    desired_roots = unique_keep_order([
        *(str(root) for root in current_roots if is_real_project_workspace(str(root))),
        *backup_roots,
        *inferred_roots,
    ])

    current_order = state.get("project-order")
    if not isinstance(current_order, list):
        current_order = []
    desired_order = unique_keep_order([
        *(str(root) for root in current_order if is_real_project_workspace(str(root))),
        *desired_roots,
    ])

    current_active = state.get("active-workspace-roots")
    if not isinstance(current_active, list):
        current_active = []
    desired_active = unique_keep_order([
        *(str(root) for root in current_active if is_real_project_workspace(str(root))),
        *backup_active,
    ])
    if not desired_active and desired_roots:
        desired_active = [desired_roots[0]]

    labels = state.get("electron-workspace-root-labels")
    if not isinstance(labels, dict):
        labels = {}
    labels = {str(k): str(v) for k, v in labels.items() if is_real_project_workspace(str(k))}
    labels.update(backup_labels)
    for root in desired_roots:
        labels.setdefault(root, Path(root).name)

    projectless_ids, hints, outputs = projectless_thread_maps(codex_home, state, desired_roots)
    assignments, assignment_counts, assignment_updates = thread_project_assignments(codex_home, state, desired_roots)

    desired_state["electron-saved-workspace-roots"] = desired_roots
    desired_state["project-order"] = desired_order
    desired_state["active-workspace-roots"] = desired_active
    desired_state["electron-workspace-root-labels"] = labels
    desired_state["projectless-thread-ids"] = projectless_ids
    desired_state["thread-workspace-root-hints"] = hints
    desired_state["thread-project-assignments"] = assignments
    desired_state["thread-projectless-output-directories"] = outputs

    all_keys = sorted(set(state.keys()) | set(desired_state.keys()))
    changed_keys = [
        key for key in all_keys
        if stable_json(state.get(key)) != stable_json(desired_state.get(key))
    ]
    disallowed_changed_keys = [key for key in changed_keys if key not in SIDEBAR_UI_KEYS]

    summary: dict[str, object] = {
        "project_roots": len(desired_roots),
        "projectless_threads": len(projectless_ids),
        "thread_workspace_hints": len(hints),
        "project_thread_hints": sum(
            1 for value in hints.values()
            if norm_path_key(str(value)) in {norm_path_key(root) for root in desired_roots}
        ),
        "thread_project_assignments": len(assignments),
        "project_thread_assignments": sum(assignment_counts.values()),
        "new_or_updated_project_assignments": assignment_updates,
        "project_assignment_counts": assignment_counts,
        "active_workspace_roots": desired_active,
        "restored_project_roots": desired_roots,
        "changed_keys": changed_keys,
        "allowed_keys": list(SIDEBAR_UI_KEYS),
        "disallowed_changed_keys": disallowed_changed_keys,
    }
    return state, desired_state, summary


def write_sidebar_registry_plan(codex_home: Path) -> Path:
    current, desired, summary = build_sidebar_registry_projection(codex_home)
    current_path = codex_home / ".codex-global-state.json"
    desired_keys = {key: desired.get(key) for key in SIDEBAR_UI_KEYS}
    plan = {
        "version": "SQLSwitchCodex V2.5 sidebar-ui-plan",
        "created_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "codex_home": str(codex_home),
        "global_state": str(current_path),
        "global_state_sha256": sha256_file(current_path),
        "allowed_keys": list(SIDEBAR_UI_KEYS),
        "desired": desired_keys,
        "summary": summary,
    }
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    path = PLANS_DIR / f"sidebar-ui-plan-{now_stamp()}.json"
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def diff_top_level_keys(before: dict[str, object], after: dict[str, object]) -> list[str]:
    return sorted(
        key for key in set(before.keys()) | set(after.keys())
        if stable_json(before.get(key)) != stable_json(after.get(key))
    )


def apply_sidebar_registry_plan(plan_path: Path, codex_home: Path) -> dict[str, object]:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    current_path = codex_home / ".codex-global-state.json"
    if Path(str(plan.get("global_state") or "")).resolve() != current_path.resolve():
        raise RuntimeError("UI 注册表计划不属于当前 Codex home。")
    if sha256_file(current_path) != str(plan.get("global_state_sha256") or ""):
        raise RuntimeError("global-state 已变化，请重新生成 UI 注册表计划。")
    allowed_keys = set(str(key) for key in plan.get("allowed_keys", []))
    if allowed_keys != set(SIDEBAR_UI_KEYS):
        raise RuntimeError("UI 注册表计划的白名单不匹配。")
    desired = plan.get("desired")
    if not isinstance(desired, dict):
        raise RuntimeError("UI 注册表计划缺少 desired。")

    before = json.loads(current_path.read_text(encoding="utf-8"))
    after = copy.deepcopy(before)
    for key, value in desired.items():
        key = str(key)
        if key not in SIDEBAR_UI_KEYS:
            raise RuntimeError(f"计划包含非白名单 key：{key}")
        after[key] = value

    changed = diff_top_level_keys(before, after)
    disallowed = [key for key in changed if key not in SIDEBAR_UI_KEYS]
    if disallowed:
        raise RuntimeError(f"拒绝写入：检测到非白名单变化 {disallowed}")

    tmp = current_path.with_name(current_path.name + ".tmp-v24-sidebar-ui")
    tmp.write_text(json.dumps(after, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(current_path)

    written = json.loads(current_path.read_text(encoding="utf-8"))
    written_changed = diff_top_level_keys(before, written)
    written_disallowed = [key for key in written_changed if key not in SIDEBAR_UI_KEYS]
    if written_disallowed:
        current_path.write_text(json.dumps(before, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        raise RuntimeError(f"写入后发现非白名单变化，已撤销：{written_disallowed}")

    return {
        "global_state": str(current_path),
        "changed_keys": written_changed,
        "summary": plan.get("summary", {}),
    }


def sync_sidebar_registry(codex_home: Path) -> dict[str, object]:
    current_path = codex_home / ".codex-global-state.json"
    before, after, summary = build_sidebar_registry_projection(codex_home)
    changed = diff_top_level_keys(before, after)
    disallowed = [key for key in changed if key not in SIDEBAR_UI_KEYS]
    if disallowed:
        raise RuntimeError(f"拒绝写入：检测到非白名单变化 {disallowed}")
    tmp = current_path.with_name(current_path.name + ".tmp-v24-sidebar-ui")
    tmp.write_text(json.dumps(after, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(current_path)
    return summary


def connect_sqlite_readonly(path: Path, timeout: float = 1.0) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    return conn


def sqlite_table_columns_local(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")]


def visible_thread_dicts(codex_home: Path) -> list[dict[str, object]]:
    db = codex_home / "state_5.sqlite"
    conn = connect_sqlite_readonly(db)
    try:
        columns = set(sqlite_table_columns_local(conn, "threads"))
        select_cols = [
            "id",
            "title",
            "cwd",
            "rollout_path",
            "source",
            "model_provider",
            "has_user_event",
            "archived",
            "thread_source",
            "updated_at_ms",
            "updated_at",
        ]
        selected = [col for col in select_cols if col in columns]
        rows = conn.execute(
            f"""
            SELECT {",".join(selected)}
            FROM threads
            WHERE archived=0 AND source='vscode'
              AND thread_source='user' AND has_user_event=1
            ORDER BY COALESCE(updated_at_ms, updated_at * 1000, created_at_ms, created_at * 1000) ASC, id ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def read_session_index_entries(path: Path) -> tuple[list[dict[str, object]], int]:
    entries: list[dict[str, object]] = []
    parse_errors = 0
    if not path.exists():
        return entries, parse_errors
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            parse_errors += 1
            continue
        if isinstance(obj, dict):
            entries.append(obj)
        else:
            parse_errors += 1
    return entries, parse_errors


def parse_rollout_health(path: Path, expected_thread_id: str) -> dict[str, object]:
    result: dict[str, object] = {
        "exists": path.exists(),
        "first_is_session_meta": False,
        "thread_id_matches": False,
        "json_parse_errors": 0,
        "message_events": 0,
        "assistant_messages": 0,
        "user_messages": 0,
        "source": "",
        "thread_source": "",
        "ephemeral": False,
        "provider_hits": [],
        "app_error_hits": [],
    }
    if not path.exists():
        return result

    first_obj: dict[str, object] | None = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    result["json_parse_errors"] = int(result["json_parse_errors"]) + 1
                    continue
                if first_obj is None:
                    first_obj = obj if isinstance(obj, dict) else {}
                if not isinstance(obj, dict):
                    continue
                event_type = str(obj.get("type") or "")
                payload = obj.get("payload")
                if event_type in {"response_item", "event_msg"}:
                    result["message_events"] = int(result["message_events"]) + 1
                if isinstance(payload, dict):
                    role = str(payload.get("role") or payload.get("type") or "").lower()
                    if role == "assistant":
                        result["assistant_messages"] = int(result["assistant_messages"]) + 1
                    elif role == "user":
                        result["user_messages"] = int(result["user_messages"]) + 1
                if index > 20000:
                    break
    except OSError as exc:
        result["read_error"] = str(exc)
        return result

    if isinstance(first_obj, dict):
        result["first_is_session_meta"] = first_obj.get("type") == "session_meta"
        payload = first_obj.get("payload")
        if isinstance(payload, dict):
            result["thread_id_matches"] = str(payload.get("id") or "") == expected_thread_id
            result["source"] = str(payload.get("source") or "")
            result["thread_source"] = str(payload.get("thread_source") or "")
            result["ephemeral"] = bool(payload.get("ephemeral") or False)
    return result


def infer_thread_id_from_rollout_name(path: Path) -> str | None:
    match = THREAD_ID_RE.search(path.name)
    return match.group(0) if match else None


def discover_live_ready_rollout_ids(codex_home: Path, limit_files: int = 5000) -> dict[str, str]:
    sessions = codex_home / "sessions"
    if not sessions.exists():
        return {}
    result: dict[str, str] = {}
    for index, rollout in enumerate(sorted(sessions.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)):
        if index >= limit_files:
            break
        if "rollout-sqlmaster-" in rollout.name.lower():
            continue
        thread_id = infer_thread_id_from_rollout_name(rollout)
        if not thread_id or thread_id in result:
            continue
        health = parse_rollout_health(rollout, thread_id)
        if (
            health.get("exists")
            and health.get("first_is_session_meta")
            and health.get("thread_id_matches")
            and not health.get("ephemeral")
            and str(health.get("thread_source") or "user") == "user"
            and int(health.get("user_messages") or 0) > 0
            and int(health.get("assistant_messages") or 0) > 0
            and int(health.get("json_parse_errors") or 0) == 0
        ):
            result[thread_id] = str(rollout)
    return result


def doctor_local_db(report: dict[str, object], codex_home: Path) -> None:
    category = "Local DB"
    db = codex_home / "state_5.sqlite"
    if not db.exists():
        add_check(report, category, "FATAL", "state_5.sqlite", f"缺失：{db}")
        return
    add_check(report, category, "PASS", "state_5.sqlite", f"存在，大小 {db.stat().st_size} bytes")

    try:
        conn = connect_sqlite_readonly(db)
        try:
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            add_check(
                report,
                category,
                "PASS" if integrity == "ok" else "FATAL",
                "PRAGMA integrity_check",
                integrity,
            )
            tables = [str(row["name"]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            add_check(
                report,
                category,
                "PASS" if "threads" in tables else "FATAL",
                "threads 表",
                "存在" if "threads" in tables else "缺失",
                {"tables": tables},
            )
            indexes = [str(row["name"]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")]
            triggers = [str(row["name"]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")]
            add_check(report, category, "PASS", "索引/触发器", f"indexes={len(indexes)}, triggers={len(triggers)}")
        finally:
            conn.close()
    except sqlite3.Error as exc:
        text = str(exc)
        if "locked" in text.lower():
            status = "APP_ERROR"
        elif "no such table" in text.lower() or "malformed" in text.lower():
            status = "FATAL"
        else:
            status = "APP_ERROR"
        add_check(report, category, status, "数据库读取", text)

    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db) + suffix)
        if not sidecar.exists():
            add_check(report, category, "PASS", sidecar.name, "不存在，正常")
            continue
        size = sidecar.stat().st_size
        status = "WARN" if size > 256 * 1024 * 1024 else "PASS"
        add_check(report, category, status, sidecar.name, f"大小 {size} bytes")


def doctor_left_sidebar(report: dict[str, object], codex_home: Path) -> None:
    category = "Left Sidebar"
    db = codex_home / "state_5.sqlite"
    if not db.exists():
        add_check(report, category, "FATAL", "threads", "数据库缺失，无法检查左侧列表")
        return
    try:
        conn = connect_sqlite_readonly(db)
        try:
            columns = set(sqlite_table_columns_local(conn, "threads"))
            required = {"id", "source", "has_user_event", "archived", "thread_source"}
            missing_columns = sorted(required - columns)
            add_check(
                report,
                category,
                "PASS" if not missing_columns else "FATAL",
                "threads 可见条件列",
                "完整" if not missing_columns else f"缺失：{missing_columns}",
            )
            if missing_columns:
                return
            total = int(conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0])
            visible = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM threads
                    WHERE archived=0 AND source='vscode'
                      AND thread_source='user' AND has_user_event=1
                    """
                ).fetchone()[0]
            )
            add_check(report, category, "PASS", "可见 threads 数量", f"visible={visible}, total={total}")
            duplicates = conn.execute(
                "SELECT id, COUNT(*) AS c FROM threads GROUP BY id HAVING c > 1 LIMIT 10"
            ).fetchall()
            add_check(
                report,
                category,
                "PASS" if not duplicates else "FATAL",
                "重复 thread_id",
                "无重复" if not duplicates else f"发现 {len(duplicates)} 个样本",
            )
            if "rollout_path" in columns:
                missing_rollout = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM threads
                        WHERE archived=0 AND source='vscode'
                          AND thread_source='user' AND has_user_event=1
                          AND (rollout_path IS NULL OR rollout_path='')
                        """
                    ).fetchone()[0]
                )
                add_check(
                    report,
                    category,
                    "PASS" if missing_rollout == 0 else "WARN",
                    "可见 threads rollout_path",
                    f"缺失 {missing_rollout} 条",
                )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        add_check(report, category, "APP_ERROR", "左侧列表查询", str(exc))
        return

    index_path = codex_home / "session_index.jsonl"
    entries, parse_errors = read_session_index_entries(index_path)
    if not index_path.exists():
        add_check(report, category, "WARN", "session_index.jsonl", "缺失，Codex 可能重建但左侧显示会异常")
        return
    ids = [str(item.get("id") or "") for item in entries if item.get("id")]
    visible_ids = {str(row.get("id") or "") for row in visible_thread_dicts(codex_home)}
    unknown_refs = sorted(set(ids) - visible_ids)
    missing_refs = sorted(visible_ids - set(ids))
    status = "PASS"
    detail = f"rows={len(entries)}, parse_errors={parse_errors}, unknown_refs={len(unknown_refs)}, missing_visible_refs={len(missing_refs)}"
    if parse_errors or unknown_refs or missing_refs:
        status = "WARN"
    add_check(report, category, status, "session_index 对齐", detail)

    try:
        patch = inspect_provider_display_patch()
        visibility = provider_visibility_summary(codex_home)
        if visibility:
            patch["provider_visibility"] = visibility
        patch_status = str(patch.get("status") or "unknown")
        if patch_status == "patched":
            add_check(
                report,
                category,
                "PASS",
                "Provider 显示过滤",
                f"已补丁：modelProviders=[]，patched={patch.get('patched_occurrences')}",
                patch,
            )
        elif patch_status == "needs_patch":
            add_check(
                report,
                category,
                "WARN",
                "Provider 显示过滤",
                "桌面端仍发送 modelProviders:null，"
                f"当前 provider={visibility.get('current_provider')} 时预计只显示 "
                f"{visibility.get('visible_current_provider')}/{visibility.get('visible_all_providers')} 条；"
                f"occurrences={patch.get('unpatched_occurrences')}",
                patch,
            )
        else:
            add_check(report, category, "WARN", "Provider 显示过滤", f"无法确认桌面端补丁状态：{patch_status}", patch)
    except Exception as exc:
        add_check(report, category, "WARN", "Provider 显示过滤", f"检查失败：{exc}")


def doctor_rollout(report: dict[str, object], codex_home: Path) -> None:
    category = "Rollout"
    try:
        rows = visible_thread_dicts(codex_home)
    except sqlite3.Error as exc:
        add_check(report, category, "APP_ERROR", "读取可见 threads", str(exc))
        return
    missing = 0
    parse_errors = 0
    session_meta_errors = 0
    thread_id_mismatches = 0
    sqlmaster = 0
    checked = 0
    for row in rows:
        thread_id = str(row.get("id") or "")
        rollout_text = str(row.get("rollout_path") or "")
        if not rollout_text:
            missing += 1
            continue
        rollout = Path(strip_unc_prefix(rollout_text))
        if "rollout-sqlmaster-" in rollout.name.lower():
            sqlmaster += 1
        health = parse_rollout_health(rollout, thread_id)
        checked += 1
        if not health.get("exists"):
            missing += 1
            continue
        if not health.get("first_is_session_meta"):
            session_meta_errors += 1
        if not health.get("thread_id_matches"):
            thread_id_mismatches += 1
        parse_errors += int(health.get("json_parse_errors") or 0)

    add_check(
        report,
        category,
        "PASS" if missing == 0 else "WARN",
        "rollout_path 文件",
        f"checked={checked}, missing={missing}",
    )
    add_check(
        report,
        category,
        "PASS" if session_meta_errors == 0 else "WARN",
        "session_meta 第一行",
        f"errors={session_meta_errors}",
    )
    add_check(
        report,
        category,
        "PASS" if thread_id_mismatches == 0 else "WARN",
        "thread_id 匹配",
        f"mismatches={thread_id_mismatches}",
    )
    add_check(
        report,
        category,
        "PASS" if parse_errors == 0 else "WARN",
        "JSONL 可解析",
        f"parse_errors={parse_errors}",
    )
    add_check(
        report,
        category,
        "PASS" if sqlmaster == 0 else "APP_ERROR",
        "rollout-sqlmaster 污染",
        f"visible_sqlmaster={sqlmaster}",
    )

    live_ready = discover_live_ready_rollout_ids(codex_home)
    visible_ids = {str(row.get("id") or "") for row in rows}
    index_entries, _index_parse_errors = read_session_index_entries(codex_home / "session_index.jsonl")
    index_ids = {str(item.get("id") or "") for item in index_entries if item.get("id")}
    missing_db = sorted(set(live_ready) - visible_ids)
    missing_index = sorted(set(live_ready) - index_ids)
    add_check(
        report,
        category,
        "PASS" if not missing_db else "WARN",
        "live sessions 未入 state_5",
        f"live_ready={len(live_ready)}, missing_db={len(missing_db)}",
        {"sample": missing_db[:10]},
    )
    add_check(
        report,
        category,
        "PASS" if not missing_index else "WARN",
        "live sessions 未入 session_index",
        f"live_ready={len(live_ready)}, missing_index={len(missing_index)}",
        {"sample": missing_index[:10]},
    )

    profile_report = None
    try:
        import tomllib

        profile = tomllib.loads(DEFAULT_PROFILE.read_text(encoding="utf-8"))
        raw_report = str(profile.get("recovery_report") or "")
        if raw_report:
            profile_report = Path(raw_report)
            if not profile_report.is_absolute():
                profile_report = ROOT / profile_report
    except Exception:
        profile_report = None
    if profile_report and profile_report.exists():
        try:
            data = json.loads(profile_report.read_text(encoding="utf-8"))
            statuses: dict[str, int] = {}
            for item in data.get("threads", []):
                status = str(item.get("status") or "unknown")
                statuses[status] = statuses.get(status, 0) + 1
            add_check(report, category, "PASS", "recovery_report 状态统计", stable_json(statuses), statuses)
        except Exception as exc:
            add_check(report, category, "WARN", "recovery_report", f"读取失败：{exc}")


def doctor_app_state(report: dict[str, object], codex_home: Path) -> None:
    category = "App State"
    path = codex_home / ".codex-global-state.json"
    if not path.exists():
        add_check(report, category, "WARN", ".codex-global-state.json", "缺失，Codex 可重建，但 Project/Chats 可能异常")
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    size = path.stat().st_size
    status = "PASS"
    if size > 5 * 1024 * 1024:
        status = "APP_ERROR"
    elif size > 1024 * 1024:
        status = "WARN"
    add_check(report, category, status, "global-state 大小", f"{size} bytes")
    if "\ufffd" in text or "\x00" in text:
        add_check(report, category, "WARN", "编码污染", "发现替换字符或 NUL 字符")
    try:
        state = json.loads(text)
    except json.JSONDecodeError as exc:
        add_check(report, category, "FATAL", "global-state JSON", str(exc))
        return
    add_check(report, category, "PASS", "global-state JSON", "有效 JSON")
    keys = sorted(str(key) for key in state.keys())
    dangerous = [
        key for key in keys
        if any(fragment in key.lower() for fragment in DISALLOWED_GLOBAL_KEY_FRAGMENTS)
    ]
    add_check(
        report,
        category,
        "PASS" if not dangerous else "WARN",
        "危险运行态 key",
        "无" if not dangerous else ", ".join(dangerous[:20]),
    )
    roots = state.get("electron-saved-workspace-roots")
    projectless = state.get("projectless-thread-ids")
    hints = state.get("thread-workspace-root-hints")
    assignments = state.get("thread-project-assignments")
    roots_count = len(roots) if isinstance(roots, list) else 0
    projectless_count = len(projectless) if isinstance(projectless, list) else 0
    hints_count = len(hints) if isinstance(hints, dict) else 0
    assignment_count = len(assignments) if isinstance(assignments, dict) else 0
    add_check(
        report,
        category,
        "PASS" if roots_count or projectless_count or hints_count else "WARN",
        "Sidebar UI 注册表",
        f"project_roots={roots_count}, projectless={projectless_count}, hints={hints_count}, assignments={assignment_count}",
    )
    if isinstance(hints, dict):
        visible_ids = {str(row.get("id") or "") for row in visible_thread_dicts(codex_home)}
        dangling = sorted(set(str(key) for key in hints.keys()) - visible_ids)
        add_check(
            report,
            category,
            "PASS" if not dangling else "WARN",
            "thread-workspace-root-hints 悬空引用",
            f"dangling={len(dangling)}",
        )
    if isinstance(roots, list):
        try:
            project_rows: list[tuple[str, str]] = []
            for row in visible_thread_dicts(codex_home):
                thread_id = str(row.get("id") or "")
                cwd = strip_unc_prefix(str(row.get("cwd") or ""))
                root = matching_project_root_for_cwd(cwd, [str(root) for root in roots])
                if thread_id and root:
                    project_rows.append((thread_id, root))
            missing_or_mismatch: list[str] = []
            assignment_map = assignments if isinstance(assignments, dict) else {}
            for thread_id, root in project_rows:
                assignment = assignment_map.get(thread_id)
                if not isinstance(assignment, dict):
                    missing_or_mismatch.append(thread_id)
                    continue
                if str(assignment.get("projectKind") or "") != "local":
                    missing_or_mismatch.append(thread_id)
                    continue
                project_id = str(assignment.get("projectId") or assignment.get("path") or "")
                if norm_path_key(project_id) != norm_path_key(root):
                    missing_or_mismatch.append(thread_id)
            add_check(
                report,
                category,
                "PASS" if not missing_or_mismatch else "WARN",
                "thread-project-assignments",
                f"project_threads={len(project_rows)}, assignments={assignment_count}, missing_or_mismatch={len(missing_or_mismatch)}",
                {"samples": missing_or_mismatch[:10]} if missing_or_mismatch else None,
            )
        except sqlite3.Error as exc:
            add_check(report, category, "WARN", "thread-project-assignments", f"无法检查：{exc}")


def log_candidate_files(codex_home: Path) -> list[Path]:
    candidates: list[Path] = []
    for folder in (codex_home / ".sandbox", codex_home / "logs", codex_home / "log"):
        if folder.exists():
            for pattern in ("*.log", "*.txt"):
                candidates.extend(folder.rglob(pattern))
    for path in codex_home.glob("*.log"):
        candidates.append(path)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        try:
            if path.stat().st_size > 20 * 1024 * 1024:
                continue
            key = str(path.resolve()).lower()
        except OSError:
            continue
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return sorted(unique, key=lambda p: p.stat().st_mtime, reverse=True)[:30]


WRITE_ACE_FAILURE_RE = re.compile(
    r"write ACE grant failed on (?P<path>.*?): SetNamedSecurityInfoW failed: (?P<code>\d+)",
    re.IGNORECASE,
)


def ps_single_quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def windows_acl_summary(path_text: str) -> dict[str, object]:
    summary: dict[str, object] = {"path": path_text}
    path = Path(path_text)
    summary["exists"] = path.exists()
    if not path.exists():
        return summary
    command = (
        f"$p={ps_single_quoted(path_text)};"
        "$acl=Get-Acl -LiteralPath $p;"
        "[PSCustomObject]@{"
        "Owner=$acl.Owner;"
        "Group=$acl.Group;"
        "AreAccessRulesProtected=$acl.AreAccessRulesProtected;"
        "AccessToString=$acl.AccessToString"
        "} | ConvertTo-Json -Depth 3"
    )
    rc, output = run_capture(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            command,
        ],
        timeout=8,
    )
    summary["acl_read_returncode"] = rc
    if rc == 0 and output.strip():
        try:
            loaded = json.loads(output)
            if isinstance(loaded, dict):
                summary.update(loaded)
                return summary
        except json.JSONDecodeError:
            pass
    summary["acl_read_output"] = output.strip()[:1000]
    return summary


def acl_summary_has_current_user_full_control(summary: dict[str, object]) -> bool:
    access = str(summary.get("AccessToString") or "").lower()
    username = os.environ.get("USERNAME", "").strip().lower()
    domain = os.environ.get("USERDOMAIN", "").strip().lower()
    if not username or "fullcontrol" not in access:
        return False
    candidates = {username}
    if domain:
        candidates.add(f"{domain}\\{username}")
    return any(candidate in access for candidate in candidates)


def sandbox_write_ace_failures(codex_home: Path) -> dict[str, dict[str, object]]:
    failures: dict[str, dict[str, object]] = {}
    sandbox_dir = codex_home / ".sandbox"
    if not sandbox_dir.exists():
        return failures
    logs = sorted(
        [path for path in sandbox_dir.glob("sandbox.*.log") if path.is_file()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:14]
    for log_path in logs:
        text = read_text_tail(log_path, limit=512 * 1024)
        for match in WRITE_ACE_FAILURE_RE.finditer(text):
            path_text = match.group("path").strip()
            code = match.group("code")
            item = failures.setdefault(
                norm_path_key(path_text),
                {"path": path_text, "code": code, "count": 0, "logs": []},
            )
            item["count"] = int(item.get("count") or 0) + 1
            logs_seen = item.setdefault("logs", [])
            if isinstance(logs_seen, list) and str(log_path) not in logs_seen:
                logs_seen.append(str(log_path))
    for item in failures.values():
        acl = windows_acl_summary(str(item.get("path") or ""))
        item["acl"] = acl
        item["current_user_has_full_control"] = acl_summary_has_current_user_full_control(acl)
    return failures


def doctor_sandbox_runtime(report: dict[str, object], codex_home: Path) -> None:
    category = "Sandbox / Runtime"
    logs_db = codex_home / "logs_2.sqlite"
    if logs_db.exists():
        try:
            conn = connect_sqlite_readonly(logs_db)
            try:
                integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
                add_check(
                    report,
                    category,
                    "PASS" if integrity == "ok" else "APP_ERROR",
                    "logs_2.sqlite",
                    f"integrity={integrity}",
                )
            finally:
                conn.close()
        except sqlite3.Error as exc:
            add_check(report, category, "WARN", "logs_2.sqlite", f"无法读取：{exc}")
    else:
        add_check(report, category, "PASS", "logs_2.sqlite", "不存在，不作为错误")

    app_hits: dict[str, list[str]] = {}
    provider_hits: dict[str, list[str]] = {}
    for path in log_candidate_files(codex_home):
        text = read_text_tail(path)
        hits = keyword_hits(text, APP_ERROR_KEYWORDS)
        if hits:
            app_hits[str(path)] = hits
        phits = keyword_hits(text, PROVIDER_KEYWORDS)
        if phits:
            provider_hits[str(path)] = phits
    add_check(
        report,
        category,
        "PASS" if not app_hits else "APP_ERROR",
        "沙箱/App 日志关键词",
        "无" if not app_hits else f"命中 {len(app_hits)} 个日志文件",
        {"hits": app_hits} if app_hits else None,
    )
    if provider_hits:
        add_check(
            report,
            category,
            "PROVIDER",
            "provider/network 日志关键词",
            f"命中 {len(provider_hits)} 个日志文件",
            {"hits": provider_hits},
        )
    auth_status = auth_env_status(codex_home)
    add_check(
        report,
        category,
        "PROVIDER" if auth_status.get("conflict") else "PASS",
        "provider auth/env overlap",
        (
            "third-party provider mode is using OPENAI_API_KEY/auth.json; may be intentional for custom base_url"
            if auth_status.get("conflict")
            else "no overlap"
        ),
        auth_status,
    )
    ace_failures = sandbox_write_ace_failures(codex_home) if os.name == "nt" else {}
    unresolved_ace_failures = [
        item for item in ace_failures.values()
        if not bool(item.get("current_user_has_full_control"))
    ]
    ace_detail = "无写 ACE 失败"
    ace_status = "PASS"
    if ace_failures:
        if unresolved_ace_failures:
            ace_status = "APP_ERROR"
            ace_detail = f"写 ACE 失败路径={len(ace_failures)}，未确认修复={len(unresolved_ace_failures)}"
        else:
            ace_status = "WARN"
            ace_detail = f"历史写 ACE 失败路径={len(ace_failures)}；当前 ACL 已包含用户 FullControl，重启 Codex 后验证"
    add_check(
        report,
        category,
        ace_status,
        "Windows sandbox workspace ACL",
        ace_detail,
        {"failures": list(ace_failures.values())} if ace_failures else None,
    )
    processes = codex_processes()
    add_check(
        report,
        category,
        "PASS",
        "Codex 进程",
        "未运行" if not processes else ", ".join(processes),
    )


def doctor_environment(report: dict[str, object], codex_home: Path) -> None:
    category = "Environment"
    add_check(report, category, "PASS", "Python", sys.executable)
    add_check(report, category, "PASS" if codex_home.exists() else "FATAL", "Codex home", str(codex_home))
    sessions = codex_home / "sessions"
    add_check(report, category, "PASS" if sessions.exists() else "FATAL", "sessions 资产目录", str(sessions))
    rc, output = run_capture(["where", "codex"], timeout=4)
    add_check(
        report,
        category,
        "PASS" if rc == 0 else "WARN",
        "PATH codex",
        output.strip()[:500] if output.strip() else "未在 PATH 中找到 codex，不一定影响 Desktop",
    )
    rc, output = run_capture(["wsl", "-l", "-v"], timeout=6)
    add_check(
        report,
        category,
        "PASS" if rc == 0 else "WARN",
        "WSL2",
        output.strip()[:800] if output.strip() else "wsl 不可用或超时",
    )
    for feature in ("Microsoft-Hyper-V-All", "VirtualMachinePlatform", "Containers-DisposableClientVM"):
        rc, output = run_capture(
            ["dism.exe", "/Online", "/Get-FeatureInfo", f"/FeatureName:{feature}", "/English"],
            timeout=10,
        )
        state_line = ""
        for line in output.splitlines():
            if "State :" in line:
                state_line = line.strip()
                break
        detail = state_line or output.strip()[:300] or "无法读取"
        add_check(report, category, "PASS" if rc == 0 else "WARN", feature, detail)


def build_doctor_report() -> dict[str, object]:
    codex_home = load_profile_codex_home()
    report: dict[str, object] = {
        "version": "SQLSwitchCodex V2.5 doctor-6",
        "created_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "tool_root": str(ROOT),
        "profile": str(DEFAULT_PROFILE),
        "codex_home": str(codex_home) if codex_home else "",
        "codex_processes": codex_processes(),
        "checks": [],
    }
    if not codex_home:
        add_check(report, "Environment", "FATAL", "profile codex_home", "无法从 profile.toml 读取 codex_home")
        return finalize_doctor_report(report)
    doctor_local_db(report, codex_home)
    doctor_left_sidebar(report, codex_home)
    doctor_rollout(report, codex_home)
    doctor_app_state(report, codex_home)
    doctor_sandbox_runtime(report, codex_home)
    doctor_environment(report, codex_home)
    return finalize_doctor_report(report)


def run_doctor(write_report: bool = True) -> dict[str, object]:
    report = build_doctor_report()
    print_doctor_report(report)
    if write_report:
        path = write_tool_report("doctor", report)
        print()
        print(f"报告已保存     : {path}")
    return report


def status_summary() -> None:
    print("左侧对话同步状态检查")
    hr()
    print(f"项目目录       : {ROOT}")
    print(f"当前 Python    : {sys.executable}")
    print(f"配置文件       : {DEFAULT_PROFILE}")
    print(f"Codex 是否运行 : {'是' if codex_processes() else '否'}")
    codex_home = load_profile_codex_home()
    if not codex_home:
        print("Codex home    : 无法从 profile 读取")
        return
    print(f"Codex home    : {codex_home}")

    db = codex_home / "state_5.sqlite"
    session_index = codex_home / "session_index.jsonl"
    global_state = codex_home / ".codex-global-state.json"
    print(f"state_5 数据库 : {'存在' if db.exists() else '缺失'}")
    print(f"session_index : {'存在' if session_index.exists() else '缺失'}")

    visible = 0
    if db.exists():
        try:
            conn = sqlite3.connect(str(db))
            visible = conn.execute(
                """
                SELECT COUNT(*) FROM threads
                WHERE archived=0 AND source='vscode'
                  AND thread_source='user' AND has_user_event=1
                """
            ).fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            conn.close()
            print(f"对话表         : 共 {total} 条，左侧应显示 {visible} 条，完整性={integrity}")
        except sqlite3.Error as exc:
            print(f"对话表         : 读取错误：{exc}")

    if global_state.exists():
        try:
            state = json.loads(global_state.read_text(encoding="utf-8"))
            projectless = state.get("projectless-thread-ids")
            registered = len(projectless) if isinstance(projectless, list) else 0
            roots = state.get("electron-saved-workspace-roots")
            root_count = len(roots) if isinstance(roots, list) else 0
            hints = state.get("thread-workspace-root-hints")
            hint_values = list(hints.values()) if isinstance(hints, dict) else []
            root_keys = {norm_path_key(str(root)) for root in roots} if isinstance(roots, list) else set()
            project_hint_count = sum(1 for value in hint_values if norm_path_key(str(value)) in root_keys)
            assignments = state.get("thread-project-assignments")
            assignment_values = assignments.values() if isinstance(assignments, dict) else []
            project_assignment_count = sum(
                1 for value in assignment_values
                if isinstance(value, dict)
                and str(value.get("projectKind") or "") == "local"
                and norm_path_key(str(value.get("projectId") or value.get("path") or "")) in root_keys
            )
            print(f"Project 工作区 : {root_count} 个")
            print(f"Chats 注册表   : {registered} 条")
            print(f"Project 对话映射: {project_hint_count} 条")
            print(f"Project 归属绑定: {project_assignment_count} 条")
            if visible and root_count == 0:
                print("提示           : Project 工作区缺失，可考虑谨慎 UI 注册表修复。")
            elif root_count and (project_hint_count == 0 or project_assignment_count == 0):
                print("提示           : Project 缺少对话映射/归属绑定，可考虑谨慎 UI 注册表修复。")
        except Exception as exc:
            print(f"左侧 UI 注册表 : 读取错误：{exc}")

    if session_index.exists():
        try:
            count = len([line for line in session_index.read_text(encoding="utf-8").splitlines() if line.strip()])
            print(f"session 行数   : {count}")
        except OSError as exc:
            print(f"session 行数   : 读取错误：{exc}")

    try:
        live_ready = discover_live_ready_rollout_ids(codex_home)
        visible_ids = {str(row.get("id") or "") for row in visible_thread_dicts(codex_home)}
        index_entries, _parse_errors = read_session_index_entries(session_index)
        index_ids = {str(item.get("id") or "") for item in index_entries if item.get("id")}
        missing_db = len(set(live_ready) - visible_ids)
        missing_index = len(set(live_ready) - index_ids)
        print(f"live sessions  : ready={len(live_ready)}, 未入DB={missing_db}, 未入index={missing_index}")
    except Exception as exc:
        print(f"live sessions  : 检查错误：{exc}")

    plan = latest_file(PLANS_DIR, "projection-plan-*.json")
    snap = latest_snapshot()
    backup = latest_backup()
    print(f"最新同步计划   : {plan if plan else '(无)'}")
    print(f"最新快照       : {snap if snap else '(无)'}")
    print(f"最新 backup    : {backup if backup else '(无)'}")


def show_guide() -> None:
    print("SQLSwitchCodex V2.5 指南")
    hr()
    print("这个工具以 Python 为主，cmd 只负责启动。日常菜单不调用 ps1。")
    print()
    print("安全边界：")
    print("  Safe Sync 只写 state_5.sqlite 和 session_index.jsonl。")
    print("  Safe Sync 默认不写 .codex-global-state.json。")
    print("  Safe Sync 保留历史 model_provider；不会把旧对话改成当前 provider。")
    print("  Safe Sync 会扫描旧 recovery_report 和当前 sessions\\**\\*.jsonl。")
    print("  只导入严格有效的用户对话；subagent/user-only/partial 只作为资产保留。")
    print("  ready_original 会进入可续聊列表；partial 只保存，不批量导入。")
    print("  不修改 rollout JSONL 正文、auth、provider、模型、沙箱、approval。")
    print("  Project/Chats UI 注册表修复是单独的谨慎功能，只允许 Sidebar UI 白名单 key。")
    print()
    print("6 类 Doctor：")
    print("  Local DB / Left Sidebar / Rollout / App State / Sandbox-Runtime / Environment。")
    print("  PROVIDER 网络/API 错误会单独分类，不作为自动撤销依据。")
    print("  APP_ERROR 或 FATAL 才建议撤销或极限修复。")
    print()
    print("推荐流程：")
    print("  1. 主菜单 3：检查并生成修复计划，只读。")
    print("  2. 主菜单 4：应用日常修复，自动执行 Safe Sync + Provider 显示补丁。")
    print("  3. 若 Project/Chats 归属仍不对，再用主菜单 5：谨慎 UI 注册表修复。")
    print("  4. 若出现 APP_ERROR/FATAL，进入主菜单 6：撤销与三级极限修复。")
    print()
    print("回滚方式：")
    print("  使用“撤销与三级极限修复”里的 Undo Last Sync。")
    print()
    print("启动方式：")
    print(f"  cd /d {ROOT}")
    print("  RUN_SQLSwitchCodex.cmd")
    print("  若 Doctor 显示 modelProviders:null 且 WindowsApps 不可写，请用管理员运行 RUN_SQLSwitchCodex.cmd option 1。")


def plan_projection() -> None:
    print("正在生成 Safe Sync Left Sidebar 计划...")
    print("范围：只投影 ready_original；partial 只保留为资产，不进入左侧可续聊列表。")
    print("输入：旧 recovery_report + 当前 sessions 中严格有效的用户对话。")
    print("写入范围：计划阶段不写 Codex 状态。")
    print("provider：保留历史 model_provider；不把旧线程改成当前 provider。")
    rc = run_profile_command("plan-projection")
    print(f"\n退出码：{rc}")


def apply_projection() -> None:
    plan = latest_file(PLANS_DIR, "projection-plan-*.json")
    if not plan:
        print("没有找到同步计划。请先运行“生成同步计划”。")
        return
    print(f"最新同步计划：{plan}")
    print()
    print("先运行 6 类 Doctor。若发现 APP_ERROR/FATAL，默认不继续同步。")
    report = run_doctor(write_report=True)
    if str(report.get("overall_status")) in {"APP_ERROR", "FATAL"}:
        print()
        print("Doctor 发现 Codex App/数据库级错误。建议先撤销或进入三级极限修复。")
        confirm_anyway = input("如仍要继续，请输入 APPLY_ANYWAY；直接回车取消：").strip()
        if confirm_anyway != "APPLY_ANYWAY":
            print("已取消。")
            return
    if not require_codex_closed("左侧对话 Safe Sync"):
        print("已取消。")
        return
    print()
    print("本步骤会：")
    print("  1. 创建 V1 风格 backup 存档。")
    print("  2. 写入 state_5.sqlite 和 session_index.jsonl。")
    print("  3. 只导入计划中的 ready_original，并保留历史 model_provider。")
    print("不会修改 .codex-global-state.json、沙箱、provider、auth、模型或 rollout 正文。")
    confirm = input("请输入 APPLY 以执行左侧对话同步：").strip()
    if confirm != "APPLY":
        print("已取消。")
        return
    codex_home = load_profile_codex_home()
    if not codex_home:
        print("无法读取 Codex home，已取消。")
        return
    backup = create_backup(codex_home, "sidebar_sync")
    print(f"已创建 backup：{backup}")
    rc = run_profile_command("apply-projection", ["--plan", str(plan)])
    print(f"\n退出码：{rc}")
    print()
    print("同步后自动 Doctor：")
    run_doctor(write_report=True)
    print()
    print("如果切换 provider 后旧对话仍不可见，请使用“显示全部 Provider 对话补丁”。")
    print("如果 Project/Chats 归属仍显示不对，再使用“谨慎 UI 注册表修复”。")


def check_and_plan_daily_fix() -> None:
    print("检查并生成修复计划")
    hr()
    print("本步骤只读：运行 Doctor、生成 Safe Sync 计划、检查 Provider 显示补丁状态。")
    print("不会写入 .codex，也不会修改 Codex Desktop 安装包。")
    print()
    report = run_doctor(write_report=True)
    if str(report.get("overall_status")) in {"APP_ERROR", "FATAL"}:
        print()
        print("Doctor 发现 App/数据库级错误。先不要应用普通修复，建议进入“撤销与三级极限修复”。")
        return

    print()
    print("正在生成 Safe Sync 计划...")
    rc = run_profile_command("plan-projection")
    print(f"生成计划退出码：{rc}")
    if rc != 0:
        print("计划生成失败，已停止。")
        return
    plan = latest_file(PLANS_DIR, "projection-plan-*.json")
    if plan and plan.exists():
        try:
            data = json.loads(plan.read_text(encoding="utf-8"))
            print("计划摘要：")
            print(json.dumps(data.get("summary", {}), ensure_ascii=False, indent=2))
        except Exception as exc:
            print(f"计划摘要读取失败：{exc}")

    print()
    show_provider_display_patch_status()
    print()
    print("下一步：若计划和补丁状态符合预期，回主菜单选择 4 应用日常修复。")


def apply_daily_fix() -> None:
    plan = latest_file(PLANS_DIR, "projection-plan-*.json")
    if not plan:
        print("没有找到 Safe Sync 计划。请先运行 3：检查并生成计划。")
        return
    codex_home = load_profile_codex_home()
    if not codex_home:
        print("无法读取 Codex home，已取消。")
        return

    print("应用日常修复")
    hr()
    print(f"使用计划：{plan}")
    print("本步骤会：")
    print("  1. 运行 Doctor；若发现 APP_ERROR/FATAL，默认停止。")
    print("  2. 自动关闭 Codex。")
    print("  3. 创建 backup，然后应用 Safe Sync。")
    print("  4. 若需要，备份并应用 Provider 显示补丁。")
    print("不会修改 config.toml / auth.json / 沙箱 / provider 配置 / rollout 正文。")
    print()
    report = run_doctor(write_report=True)
    if str(report.get("overall_status")) in {"APP_ERROR", "FATAL"}:
        print()
        print("Doctor 发现 Codex App/数据库级错误。建议先撤销或进入三级极限修复。")
        confirm_anyway = input("如仍要继续，请输入 APPLY_ANYWAY；直接回车取消：").strip()
        if confirm_anyway != "APPLY_ANYWAY":
            print("已取消。")
            return

    patch_info = inspect_provider_display_patch()
    if patch_info.get("status") == "missing":
        print()
        print("提示：未找到 Codex Desktop app.asar，本次只会应用 Safe Sync。")
    elif patch_info.get("status") == "unknown":
        print()
        print("提示：Provider 显示补丁状态不明确，本次只会应用 Safe Sync。")
    elif patch_info.get("status") == "needs_patch" and not patch_info.get("writable"):
        print()
        print("提示：已找到 Codex Desktop app.asar，但当前用户不可写。")
        print("Safe Sync 可继续；Provider 显示补丁需要关闭 Codex 后用管理员方式运行本菜单。")
        if patch_info.get("write_error"):
            print(f"写入检测：{patch_info.get('write_error')}")

    confirm = input("请输入 APPLY_FIX 以应用 Safe Sync + Provider 显示补丁：").strip()
    if confirm != "APPLY_FIX":
        print("已取消。")
        return
    if not require_codex_closed("日常修复：Safe Sync + Provider 显示补丁"):
        print("已取消。")
        return

    backup = create_backup(codex_home, "daily_fix_sidebar_sync")
    print(f"已创建 .codex backup：{backup}")
    rc = run_profile_command("apply-projection", ["--plan", str(plan)])
    print(f"Safe Sync 退出码：{rc}")
    if rc != 0:
        print("Safe Sync 未成功，已停止后续 Provider 显示补丁。")
        print("可查看上方错误，或进入撤销与三级极限修复。")
        return

    patch_info = inspect_provider_display_patch()
    if patch_info.get("status") == "needs_patch":
        app_asar = Path(str(patch_info.get("app_asar") or ""))
        app_backup = create_app_file_backup(app_asar, "provider_display_patch")
        print(f"已创建 app.asar backup：{app_backup}")
        try:
            result = apply_provider_display_patch_resilient(app_asar)
        except PermissionError as exc:
            print(f"权限不足，无法写入 app.asar：{exc}")
            print("Safe Sync 已完成；请以管理员身份运行 RUN_SQLSwitchCodex.cmd 后重试日常修复，或单独重新运行 4。")
        except RuntimeError as exc:
            print(f"Provider 显示补丁未完成：{exc}")
            print("Safe Sync 已完成；如果这里提示 SYSTEM 计划任务失败，请把本段输出发给我。")
        else:
            print("Provider 显示补丁完成：")
            print(json.dumps(result, ensure_ascii=False, indent=2))
    elif patch_info.get("status") == "patched":
        print("Provider 显示补丁已存在，跳过。")
    else:
        print("Provider 显示补丁未应用：状态不明确或未找到安装包。")

    print()
    print("修复后自动 Doctor：")
    run_doctor(write_report=True)
    print()
    print("若 Project/Chats 归属仍不对，再进入主菜单 5：谨慎 UI 注册表修复。")
def plan_sidebar_ui_repair() -> None:
    codex_home = load_profile_codex_home()
    if not codex_home:
        print("无法读取 Codex home，已取消。")
        return
    print("正在生成 Project/Chats UI 注册表修复计划...")
    print("这一步只生成计划，不写 .codex-global-state.json。")
    path = write_sidebar_registry_plan(codex_home)
    plan = json.loads(path.read_text(encoding="utf-8"))
    print(f"计划已生成：{path}")
    print("白名单 key：")
    for key in SIDEBAR_UI_KEYS:
        print(f"  - {key}")
    print("计划摘要：")
    print(json.dumps(plan.get("summary", {}), ensure_ascii=False, indent=2))


def apply_sidebar_ui_repair() -> None:
    plan = latest_file(PLANS_DIR, "sidebar-ui-plan-*.json")
    if not plan:
        print("没有找到 UI 注册表修复计划。请先生成计划。")
        return
    codex_home = load_profile_codex_home()
    if not codex_home:
        print("无法读取 Codex home，已取消。")
        return
    print(f"最新 UI 注册表修复计划：{plan}")
    plan_data = json.loads(plan.read_text(encoding="utf-8"))
    summary = plan_data.get("summary", {})
    print("计划摘要：")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    disallowed: list[str] = []
    if isinstance(summary, dict):
        raw = summary.get("disallowed_changed_keys", [])
        if isinstance(raw, list):
            disallowed = [str(item) for item in raw]
    if disallowed:
        print(f"计划包含非白名单变化，已拒绝：{disallowed}")
        return
    print()
    print("本步骤只允许写入 Sidebar Projection 白名单 key：")
    for key in SIDEBAR_UI_KEYS:
        print(f"  - {key}")
    print("不会写入 sandbox/provider/auth/runtime/cache/heartbeat/process_manager。")
    confirm = input("请输入 UI_APPLY 以应用谨慎 UI 注册表修复：").strip()
    if confirm != "UI_APPLY":
        print("已取消。")
        return
    if not require_codex_closed("Project/Chats UI 注册表修复"):
        print("已取消。")
        return
    print()
    print("Codex 已关闭，正在重新生成最终 UI 修复计划，避免使用已过期的 global-state hash...")
    final_plan = write_sidebar_registry_plan(codex_home)
    final_plan_data = json.loads(final_plan.read_text(encoding="utf-8"))
    final_summary = final_plan_data.get("summary", {})
    print(f"最终计划：{final_plan}")
    print(json.dumps(final_summary, ensure_ascii=False, indent=2))
    final_disallowed: list[str] = []
    if isinstance(final_summary, dict):
        raw = final_summary.get("disallowed_changed_keys", [])
        if isinstance(raw, list):
            final_disallowed = [str(item) for item in raw]
    if final_disallowed:
        print(f"最终计划包含非白名单变化，已拒绝：{final_disallowed}")
        return
    backup = create_backup(codex_home, "sidebar_ui_registry")
    print(f"已创建 backup：{backup}")
    result = apply_sidebar_registry_plan(final_plan, codex_home)
    print("UI 注册表修复完成：")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print()
    print("修复后自动 Doctor：")
    run_doctor(write_report=True)


def show_provider_display_patch_status() -> dict[str, object]:
    info = inspect_provider_display_patch()
    print("显示全部 Provider 对话补丁状态")
    hr()
    print(f"状态           : {info.get('status')}")
    print(f"app.asar       : {info.get('app_asar', '(未找到)')}")
    print(f"未补丁标记     : {info.get('unpatched_occurrences', 0)}")
    print(f"已补丁标记     : {info.get('patched_occurrences', 0)}")
    if info.get("app_asar"):
        print(f"当前权限       : {'可写' if info.get('writable') else '不可写'}")
        if info.get("write_error"):
            print(f"写入检测       : {info.get('write_error')}")
    print("说明           : modelProviders=[] 会让左侧列表显示全部 provider 的线程。")
    print("边界           : 不改 state_5.sqlite / session_index / config / auth / sandbox / rollout。")
    return info


def apply_provider_display_patch() -> None:
    info = show_provider_display_patch_status()
    if info.get("status") == "missing":
        print("没有找到 Codex Desktop 的 app.asar，无法应用补丁。")
        return
    if info.get("status") == "patched":
        print("当前安装包已经是补丁状态，不需要重复应用。")
        return
    if info.get("status") != "needs_patch":
        print("无法确认安装包结构，已取消。")
        return
    if not info.get("writable"):
        print()
        print("当前用户没有 app.asar 写权限。")
        print("这通常是 WindowsApps/TrustedInstaller 保护导致的；请关闭 Codex 后，用管理员方式启动 RUN_SQLSwitchCodex.cmd 再试。")
        print("如果管理员方式仍提示 PermissionError，不要强行 takeown，先把输出发给我。")
    app_asar = Path(str(info.get("app_asar") or ""))
    print()
    print("本步骤会：")
    print("  1. 自动关闭 Codex。")
    print("  2. 备份 app.asar 到 backups。")
    print("  3. 仅替换 modelProviders:null 为 modelProviders:[]。")
    print("不会修改 .codex、provider、auth、沙箱、模型或任何对话正文。")
    confirm = input("请输入 PATCH 以应用显示补丁：").strip()
    if confirm != "PATCH":
        print("已取消。")
        return
    if not require_codex_closed("显示全部 Provider 对话补丁"):
        print("已取消。")
        return
    backup = create_app_file_backup(app_asar, "provider_display_patch")
    print(f"已创建 backup：{backup}")
    try:
        result = apply_provider_display_patch_resilient(app_asar)
    except PermissionError as exc:
        print(f"权限不足，无法写入 app.asar：{exc}")
        print("请以管理员身份运行 RUN_SQLSwitchCodex.cmd 后重试。")
        return
    except RuntimeError as exc:
        print(f"Provider 显示补丁未完成：{exc}")
        print("如果这里提示 SYSTEM 计划任务失败，请把本段输出发给我。")
        return
    print("补丁完成：")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def undo_provider_display_patch() -> None:
    info = show_provider_display_patch_status()
    if info.get("status") == "missing":
        print("没有找到 Codex Desktop 的 app.asar，无法撤销补丁。")
        return
    if int(info.get("patched_occurrences") or 0) == 0:
        print("没有发现已补丁标记，不需要撤销。")
        return
    app_asar = Path(str(info.get("app_asar") or ""))
    print()
    print("本步骤会把 modelProviders:[]  反向恢复为 modelProviders:null。")
    confirm = input("请输入 UNPATCH 以撤销显示补丁：").strip()
    if confirm != "UNPATCH":
        print("已取消。")
        return
    if not require_codex_closed("撤销显示全部 Provider 对话补丁"):
        print("已取消。")
        return
    backup = create_app_file_backup(app_asar, "provider_display_unpatch")
    print(f"已创建撤销前 backup：{backup}")
    try:
        result = undo_provider_display_patch_resilient(app_asar)
    except PermissionError as exc:
        print(f"权限不足，无法写入 app.asar：{exc}")
        print("请以管理员身份运行 RUN_SQLSwitchCodex.cmd 后重试。")
        return
    except RuntimeError as exc:
        print(f"撤销 Provider 显示补丁未完成：{exc}")
        print("如果这里提示 SYSTEM 计划任务失败，请把本段输出发给我。")
        return
    print("撤销完成：")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def verify_current_state() -> None:
    report = run_doctor(write_report=True)
    print()
    if report.get("should_offer_undo"):
        print("Doctor 发现 APP_ERROR/FATAL，建议进入“撤销与三级极限修复”。")
    else:
        print("自动检测没有发现需要立即撤销的 App/数据库级错误。")
        print("如果切换 provider 后旧对话仍不可见，优先检查“显示全部 Provider 对话补丁”。")
        print("如果只是 Project/Chats 归属不对，再使用“谨慎 UI 注册表修复”。")


def provider_switch_wizard() -> None:
    print("Provider 切换后同步向导")
    hr()
    print("这里修的是左侧导航可见性，不是 provider 配置。")
    print("原则：保留历史 model_provider；显示层用 modelProviders=[] 显示全部 provider。")
    print("不会修改 config.toml / auth.json / 沙箱 / 模型 / rollout 正文。")
    print()
    print("步骤 1：自动 Doctor")
    report = run_doctor(write_report=True)
    if str(report.get("overall_status")) in {"APP_ERROR", "FATAL"}:
        print()
        print("Doctor 发现 App/数据库级错误。此时不建议做普通同步。")
        print("推荐进入“撤销与三级极限修复”。")
        confirm = input("如果你仍要继续向导，请输入 CONTINUE；直接回车取消：").strip()
        if confirm != "CONTINUE":
            print("已取消。")
            return

    print()
    print("步骤 2：生成 Safe Sync 计划")
    rc = run_profile_command("plan-projection")
    print(f"生成计划退出码：{rc}")
    if rc != 0:
        print("计划生成失败，已停止。")
        return

    plan = latest_file(PLANS_DIR, "projection-plan-*.json")
    if plan and plan.exists():
        try:
            data = json.loads(plan.read_text(encoding="utf-8"))
            print("计划摘要：")
            print(json.dumps(data.get("summary", {}), ensure_ascii=False, indent=2))
        except Exception as exc:
            print(f"计划摘要读取失败：{exc}")

    print()
    print("步骤 3：应用 Safe Sync")
    print("Safe Sync 只补 state_5.sqlite/session_index 缺失，不解决桌面端 provider 过滤。")
    print("如果计划里只有 noops，可以跳过这一步。")
    confirm = input("输入 APPLY 进入 Safe Sync 应用；直接回车跳过：").strip()
    if confirm == "APPLY":
        apply_projection()
    else:
        print("已跳过 Safe Sync 应用。")

    print()
    print("步骤 4：显示全部 Provider 对话补丁")
    patch = show_provider_display_patch_status()
    if patch.get("status") == "needs_patch":
        print()
        print("这是频繁切换 provider 后旧对话消失的主要显示层原因。")
        confirm = input("输入 PATCH 进入补丁应用；直接回车跳过：").strip()
        if confirm == "PATCH":
            apply_provider_display_patch()
        else:
            print("已跳过显示补丁。")
    elif patch.get("status") == "patched":
        print("显示补丁已存在。")
    else:
        print("无法自动确认显示补丁状态，可从主菜单 6 单独检查。")

    print()
    print("步骤 5：Project/Chats UI 注册表")
    print("只有 Project/Chats 归属仍异常时，才生成谨慎 UI 修复计划。")
    confirm = input("输入 UI_PLAN 生成 UI 修复计划；直接回车结束向导：").strip()
    if confirm == "UI_PLAN":
        plan_sidebar_ui_repair()
        print()
        print("若计划只包含白名单变化，可回到主菜单 7 继续应用 UI 修复计划。")
    else:
        print("向导结束。")


def undo_latest() -> None:
    snap = latest_snapshot()
    if not snap:
        print("没有找到快照。")
        return
    print(f"最新快照：{snap}")
    if not require_codex_closed("快照回滚"):
        print("已取消。")
        return
    confirm = input("请输入 UNDO 以恢复最新快照：").strip()
    if confirm != "UNDO":
        print("已取消。")
        return
    rc = run_profile_command("undo")
    print(f"\n退出码：{rc}")


def emergency_backup_dir(label: str) -> Path:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    path = BACKUPS_DIR / f"emergency_{now_stamp()}_{label}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def move_path_to_backup(source: Path, codex_home: Path, backup_dir: Path) -> str | None:
    if not source.exists():
        return None
    if not is_inside_path(codex_home, source):
        raise RuntimeError(f"拒绝移动 Codex home 之外的路径：{source}")
    rel = source.resolve().relative_to(codex_home.resolve())
    target = backup_dir / "moved" / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    return str(rel)


def move_named_children(codex_home: Path, backup_dir: Path, names: list[str]) -> list[str]:
    moved: list[str] = []
    for name in names:
        result = move_path_to_backup(codex_home / name, codex_home, backup_dir)
        if result:
            moved.append(result)
    return moved


def write_emergency_manifest(backup_dir: Path, codex_home: Path, level: str, moved: list[str], preserved: list[str]) -> None:
    manifest = {
        "created_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "level": level,
        "codex_home": str(codex_home),
        "moved_to_backup": moved,
        "preserved": preserved,
        "note": "Python-only emergency rebuild. Files are moved, not deleted.",
    }
    (backup_dir / "emergency-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def emergency_rebuild(level: int) -> None:
    codex_home = load_profile_codex_home()
    if not codex_home:
        print("无法读取 Codex home，已取消。")
        return
    if not codex_home.exists() or codex_home.name.lower() != ".codex":
        print(f"Codex home 看起来不正确，已取消：{codex_home}")
        return
    if not require_codex_closed(f"Emergency Level {level}"):
        print("已取消。")
        return

    if level == 1:
        print("Level 1 状态重建：移走 global-state / session_index / cache / process_manager。")
        print("保留 state_5.sqlite / config.toml / auth.json / sessions。")
        confirm_word = "LEVEL1"
        names = [".codex-global-state.json", "session_index.jsonl", "cache", "process_manager"]
        preserved = ["state_5.sqlite", "config.toml", "auth.json", "sessions"]
    elif level == 2:
        print("Level 2 数据库重建：Level 1 + 移走 state_5.sqlite / wal / shm。")
        print("保留 config.toml / auth.json / sessions；之后用 Safe Sync 重建左侧列表。")
        confirm_word = "LEVEL2"
        names = [
            ".codex-global-state.json",
            "session_index.jsonl",
            "cache",
            "process_manager",
            "state_5.sqlite",
            "state_5.sqlite-wal",
            "state_5.sqlite-shm",
        ]
        preserved = ["config.toml", "auth.json", "sessions"]
    elif level == 3:
        print("Level 3 近似新安装：备份并移走大多数 .codex 状态。")
        print("请选择目标模式：")
        print("  1. OpenAI 官方登录：保留 sessions 和 auth.json，移走 config.toml")
        print("  2. CC Switch / 第三方 provider：只保留 sessions，移走 auth.json 和 config.toml")
        mode = input("请选择 1/2，或回车取消：").strip()
        if mode not in {"1", "2"}:
            print("已取消。")
            return
        confirm_word = "LEVEL3_OPENAI" if mode == "1" else "LEVEL3_CCSWITCH"
        preserve_names = {"sessions"}
        if mode == "1":
            preserve_names.add("auth.json")
        names = [child.name for child in codex_home.iterdir() if child.name not in preserve_names]
        preserved = sorted(preserve_names)
    else:
        print("未知 Level。")
        return

    print()
    print("注意：本操作会移动文件到 backup，不会直接删除。")
    confirm = input(f"请输入 {confirm_word} 继续：").strip()
    if confirm != confirm_word:
        print("已取消。")
        return
    backup_dir = emergency_backup_dir(f"level{level}")
    moved = move_named_children(codex_home, backup_dir, names)
    write_emergency_manifest(backup_dir, codex_home, f"level{level}", moved, preserved)
    print(f"Emergency Level {level} 完成。")
    print(f"移动备份目录：{backup_dir}")
    print("已移动：")
    for item in moved:
        print(f"  - {item}")
    print("下一步：重新打开 Codex，让它重建状态；必要时再运行 Safe Sync。")


def recovery_menu() -> None:
    while True:
        clear()
        print("撤销与三级极限修复")
        hr()
        print("所有修复都使用 Python 移动/恢复文件；不调用 ps1。")
        print("Emergency 会移动到 backups\\emergency_*，不会直接删除。")
        print()
        print("  1. Undo Last Sync / 撤销最新快照       [会自动关闭 Codex]")
        print("  2. 撤销 Provider 显示补丁              [会自动关闭 Codex]")
        print("  3. Emergency Level 1 / 状态重建        [会自动关闭 Codex]")
        print("  4. Emergency Level 2 / 数据库重建      [会自动关闭 Codex]")
        print("  5. Emergency Level 3 / 近似新安装      [会自动关闭 Codex]")
        print("  0. 返回")
        print()
        try:
            choice = input("请选择：").strip()
        except EOFError:
            return
        clear()
        try:
            if choice == "1":
                undo_latest()
            elif choice == "2":
                undo_provider_display_patch()
            elif choice == "3":
                emergency_rebuild(1)
            elif choice == "4":
                emergency_rebuild(2)
            elif choice == "5":
                emergency_rebuild(3)
            elif choice == "0":
                return
            else:
                print("未知选项。")
        except KeyboardInterrupt:
            print("\n已取消。")
        except Exception as exc:
            print(f"ERROR: {exc}")
        pause_en()


def sidebar_ui_menu() -> None:
    while True:
        clear()
        print("Project/Chats UI 注册表修复（谨慎）")
        hr()
        print("只处理 .codex-global-state.json 中的 Sidebar Projection 白名单 key。")
        print("建议只在 Doctor 正常但左侧 Project/Chats 仍异常时使用。")
        print()
        print("  1. 生成 UI 修复计划              [只读]")
        print("  2. 应用 UI 修复计划              [会自动关闭 Codex]")
        print("  0. 返回")
        print()
        try:
            choice = input("请选择：").strip()
        except EOFError:
            return
        clear()
        try:
            if choice == "1":
                plan_sidebar_ui_repair()
            elif choice == "2":
                apply_sidebar_ui_repair()
            elif choice == "0":
                return
            else:
                print("未知选项。")
        except KeyboardInterrupt:
            print("\n已取消。")
        except Exception as exc:
            print(f"ERROR: {exc}")
        pause_en()


def provider_display_patch_menu() -> None:
    while True:
        clear()
        print("显示全部 Provider 对话补丁")
        hr()
        print("用途：修复切换 provider 后左侧只显示当前 provider 对话的问题。")
        print("依据：桌面端若发送 modelProviders:null，app-server 会按当前 provider 过滤；[] 才是不按 provider 过滤。")
        print()
        print("  1. 查看补丁状态                 [只读]")
        print("  2. 应用显示补丁                 [会自动关闭 Codex]")
        print("  3. 撤销显示补丁                 [会自动关闭 Codex]")
        print("  0. 返回")
        print()
        try:
            choice = input("请选择：").strip()
        except EOFError:
            return
        clear()
        try:
            if choice == "1":
                show_provider_display_patch_status()
            elif choice == "2":
                apply_provider_display_patch()
            elif choice == "3":
                undo_provider_display_patch()
            elif choice == "0":
                return
            else:
                print("未知选项。")
        except KeyboardInterrupt:
            print("\n已取消。")
        except Exception as exc:
            print(f"ERROR: {exc}")
        pause()


def require_codex_closed_en(operation: str) -> bool:
    processes = codex_process_records()
    if not processes:
        return True
    print()
    print("Codex is still running:")
    for name, pid in processes:
        print(f"  {name}({pid})")
    print(f"{operation} needs Codex closed. The tool will close Codex now.")
    for name, pid in processes:
        print(f"Closing {name}({pid}) ...")
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    for _ in range(40):
        remaining = codex_process_records()
        if not remaining:
            print("Codex is closed.")
            return True
        time.sleep(0.5)
    print("Codex is still running. Write operation cancelled.")
    for name, pid in codex_process_records():
        print(f"  {name}({pid})")
    return False


def print_provider_patch_status_en(info: dict[str, object], codex_home: Path | None = None) -> None:
    print("Provider Display Patch Status")
    hr()
    print(f"Status          : {info.get('status')}")
    print(f"app.asar        : {info.get('app_asar', '(not found)')}")
    print(f"null markers    : {info.get('unpatched_occurrences', 0)}")
    print(f"[] markers      : {info.get('patched_occurrences', 0)}")
    if info.get("app_asar"):
        print(f"Direct writable : {'yes' if info.get('writable') else 'no'}")
        if info.get("write_error"):
            print(f"Write check     : {info.get('write_error')}")
    if codex_home:
        try:
            visibility = provider_visibility_summary(codex_home)
            counts = visibility.get("provider_counts")
            print(f"Current provider: {visibility.get('current_provider')}")
            print(
                "Visible now     : "
                f"{visibility.get('visible_current_provider')}/"
                f"{visibility.get('visible_all_providers')} before patch"
            )
            print(f"Provider counts : {counts}")
        except Exception as exc:
            print(f"Visibility check: failed: {type(exc).__name__}: {exc}")


def write_provider_patch_audit(mode: str, stage: str, info: dict[str, object], extra: dict[str, object] | None = None) -> Path:
    payload: dict[str, object] = {
        "created_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "mode": mode,
        "stage": stage,
        "is_admin": is_windows_admin(),
        "codex_processes": codex_processes(),
        "patch_info": info,
    }
    if extra:
        payload.update(extra)
    return write_tool_report(f"provider_{mode}", payload)


def verify_current_state_en() -> None:
    report = build_doctor_report()
    path = write_tool_report("doctor", report)
    print("SQLSwitchCodex Doctor")
    hr()
    print(f"Overall         : {report.get('overall_status')}")
    print(f"Suggest undo    : {'yes' if report.get('should_offer_undo') else 'no'}")
    print(f"Codex home      : {report.get('codex_home')}")
    print(f"Codex running   : {'yes' if report.get('codex_processes') else 'no'}")
    print()
    categories = report.get("categories")
    if isinstance(categories, dict):
        for name in (
            "Local DB",
            "Left Sidebar",
            "Rollout",
            "App State",
            "Sandbox / Runtime",
            "Environment",
        ):
            info = categories.get(name, {})
            status = info.get("status", "PASS") if isinstance(info, dict) else "PASS"
            print(f"{name:<18} {status}")
    print()
    patch = inspect_provider_display_patch()
    print_provider_patch_status_en(patch, Path(str(report.get("codex_home"))))
    print()
    if str(patch.get("status")) == "needs_patch":
        print("Main blocker    : provider display patch is not applied.")
    elif str(patch.get("status")) == "patched":
        print("Main blocker    : provider display patch is applied; restart Codex and recheck UI.")
    else:
        print("Main blocker    : provider display patch status is unknown.")
    print()
    print(f"Report saved    : {path}")


def print_doctor_brief_en(report: dict[str, object], report_path: Path | None = None) -> None:
    print(f"Overall         : {report.get('overall_status')}")
    print(f"Suggest undo    : {'yes' if report.get('should_offer_undo') else 'no'}")
    print(f"Codex home      : {report.get('codex_home')}")
    print(f"Codex running   : {'yes' if report.get('codex_processes') else 'no'}")
    if report_path:
        print(f"Report saved    : {report_path}")


def safe_sync_plan_en() -> None:
    print("Safe Sync Plan")
    hr()
    print("Read-only step: Doctor, Safe Sync plan, and provider display patch status.")
    print("No .codex state or Desktop app package is modified here.")
    print()
    report = build_doctor_report()
    report_path = write_tool_report("doctor", report)
    print_doctor_brief_en(report, report_path)
    if str(report.get("overall_status")) in {"APP_ERROR", "FATAL"}:
        print()
        print("Doctor found an app/database-level error. Do not apply ordinary Safe Sync yet.")
        print("Use undo or emergency repair first.")
        return

    print()
    print("Creating Safe Sync plan...")
    rc = run_profile_command("plan-projection")
    print(f"Plan command exit code: {rc}")
    if rc != 0:
        print("Plan creation failed.")
        return
    plan = latest_file(PLANS_DIR, "projection-plan-*.json")
    if plan:
        print(f"Latest plan     : {plan}")
        try:
            data = json.loads(plan.read_text(encoding="utf-8"))
            print("Plan summary:")
            print(json.dumps(data.get("summary", {}), ensure_ascii=True, indent=2))
        except Exception as exc:
            print(f"Could not read plan summary: {type(exc).__name__}: {exc}")
    print()
    print_provider_patch_status_en(inspect_provider_display_patch(), load_profile_codex_home())


def apply_safe_sync_en(preconfirmed: bool = False) -> None:
    print("Apply Safe Sync")
    hr()
    plan = latest_file(PLANS_DIR, "projection-plan-*.json")
    if not plan:
        print("No Safe Sync plan found. Run option 6 first.")
        return
    codex_home = load_profile_codex_home()
    if not codex_home:
        print(f"Could not read Codex home from profile: {DEFAULT_PROFILE}")
        return
    print(f"Plan            : {plan}")
    print(f"Codex home      : {codex_home}")
    print()
    print("This will:")
    print("  1. Run Doctor.")
    print("  2. Close Codex if it is running.")
    print("  3. Create a backup under the tool backups folder.")
    print("  4. Write state_5.sqlite and session_index.jsonl from valid rollout-backed threads.")
    print()
    print("It will not edit config.toml, auth.json, provider config, sandbox, model,")
    print("rollout JSONL content, or .codex-global-state.json.")
    print()
    report = build_doctor_report()
    report_path = write_tool_report("doctor", report)
    print_doctor_brief_en(report, report_path)
    if str(report.get("overall_status")) in {"APP_ERROR", "FATAL"}:
        print()
        confirm_anyway = input("Doctor found APP_ERROR/FATAL. Type APPLY_ANYWAY to continue: ").strip()
        if confirm_anyway != "APPLY_ANYWAY":
            print("Cancelled.")
            return
    if not preconfirmed:
        confirm = input("Type APPLY_SAFE_SYNC to write the local sidebar index: ").strip()
        if confirm != "APPLY_SAFE_SYNC":
            print("Cancelled.")
            return
    if not require_codex_closed_en("Safe Sync"):
        print("Cancelled.")
        return
    backup = create_backup(codex_home, "safe_sync")
    print(f"Backup created  : {backup}")
    rc = run_profile_command("apply-projection", ["--plan", str(plan)])
    print(f"Safe Sync exit code: {rc}")
    print()
    after = build_doctor_report()
    after_path = write_tool_report("doctor", after)
    print("Post-sync Doctor:")
    print_doctor_brief_en(after, after_path)
    if rc != 0:
        print()
        print("Safe Sync did not complete successfully. Check the output above and the backup path.")


def apply_provider_display_patch_en() -> None:
    codex_home = load_profile_codex_home()
    info = inspect_provider_display_patch()
    print_provider_patch_status_en(info, codex_home)
    audit_path = write_provider_patch_audit("apply", "started", info)
    print(f"Patch audit     : {audit_path}")
    status = str(info.get("status") or "unknown")
    if status == "missing":
        write_provider_patch_audit("apply", "missing_app_asar", info)
        print("Codex Desktop app.asar was not found. Patch cancelled.")
        return
    if status == "patched":
        write_provider_patch_audit("apply", "already_patched", info)
        print("Patch is already applied.")
        return
    if status != "needs_patch":
        write_provider_patch_audit("apply", "unknown_marker", info)
        print("The app package marker is unknown. Patch cancelled.")
        return
    if not info.get("writable") and not is_windows_admin():
        write_provider_patch_audit("apply", "needs_admin", info)
        print()
        print("This app.asar is protected by WindowsApps.")
        print("Please run RUN_SQLSwitchCodex.cmd as Administrator and choose option 1.")
        return
    print()
    print("This will:")
    print("  1. Close Codex if it is running.")
    print(f"  2. Back up app.asar under {BACKUPS_DIR}.")
    print("  3. Replace modelProviders:null with modelProviders:[]  .")
    print("It will not edit .codex data, provider config, auth, sandbox, model, or rollouts.")
    confirm = input("Type PATCH to apply: ").strip()
    if confirm != "PATCH":
        write_provider_patch_audit("apply", "cancelled_by_user", info)
        print("Cancelled.")
        return
    if not require_codex_closed_en("Provider display patch"):
        write_provider_patch_audit("apply", "codex_not_closed", info)
        print("Cancelled.")
        return
    app_asar = Path(str(info.get("app_asar") or ""))
    backup = create_app_file_backup(app_asar, "provider_display_patch")
    print(f"Backup created  : {backup}")
    try:
        result = apply_provider_display_patch_resilient(app_asar)
    except Exception as exc:
        write_provider_patch_audit("apply", "failed", info, {"error": f"{type(exc).__name__}: {exc}"})
        print(f"Patch failed    : {type(exc).__name__}: {exc}")
        print("If this mentions SYSTEM task failure, send me this output.")
        return
    after = inspect_provider_display_patch()
    write_provider_patch_audit("apply", "completed", after, {"backup": str(backup), "result": result})
    print("Patch result:")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print()
    print_provider_patch_status_en(after, codex_home)
    print()
    print("Restart Codex Desktop after the status shows patched.")


def undo_provider_display_patch_en() -> None:
    info = inspect_provider_display_patch()
    print_provider_patch_status_en(info, load_profile_codex_home())
    audit_path = write_provider_patch_audit("undo", "started", info)
    print(f"Patch audit     : {audit_path}")
    if int(info.get("patched_occurrences") or 0) == 0:
        write_provider_patch_audit("undo", "nothing_to_undo", info)
        print("No patched marker found. Nothing to undo.")
        return
    if not info.get("writable") and not is_windows_admin():
        write_provider_patch_audit("undo", "needs_admin", info)
        print()
        print("This app.asar is protected by WindowsApps.")
        print("Please run RUN_SQLSwitchCodex.cmd as Administrator, then open Advanced menu.")
        return
    confirm = input("Type UNPATCH to undo: ").strip()
    if confirm != "UNPATCH":
        write_provider_patch_audit("undo", "cancelled_by_user", info)
        print("Cancelled.")
        return
    if not require_codex_closed_en("Provider display patch undo"):
        write_provider_patch_audit("undo", "codex_not_closed", info)
        print("Cancelled.")
        return
    app_asar = Path(str(info.get("app_asar") or ""))
    backup = create_app_file_backup(app_asar, "provider_display_unpatch")
    print(f"Backup created  : {backup}")
    try:
        result = undo_provider_display_patch_resilient(app_asar)
    except Exception as exc:
        write_provider_patch_audit("undo", "failed", info, {"error": f"{type(exc).__name__}: {exc}"})
        print(f"Undo failed     : {type(exc).__name__}: {exc}")
        return
    write_provider_patch_audit("undo", "completed", inspect_provider_display_patch(), {"backup": str(backup), "result": result})
    print("Undo result:")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def create_patched_desktop_copy_en() -> None:
    print("Create Patched Codex Desktop Copy")
    hr()
    print(f"This creates a separate copy under {ROOT / 'patched-desktop'}.")
    print("Original WindowsApps package is not modified.")
    print("Disk usage is about 1.4 GB.")
    print()
    confirm = input("Type COPY to create/update the patched copy: ").strip()
    if confirm != "COPY":
        print("Cancelled.")
        return
    try:
        result = create_patched_desktop_copy()
    except Exception as exc:
        print(f"Create failed    : {type(exc).__name__}: {exc}")
        return
    print("Patched copy ready.")
    after = result.get("after")
    if isinstance(after, dict):
        print(f"Patched status   : {after.get('status')}")
        print(f"null markers     : {after.get('unpatched_occurrences')}")
        print(f"[] markers       : {after.get('patched_occurrences')}")
    print()
    print("Use RUN_SQLSwitchCodex.cmd option 4 to launch the patched Desktop copy.")


def patched_desktop_paths() -> tuple[Path, Path]:
    app_dir = ROOT / "patched-desktop" / "app"
    return app_dir, app_dir / "resources" / "app.asar"


def patched_desktop_copy_status() -> dict[str, object]:
    _app_dir, asar = patched_desktop_paths()
    if not asar.exists():
        return {"status": "missing", "app_asar": str(asar)}
    return inspect_provider_display_patch(asar)


def launch_patched_desktop_copy_en() -> None:
    app_dir, asar = patched_desktop_paths()
    exe = app_dir / "Codex.exe"
    if not exe.exists() or not asar.exists():
        print("Patched Desktop copy is not ready yet. Run Provider switch repair first.")
        return
    status = patched_desktop_copy_status()
    if status.get("status") != "patched":
        print("Patched Desktop copy exists but is not patched. Recreate it first.")
        print_provider_patch_status_en(status, load_profile_codex_home())
        return
    if not require_codex_closed_en("Launching patched Codex Desktop"):
        print("Cancelled.")
        return
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen([str(exe)], cwd=str(app_dir), creationflags=creationflags)
    print("Patched Codex Desktop launched.")


def provider_switch_repair_en() -> None:
    print("Provider Switch Repair")
    hr()
    print("Use this after changing API/provider in CC Switch or config.toml.")
    print("Default scope: provider display patch or patched Desktop copy only.")
    print("No auth, provider config, model, sandbox, rollout, or conversation metadata is rewritten.")
    print()
    if not is_windows_admin():
        print("This repair should run from an Administrator console.")
        print("Close this window and start RUN_SQLSwitchCodex.cmd again.")
        return

    codex_home = load_profile_codex_home()
    report = build_doctor_report()
    report_path = write_tool_report("doctor", report)
    print_doctor_brief_en(report, report_path)
    sync_needed, sync_reasons = safe_sync_needed_from_report(report)
    auth_status = auth_env_status(codex_home)
    print()
    print_auth_env_status_en(auth_status)
    print()
    if sync_needed:
        print("Safe Sync need  : yes")
        for reason in sync_reasons[:8]:
            print(f"  - {reason}")
    else:
        print("Safe Sync need  : no - local DB and session_index are already aligned.")
    if auth_status.get("conflict"):
        print()
        print("Auth warning    : current provider is third-party/custom but OpenAI auth/env residue exists.")
        print("                 This can cause invalid_api_key when opening historical OpenAI-provider threads.")
        print("                 V2.5 only warns; it does not inject or delete API keys automatically.")
    if str(report.get("overall_status")) in {"APP_ERROR", "FATAL"}:
        print()
        print("Doctor found an app/database-level error. Provider switch repair is not the right first step.")
        confirm = input("Type FIX_ANYWAY to continue with display repair only: ").strip()
        if confirm != "FIX_ANYWAY":
            print("Cancelled.")
            return

    print()
    official = inspect_provider_display_patch()
    print("Official Desktop package:")
    print_provider_patch_status_en(official, codex_home)
    print()
    copied = patched_desktop_copy_status()
    print("Patched Desktop copy:")
    print_provider_patch_status_en(copied, codex_home)
    print()

    confirm = input("Type FIX to repair provider-switch sidebar display: ").strip()
    if confirm != "FIX":
        print("Cancelled.")
        return
    if not require_codex_closed_en("Provider switch repair"):
        print("Cancelled.")
        return

    official_fixed = official.get("status") == "patched"
    if official.get("status") == "needs_patch":
        app_asar = Path(str(official.get("app_asar") or ""))
        try:
            backup = create_app_file_backup(app_asar, "provider_display_patch")
            print(f"Official package backup: {backup}")
            result = apply_provider_display_patch_resilient(app_asar)
            print("Official package patch result:")
            print(json.dumps(result, ensure_ascii=True, indent=2))
            official_fixed = inspect_provider_display_patch().get("status") == "patched"
        except Exception as exc:
            print(f"Official package patch failed: {type(exc).__name__}: {exc}")
            official_fixed = False
    elif official.get("status") == "patched":
        print("Official package is already patched.")
    else:
        print(f"Official package patch status is {official.get('status')}; using copied Desktop fallback if possible.")

    if official_fixed:
        print()
        print("Provider display repair is ready in the official Desktop package.")
        if sync_needed:
            print("Safe Sync is also needed for missing local index entries.")
            safe_sync_repair_flow_en()
        else:
            print("Safe Sync was not needed. Open Codex normally.")
        return

    print()
    fallback = patched_desktop_copy_status()
    if fallback.get("status") == "patched":
        print("Patched Desktop copy is already ready.")
    else:
        print("Creating or updating patched Desktop copy fallback...")
        try:
            result = create_patched_desktop_copy()
        except Exception as exc:
            print(f"Patched Desktop copy failed: {type(exc).__name__}: {exc}")
            return
        after = result.get("after")
        if isinstance(after, dict):
            print(f"Patched copy status: {after.get('status')}")
            print(f"null markers      : {after.get('unpatched_occurrences')}")
            print(f"[] markers        : {after.get('patched_occurrences')}")
    print()
    launch = input("Type LAUNCH to start the patched Desktop copy now, or press Enter to skip: ").strip()
    if launch == "LAUNCH":
        launch_patched_desktop_copy_en()
    else:
        print("Repair complete. Use option 4 later to launch the patched Desktop copy.")
    if sync_needed:
        print()
        print("Doctor also found missing local index entries.")
        safe_sync_repair_flow_en()
    else:
        print()
        print("Safe Sync was not needed, so V2.5 skipped it.")


def safe_sync_repair_flow_en() -> None:
    safe_sync_plan_en()
    print()
    plan = latest_file(PLANS_DIR, "projection-plan-*.json")
    if not plan:
        print("No Safe Sync plan was created.")
        return
    confirm = input("Type APPLY_SAFE_SYNC to apply the latest Safe Sync plan, or press Enter to skip: ").strip()
    if confirm == "APPLY_SAFE_SYNC":
        apply_safe_sync_en(preconfirmed=True)
    else:
        print("Skipped Safe Sync apply.")


def simple_menu() -> int:
    while True:
        clear()
        print("Codex State Health Tool V2.5 - Simple Admin Menu")
        hr()
        print("Daily provider-switch flow:")
        print("  Change provider/API first, close Codex, then run option 1.")
        print()
        print("Safe boundary:")
        print("  Option 1 fixes provider display and auto-detects whether Safe Sync is needed.")
        print("  Option 2 is manual index rebuild for Doctor-confirmed missing entries.")
        print("  No option rewrites auth, provider config, model, sandbox, or rollout content by default.")
        print(f"Tool path: {ROOT}")
        print()
        print("  1. Provider switch auto repair   [recommended after changing API/provider]")
        print("  2. Manual Safe Sync index rebuild [advanced]")
        print("  3. Doctor status                 [read-only]")
        print("  4. Launch patched Desktop copy")
        print("  5. Advanced menu")
        print("  0. Exit")
        print()
        try:
            choice = input("Select: ").strip()
        except EOFError:
            return 0
        clear()
        try:
            if choice == "1":
                provider_switch_repair_en()
            elif choice == "2":
                safe_sync_repair_flow_en()
            elif choice == "3":
                verify_current_state_en()
            elif choice == "4":
                launch_patched_desktop_copy_en()
            elif choice == "5":
                english_menu()
            elif choice == "0":
                return 0
            else:
                print("Unknown option.")
        except KeyboardInterrupt:
            print("\nCancelled.")
        except Exception as exc:
            print(f"ERROR: {type(exc).__name__}: {exc}")
        pause_en()


def english_menu() -> int:
    while True:
        clear()
        print("SQLSwitchCodex V2.5 - English Safe Menu")
        hr()
        print("Goal: restore Codex Desktop left sidebar visibility after provider switching.")
        print("Main fix: make Desktop request all providers by changing modelProviders:null to modelProviders:[]  .")
        print("Safe boundary: do not rewrite conversations, rollouts, auth, provider config, model, or sandbox.")
        print(f"Tool path: {ROOT}")
        print()
        print("  1. Doctor status")
        print("  2. Apply Provider Display Patch")
        print("  3. Undo Provider Display Patch")
        print("  4. Provider Patch Status")
        print("  5. Create Patched Desktop Copy")
        print("  6. Safe Sync: check and create plan")
        print("  7. Safe Sync: apply latest plan")
        print("  0. Exit")
        print()
        try:
            choice = input("Select: ").strip()
        except EOFError:
            return 0
        clear()
        try:
            if choice == "1":
                verify_current_state_en()
            elif choice == "2":
                apply_provider_display_patch_en()
            elif choice == "3":
                undo_provider_display_patch_en()
            elif choice == "4":
                print_provider_patch_status_en(inspect_provider_display_patch(), load_profile_codex_home())
            elif choice == "5":
                create_patched_desktop_copy_en()
            elif choice == "6":
                safe_sync_plan_en()
            elif choice == "7":
                apply_safe_sync_en()
            elif choice == "0":
                return 0
            else:
                print("Unknown option.")
        except KeyboardInterrupt:
            print("\nCancelled.")
        except Exception as exc:
            print(f"ERROR: {type(exc).__name__}: {exc}")
        pause_en()


def menu() -> int:
    while True:
        clear()
        print("SQLSwitchCodex V2.5 - 左侧导航安全修复")
        hr()
        print("目标：日常两步修复左侧会话和 provider 显示；必要时再修 Project/Chats。")
        print("边界：默认不碰 global-state / 沙箱 / provider / auth / 模型 / rollout 正文。")
        print("Python 为主：cmd 只负责启动，菜单不调用 ps1。")
        print("写入前若检测到 Codex 仍在运行，会自动强制关闭。")
        print(f"项目目录：{ROOT}")
        print()
        print("  1. 指南")
        print("  2. Doctor 自动检测")
        print("  3. 检查并生成修复计划        [只读，推荐先跑]")
        print("  4. 应用日常修复              [Safe Sync + Provider 补丁]")
        print("  5. 谨慎 UI 注册表修复        [Project/Chats 归属问题]")
        print("  6. 撤销与三级极限修复")
        print("  0. 退出")
        print()
        try:
            choice = input("请选择：").strip()
        except EOFError:
            return 0
        clear()
        try:
            if choice == "1":
                show_guide()
            elif choice == "2":
                verify_current_state()
            elif choice == "3":
                check_and_plan_daily_fix()
            elif choice == "4":
                apply_daily_fix()
            elif choice == "5":
                sidebar_ui_menu()
            elif choice == "6":
                recovery_menu()
            elif choice == "0":
                return 0
            else:
                print("未知选项。")
        except KeyboardInterrupt:
            print("\n已取消。")
        except Exception as exc:
            print(f"ERROR: {exc}")
        pause()


def show_help() -> None:
    print("SQLSwitchCodex Python 启动器")
    print()
    print("进入左侧对话同步菜单：")
    print("  RUN_SQLSwitchCodex.cmd")
    print("  py SQLSwitchCodex.py")
    print("  python SQLSwitchCodex.py")
    print()
    print("日常只需要菜单。cmd 只负责启动；主流程在 SQLSwitchCodex.py 内完成。")


def show_help() -> None:
    print("Codex State Health Tool")
    print()
    print("Daily entry:")
    print("  RUN_SQLSwitchCodex.cmd")
    print("  py SQLSwitchCodex.py simple-menu")
    print("  python SQLSwitchCodex.py simple-menu")
    print()
    print("Read-only status:")
    print("  py SQLSwitchCodex.py doctor")
    print("  py SQLSwitchCodex.py status")
    print()
    print("After changing provider/API, run the daily entry and choose option 1.")


def main() -> int:
    if len(sys.argv) > 1:
        if sys.argv[1] == "--internal-provider-patch":
            if len(sys.argv) != 5:
                print("internal provider patch usage error")
                return 2
            return internal_provider_patch_child(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]))
        if sys.argv[1] in {"-h", "--help", "help"}:
            show_help()
            return 0
        if sys.argv[1] == "menu":
            return menu()
        if sys.argv[1] in {"simple-menu", "simple", "admin-menu"}:
            return simple_menu()
        if sys.argv[1] in {"english-menu", "en-menu", "en"}:
            return english_menu()
        if sys.argv[1] in {"doctor", "status", "check"}:
            verify_current_state_en()
            return 0
        if sys.argv[1] == "legacy-doctor":
            return run_cli(["doctor", *sys.argv[2:]])
        if sys.argv[1] == "provider-patch":
            apply_provider_display_patch_en()
            return 0
        if sys.argv[1] == "provider-unpatch":
            undo_provider_display_patch_en()
            return 0
        if sys.argv[1] == "desktop-copy":
            create_patched_desktop_copy_en()
            return 0
        return run_cli(sys.argv[1:])
    return simple_menu()


if __name__ == "__main__":
    raise SystemExit(main())

