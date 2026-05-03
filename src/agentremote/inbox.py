from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .common import AgentRemoteError, INBOX_DIR_NAME, make_token
from .handoff import receive_handoff
from .workmem import append_event


def inbox_root(root: Path) -> Path:
    path = root.resolve() / INBOX_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_instruction(
    root: Path,
    task: str,
    *,
    from_name: str = "",
    expect_report: str = "",
    paths: list[str] | None = None,
    auto_run: bool = False,
    handoff: dict[str, Any] | None = None,
    executor_model: str = "agentremote-slave",
) -> dict[str, Any]:
    instruction_id = time.strftime("%Y%m%d-%H%M%S-") + make_token()[:10]
    folder = inbox_root(root) / instruction_id
    folder.mkdir(parents=False, exist_ok=False)
    manifest = {
        "version": 1,
        "id": instruction_id,
        "createdAt": time.time(),
        "from": from_name,
        "task": task,
        "paths": paths or [],
        "expectedReport": expect_report,
        "autoRun": auto_run,
        "state": "received",
        "handoffFile": "",
        "callbackAlias": "",
        "executorModel": executor_model,
        "executionProfile": "slave-agent-default",
    }
    if handoff:
        enriched_handoff = dict(handoff)
        enriched_handoff["executorModel"] = executor_model
        received = receive_handoff(root, enriched_handoff)
        manifest["handoffFile"] = received["file"]
        manifest["handoffId"] = received["id"]
        manifest["callbackAlias"] = str(enriched_handoff.get("callbackAlias", ""))
    (folder / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    return manifest


def list_instructions(root: Path) -> list[dict[str, Any]]:
    base = inbox_root(root)
    items: list[dict[str, Any]] = []
    for child in sorted(base.iterdir(), reverse=True):
        manifest_path = child / "manifest.json"
        if child.is_dir() and manifest_path.exists():
            try:
                items.append(json.loads(manifest_path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                items.append({"id": child.name, "state": "corrupt"})
    return items


def read_instruction(root: Path, instruction_id: str) -> dict[str, Any]:
    manifest_path = instruction_manifest_path(root, instruction_id)
    if not manifest_path.exists():
        raise AgentRemoteError(404, "instruction_not_found", f"Instruction {instruction_id} was not found")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def instruction_manifest_path(root: Path, instruction_id: str) -> Path:
    safe_id = Path(instruction_id).name
    return inbox_root(root) / safe_id / "manifest.json"


def write_instruction(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    instruction_id = str(manifest.get("id", ""))
    if not instruction_id:
        raise AgentRemoteError(400, "missing_instruction_id", "Instruction id is required")
    path = instruction_manifest_path(root, instruction_id)
    if not path.exists():
        raise AgentRemoteError(404, "instruction_not_found", f"Instruction {instruction_id} was not found")
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    return manifest


def claim_instruction(root: Path, instruction_id: str, *, claimed_by: str = "agentremote-worker") -> dict[str, Any]:
    manifest = read_instruction(root, instruction_id)
    state = str(manifest.get("state", "received"))
    if state != "received":
        raise AgentRemoteError(409, "instruction_not_claimable", f"Instruction is already {state}")
    manifest["state"] = "claimed"
    manifest["claimedAt"] = time.time()
    manifest["claimedBy"] = claimed_by
    write_instruction(root, manifest)
    append_event(
        root,
        "HANDOFF_CLAIMED",
        f"Claimed external agent-remote-sync instruction {instruction_id}.\n"
        f"Handoff: {manifest.get('handoffFile', 'none')}\n"
        f"Task: {str(manifest.get('task', ''))[:200]}",
    )
    return manifest


def update_instruction_state(
    root: Path,
    instruction_id: str,
    state: str,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = read_instruction(root, instruction_id)
    manifest["state"] = state
    manifest["updatedAt"] = time.time()
    if extra:
        manifest.update(extra)
    write_instruction(root, manifest)
    return manifest
