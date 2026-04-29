from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import AgentFTPError, clean_rel_path, resolve_path
from .connections import get_connection
from .handoff import create_handoff
from .headless import report as send_report
from .inbox import claim_instruction, list_instructions, read_instruction, update_instruction_state
from .workmem import append_event


BLOCKED_COMMAND_FRAGMENTS = (
    "rm -rf /",
    "git reset --hard",
    "shutdown",
    "reboot",
    "mkfs",
    "diskpart",
    "format ",
    "reg delete",
    "del /s",
    "rmdir /s",
    "remove-item",
    "sudo ",
)


@dataclass
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "exitCode": self.exit_code,
            "stdout": truncate(self.stdout),
            "stderr": truncate(self.stderr),
            "duration": round(self.duration, 3),
        }


def run_worker_once(
    root: Path,
    *,
    instruction_id: str = "",
    execute: str = "never",
    include_manual: bool = False,
    report_to: str = "",
    from_name: str = "agentftp-worker",
    timeout: int = 600,
) -> dict[str, Any]:
    root = root.resolve()
    manifest = select_instruction(root, instruction_id=instruction_id, include_manual=include_manual)
    if str(manifest.get("state", "received")) == "received":
        manifest = claim_instruction(root, str(manifest["id"]), claimed_by=from_name)
    plan = build_plan(root, manifest)
    manifest = update_instruction_state(
        root,
        str(manifest["id"]),
        "claimed",
        extra={"workerPlan": plan, "updatedAt": time.time()},
    )
    print_plan(plan)
    if execute == "never":
        return {"state": "claimed", "instruction": manifest, "plan": plan}
    approve_execution(execute, plan)
    if plan["blockedCommands"]:
        return finish_without_execution(root, manifest, plan, "blocked", report_to, from_name)
    if not plan["commands"]:
        return finish_without_execution(root, manifest, plan, "blocked", report_to, from_name)
    append_event(
        root,
        "HANDOFF_EXECUTION_STARTED",
        f"Instruction: {manifest['id']}\nCommands: {len(plan['commands'])}\nExecutor: {from_name}",
    )
    results = execute_commands(root, list(plan["commands"]), timeout=timeout)
    failed = [result for result in results if result.exit_code != 0]
    state = "failed" if failed else "completed"
    report_text = render_report(manifest, plan, state, results)
    report_info = deliver_report(root, manifest, report_text, report_to=report_to, from_name=from_name)
    updated = update_instruction_state(
        root,
        str(manifest["id"]),
        state,
        extra={
            "completedAt": time.time(),
            "workerResults": [result.as_dict() for result in results],
            "report": report_info,
        },
    )
    append_event(
        root,
        "HANDOFF_CLOSED",
        f"Instruction: {manifest['id']}\nState: {state}\nReport: {report_info.get('state', 'none')}",
    )
    return {
        "state": state,
        "instruction": updated,
        "plan": plan,
        "results": [result.as_dict() for result in results],
        "report": report_info,
    }


def run_worker_loop(
    root: Path,
    *,
    execute: str = "never",
    include_manual: bool = False,
    report_to: str = "",
    from_name: str = "agentftp-worker",
    timeout: int = 600,
    interval: float = 5.0,
    max_iterations: int | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    iterations = 0
    processed = 0
    idle = 0
    results: list[dict[str, Any]] = []
    try:
        while True:
            iterations += 1
            try:
                result = run_worker_once(
                    root,
                    execute=execute,
                    include_manual=include_manual,
                    report_to=report_to,
                    from_name=from_name,
                    timeout=timeout,
                )
                processed += 1
                results.append(result)
            except AgentFTPError as exc:
                if exc.code != "no_runnable_instruction":
                    raise
                idle += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
            time.sleep(max(0.1, interval))
    except KeyboardInterrupt:
        return {
            "state": "interrupted",
            "iterations": iterations,
            "processed": processed,
            "idle": idle,
            "results": results,
        }
    return {
        "state": "stopped",
        "iterations": iterations,
        "processed": processed,
        "idle": idle,
        "results": results,
    }


def select_instruction(root: Path, *, instruction_id: str = "", include_manual: bool = False) -> dict[str, Any]:
    if instruction_id:
        manifest = read_instruction(root, instruction_id)
        state = str(manifest.get("state", "received"))
        if state not in ("received", "claimed"):
            raise AgentFTPError(409, "instruction_not_runnable", f"Instruction is already {state}")
        if not manifest.get("autoRun") and not include_manual:
            raise AgentFTPError(409, "manual_instruction", "Instruction is not marked autoRun")
        return manifest
    for manifest in list_instructions(root):
        if manifest.get("state") == "received" and (manifest.get("autoRun") or include_manual):
            return manifest
    raise AgentFTPError(404, "no_runnable_instruction", "No received autoRun instruction is available")


def build_plan(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    commands = extract_commands(manifest)
    blocked = [command for command in commands if is_blocked_command(command)]
    paths = []
    for raw_path in manifest.get("paths", []):
        try:
            clean = clean_rel_path(str(raw_path))
            target = resolve_path(root, clean)
            paths.append({"path": clean, "exists": True, "type": "dir" if target.is_dir() else "file"})
        except Exception as exc:
            paths.append({"path": str(raw_path), "exists": False, "error": str(exc)})
    return {
        "instructionId": manifest.get("id", ""),
        "handoffId": manifest.get("handoffId", ""),
        "task": manifest.get("task", ""),
        "autoRun": bool(manifest.get("autoRun", False)),
        "paths": paths,
        "commands": commands,
        "blockedCommands": blocked,
        "callbackAlias": manifest.get("callbackAlias", ""),
        "expectedReport": manifest.get("expectedReport", ""),
    }


def extract_commands(manifest: dict[str, Any]) -> list[str]:
    commands = []
    raw_commands = manifest.get("commands", [])
    if isinstance(raw_commands, list):
        commands.extend(str(command).strip() for command in raw_commands if str(command).strip())
    for line in str(manifest.get("task", "")).splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("agentftp-run:"):
            command = stripped.split(":", 1)[1].strip()
            if command:
                commands.append(command)
    return commands


def is_blocked_command(command: str) -> bool:
    lower = command.lower()
    return any(fragment in lower for fragment in BLOCKED_COMMAND_FRAGMENTS)


def print_plan(plan: dict[str, Any]) -> None:
    print(f"instruction: {plan['instructionId']}")
    print(f"autoRun: {'yes' if plan['autoRun'] else 'no'}")
    print("paths:")
    for item in plan["paths"] or [{"path": "none"}]:
        status = "ok" if item.get("exists") else item.get("error", "missing")
        print(f"- {item['path']} ({status})")
    print("commands:")
    if not plan["commands"]:
        print("- none")
    for command in plan["commands"]:
        marker = " blocked" if command in plan["blockedCommands"] else ""
        print(f"- {command}{marker}")
    if plan.get("callbackAlias"):
        print(f"callback: {plan['callbackAlias']}")


def approve_execution(execute: str, plan: dict[str, Any]) -> None:
    if execute == "yes":
        return
    if execute != "ask":
        raise AgentFTPError(400, "bad_execute_mode", "execute must be never, ask, or yes")
    if not sys.stdin.isatty():
        raise AgentFTPError(409, "execution_needs_approval", "Execution approval requires an interactive terminal")
    answer = input("Run these agentftp-run commands? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        raise AgentFTPError(409, "execution_cancelled", "Worker execution was cancelled")


def execute_commands(root: Path, commands: list[str], *, timeout: int) -> list[CommandResult]:
    results = []
    for command in commands:
        if is_blocked_command(command):
            raise AgentFTPError(403, "blocked_command", f"Command is blocked by policy: {command}")
        started = time.time()
        completed = subprocess.run(
            command,
            cwd=root,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        results.append(
            CommandResult(
                command=command,
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                duration=time.time() - started,
            )
        )
    return results


def finish_without_execution(
    root: Path,
    manifest: dict[str, Any],
    plan: dict[str, Any],
    state: str,
    report_to: str,
    from_name: str,
) -> dict[str, Any]:
    reason = "No explicit agentftp-run commands were provided."
    if plan["blockedCommands"]:
        reason = "One or more commands were blocked by the worker safety policy."
    report_text = render_report(manifest, plan, state, [], note=reason)
    report_info = deliver_report(root, manifest, report_text, report_to=report_to, from_name=from_name)
    updated = update_instruction_state(
        root,
        str(manifest["id"]),
        state,
        extra={"completedAt": time.time(), "workerResults": [], "report": report_info},
    )
    append_event(
        root,
        "HANDOFF_CLOSED",
        f"Instruction: {manifest['id']}\nState: {state}\nReason: {reason}",
    )
    return {"state": state, "instruction": updated, "plan": plan, "results": [], "report": report_info}


def deliver_report(
    root: Path,
    manifest: dict[str, Any],
    report_text: str,
    *,
    report_to: str = "",
    from_name: str = "agentftp-worker",
) -> dict[str, Any]:
    alias = report_to or str(manifest.get("callbackAlias", ""))
    parent_id = str(manifest.get("handoffId") or manifest.get("id", ""))
    paths = [str(path) for path in manifest.get("paths", []) if isinstance(path, str)]
    if alias:
        saved = get_connection(alias)
        if saved and saved.get("token"):
            instruction = send_report(
                str(saved["host"]),
                int(saved["port"]),
                None,
                parent_id,
                report_text,
                token=str(saved["token"]),
                local_root=root,
                from_name=from_name,
                to_name=str(saved["name"]),
                alias=str(saved["name"]),
                paths=paths,
                tls_fingerprint=str(saved.get("tlsFingerprint", "")),
                tls_insecure=bool(saved.get("tlsInsecure", False)),
                ca_file=str(saved.get("caFile", "")),
            )
            return {"state": "sent", "target": saved["name"], "remoteInstruction": instruction["id"]}
    handoff = create_handoff(
        root,
        title=f"Report for {parent_id}",
        task=report_text,
        from_model=from_name,
        to_model=alias or "local-review",
        message_type="STATUS_REPORT",
        paths=paths,
        expected_report="no reply needed",
        parent_id=parent_id,
        direction="local",
    )
    return {
        "state": "pending" if alias else "local",
        "target": alias,
        "handoffFile": handoff["file"],
    }


def render_report(
    manifest: dict[str, Any],
    plan: dict[str, Any],
    state: str,
    results: list[CommandResult],
    *,
    note: str = "",
) -> str:
    lines = [
        f"agentFTP worker finished instruction {manifest.get('id', '')}.",
        f"State: {state}",
        f"Task: {manifest.get('task', '')}",
    ]
    if note:
        lines.append(f"Note: {note}")
    lines.append("")
    lines.append("Related paths:")
    for item in plan.get("paths", []):
        lines.append(f"- {item.get('path')} ({'exists' if item.get('exists') else item.get('error', 'missing')})")
    lines.append("")
    lines.append("Commands:")
    if not results and not plan.get("commands"):
        lines.append("- none")
    for result in results:
        lines.append(f"- `{result.command}` -> exit {result.exit_code} in {result.duration:.2f}s")
        if result.stdout:
            lines.append("  stdout:")
            lines.append(indent(truncate(result.stdout)))
        if result.stderr:
            lines.append("  stderr:")
            lines.append(indent(truncate(result.stderr)))
    if plan.get("blockedCommands"):
        lines.append("")
        lines.append("Blocked commands:")
        for command in plan["blockedCommands"]:
            lines.append(f"- `{command}`")
    return "\n".join(lines)


def truncate(text: str, limit: int = 4000) -> str:
    return text if len(text) <= limit else text[:limit] + "\n[truncated]"


def indent(text: str) -> str:
    return "\n".join("  " + line for line in text.splitlines())
