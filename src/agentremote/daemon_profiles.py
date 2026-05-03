from __future__ import annotations

import json
import os
import string
import time
from pathlib import Path
from typing import Any

from .common import AgentRemoteError, DEFAULT_PORT
from .connections import config_home


DAEMON_PROFILE_NAME_CHARS = set(string.ascii_letters + string.digits + "-_")


def daemon_profiles_dir(*, create: bool = False) -> Path:
    path = config_home() / "daemon-profiles"
    if create:
        path.mkdir(parents=True, exist_ok=True)
        harden_daemon_profile_path(path, is_dir=True)
    return path


def normalize_daemon_profile_name(name: str) -> str:
    raw = str(name or "").strip()
    if raw.startswith("::"):
        raw = raw[2:]
    normalized = []
    previous_dash = False
    for ch in raw:
        if ch in DAEMON_PROFILE_NAME_CHARS:
            normalized.append(ch)
            previous_dash = False
        elif ch.isspace() or ch in ".:/\\":
            if not previous_dash:
                normalized.append("-")
                previous_dash = True
    value = "".join(normalized).strip("-_")
    if not value:
        raise AgentRemoteError(400, "bad_daemon_profile_name", "Daemon profile name is empty or unsafe")
    return value[:80]


def daemon_profile_path(name: str, *, create: bool = False) -> Path:
    return daemon_profiles_dir(create=create) / f"{normalize_daemon_profile_name(name)}.json"


def sanitize_daemon_profile(data: dict[str, Any]) -> dict[str, Any]:
    name = normalize_daemon_profile_name(str(data.get("name", "")))
    try:
        port = int(data.get("port", DEFAULT_PORT) or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    return {
        "name": name,
        "root": str(data.get("root", "")),
        "host": str(data.get("host", "127.0.0.1") or "127.0.0.1"),
        "port": port,
        "updatedAt": data.get("updatedAt"),
    }


def load_daemon_profiles(root: Path | None = None) -> list[dict[str, Any]]:
    profile_dir = daemon_profiles_dir(create=False)
    if not profile_dir.exists():
        return []
    root_filter = root.resolve() if root else None
    profiles: list[dict[str, Any]] = []
    for path in sorted(profile_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        try:
            profile = sanitize_daemon_profile(data)
        except AgentRemoteError:
            continue
        if root_filter is not None:
            try:
                if Path(profile.get("root", "")).resolve() != root_filter:
                    continue
            except OSError:
                continue
        profiles.append(profile)
    return profiles


def save_daemon_profile(name: str, root: Path, host: str, port: int) -> dict[str, Any]:
    safe_name = normalize_daemon_profile_name(name)
    data: dict[str, Any] = {
        "name": safe_name,
        "root": str(root.resolve()),
        "host": str(host or "127.0.0.1"),
        "port": int(port or DEFAULT_PORT),
        "updatedAt": time.time(),
    }
    path = daemon_profile_path(safe_name, create=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        harden_daemon_profile_path(tmp_path, is_dir=False)
        os.replace(tmp_path, path)
        harden_daemon_profile_path(path, is_dir=False)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return data


def remove_daemon_profile(name: str) -> bool:
    path = daemon_profile_path(name)
    if not path.exists():
        return False
    path.unlink()
    return True


def daemon_profile_runtime_status(profile: dict[str, Any], processes: list[dict[str, Any]]) -> str:
    profile_root = str(profile.get("root", ""))
    try:
        profile_port = int(profile.get("port", 0) or 0)
    except (TypeError, ValueError):
        profile_port = 0
    for process in processes:
        if process.get("role") not in ("daemon-serve", "slave"):
            continue
        try:
            same_root = Path(str(process.get("root", ""))).resolve() == Path(profile_root).resolve()
        except OSError:
            same_root = False
        try:
            same_port = int(process.get("port", 0) or 0) == profile_port
        except (TypeError, ValueError):
            same_port = False
        if same_root and same_port and process.get("status") == "running":
            return "running"
    return "not-running"


def summarize_daemon_profiles(
    profiles: list[dict[str, Any]],
    processes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    for profile in profiles:
        row = dict(profile)
        row["status"] = daemon_profile_runtime_status(profile, processes)
        rows.append(row)
    return rows


def harden_daemon_profile_path(path: Path, *, is_dir: bool) -> None:
    if os.name == "nt":
        return
    try:
        os.chmod(path, 0o700 if is_dir else 0o600)
    except OSError:
        pass
