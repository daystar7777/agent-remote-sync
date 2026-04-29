from __future__ import annotations

import platform
import shutil
import subprocess
import sys

from .common import AgentFTPError


def maybe_open_firewall(port: int, mode: str = "ask") -> None:
    if mode == "no":
        return
    if mode == "ask":
        if not sys.stdin.isatty():
            print("Firewall rule not changed; run with --firewall yes to open it explicitly.")
            return
        answer = input(
            f"Open the local firewall for agentFTP TCP port {port}? "
            "Only do this on a trusted network. [y/N] "
        ).strip().lower()
        if answer not in ("y", "yes"):
            print("Firewall rule not changed.")
            return
    open_firewall_port(port)


def open_firewall_port(port: int) -> None:
    if port <= 0 or port > 65535:
        raise AgentFTPError(400, "bad_port", "Port must be between 1 and 65535")
    system = platform.system().lower()
    if system == "windows":
        run_command(
            [
                "netsh",
                "advfirewall",
                "firewall",
                "add",
                "rule",
                f"name=agentFTP {port}",
                "dir=in",
                "action=allow",
                "protocol=TCP",
                f"localport={port}",
            ]
        )
        return
    if system == "linux":
        if shutil.which("ufw"):
            run_command(["ufw", "allow", f"{port}/tcp"])
            return
        if shutil.which("firewall-cmd"):
            run_command(["firewall-cmd", "--add-port", f"{port}/tcp", "--permanent"])
            run_command(["firewall-cmd", "--reload"])
            return
    if system == "darwin":
        raise AgentFTPError(
            501,
            "firewall_manual_required",
            "macOS port firewall rules require manual pf/socketfilterfw configuration.",
        )
    raise AgentFTPError(
        501,
        "firewall_unsupported",
        "Automatic firewall rule creation is not supported on this OS.",
    )


def run_command(command: list[str]) -> None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise AgentFTPError(500, "firewall_command_failed", str(exc)) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise AgentFTPError(
            500,
            "firewall_command_failed",
            detail or f"Command failed: {' '.join(command)}",
        )

