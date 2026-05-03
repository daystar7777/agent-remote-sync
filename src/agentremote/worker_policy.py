from __future__ import annotations

import ctypes
import json
import os
import re
import shlex
import time
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

WORKER_POLICY_FILE = ".agentremote/worker-policy.json"
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_MAX_STDOUT_BYTES = 0
NETWORK_MODES = {"off", "on", "inherit"}
METADATA_ONLY_FIELDS = ("network", "cwdPattern", "envAllowlist")
METADATA_ONLY_NOTE = (
    "network, cwdPattern, and envAllowlist are descriptive metadata in v0.1; "
    "they do not sandbox commands or restrict network/filesystem/environment access."
)
SECRET_PATTERN = re.compile(
    r"(?i)(password|passwd|pwd|token|secret|credential|api[_-]?key|private[_-]?key|proof)"
    r"(\s*[:=]\s*)?([^\s,;&]+)?"
)


def _policy_path(root: Path) -> Path:
    return root.resolve() / WORKER_POLICY_FILE


def default_policy() -> dict[str, Any]:
    return {"version": 1, "allowlist": {}}


def load_policy(root: Path) -> dict[str, Any]:
    path = _policy_path(root)
    if not path.exists():
        return default_policy()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return default_policy()
    if not isinstance(data, dict):
        return default_policy()
    if not isinstance(data.get("allowlist", {}), dict):
        data["allowlist"] = {}
    data.setdefault("version", 1)
    return data


def save_policy(root: Path, policy: dict[str, Any]) -> None:
    path = _policy_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp.write_text(json.dumps(policy, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def init_policy(root: Path) -> dict[str, Any]:
    path = _policy_path(root)
    if path.exists():
        return load_policy(root)
    policy = default_policy()
    save_policy(root, policy)
    return policy


def allow_rule(
    root: Path,
    name: str,
    command: str,
    *,
    args_pattern: str | list[str] = "*",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_stdout_bytes: int = DEFAULT_MAX_STDOUT_BYTES,
    network: str = "off",
    shell: bool = False,
    description: str = "",
    cwd_pattern: str = "*",
    env_allowlist: list[str] | None = None,
) -> dict[str, Any]:
    policy = load_policy(root)
    clean_name = _clean_rule_name(name)
    rule = _normalize_rule(
        clean_name,
        {
            "command": command,
            "argsPattern": args_pattern,
            "timeoutSeconds": timeout_seconds,
            "maxStdoutBytes": max_stdout_bytes,
            "network": network,
            "shell": shell,
            "description": description,
            "cwdPattern": cwd_pattern,
            "envAllowlist": env_allowlist or [],
        },
    )
    policy["allowlist"][clean_name] = rule
    save_policy(root, policy)
    return rule


def remove_rule(root: Path, name: str) -> bool:
    policy = load_policy(root)
    clean_name = _clean_rule_name(name)
    if clean_name not in policy["allowlist"]:
        return False
    policy["allowlist"].pop(clean_name)
    save_policy(root, policy)
    return True


def list_rules(root: Path) -> list[dict[str, Any]]:
    policy = load_policy(root)
    return [_normalize_rule(name, rule) for name, rule in policy["allowlist"].items()]


def worker_policy_summary(root: Path) -> dict[str, Any]:
    has_policy = _policy_path(root).exists()
    rules = list_rules(root) if has_policy else []
    templates = list_templates()
    return {
        "hasPolicy": has_policy,
        "ruleCount": len(rules),
        "rules": [sanitize_policy_rule(rule) for rule in rules],
        "templates": [sanitize_policy_rule(template) for template in templates.values()],
        "metadataOnlyFields": list(METADATA_ONLY_FIELDS),
        "metadataNote": METADATA_ONLY_NOTE,
    }


def sanitize_policy_rule(rule: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key in (
        "name",
        "command",
        "argsPattern",
        "timeoutSeconds",
        "maxStdoutBytes",
        "network",
        "shell",
        "description",
        "cwdPattern",
        "envAllowlist",
    ):
        if key not in rule:
            continue
        value = rule[key]
        if isinstance(value, str):
            sanitized[key] = _sanitize_secret_text(value)
        elif isinstance(value, list):
            sanitized[key] = [_sanitize_secret_text(str(item)) for item in value]
        else:
            sanitized[key] = value
    sanitized.setdefault("name", str(rule.get("name", "")))
    return sanitized


def sanitize_policy_text(value: Any) -> str:
    return _sanitize_secret_text(str(value or ""))


def check_command(root: Path, command_line: str) -> dict[str, Any]:
    policy = load_policy(root)
    try:
        tokens = split_command_line(command_line)
    except ValueError as exc:
        return {"allowed": False, "reason": "unparseable_command", "command": command_line, "error": str(exc)}

    if not tokens:
        return {"allowed": False, "reason": "empty_command", "command": command_line}

    exe = tokens[0]
    args = tokens[1:]

    for name, raw_rule in policy["allowlist"].items():
        rule = _normalize_rule(name, raw_rule)
        if not _command_matches(exe, str(rule.get("command", ""))):
            continue
        if not _args_match(args, rule.get("argsPattern", "*")):
            continue

        return {
            "allowed": True,
            "rule": rule["name"],
            "command": command_line,
            "argv": tokens,
            "argsPattern": rule["argsPattern"],
            "timeoutSeconds": rule["timeoutSeconds"],
            "maxStdoutBytes": rule["maxStdoutBytes"],
            "network": rule["network"],
            "shell": rule["shell"],
            "description": rule.get("description", ""),
            "cwdPattern": rule.get("cwdPattern", "*"),
            "envAllowlist": rule.get("envAllowlist", []),
        }

    return {"allowed": False, "reason": "no_matching_rule", "command": command_line}


def split_command_line(command_line: str) -> list[str]:
    text = str(command_line or "").strip()
    if not text:
        return []
    if os.name == "nt":
        return _split_windows_command_line(text)
    return shlex.split(text, posix=True)


def _split_windows_command_line(command_line: str) -> list[str]:
    try:
        from ctypes import wintypes

        argc = ctypes.c_int()
        command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
        command_line_to_argv.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        command_line_to_argv.restype = ctypes.POINTER(wintypes.LPWSTR)
        argv = command_line_to_argv(command_line, ctypes.byref(argc))
        if not argv:
            raise ValueError("CommandLineToArgvW returned no argv")
        try:
            return [argv[index] for index in range(argc.value)]
        finally:
            ctypes.windll.kernel32.LocalFree(argv)
    except Exception as exc:
        try:
            return shlex.split(command_line, posix=True)
        except ValueError as shlex_exc:
            raise ValueError(str(shlex_exc)) from exc


def _normalize_rule(name: str, raw_rule: Any) -> dict[str, Any]:
    rule = raw_rule if isinstance(raw_rule, dict) else {}
    clean_name = _clean_rule_name(rule.get("name", name))
    command = _clean_command(rule.get("command", ""))
    return {
        "name": clean_name,
        "command": command,
        "argsPattern": _clean_args_pattern(rule.get("argsPattern", "*")),
        "timeoutSeconds": _positive_int(rule.get("timeoutSeconds", DEFAULT_TIMEOUT_SECONDS), DEFAULT_TIMEOUT_SECONDS),
        "maxStdoutBytes": _nonnegative_int(rule.get("maxStdoutBytes", DEFAULT_MAX_STDOUT_BYTES), DEFAULT_MAX_STDOUT_BYTES),
        "network": _normalize_network(rule.get("network", "off")),
        "shell": _normalize_bool(rule.get("shell", False)),
        "description": _clean_description(rule.get("description", "")),
        "cwdPattern": _clean_cwd_pattern(rule.get("cwdPattern", "*")),
        "envAllowlist": _clean_env_allowlist(rule.get("envAllowlist", [])),
    }


def _command_matches(exe: str, rule_cmd: str) -> bool:
    rule = str(rule_cmd or "").strip()
    if rule == "*":
        return True
    if not rule:
        return False
    exe_norm = os.path.normcase(os.path.normpath(exe))
    rule_norm = os.path.normcase(os.path.normpath(rule))
    if exe_norm == rule_norm:
        return True
    exe_base = os.path.basename(exe_norm)
    rule_base = os.path.basename(rule_norm)
    return bool(exe_base and exe_base == rule_base)


def _args_match(args: list[str], pattern: Any) -> bool:
    patterns = pattern if isinstance(pattern, list) else [pattern]
    joined = " ".join(shlex.quote(arg) for arg in args)
    plain = " ".join(args)
    for item in patterns:
        current = str(item or "*")
        if current == "*":
            return True
        if current == "none":
            if not args:
                return True
            continue
        if fnmatchcase(joined, current) or fnmatchcase(plain, current):
            return True
    return False


WORKER_POLICY_TEMPLATES = {
    "python-tests": {
        "name": "python-tests",
        "command": "python",
        "argsPattern": ["-m pytest*", "-m unittest*", "*pytest*"],
        "timeoutSeconds": 1800,
        "maxStdoutBytes": 0,
        "network": "off",
        "shell": False,
        "description": "Run Python test suites without shell expansion",
    },
    "python-compile": {
        "name": "python-compile",
        "command": "python",
        "argsPattern": "-m compileall *",
        "timeoutSeconds": 300,
        "maxStdoutBytes": 100000,
        "network": "off",
        "shell": False,
        "description": "Run Python compile checks without shell expansion",
    },
    "git-readonly": {
        "name": "git-readonly",
        "command": "git",
        "argsPattern": ["status*", "log*", "diff*", "show*"],
        "timeoutSeconds": 60,
        "maxStdoutBytes": 50000,
        "network": "off",
        "shell": False,
        "description": "Git read-only status/log/diff/show operations",
    },
    "echo-safe": {
        "name": "echo-safe",
        "command": "echo",
        "argsPattern": "*",
        "timeoutSeconds": 10,
        "maxStdoutBytes": 10000,
        "network": "off",
        "shell": True,
        "description": "Echo via shell for platforms where echo is a shell builtin",
    },
    "node-tests": {
        "name": "node-tests",
        "command": "node",
        "argsPattern": "*",
        "timeoutSeconds": 1200,
        "maxStdoutBytes": 0,
        "network": "off",
        "shell": False,
        "description": "Run Node.js test entrypoints without shell expansion",
    },
}


def apply_template(root: Path, template_name: str) -> dict[str, Any] | None:
    template = WORKER_POLICY_TEMPLATES.get(template_name)
    if not template:
        return None
    return allow_rule(
        root,
        name=str(template["name"]),
        command=str(template["command"]),
        args_pattern=template.get("argsPattern", "*"),
        timeout_seconds=int(template.get("timeoutSeconds", DEFAULT_TIMEOUT_SECONDS)),
        max_stdout_bytes=int(template.get("maxStdoutBytes", DEFAULT_MAX_STDOUT_BYTES)),
        network=str(template.get("network", "off")),
        shell=_normalize_bool(template.get("shell", False)),
        description=str(template.get("description", "")),
        cwd_pattern=str(template.get("cwdPattern", "*")),
        env_allowlist=list(template.get("envAllowlist", [])),
    )


def list_templates() -> dict[str, dict[str, Any]]:
    return {name: dict(template) for name, template in WORKER_POLICY_TEMPLATES.items()}


def _sanitize_secret_text(value: str) -> str:
    return SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=[redacted]", str(value or ""))


def _clean_rule_name(name: Any) -> str:
    cleaned = str(name or "").strip()
    if not cleaned:
        raise ValueError("rule name is required")
    if any(part in cleaned for part in ("/", "\\", "\0")):
        raise ValueError("rule name cannot contain path separators")
    return cleaned[:120]


def _clean_command(command: Any) -> str:
    cleaned = str(command or "").strip()
    if not cleaned:
        raise ValueError("command is required")
    return cleaned[:500]


def _clean_args_pattern(pattern: Any) -> str | list[str]:
    if isinstance(pattern, list):
        cleaned = [str(item or "*")[:1000] for item in pattern if str(item or "").strip()]
        return cleaned or "*"
    return str(pattern or "*")[:1000]


def _clean_description(value: Any) -> str:
    return str(value or "").strip()[:500]


def _clean_cwd_pattern(value: Any) -> str:
    return str(value or "*").strip()[:500] or "*"


def _clean_env_allowlist(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned = []
    for item in value:
        text = str(item or "").strip()
        if text and all(ch not in text for ch in ("\0", "=", "\n", "\r")):
            cleaned.append(text[:120])
    return cleaned[:100]


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _nonnegative_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _normalize_network(value: Any) -> str:
    mode = str(value or "off").strip().lower()
    return mode if mode in NETWORK_MODES else "off"


def _normalize_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
