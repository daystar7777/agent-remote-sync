from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from .common import AgentFTPError


AIMEMORY_DIR = "AIMemory"
PROTOCOL_URL = "https://raw.githubusercontent.com/daystar7777/agent-work-mem/main/PROTOCOL.md"
MODEL_ID = "agentftp"
_PROTOCOL_CACHE: str | None = None


def memory_dir(root: Path) -> Path:
    return root.resolve() / AIMEMORY_DIR


def is_installed(root: Path) -> bool:
    base = memory_dir(root)
    return all(
        (base / name).exists()
        for name in ("INDEX.md", "PROJECT_OVERVIEW.md", "PROTOCOL.md", "work.log")
    )


def require_work_mem(root: Path, *, prompt_install: bool = True) -> None:
    if is_installed(root):
        return
    if prompt_install and sys.stdin.isatty():
        answer = input(
            "agentFTP pairs with the agent in this project through "
            f"agent-work-mem AIMemory at {root.resolve() / AIMEMORY_DIR}. "
            "Install/setup it now? [y/N] "
        ).strip().lower()
        if answer in ("y", "yes"):
            install_work_mem(root)
            return
    raise AgentFTPError(
        500,
        "missing_agent_work_mem",
        "agent-work-mem AIMemory is required. Install it before running agentFTP.",
    )


def install_work_mem(root: Path) -> None:
    root = root.resolve()
    base = memory_dir(root)
    if is_installed(root):
        append_event(
            root,
            "RE_ENGAGED",
            "Vendor: OpenAI\n"
            "Harness: agentFTP\n"
            "Capabilities: filesystem-read, filesystem-write, shell-exec\n"
            "Strengths: file transfer and handoff transport\n"
            "Context: n/a\n"
            "Notes: agentFTP re-engaged with existing AIMemory.",
        )
        return
    (base / "archive").mkdir(parents=True, exist_ok=True)
    (base / "cold").mkdir(parents=True, exist_ok=True)
    protocol = fetch_protocol()
    write_if_missing(base / "PROTOCOL.md", protocol)
    write_if_missing(base / "work.log", work_log_stub())
    write_if_missing(base / "INDEX.md", index_stub())
    write_if_missing(base / "PROJECT_OVERVIEW.md", overview_stub())
    append_event(
        root,
        "PROJECT_BOOTSTRAPPED",
        "Vendor: OpenAI\n"
        "Harness: agentFTP\n"
        "Capabilities: filesystem-read, filesystem-write, shell-exec\n"
        "Strengths: file transfer and handoff transport\n"
        "Context: n/a\n"
        "Notes: agent-work-mem AIMemory installed by agentFTP.",
    )


def fetch_protocol() -> str:
    global _PROTOCOL_CACHE
    if _PROTOCOL_CACHE is not None:
        return _PROTOCOL_CACHE
    try:
        with urlopen(PROTOCOL_URL, timeout=20) as response:
            _PROTOCOL_CACHE = response.read().decode("utf-8")
            return _PROTOCOL_CACHE
    except Exception as exc:
        _PROTOCOL_CACHE = f"# agent-work-mem Protocol\n\nFetch failed: {exc}\n"
        return _PROTOCOL_CACHE


def write_if_missing(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def append_event(root: Path, event_type: str, body: str, *, model_id: str = MODEL_ID) -> None:
    require_work_mem(root, prompt_install=False)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    entry = f"\n### {stamp} | {model_id} | {event_type}\n{body.rstrip()}\n"
    with (memory_dir(root) / "work.log").open("a", encoding="utf-8") as handle:
        handle.write(entry)


def record_host_event(
    root: Path,
    alias: str,
    *,
    host: str,
    port: int,
    event_type: str,
    summary: str,
    handoff_file: str = "",
    extra: dict[str, Any] | None = None,
) -> str:
    require_work_mem(root, prompt_install=False)
    hosts_dir = memory_dir(root) / "agentftp_hosts"
    hosts_dir.mkdir(parents=True, exist_ok=True)
    safe_alias = host_slug(alias)
    path = hosts_dir / f"{safe_alias}.md"
    if not path.exists():
        path.write_text(
            f"# agentFTP Host {alias}\n\n"
            f"- alias: `{alias}`\n"
            f"- host: `{host}`\n"
            f"- port: `{port}`\n\n"
            "## Events\n\n",
            encoding="utf-8",
        )
        append_event(root, "FILES_CREATED", f"- AIMemory/agentftp_hosts/{path.name}")
    stamp = time.strftime("%Y-%m-%d %H:%M")
    extra_lines = ""
    if handoff_file:
        extra_lines += f"- handoff: `{handoff_file}`\n"
    if extra:
        for key, value in extra.items():
            extra_lines += f"- {key}: `{value}`\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            f"### {stamp} | {event_type}\n"
            f"{summary}\n\n"
            f"{extra_lines}\n"
        )
    return f"AIMemory/agentftp_hosts/{path.name}"


def host_slug(alias: str) -> str:
    text = alias[2:] if alias.startswith("::") else alias
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in text).strip("-")
    return cleaned[:60] or "default"


def work_log_stub() -> str:
    return """# AIMemory work.log
#
# Append-only event log. Newest events at the bottom.
#
# Event grammar:
#
# ### YYYY-MM-DD HH:MM | <model-id> | <EVENT_TYPE>
# <body>
#
# Events: PROMPT, WORK_START, WORK_END, FILES_CREATED, FILES_MODIFIED,
# FILES_MOVED, FILES_DELETED, HANDOFF, HANDOFF_RECEIVED,
# HANDOFF_CLOSED, NOTE, PROJECT_BOOTSTRAPPED, RE_ENGAGED, CORRECTION
#
# READ ORDER on every new turn:
# 1. AIMemory/INDEX.md
# 2. AIMemory/PROJECT_OVERVIEW.md
# 3. this file (work.log) tail
# ============================================================
"""


def index_stub() -> str:
    today = time.strftime("%Y-%m-%d %H:%M")
    return f"""# AIMemory Index

## Configuration

- HOT_RETENTION_EVENTS: 50

## Hot — Read Every Session

- work.log — current append-only event log

## Warm — Read Only When Needed

| File | Date range | Events | Topics | Summary |
|------|------------|--------|--------|---------|

## Cold — Fetch Only On Explicit Need

| File | Period covered | Topics | Summary |
|------|----------------|--------|---------|

## Topic Index — Grep Me

agentftp → work.log
handoff → work.log

## Active Handoffs

- none

## Other Notable Files

- PROJECT_OVERVIEW.md — onboarding primer
- PROTOCOL.md — collaboration and AICP rules

---

Last update: {today} by agentftp
"""


def overview_stub() -> str:
    today = time.strftime("%Y-%m-%d")
    return f"""# Project Overview

> Onboarding for new LLMs joining this project. Read this after
> AIMemory/INDEX.md and before AIMemory/work.log tail.

## What Is This Project?

This project uses agentFTP for cross-machine file transfer and handoff.

## Tech Stack

- agentFTP
- agent-work-mem AIMemory protocol

## Key Decisions Locked In

- {today}, agentftp — agent-work-mem is required for handoff records.

## Major Work Completed

- AIMemory initialized.

## Active Concerns

- Keep handoff records append-only and namespaced through AICP files.

## Where To Look

- Recent activity → AIMemory/work.log
- Topic-based history → AIMemory/INDEX.md
- Long-term history → AIMemory/cold/digest-*.md

---

Last rebuild: {today} by agentftp
Source: initial bootstrap
"""
