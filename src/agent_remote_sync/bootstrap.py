from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.request import urlopen

from .workmem import install_work_mem, is_installed


MIN_PYTHON = (3, 10)


@dataclass
class BootstrapCheck:
    name: str
    ok: bool
    required: bool
    detail: str
    installable: bool = False


@dataclass
class BootstrapSummary:
    checks: list[BootstrapCheck]
    installed: list[str]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks if check.required)


Runner = Callable[[list[str]], int]
Prompter = Callable[[str], bool]


def run_bootstrap(
    root: Path,
    *,
    install: str = "ask",
    runner: Runner | None = None,
    prompter: Prompter | None = None,
    check_network: bool = True,
) -> BootstrapSummary:
    root = root.resolve()
    runner = runner or default_runner
    prompter = prompter or default_prompter
    installed: list[str] = []

    checks = collect_checks(root, check_network=check_network)
    for check in checks:
        if check.ok or not check.installable:
            continue
        if should_install(check, install, prompter):
            if check.name == "agent-work-mem":
                install_work_mem(root)
                installed.append(check.name)
            elif check.name == "pipx":
                if run_commands(pipx_install_commands(), runner):
                    installed.append(check.name)
            elif check.name == "git":
                if run_commands(git_install_commands(), runner):
                    installed.append(check.name)

    if installed:
        checks = collect_checks(root, check_network=check_network)
    return BootstrapSummary(checks=checks, installed=installed)


def collect_checks(root: Path, *, check_network: bool = True) -> list[BootstrapCheck]:
    checks = [
        check_python(),
        check_pip(),
        check_git(),
        check_pipx(),
        check_agent_work_mem(root),
        check_agent_runtime(),
    ]
    if check_network:
        checks.append(check_github_network())
    return checks


def check_python() -> BootstrapCheck:
    version = sys.version_info
    ok = (version.major, version.minor) >= MIN_PYTHON
    detail = f"{platform.python_version()} at {sys.executable}"
    return BootstrapCheck("python", ok, True, detail, installable=False)


def check_pip() -> BootstrapCheck:
    result = subprocess.run(
        [sys.executable, "-m", "pip", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    return BootstrapCheck(
        "pip",
        result.returncode == 0,
        True,
        (result.stdout or result.stderr).strip() or "pip is not available",
        installable=False,
    )


def check_git() -> BootstrapCheck:
    path = shutil.which("git")
    return BootstrapCheck(
        "git",
        bool(path),
        True,
        path or "git is required for git+https installs and source updates",
        installable=True,
    )


def check_pipx() -> BootstrapCheck:
    path = shutil.which("pipx")
    return BootstrapCheck(
        "pipx",
        bool(path),
        False,
        path or "pipx is recommended for isolated CLI installs",
        installable=True,
    )


def check_agent_work_mem(root: Path) -> BootstrapCheck:
    ok = is_installed(root)
    return BootstrapCheck(
        "agent-work-mem",
        ok,
        True,
        str(root.resolve() / "AIMemory") if ok else "AIMemory is required in this project",
        installable=True,
    )


def check_agent_runtime() -> BootstrapCheck:
    markers = []
    for name in ("CODEX_HOME", "CURSOR_TRACE_ID", "AIDER_MODEL", "AGENT_REMOTE_SYNC_AGENT"):
        import os

        if os.environ.get(name):
            markers.append(name)
    if markers:
        return BootstrapCheck("agent-runtime", True, False, ", ".join(markers))
    return BootstrapCheck(
        "agent-runtime",
        True,
        False,
        "no explicit agent marker detected; manual CLI use is still supported",
    )


def check_github_network() -> BootstrapCheck:
    try:
        with urlopen("https://github.com", timeout=5) as response:
            ok = 200 <= response.status < 500
    except Exception as exc:
        return BootstrapCheck(
            "github-network",
            False,
            False,
            f"GitHub reachability check failed: {exc}",
        )
    return BootstrapCheck("github-network", ok, False, "https://github.com reachable")


def should_install(check: BootstrapCheck, install: str, prompter: Prompter) -> bool:
    if install == "yes":
        return True
    if install == "no":
        return False
    return prompter(f"{check.name} is missing. Install/setup it now?")


def default_prompter(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    answer = input(f"{question} [y/N] ").strip().lower()
    return answer in ("y", "yes")


def default_runner(command: list[str]) -> int:
    print("+ " + " ".join(command))
    return subprocess.run(command, check=False).returncode


def run_commands(commands: list[list[str]], runner: Runner) -> bool:
    if not commands:
        return False
    for command in commands:
        if runner(command) != 0:
            return False
    return True


def pipx_install_commands() -> list[list[str]]:
    return [
        [sys.executable, "-m", "pip", "install", "--user", "pipx"],
        [sys.executable, "-m", "pipx", "ensurepath"],
    ]


def git_install_commands() -> list[list[str]]:
    system = platform.system().lower()
    if system == "windows":
        if shutil.which("winget"):
            return [["winget", "install", "--id", "Git.Git", "-e", "--source", "winget"]]
        if shutil.which("choco"):
            return [["choco", "install", "git", "-y"]]
    if system == "darwin":
        if shutil.which("brew"):
            return [["brew", "install", "git"]]
        return [["xcode-select", "--install"]]
    if system == "linux":
        if shutil.which("apt-get"):
            return [["sudo", "apt-get", "update"], ["sudo", "apt-get", "install", "-y", "git"]]
        if shutil.which("dnf"):
            return [["sudo", "dnf", "install", "-y", "git"]]
        if shutil.which("yum"):
            return [["sudo", "yum", "install", "-y", "git"]]
        if shutil.which("pacman"):
            return [["sudo", "pacman", "-S", "--noconfirm", "git"]]
        if shutil.which("zypper"):
            return [["sudo", "zypper", "install", "-y", "git"]]
    return []


def format_summary(summary: BootstrapSummary) -> str:
    lines = ["agent-remote-sync bootstrap"]
    for check in summary.checks:
        mark = "OK" if check.ok else ("MISSING" if check.required else "WARN")
        requirement = "required" if check.required else "optional"
        lines.append(f"- {mark:<7} {check.name} ({requirement}): {check.detail}")
    if summary.installed:
        lines.append("Installed/setup: " + ", ".join(summary.installed))
    lines.append("Ready: yes" if summary.ok else "Ready: no")
    return "\n".join(lines)

