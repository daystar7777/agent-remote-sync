from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path


CONSOLE_CHILD_ENV = "AGENTFTP_CONSOLE_CHILD"


def should_relaunch_in_console(
    mode: str,
    *,
    stdin_isatty: bool | None = None,
    stdout_isatty: bool | None = None,
    is_child: bool | None = None,
    system: str | None = None,
) -> bool:
    if mode == "no":
        return False
    if is_child is None:
        is_child = os.environ.get(CONSOLE_CHILD_ENV) == "1"
    if is_child:
        return False
    if system is None:
        system = platform.system().lower()
    else:
        system = system.lower()
    if system != "windows":
        return False
    if mode == "auto":
        if stdin_isatty is None:
            stdin_isatty = sys.stdin.isatty()
        if stdout_isatty is None:
            stdout_isatty = sys.stdout.isatty()
        if stdin_isatty and stdout_isatty:
            return False
    return mode in ("auto", "yes")


def relaunch_in_console_if_needed(argv: list[str], *, mode: str, cwd: Path | None = None) -> bool:
    if not should_relaunch_in_console(mode):
        return False
    env = dict(os.environ)
    env[CONSOLE_CHILD_ENV] = "1"
    command = [sys.executable, "-m", "agentftp", *argv]
    try:
        subprocess.Popen(
            command,
            cwd=str((cwd or Path.cwd()).resolve()),
            env=env,
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
    except OSError as exc:
        print(f"agentFTP could not open a new console window: {exc}")
        print("Continuing in the current process.")
        return False
    print("agentFTP opened in a new console window.")
    return True
