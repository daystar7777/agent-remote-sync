from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def config_home() -> Path:
    override = os.environ.get("AGENTREMOTE_HOME") or os.environ.get("AGENTREMOTE_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".agentremote"


def connections_path() -> Path:
    return config_home() / "connections.json"


def load_connections() -> dict[str, Any]:
    path = connections_path()
    if not path.exists():
        return {"connections": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"connections": {}}
    if not isinstance(data, dict):
        return {"connections": {}}
    if not isinstance(data.get("connections"), dict):
        data["connections"] = {}
    return data


def save_connections(data: dict[str, Any]) -> None:
    path = connections_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _harden_secret_dir(path.parent)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        _harden_secret_file(tmp_path)
        os.replace(tmp_path, path)
        _harden_secret_file(path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def get_connection(name: str) -> dict[str, Any] | None:
    connections = load_connections()["connections"]
    return connections.get(normalize_alias(name)) or connections.get(name)


def set_connection(
    name: str,
    host: str,
    port: int,
    token: str,
    *,
    tls_fingerprint: str = "",
    tls_insecure: bool = False,
    ca_file: str = "",
    scopes: list[str] | None = None,
) -> dict[str, Any]:
    data = load_connections()
    now = time.time()
    alias = normalize_alias(name)
    current = data["connections"].get(alias, {})
    entry = {
        "name": alias,
        "rawName": strip_alias_prefix(name),
        "host": host,
        "port": port,
        "token": token,
        "tlsFingerprint": tls_fingerprint,
        "tlsInsecure": tls_insecure,
        "caFile": ca_file,
        "scopes": scopes or current.get("scopes", []),
        "createdAt": current.get("createdAt", now),
        "updatedAt": now,
    }
    data["connections"][alias] = entry
    save_connections(data)
    return entry


def remove_connection(name: str) -> bool:
    data = load_connections()
    alias = normalize_alias(name)
    existed = alias in data["connections"]
    data["connections"].pop(alias, None)
    save_connections(data)
    return existed


def iter_connections() -> list[dict[str, Any]]:
    return sorted(load_connections()["connections"].values(), key=lambda item: item["name"])


def normalize_alias(name: str) -> str:
    stripped = strip_alias_prefix(name)
    return f"::{stripped}" if stripped else "::default"


def strip_alias_prefix(name: str) -> str:
    return name[2:] if name.startswith("::") else name


def _harden_secret_dir(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _harden_secret_file(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
