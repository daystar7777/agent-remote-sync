from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .common import make_token


STATE_DIR_NAME = ".agentftp"
LOG_DIR_NAME = "logs"
SESSION_DIR_NAME = "sessions"
PLAN_DIR_NAME = "plans"
DEFAULT_TRANSFER_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_TRANSFER_LOG_KEEP = 5


def state_dir(root: Path) -> Path:
    path = root.resolve() / STATE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir(root: Path) -> Path:
    path = state_dir(root) / LOG_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def sessions_dir(root: Path) -> Path:
    path = state_dir(root) / SESSION_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def plans_dir(root: Path) -> Path:
    path = state_dir(root) / PLAN_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


class TransferLogger:
    def __init__(
        self,
        root: Path,
        kind: str,
        *,
        remote: str = "",
        alias: str = "",
        session_id: str = "",
        max_bytes: int = DEFAULT_TRANSFER_LOG_MAX_BYTES,
        keep: int = DEFAULT_TRANSFER_LOG_KEEP,
    ):
        self.root = root.resolve()
        self.kind = kind
        self.remote = remote
        self.alias = alias
        self.session_id = session_id or make_session_id(kind)
        self.max_bytes = max_bytes
        self.keep = keep
        self.started_at = time.time()
        self.total_files = 0
        self.total_bytes = 0
        self.done_files = 0
        self.done_bytes = 0
        self.status = "created"
        self.log_path = current_transfer_log_path(self.root)
        self.session_path = sessions_dir(self.root) / f"{self.session_id}.json"

    def start(self, *, total_files: int = 0, total_bytes: int = 0, **extra: Any) -> None:
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.status = "running"
        self.event("session_started", totalFiles=total_files, totalBytes=total_bytes, **extra)
        self.write_session(extra)

    def file_started(self, source: str, target: str, size: int, *, resume_offset: int = 0) -> None:
        self.event(
            "file_started",
            source=source,
            target=target,
            size=size,
            resumeOffset=resume_offset,
        )

    def file_completed(self, source: str, target: str, size: int) -> None:
        self.done_files += 1
        self.done_bytes += size
        self.event("file_completed", source=source, target=target, size=size)

    def complete(self, **extra: Any) -> None:
        self.status = "completed"
        self.event(
            "session_completed",
            doneFiles=self.done_files,
            doneBytes=self.done_bytes,
            duration=round(time.time() - self.started_at, 3),
            **extra,
        )
        self.write_session(extra)

    def fail(self, exc: Exception) -> None:
        self.status = "failed"
        code = getattr(exc, "code", exc.__class__.__name__)
        message = getattr(exc, "message", str(exc))
        self.event("session_failed", error=code, message=message)
        self.write_session({"error": code, "message": message})

    def event(self, event_type: str, **payload: Any) -> None:
        row = {
            "ts": time.time(),
            "session": self.session_id,
            "kind": self.kind,
            "event": event_type,
            "alias": self.alias,
            "remote": self.remote,
        }
        row.update(payload)
        write_log_row(self.root, row, max_bytes=self.max_bytes, keep=self.keep)
        self.log_path = current_transfer_log_path(self.root)

    def write_session(self, extra: dict[str, Any] | None = None) -> None:
        payload = {
            "id": self.session_id,
            "kind": self.kind,
            "status": self.status,
            "alias": self.alias,
            "remote": self.remote,
            "startedAt": self.started_at,
            "updatedAt": time.time(),
            "totalFiles": self.total_files,
            "totalBytes": self.total_bytes,
            "doneFiles": self.done_files,
            "doneBytes": self.done_bytes,
            "log": rel_state_path(self.root, current_transfer_log_path(self.root)),
        }
        if extra:
            payload.update(extra)
        self.session_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def summary(self) -> dict[str, Any]:
        return {
            "session": self.session_id,
            "log": rel_state_path(self.root, current_transfer_log_path(self.root)),
            "sessionFile": rel_state_path(self.root, self.session_path),
            "status": self.status,
            "doneFiles": self.done_files,
            "doneBytes": self.done_bytes,
            "totalFiles": self.total_files,
            "totalBytes": self.total_bytes,
        }


def make_session_id(kind: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{kind}-{stamp}-{make_token()[:8]}"


def current_transfer_log_path(root: Path) -> Path:
    day = time.strftime("%Y%m%d")
    return logs_dir(root) / f"transfer-{day}.jsonl"


def write_log_row(
    root: Path,
    row: dict[str, Any],
    *,
    max_bytes: int = DEFAULT_TRANSFER_LOG_MAX_BYTES,
    keep: int = DEFAULT_TRANSFER_LOG_KEEP,
) -> None:
    path = current_transfer_log_path(root)
    line = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
    if path.exists() and path.stat().st_size + len(line.encode("utf-8")) > max_bytes:
        rotate_log_file(path)
    with current_transfer_log_path(root).open("a", encoding="utf-8") as handle:
        handle.write(line)
    prune_transfer_logs(logs_dir(root), keep)


def rotate_log_file(path: Path) -> None:
    if not path.exists():
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for index in range(1000):
        suffix = f"{stamp}-{index}" if index else stamp
        candidate = path.with_name(f"{path.stem}-{suffix}{path.suffix}")
        if not candidate.exists():
            path.replace(candidate)
            return


def prune_transfer_logs(directory: Path, keep: int) -> None:
    if keep <= 0:
        return
    logs = sorted(
        directory.glob("transfer-*.jsonl"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for old in logs[keep:]:
        old.unlink(missing_ok=True)


def rel_state_path(root: Path, path: Path) -> str:
    try:
        rel = path.resolve().relative_to(root.resolve())
        return rel.as_posix()
    except ValueError:
        return str(path.resolve())
