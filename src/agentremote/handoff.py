from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from .common import AgentRemoteError, make_token
from .workmem import append_event, memory_dir, require_work_mem


VALID_TYPES = {
    "DECISION_RELAY",
    "REVIEW_REQUEST",
    "REVIEW_RESPONSE",
    "QUESTION",
    "ANSWER",
    "BLOCKER_RAISED",
    "STATUS_REPORT",
    "PROPOSAL",
}


def create_handoff(
    root: Path,
    *,
    title: str,
    task: str,
    from_model: str = "agentremote",
    to_model: str = "any-capable",
    message_type: str = "DECISION_RELAY",
    priority: str = "NORMAL",
    reply_by: str = "when convenient",
    re: str = "new topic",
    required_capability: str = "none",
    paths: list[str] | None = None,
    expected_report: str = "",
    auto_run: bool = False,
    parent_id: str = "",
    direction: str = "local",
    executor_model: str = "",
    callback_alias: str = "",
) -> dict[str, Any]:
    require_work_mem(root, prompt_install=False)
    if message_type not in VALID_TYPES:
        raise AgentRemoteError(400, "bad_handoff_type", "Unsupported AICP message type")
    handoff_id = time.strftime("%Y%m%d-%H%M%S-") + make_token()[:10]
    slug = slugify(title or task or handoff_id)
    author = slugify(from_model) or "agentremote"
    filename = f"handoff_{slug}.{author}.md"
    path = unique_path(memory_dir(root) / filename)
    content = render_handoff(
        title=title,
        task=task,
        from_model=from_model,
        to_model=to_model,
        message_type=message_type,
        priority=priority,
        reply_by=reply_by,
        re=re,
        required_capability=required_capability,
        paths=paths or [],
        expected_report=expected_report,
        auto_run=auto_run,
        parent_id=parent_id,
        direction=direction,
        handoff_id=handoff_id,
        executor_model=executor_model,
        callback_alias=callback_alias,
    )
    path.write_text(content, encoding="utf-8")
    rel = f"AIMemory/{path.name}"
    append_event(root, "FILES_CREATED", f"- {rel}")
    if direction == "external":
        append_event(
            root,
            "HANDOFF_RECEIVED",
            f"??{from_model}: {path.name}\nAcknowledged. External agent-remote-sync handoff received.",
        )
    else:
        append_event(
            root,
            "HANDOFF",
            f"??{to_model}: {one_line(task)} See {path.name}.\n"
            f"Priority: {priority}. Reply by: {reply_by}.",
        )
    return {
        "id": handoff_id,
        "file": rel,
        "filename": path.name,
        "content": content,
        "direction": direction,
        "from": from_model,
        "to": to_model,
        "type": message_type,
        "task": task,
        "paths": paths or [],
        "expectedReport": expected_report,
        "autoRun": auto_run,
        "parentId": parent_id,
        "executorModel": executor_model,
        "callbackAlias": callback_alias,
    }


def receive_handoff(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    title = str(payload.get("title") or payload.get("task") or "agent-remote-sync handoff")
    return create_handoff(
        root,
        title=title,
        task=str(payload.get("task", "")),
        from_model=str(payload.get("from", "external-agent")),
        to_model=str(payload.get("to", "any-capable")),
        message_type=str(payload.get("type", "DECISION_RELAY")),
        priority=str(payload.get("priority", "NORMAL")),
        reply_by=str(payload.get("replyBy", "when convenient")),
        re=str(payload.get("re", "agent-remote-sync external handoff")),
        required_capability=str(payload.get("requiredCapability", "none")),
        paths=list(payload.get("paths", [])) if isinstance(payload.get("paths", []), list) else [],
        expected_report=str(payload.get("expectedReport", "")),
        auto_run=bool(payload.get("autoRun", False)),
        parent_id=str(payload.get("parentId", "")),
        direction="external",
        executor_model=str(payload.get("executorModel", "agentremote-slave")),
        callback_alias=str(payload.get("callbackAlias", "")),
    )


def list_handoffs(root: Path) -> list[dict[str, Any]]:
    require_work_mem(root, prompt_install=False)
    items = []
    for path in sorted(memory_dir(root).glob("handoff_*.md"), reverse=True):
        items.append({"filename": path.name, "path": f"AIMemory/{path.name}"})
    return items


def read_handoff(root: Path, filename: str) -> str:
    require_work_mem(root, prompt_install=False)
    safe = Path(filename).name
    path = memory_dir(root) / safe
    if not path.exists() or not path.name.startswith("handoff_"):
        raise FileNotFoundError(filename)
    return path.read_text(encoding="utf-8")


def render_handoff(
    *,
    title: str,
    task: str,
    from_model: str,
    to_model: str,
    message_type: str,
    priority: str,
    reply_by: str,
    re: str,
    required_capability: str,
    paths: list[str],
    expected_report: str,
    auto_run: bool,
    parent_id: str,
    direction: str,
    handoff_id: str,
    executor_model: str,
    callback_alias: str,
) -> str:
    now = time.strftime("%Y-%m-%d %H:%M")
    path_lines = "\n".join(f"- `{path}`" for path in paths) or "- none"
    return f"""# {title}

**From**: {from_model}
**From-vendor**: OpenAI
**To**: {to_model}
**Date**: {now}
**Type**: {message_type}
**Priority**: {priority}
**Reply by**: {reply_by}
**Re**: {re}
**Required capability**: {required_capability}

## agent-remote-sync metadata

- handoffId: `{handoff_id}`
- parentId: `{parent_id or "none"}`
- direction: `{direction}`
- external: `{"yes" if direction == "external" else "no"}`
- autoRun: `{"yes" if auto_run else "no"}`
- executorModel: `{executor_model or "sender-default"}`
- callbackAlias: `{callback_alias or "none"}`
- executionRule: `remote handoffs run under the model/profile that started the slave`

## Summary

agent-remote-sync handoff for a remote agent task.

## Context

This handoff arrived through agent-remote-sync. Treat paths as relative to the receiving
slave root unless otherwise stated.

## Content

{task}

Related paths:

{path_lines}

Expected report:

{expected_report or "Report what was done, files changed, tests run, and blockers."}

## Action items

- [ ] Receiver: perform the requested task or raise a blocker.
- [ ] Receiver: create a STATUS_REPORT handoff when finished.

## Waiting on

Receiver report.
"""


def slugify(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return text[:40].strip("-") or "handoff"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise AgentRemoteError(500, "handoff_name_exhausted", "Could not create unique handoff file")


def one_line(text: str) -> str:
    collapsed = " ".join(text.split())
    return collapsed[:100] if collapsed else "agentremote handoff"
