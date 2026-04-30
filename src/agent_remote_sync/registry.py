from __future__ import annotations

import json
import os
import signal
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .common import AgentRemoteSyncError, make_token
from .connections import config_home


REGISTRY_VERSION = 1
HEARTBEAT_INTERVAL = 5.0
STALE_AFTER = 30.0
PROCESS_WAIT_SECONDS = 1.0

try:
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - Windows
    fcntl = None

try:
    import msvcrt  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - Unix
    msvcrt = None

_thread_lock = threading.RLock()


def registry_path() -> Path:
    return config_home() / "instances.json"


def registry_lock_path() -> Path:
    return config_home() / "instances.lock"


@contextmanager
def registry_file_lock():
    lock_path = registry_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock:
        with lock_path.open("a+b") as handle:
            if os.name == "nt" and msvcrt is not None:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            elif fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            else:  # pragma: no cover - unusual platform fallback
                yield


def load_registry() -> dict[str, Any]:
    with registry_file_lock():
        return load_registry_unlocked()


def load_registry_unlocked() -> dict[str, Any]:
    path = registry_path()
    if not path.exists():
        return {"version": REGISTRY_VERSION, "instances": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"version": REGISTRY_VERSION, "instances": {}}
    if not isinstance(data, dict):
        return {"version": REGISTRY_VERSION, "instances": {}}
    if not isinstance(data.get("instances"), dict):
        data["instances"] = {}
    data["version"] = REGISTRY_VERSION
    return data


def save_registry(data: dict[str, Any]) -> None:
    with registry_file_lock():
        save_registry_unlocked(data)


def save_registry_unlocked(data: dict[str, Any]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temp.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def register_instance(
    role: str,
    *,
    root: Path,
    port: int,
    url: str = "",
    alias: str = "",
    remote: str = "",
    host: str = "",
    name: str = "",
    pid: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = time.time()
    instance_id = f"{role}-{make_token()[:10]}"
    entry = {
        "id": instance_id,
        "role": role,
        "name": name or default_name(role, root, alias),
        "pid": pid or os.getpid(),
        "root": str(root.resolve()),
        "port": port,
        "url": url,
        "alias": alias,
        "remote": remote,
        "host": host,
        "startedAt": now,
        "updatedAt": now,
        "status": "running",
    }
    if extra:
        entry.update(extra)
    with registry_file_lock():
        data = load_registry_unlocked()
        data["instances"][instance_id] = entry
        save_registry_unlocked(data)
    return entry


def update_instance(instance_id: str, **updates: Any) -> dict[str, Any] | None:
    with registry_file_lock():
        data = load_registry_unlocked()
        entry = data["instances"].get(instance_id)
        if not isinstance(entry, dict):
            return None
        entry.update(updates)
        entry["updatedAt"] = time.time()
        data["instances"][instance_id] = entry
        save_registry_unlocked(data)
        return entry


def mark_instance_stopped(instance_id: str) -> None:
    update_instance(instance_id, status="stopped", stoppedAt=time.time())


def list_instances(*, include_stopped: bool = True) -> list[dict[str, Any]]:
    with registry_file_lock():
        data = load_registry_unlocked()
        changed = False
        now = time.time()
        items: list[dict[str, Any]] = []
        for key, raw in list(data["instances"].items()):
            if not isinstance(raw, dict):
                data["instances"].pop(key, None)
                changed = True
                continue
            entry = dict(raw)
            alive = process_exists(int(entry.get("pid") or 0))
            age = now - float(entry.get("updatedAt") or 0)
            if entry.get("status") in ("running", "stopping", "stale") and not alive:
                entry["status"] = "stopped"
                entry["stoppedAt"] = entry.get("stoppedAt") or now
                data["instances"][key] = dict(entry)
                changed = True
            elif entry.get("status") == "running" and age > STALE_AFTER:
                entry["status"] = "stale"
            entry["alive"] = alive
            entry["heartbeatAge"] = max(0.0, age)
            if include_stopped or entry.get("status") not in ("stopped",):
                items.append(entry)
        if changed:
            save_registry_unlocked(data)
    items.sort(key=lambda item: float(item.get("startedAt") or 0), reverse=True)
    return items


def get_instance(instance_id: str) -> dict[str, Any]:
    for item in list_instances(include_stopped=True):
        if item.get("id") == instance_id:
            return item
    raise AgentRemoteSyncError(404, "instance_not_found", "agent-remote-sync instance was not found")


def stop_instance(instance_id: str, *, confirm: bool = False) -> dict[str, Any]:
    if not confirm:
        raise AgentRemoteSyncError(400, "confirmation_required", "Stopping a process requires confirm=true")
    entry = get_instance(instance_id)
    pid = int(entry.get("pid") or 0)
    if pid == os.getpid():
        raise AgentRemoteSyncError(400, "self_stop_blocked", "The dashboard cannot stop itself from this endpoint")
    if not process_exists(pid):
        mark_instance_stopped(instance_id)
        entry["status"] = "stopped"
        entry["alive"] = False
        return entry
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        raise AgentRemoteSyncError(500, "stop_failed", f"Could not stop process {pid}: {exc}") from exc
    deadline = time.time() + PROCESS_WAIT_SECONDS
    while time.time() < deadline and process_exists(pid):
        time.sleep(0.05)
    alive = process_exists(pid)
    if alive:
        update_instance(instance_id, status="stopping", stopRequestedAt=time.time())
        entry["status"] = "stopping"
    else:
        mark_instance_stopped(instance_id)
        entry["status"] = "stopped"
    entry["alive"] = alive
    return entry


def start_heartbeat(instance_id: str, *, interval: float = HEARTBEAT_INTERVAL) -> threading.Event:
    stop_event = threading.Event()

    def loop() -> None:
        while not stop_event.wait(interval):
            update_instance(instance_id, status="running")

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return stop_event


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def default_name(role: str, root: Path, alias: str) -> str:
    project = root.resolve().name or str(root.resolve())
    if alias:
        return f"{role} {alias} - {project}"
    return f"{role} - {project}"
