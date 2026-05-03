from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request

from . import __version__
from .bootstrap import format_summary, run_bootstrap
from .cleanup import cleanup_stale_partials
from .common import AgentRemoteError
from .common import DEFAULT_PORT, DEFAULT_UI_PORT, ensure_storage_available, format_bytes, make_token
from .console import relaunch_in_console_if_needed
from .connections import get_connection, iter_connections, normalize_alias, remove_connection, set_connection
from .daemon_profiles import (
    daemon_profile_runtime_status,
    load_daemon_profiles,
    normalize_daemon_profile_name,
    remove_daemon_profile,
    sanitize_daemon_profile,
    save_daemon_profile,
)
from .headless import handoff as send_handoff
from .headless import pull, push, report, tell
from .inbox import claim_instruction, list_instructions, read_instruction
from .master import RemoteClient, run_master
from .security import SecurityConfig
from .slave import SESSION_SCOPES, run_slave
from .state import SESSION_DIR_NAME, STATE_DIR_NAME
from .swarm import (
    MOBILE_TOKEN_SCOPES,
    TAILSCALE_CIDRS,
    create_mobile_pairing,
    forget_process,
    get_process,
    journal_call_record,
    journal_node_status,
    journal_policy_change,
    journal_route_probe,
    journal_routes_summary,
    journal_swarm_event,
    list_mobile_devices,
    list_process_registry,
    load_swarm_state,
    merged_route_rows,
    normalize_node_name,
    process_is_running,
    process_pid,
    process_stop_metadata_valid,
    probe_url,
    probe_route,
    register_process,
    remove_route,
    remove_tailscale_whitelist,
    remove_whitelist,
    revoke_mobile_device,
    save_route_health,
    save_swarm_state,
    select_best_route,
    set_route,
    set_tailscale_whitelist,
    set_whitelist,
    topology_nodes,
    update_process_heartbeat,
    whitelist_status,
)
from .sync import sync_plan_pull, sync_plan_push, sync_pull, sync_push, write_plan
from .tls import fetch_remote_fingerprint, format_fingerprint, is_https_endpoint, normalize_fingerprint, open_url
from .worker import run_worker_loop, run_worker_once
from .workmem import install_work_mem, is_installed, record_host_event, require_work_mem


DEFAULT_EXPECT_REPORT = "Report back when complete."


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agentremote")
    parser.add_argument("--version", action="version", version=f"agent-remote-sync {__version__} (agentremote CLI)")
    subcommands = parser.add_subparsers(dest="command", required=True)

    bootstrap = subcommands.add_parser("bootstrap", help="check and prepare prerequisites")
    bootstrap.add_argument("--root", default=".", help="project root to prepare")
    bootstrap.add_argument(
        "--install",
        choices=["ask", "yes", "no"],
        default="ask",
        help="ask/install/only-report missing installable prerequisites",
    )
    bootstrap.add_argument(
        "--no-network-check",
        action="store_true",
        help="skip GitHub reachability check",
    )

    slave = subcommands.add_parser("slave", help="run slave mode from a folder")
    slave.add_argument("--root", default=".", help="folder to expose; defaults to current folder")
    slave.add_argument("--port", type=int, default=DEFAULT_PORT, help="listen port")
    slave.add_argument("--host", default="0.0.0.0", help="listen host")
    slave.add_argument("--password", help="session password; omit to prompt")
    slave.add_argument("--password-env", default="", help="read session password from this environment variable")
    slave.add_argument("--model-id", default="agentremote-slave", help="model/profile used by this slave agent")
    slave.add_argument(
        "--firewall",
        choices=["ask", "yes", "no"],
        default="ask",
        help="ask/open/skip local firewall rule for the slave port",
    )
    slave.add_argument("--max-concurrent", type=int, default=32, help="maximum concurrent requests")
    slave.add_argument(
        "--authenticated-transfer-per-minute",
        type=int,
        default=SecurityConfig().authenticated_transfer_per_minute,
        help="per-client rate limit for authenticated transfer endpoints",
    )
    slave.add_argument("--verbose", action="store_true", help="print request logs in the slave console")
    slave.add_argument(
        "--panic-on-flood",
        action="store_true",
        help="shut down the slave after sustained overload/rate-limit events",
    )
    slave.add_argument(
        "--tls",
        choices=["off", "self-signed", "manual"],
        default="off",
        help="enable HTTPS with a self-signed or user-provided certificate",
    )
    slave.add_argument("--cert-file", default="", help="certificate PEM for --tls manual")
    slave.add_argument("--key-file", default="", help="private key PEM for --tls manual")
    slave.add_argument(
        "--console",
        choices=["auto", "yes", "no"],
        default="auto",
        help="open long-running slave mode in a visible console when possible",
    )
    slave.add_argument("--policy", choices=["warn", "strict", "off"], default="off", help="slave-side whitelist enforcement")
    slave.add_argument("--node-name", default="", help="friendly node name for topology identification")
    add_embedded_worker_args(slave)

    connect = subcommands.add_parser("connect", help="authenticate and save a connection alias")
    connect.add_argument("name", help="short name for the connection")
    connect.add_argument("host", help="slave host, IP, host:port, or URL")
    connect.add_argument("port", nargs="?", type=int, default=None, help="slave port")
    connect.add_argument("--password", help="slave password; omit to prompt")
    connect.add_argument(
        "--scopes",
        default="",
        help="comma-separated token scopes: read,write,delete,handoff; default is all",
    )
    add_tls_client_args(connect)

    subcommands.add_parser("connections", help="list saved connection aliases")

    disconnect = subcommands.add_parser("disconnect", help="remove a saved connection alias")
    disconnect.add_argument("name", help="connection alias to remove")

    master = subcommands.add_parser("master", help="run master browser UI")
    master.add_argument("host", help="slave host, IP, URL, or saved alias")
    master.add_argument("port", nargs="?", type=int, default=None, help="slave port")
    master.add_argument("--local", default=".", help="local root folder")
    master.add_argument("--password", help="slave password; omit to prompt")
    master.add_argument("--token", default="", help="bearer token for non-interactive connection")
    master.add_argument("--ui-port", type=int, default=DEFAULT_UI_PORT, help="local browser UI port")
    master.add_argument("--no-browser", action="store_true", help="print the UI URL without opening it")
    master.add_argument(
        "--console",
        choices=["auto", "yes", "no"],
        default="auto",
        help="open long-running master mode in a visible console when possible",
    )
    add_policy_arg(master)
    add_tls_client_args(master)

    push_parser = subcommands.add_parser("push", help="headless upload to a slave")
    push_parser.add_argument("host", help="slave host, IP, host:port, URL, or saved alias")
    push_parser.add_argument("local_path", help="local file or folder to upload")
    push_parser.add_argument("remote_dir", help="remote destination folder")
    push_parser.add_argument("--port", type=int, default=None, help="slave port")
    push_parser.add_argument("--password", help="slave password; omit to prompt")
    push_parser.add_argument("--overwrite", action="store_true", help="overwrite conflicts")
    add_policy_arg(push_parser)
    add_tls_client_args(push_parser)

    pull_parser = subcommands.add_parser("pull", help="headless download from a slave")
    pull_parser.add_argument("host", help="slave host, IP, host:port, URL, or saved alias")
    pull_parser.add_argument("remote_path", help="remote file or folder to download")
    pull_parser.add_argument("local_dir", help="local destination folder")
    pull_parser.add_argument("--port", type=int, default=None, help="slave port")
    pull_parser.add_argument("--password", help="slave password; omit to prompt")
    pull_parser.add_argument("--overwrite", action="store_true", help="overwrite conflicts")
    add_policy_arg(pull_parser)
    add_tls_client_args(pull_parser)

    sync = subcommands.add_parser("sync", help="plan or apply conservative folder sync")
    sync_commands = sync.add_subparsers(dest="sync_command", required=True)

    sync_plan = sync_commands.add_parser("plan", help="compare local and remote folders without changing files")
    sync_plan.add_argument("host", help="slave host, IP, host:port, URL, or saved alias")
    sync_plan.add_argument("local_path", help="local folder")
    sync_plan.add_argument("remote_dir", help="remote folder")
    sync_plan.add_argument("--direction", choices=["push", "pull"], default="push", help="which side is the source")
    sync_plan.add_argument("--compare-hash", action="store_true", help="hash same-size changed files to avoid false conflicts")
    sync_plan.add_argument("--port", type=int, default=None, help="slave port")
    sync_plan.add_argument("--password", help="slave password; omit to prompt")
    add_policy_arg(sync_plan)
    add_tls_client_args(sync_plan)

    sync_push_parser = sync_commands.add_parser("push", help="upload changed files from local folder to remote folder")
    sync_push_parser.add_argument("host", help="slave host, IP, host:port, URL, or saved alias")
    sync_push_parser.add_argument("local_path", help="local source folder")
    sync_push_parser.add_argument("remote_dir", help="remote destination folder")
    sync_push_parser.add_argument("--port", type=int, default=None, help="slave port")
    sync_push_parser.add_argument("--password", help="slave password; omit to prompt")
    sync_push_parser.add_argument("--overwrite", action="store_true", help="overwrite changed target files")
    sync_push_parser.add_argument("--delete", action="store_true", help="delete target files missing from the source after confirmation")
    sync_push_parser.add_argument("--compare-hash", action="store_true", help="hash same-size changed files to avoid false conflicts")
    add_policy_arg(sync_push_parser)
    add_tls_client_args(sync_push_parser)

    sync_pull_parser = sync_commands.add_parser("pull", help="download changed files from remote folder to local folder")
    sync_pull_parser.add_argument("host", help="slave host, IP, host:port, URL, or saved alias")
    sync_pull_parser.add_argument("remote_dir", help="remote source folder")
    sync_pull_parser.add_argument("local_path", help="local destination folder")
    sync_pull_parser.add_argument("--port", type=int, default=None, help="slave port")
    sync_pull_parser.add_argument("--password", help="slave password; omit to prompt")
    sync_pull_parser.add_argument("--overwrite", action="store_true", help="overwrite changed target files")
    sync_pull_parser.add_argument("--delete", action="store_true", help="delete target files missing from the source after confirmation")
    sync_pull_parser.add_argument("--compare-hash", action="store_true", help="hash same-size changed files to avoid false conflicts")
    add_policy_arg(sync_pull_parser)
    add_tls_client_args(sync_pull_parser)

    tell_parser = subcommands.add_parser("tell", help="send an instruction to a slave inbox")
    tell_parser.add_argument("host", help="slave host, IP, host:port, URL, or saved alias")
    tell_parser.add_argument("task", help="instruction text")
    tell_parser.add_argument("--port", type=int, default=None, help="slave port")
    tell_parser.add_argument("--password", help="slave password; omit to prompt")
    tell_parser.add_argument("--from-name", default="", help="sender name for the manifest")
    tell_parser.add_argument("--path", action="append", default=[], help="remote path related to the task")
    tell_parser.add_argument("--expect-report", default="", help="report requested from the receiver")
    tell_parser.add_argument("--auto-run", action="store_true", help="mark instruction as eligible for receiver auto mode")
    tell_parser.add_argument("--callback-alias", default="", help="receiver-side saved alias for sending a report back")
    add_policy_arg(tell_parser)
    add_tls_client_args(tell_parser)

    handoff_parser = subcommands.add_parser("handoff", help="push a file/folder and send an instruction")
    handoff_parser.add_argument("host", help="slave host, IP, host:port, URL, or saved alias")
    handoff_parser.add_argument("local_path", nargs="?", help="local file or folder to upload before sending the instruction")
    handoff_parser.add_argument("task", nargs="?", help="instruction text")
    handoff_parser.add_argument("--path", dest="handoff_path", default="", help="local file or folder to upload")
    handoff_parser.add_argument("--task", dest="task_option", default="", help="instruction text")
    handoff_parser.add_argument("--remote-dir", default="/incoming", help="remote destination folder")
    handoff_parser.add_argument("--port", type=int, default=None, help="slave port")
    handoff_parser.add_argument("--password", help="slave password; omit to prompt")
    handoff_parser.add_argument("--overwrite", action="store_true", help="overwrite remote conflicts")
    handoff_parser.add_argument("--from-name", default="", help="sender name for the manifest")
    handoff_parser.add_argument("--expect-report", default=DEFAULT_EXPECT_REPORT, help="report requested from the receiver")
    handoff_parser.add_argument("--auto-run", action="store_true", help="mark instruction as eligible for receiver auto mode")
    handoff_parser.add_argument("--callback-alias", default="", help="receiver-side saved alias for sending a report back")
    handoff_parser.add_argument("--wait-report", action="store_true", help="wait for a matching status report")
    handoff_parser.add_argument("--timeout", type=int, default=300, help="wait timeout in seconds")
    add_policy_arg(handoff_parser)
    add_tls_client_args(handoff_parser)

    report_parser = subcommands.add_parser("report", help="send a STATUS_REPORT handoff")
    report_parser.add_argument("host", help="slave host, IP, host:port, URL, or saved alias")
    report_parser.add_argument("parent_id", help="handoff id being answered")
    report_parser.add_argument("report", help="report text")
    report_parser.add_argument("--port", type=int, default=None, help="slave port")
    report_parser.add_argument("--password", help="slave password; omit to prompt")
    report_parser.add_argument("--from-name", default="", help="sender name for the manifest")
    report_parser.add_argument("--path", action="append", default=[], help="path related to the report")
    add_policy_arg(report_parser)
    add_tls_client_args(report_parser)

    inbox = subcommands.add_parser("inbox", help="list or read local received instructions")
    inbox.add_argument("--root", default=".", help="slave root containing .agentremote_inbox")
    inbox.add_argument("--read", help="instruction id to print")
    inbox.add_argument("--claim", help="instruction id to claim for local worker execution")

    worker = subcommands.add_parser("worker", help="claim and optionally execute received autoRun handoffs")
    worker.add_argument("--root", default=".", help="project/slave root containing .agentremote_inbox")
    worker.add_argument("--once", action="store_true", help="process one instruction and exit")
    worker.add_argument("--instruction-id", default="", help="specific instruction id to process")
    worker.add_argument(
        "--execute",
        choices=["never", "ask", "yes"],
        default="never",
        help="show plan only, ask before running, or run explicit agentremote-run commands",
    )
    worker.add_argument("--include-manual", action="store_true", help="allow instructions without autoRun")
    worker.add_argument("--report-to", default="", help="override callback alias for the STATUS_REPORT")
    worker.add_argument("--from-name", default="agentremote-worker", help="sender name for worker reports")
    worker.add_argument("--timeout", type=int, default=600, help="per-command timeout in seconds")
    worker.add_argument("--interval", type=float, default=5.0, help="daemon polling interval in seconds")
    worker.add_argument("--agent-command", default="", help="local bridge command for natural-language handoffs with no agentremote-run lines")
    worker.add_argument("--agent-command-shell", action="store_true", help="run --agent-command through the system shell")
    worker.add_argument(
        "--max-iterations",
        type=int,
        default=0,
        help="stop daemon after this many polling iterations; 0 means run until interrupted",
    )

    workmem = subcommands.add_parser("install-work-mem", help="install agent-work-mem AIMemory in a project")
    workmem.add_argument("--root", default=".", help="project root")

    cleanup = subcommands.add_parser("cleanup", help="remove stale partial transfer files")
    cleanup.add_argument("--root", default=".", help="project/slave root")
    cleanup.add_argument(
        "--older-than-hours",
        type=float,
        default=24.0,
        help="remove partial files older than this many hours",
    )

    onboarding_parser = subcommands.add_parser("onboarding", help="print the LLM onboarding prompt for agentremote")
    onboarding_parser.add_argument("--ko", action="store_true", help="print Korean notes after the English prompt")

    doctor_parser = subcommands.add_parser("doctor", help="diagnose install path, checkout, and registered processes")
    doctor_parser.add_argument("--root", default=".", help="project root")

    # --- New swarm/daemon/controller scaffold (Phase 0) ---

    daemon_parser = subcommands.add_parser("daemon", help="swarm daemon commands")
    daemon_sp = daemon_parser.add_subparsers(dest="daemon_command", required=True)

    daemon_serve = daemon_sp.add_parser("serve", help="run swarm daemon (slave mode)")
    daemon_serve.add_argument("--root", default=".", help="folder to expose; defaults to current folder")
    daemon_serve.add_argument("--port", type=int, default=DEFAULT_PORT, help="listen port")
    daemon_serve.add_argument("--host", default="0.0.0.0", help="listen host")
    daemon_serve.add_argument("--password", help="session password; omit to prompt")
    daemon_serve.add_argument("--password-env", default="", help="read session password from this environment variable")
    daemon_serve.add_argument("--model-id", default="agentremote-slave", help="model/profile used by this daemon agent")
    daemon_serve.add_argument("--firewall", choices=["ask", "yes", "no"], default="ask", help="ask/open/skip local firewall rule")
    daemon_serve.add_argument("--max-concurrent", type=int, default=32, help="maximum concurrent requests")
    daemon_serve.add_argument(
        "--authenticated-transfer-per-minute",
        type=int,
        default=SecurityConfig().authenticated_transfer_per_minute,
        help="per-client rate limit for authenticated transfer endpoints",
    )
    daemon_serve.add_argument("--verbose", action="store_true", help="print request logs")
    daemon_serve.add_argument("--panic-on-flood", action="store_true", help="shut down after sustained overload")
    daemon_serve.add_argument("--tls", choices=["off", "self-signed", "manual"], default="off", help="enable HTTPS")
    daemon_serve.add_argument("--cert-file", default="", help="certificate PEM for --tls manual")
    daemon_serve.add_argument("--key-file", default="", help="private key PEM for --tls manual")
    daemon_serve.add_argument("--console", choices=["auto", "yes", "no"], default="auto", help="open in visible console")
    daemon_serve.add_argument("--policy", choices=["warn", "strict", "off"], default="off", help="slave-side whitelist enforcement")
    daemon_serve.add_argument("--node-name", default="", help="friendly node name for topology identification")
    add_embedded_worker_args(daemon_serve)

    daemon_status = daemon_sp.add_parser("status", help="show local daemon status")
    daemon_status.add_argument("--root", default=".", help="project root")

    daemon_profile = daemon_sp.add_parser("profile", help="daemon profile management")
    daemon_profile_sp = daemon_profile.add_subparsers(dest="daemon_profile_command", required=True)
    dp_nested_save = daemon_profile_sp.add_parser("save", help="save a daemon profile")
    dp_nested_save.add_argument("--root", default=".", help="project root")
    dp_nested_save.add_argument("--name", default="", help="profile name (default: root folder name)")
    dp_nested_save.add_argument("--host", default="127.0.0.1")
    dp_nested_save.add_argument("--port", type=int, default=DEFAULT_PORT)
    dp_nested_list = daemon_profile_sp.add_parser("list", help="list saved daemon profiles")
    dp_nested_list.add_argument("--root", default="", help="filter by project root")
    dp_nested_remove = daemon_profile_sp.add_parser("remove", help="remove a daemon profile")
    dp_nested_remove.add_argument("--root", default=".", help="project root used for the default profile name")
    dp_nested_remove.add_argument("--name", default="", help="profile name to remove")
    dp_save = daemon_sp.add_parser("profile-save", help="save a daemon profile")
    dp_save.add_argument("--root", default=".", help="project root")
    dp_save.add_argument("--name", default="", help="profile name (default: root folder name)")
    dp_save.add_argument("--host", default="127.0.0.1")
    dp_save.add_argument("--port", type=int, default=DEFAULT_PORT)
    dp_list = daemon_sp.add_parser("profile-list", help="list saved daemon profiles")
    dp_list.add_argument("--root", default="", help="filter by project root")
    dp_remove = daemon_sp.add_parser("profile-remove", help="remove a daemon profile")
    dp_remove.add_argument("--root", default=".", help="project root")
    dp_remove.add_argument("--name", default="", help="profile name to remove")
    daemon_install = daemon_sp.add_parser("install", help="render daemon service install plan (dry-run)")
    daemon_install.add_argument("--root", default=".")
    daemon_install.add_argument("--name", default="", help="profile name; defaults to the profile matching --root")
    daemon_install.add_argument("--dry-run", action="store_true", default=True, help="render the plan without installing")
    daemon_uninstall = daemon_sp.add_parser("uninstall", help="render daemon service uninstall plan (dry-run)")
    daemon_uninstall.add_argument("--root", default=".")
    daemon_uninstall.add_argument("--name", default="", help="profile name; defaults to the profile matching --root")
    daemon_uninstall.add_argument("--dry-run", action="store_true", default=True, help="render the plan without uninstalling")

    controller_parser = subcommands.add_parser("controller", help="swarm controller commands")
    controller_sp = controller_parser.add_subparsers(dest="controller_command", required=True)

    controller_gui = controller_sp.add_parser("gui", help="open controller GUI (master browser UI)")
    controller_gui.add_argument("host", help="daemon host, IP, URL, or saved alias")
    controller_gui.add_argument("port", nargs="?", type=int, default=None, help="daemon port")
    controller_gui.add_argument("--local", default=".", help="local root folder")
    controller_gui.add_argument("--password", help="daemon password; omit to prompt")
    controller_gui.add_argument("--token", default="", help="bearer token for non-interactive connection")
    controller_gui.add_argument("--ui-port", type=int, default=DEFAULT_UI_PORT, help="local browser UI port")
    controller_gui.add_argument("--no-browser", action="store_true", help="print the UI URL without opening it")
    controller_gui.add_argument("--console", choices=["auto", "yes", "no"], default="auto", help="open in visible console")
    add_policy_arg(controller_gui)
    add_tls_client_args(controller_gui)

    controller_pair = controller_sp.add_parser("pair", help=argparse.SUPPRESS)
    controller_pair.add_argument("--local", default=".", help="local project root")
    controller_pair.add_argument("--name", required=True, help="mobile device label")
    controller_pair.add_argument("--ttl", type=int, default=600, help="pairing token lifetime in seconds")
    controller_pair.add_argument(
        "--scopes",
        default="read",
        help=f"comma-separated mobile scopes; available: {','.join(MOBILE_TOKEN_SCOPES)}",
    )
    controller_pair.add_argument("--json", action="store_true", help="print machine-readable pairing payload")

    controller_devices = controller_sp.add_parser("devices", help=argparse.SUPPRESS)
    controller_devices.add_argument("--local", default=".", help="local project root")
    controller_devices.add_argument("--json", action="store_true", help="print machine-readable device list")

    controller_revoke = controller_sp.add_parser("revoke-device", help=argparse.SUPPRESS)
    controller_revoke.add_argument("id", help="mobile device id")
    controller_revoke.add_argument("--local", default=".", help="local project root")
    controller_revoke.add_argument("--json", action="store_true", help="print machine-readable result")

    nodes_list = subcommands.add_parser("nodes", help="swarm node inventory").add_subparsers(dest="nodes_command", required=True)
    nodes_list_parser = nodes_list.add_parser("list", help="list saved connection nodes")
    nodes_status = nodes_list.add_parser("status", help="fetch node status")
    nodes_status.add_argument("node", nargs="?", default="", help="node alias; omit for --all")
    nodes_status.add_argument("--all", dest="all_nodes", action="store_true", help="status for all known nodes")
    nodes_status.add_argument("--json", action="store_true", help="output as JSON")
    nodes_status.add_argument("--timeout", type=float, default=3.0, help="node status timeout in seconds")

    topology_parser = subcommands.add_parser("topology", help="swarm topology view")
    topology_sp = topology_parser.add_subparsers(dest="topology_command", required=True)
    topology_show = topology_sp.add_parser("show", help="show topology from local project")
    topology_show.add_argument("--root", default=".", help="project root")

    policy_parser = subcommands.add_parser("policy", help="swarm policy visibility")
    policy_sp = policy_parser.add_subparsers(dest="policy_command", required=True)
    policy_sp.add_parser("list", help="list current built-in scopes and policy")
    policy_allow = policy_sp.add_parser("allow", help="allow a node in the local whitelist")
    policy_allow.add_argument("node", help="node alias or host")
    policy_allow.add_argument("--note", default="", help="optional note")
    policy_deny = policy_sp.add_parser("deny", help="deny a node in the local whitelist")
    policy_deny.add_argument("node", help="node alias or host")
    policy_deny.add_argument("--note", default="", help="optional note")
    policy_remove = policy_sp.add_parser("remove", help="remove a node from the local whitelist")
    policy_remove.add_argument("node", help="node alias or host")
    policy_tailscale = policy_sp.add_parser("allow-tailscale", help="allow Tailscale IPv4/IPv6 address ranges")
    policy_tailscale.add_argument("--note", default="", help="optional note for the Tailscale entries")
    policy_sp.add_parser("remove-tailscale", help="remove built-in Tailscale address ranges")

    route_parser = subcommands.add_parser("route", help="swarm route visibility")
    route_sp = route_parser.add_subparsers(dest="route_command", required=True)
    route_sp.add_parser("list", help="list routes from saved connections")
    route_probe = route_sp.add_parser("probe", help="probe route health")
    route_probe.add_argument("node", help="node alias")
    route_probe.add_argument("--all", dest="all_routes", action="store_true", help="probe all routes")
    route_probe.add_argument("--timeout", type=float, default=3.0, help="probe timeout in seconds")

    call_parser = subcommands.add_parser("call", help="send topology-aware handoff to a node")
    call_parser.add_argument("node", help="target node alias")
    call_parser.add_argument("task", help="instruction text")
    call_parser.add_argument("--path", help="local file or folder to upload before instruction")
    call_parser.add_argument("--remote-dir", default="/incoming", help="remote destination folder")
    call_parser.add_argument("--port", type=int, default=None, help="node port")
    call_parser.add_argument("--password", help="node password; omit to prompt")
    call_parser.add_argument("--overwrite", action="store_true", help="overwrite remote conflicts")
    call_parser.add_argument("--from-name", default="", help="sender name for the manifest")
    call_parser.add_argument("--expect-report", default="", help="report requested from the receiver")
    call_parser.add_argument("--auto-run", action="store_true", help="mark instruction as eligible for auto mode")
    call_parser.add_argument("--callback-alias", default="", help="receiver-side alias for sending a report back")
    add_policy_arg(call_parser)
    add_tls_client_args(call_parser)

    calls_parser = subcommands.add_parser("calls", help="manage swarm call records").add_subparsers(dest="calls_command", required=True)
    calls_list_parser = calls_parser.add_parser("list", help="list local call records")
    calls_list_parser.add_argument("--root", default=".", help="project root")
    calls_show = calls_parser.add_parser("show", help="show a specific call record")
    calls_show.add_argument("call_id", help="call ID to show")
    calls_show.add_argument("--root", default=".", help="project root")
    calls_refresh = calls_parser.add_parser("refresh", help="reconcile call records from inbox/AIMemory reports")
    calls_refresh.add_argument("--root", default=".", help="project root")
    calls_wait = calls_parser.add_parser("wait", help="wait for a report for a call record")
    calls_wait.add_argument("call_id", help="call ID to wait for")
    calls_wait.add_argument("--root", default=".", help="project root")
    calls_wait.add_argument("--timeout", type=int, default=300, help="wait timeout in seconds")

    processes_root = subcommands.add_parser("processes", help="swarm process management")
    processes_root.add_argument("--root", default=".", help="project root")
    processes_parser = processes_root.add_subparsers(dest="processes_command", required=False)
    processes_list_parser = processes_parser.add_parser("list", help="list registered local processes")
    processes_list_parser.add_argument("--root", default=".", help="project root")
    processes_forget = processes_parser.add_parser("forget", help="remove a process from the registry")
    processes_forget.add_argument("id", help="process registry id")
    processes_forget.add_argument("--root", default=".", help="project root")
    processes_stop = processes_parser.add_parser("stop", help="stop a registered process")
    processes_stop.add_argument("id", help="process registry id")
    processes_stop.add_argument("--root", default=".", help="project root")
    processes_stop_gui = processes_parser.add_parser("stop-gui", help="stop registered master/controller GUI processes")
    processes_stop_gui.add_argument("--root", default=".", help="project root")

    approvals_parser = subcommands.add_parser("approvals", help="approval request management").add_subparsers(dest="approvals_command", required=True)
    approvals_list_parser = approvals_parser.add_parser("list", help="list approval requests")
    approvals_list_parser.add_argument("--root", default=".", help="project root")
    approvals_list_parser.add_argument("--status", default="pending", help="filter by status")
    approvals_approve = approvals_parser.add_parser("approve", help="approve a request")
    approvals_approve.add_argument("id", help="approval id")
    approvals_approve.add_argument("--root", default=".", help="project root")
    approvals_deny = approvals_parser.add_parser("deny", help="deny a request")
    approvals_deny.add_argument("id", help="approval id")
    approvals_deny.add_argument("--root", default=".", help="project root")

    worker_policy_parser = subcommands.add_parser("worker-policy", help="worker command allowlist management").add_subparsers(dest="worker_policy_command", required=True)
    wp_list = worker_policy_parser.add_parser("list", help="list allowlist rules")
    wp_list.add_argument("--root", default=".", help="project root")
    wp_init = worker_policy_parser.add_parser("init", help="initialize worker policy file")
    wp_init.add_argument("--root", default=".", help="project root")
    wp_allow = worker_policy_parser.add_parser("allow", help="add an allowlist rule")
    wp_allow.add_argument("name", help="rule name")
    wp_allow.add_argument("allowed_command", help="allowed command path or name")
    wp_allow.add_argument("--args-pattern", default="*", help="glob pattern for arguments")
    wp_allow.add_argument("--timeout", type=int, default=600, help="max seconds")
    wp_allow.add_argument("--max-stdout", type=int, default=0, help="max stdout bytes (0=unlimited)")
    wp_allow.add_argument("--network", choices=["off", "on", "inherit"], default="off", help="network access marker for this rule")
    wp_allow.add_argument("--shell", action="store_true", help="run this rule through the system shell")
    wp_allow.add_argument("--description", default="", help="human-readable rule description")
    wp_allow.add_argument("--root", default=".", help="project root")
    wp_remove = worker_policy_parser.add_parser("remove", help="remove an allowlist rule")
    wp_remove.add_argument("name", help="rule name to remove")
    wp_remove.add_argument("--root", default=".", help="project root")
    wp_templates = worker_policy_parser.add_parser("templates", help="list available policy templates")
    wp_templates.add_argument("--root", default=".", help="project root")
    wp_apply = worker_policy_parser.add_parser("apply-template", help="apply a policy template")
    wp_apply.add_argument("template", help="template name")
    wp_apply.add_argument("--root", default=".", help="project root")

    # --- Simple command surface (v0.1 usability) ---
    setup_parser = subcommands.add_parser("setup", help="easy prerequisite setup (bootstrap alias)")
    setup_parser.add_argument("--root", default=".", help="project root")
    setup_parser.add_argument("--install", choices=["ask", "yes", "no"], default="ask", help="install missing prerequisites")
    setup_parser.add_argument("--no-network-check", action="store_true", help="skip GitHub reachability check")

    share_parser = subcommands.add_parser("share", help="share a folder (daemon/slave alias)")
    share_parser.add_argument("--root", default=".", help="folder to share")
    share_parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="listen port")
    share_parser.add_argument("--password", help="set a share password")
    share_parser.add_argument("--password-env", default="", help="read share password from this environment variable")
    share_parser.add_argument("--node-name", default="", help="friendly name")
    share_parser.add_argument("--host", default="127.0.0.1", help="listen host (default 127.0.0.1 for local-only safety)")
    share_parser.add_argument("--policy", choices=["warn", "strict", "off"], default="off")
    share_parser.add_argument("--firewall", choices=["auto", "ask", "yes", "no"], default="auto", help="firewall behavior; auto skips localhost and asks for network shares")
    share_parser.add_argument("--console", choices=["auto", "yes", "no"], default="auto", help="open in visible console")
    share_parser.add_argument(
        "--authenticated-transfer-per-minute",
        type=int,
        default=SecurityConfig().authenticated_transfer_per_minute,
        help="per-client rate limit for authenticated transfer endpoints",
    )
    share_parser.add_argument("--verbose", action="store_true")
    add_embedded_worker_args(share_parser)

    open_parser = subcommands.add_parser("open", help="open browser UI to a connection")
    open_parser.add_argument("name", help="saved connection alias")
    open_parser.add_argument("--port", type=int, default=None, help="override saved node port")
    open_parser.add_argument("--ui-port", type=int, default=DEFAULT_UI_PORT)
    open_parser.add_argument("--password", help="node password")
    open_parser.add_argument("--local", default=".", help="local root folder")
    open_parser.add_argument("--no-browser", action="store_true")
    open_parser.add_argument("--console", choices=["auto", "yes", "no"], default="auto", help="open in visible console")
    open_parser.add_argument("--policy", choices=["warn", "strict", "off"], default="warn")
    add_tls_client_args(open_parser)

    send_parser = subcommands.add_parser("send", help="send files to a node (push alias)")
    send_parser.add_argument("name", help="saved connection alias")
    send_parser.add_argument("path", help="local file or folder to send")
    send_parser.add_argument("remote_dir", nargs="?", default="/incoming", help="remote destination (default /incoming)")
    send_parser.add_argument("--port", type=int, default=None, help="override saved node port")
    send_parser.add_argument("--password", help="node password; omit to prompt when no saved token exists")
    send_parser.add_argument("--overwrite", action="store_true")
    send_parser.add_argument("--policy", choices=["warn", "strict", "off"], default="warn")
    add_tls_client_args(send_parser)

    ask_parser = subcommands.add_parser("ask", help="send an instruction to a node (tell alias with report default)")
    ask_parser.add_argument("name", help="saved connection alias")
    ask_parser.add_argument("task", help="instruction text")
    ask_parser.add_argument("--expect-report", default=DEFAULT_EXPECT_REPORT, help="expected report text")
    ask_parser.add_argument("--auto-run", action="store_true", help="mark for receiver auto mode")
    ask_parser.add_argument("--from-name", default="", help="sender name")
    ask_parser.add_argument("--path", action="append", default=[], help="remote path related to task")
    ask_parser.add_argument("--callback-alias", default="", help="receiver-side alias for report back")
    ask_parser.add_argument("--wait-report", action="store_true", help="wait for status report")
    ask_parser.add_argument("--timeout", type=int, default=300, help="wait timeout in seconds")
    ask_parser.add_argument("--port", type=int, default=None, help="node port")
    ask_parser.add_argument("--password", help="node password")
    ask_parser.add_argument("--policy", choices=["warn", "strict", "off"], default="warn")
    add_tls_client_args(ask_parser)

    sync_project = subcommands.add_parser("sync-project", help="sync current project to a node")
    sync_project.add_argument("name", help="saved connection alias")
    sync_project.add_argument("remote_dir", nargs="?", default="/project", help="remote destination")
    sync_project.add_argument("--local", default=None, help="local path override (default: current project root)")
    sync_project.add_argument("--overwrite", action="store_true")
    sync_project.add_argument("--delete", action="store_true")
    sync_project.add_argument("--yes", action="store_true", help="execute sync without plan confirmation")
    sync_project.add_argument("--dry-run", action="store_true", help="show plan only, do not execute")
    sync_project.add_argument("--include-memory", action="store_true", help="include AIMemory in sync")
    sync_project.add_argument(
        "--all-files",
        "--no-default-excludes",
        dest="all_files",
        action="store_true",
        help="disable sync-project default/profile excludes and only apply explicit --exclude rules",
    )
    sync_project.add_argument(
        "--include-volatile-memory",
        action="store_true",
        help="include local AIMemory connection/topology runtime files that are normally excluded",
    )
    sync_project.add_argument(
        "--profile",
        action="append",
        choices=sorted(SYNC_PROJECT_PROFILES),
        default=[],
        help="exclude profile to apply; default is standard; can be repeated",
    )
    sync_project.add_argument("--exclude", action="append", default=[], help="additional exclude pattern")
    sync_project.add_argument("--port", type=int, default=None, help="node port")
    sync_project.add_argument("--password", help="node password")
    sync_project.add_argument("--policy", choices=["warn", "strict", "off"], default="warn")
    add_tls_client_args(sync_project)

    map_parser = subcommands.add_parser("map", help="show topology map (nodes + status)")
    map_parser.add_argument("--root", default=".", help="project root")

    status_parser = subcommands.add_parser("status", help="quick local status summary")
    status_parser.add_argument("--root", default=".", help="project root")

    uninstall_parser = subcommands.add_parser("uninstall", help="safe uninstall and cleanup assistant")
    uninstall_parser.add_argument("--root", default=".", help="project root")
    uninstall_parser.add_argument("--project-state", action="store_true", help="show project-local .agentremote state cleanup plan")
    uninstall_parser.add_argument("--purge-memory", action="store_true", help="show AIMemory purge plan (use with caution)")
    stop_gui_parser = subcommands.add_parser("stop-gui", help="stop registered master/controller GUI processes")
    stop_gui_parser.add_argument("--root", default=".", help="project root")
    approvals_wait = approvals_parser.add_parser("wait", help="wait for an approval decision")
    approvals_wait.add_argument("id", help="approval id")
    approvals_wait.add_argument("--root", default=".", help="project root")
    approvals_wait.add_argument("--timeout", type=float, default=60.0, help="maximum seconds to wait")
    approvals_policy = approvals_parser.add_parser("policy", help="show or set project approval mode")
    approvals_policy.add_argument("--root", default=".", help="project root")
    approvals_policy.add_argument("--mode", choices=["auto", "ask", "strict", "deny"], default="", help="set approval mode")
    route_set = route_sp.add_parser("set", help="set a local route preference")
    route_set.add_argument("node", help="node alias or host")
    route_set.add_argument("host", help="route host, IP, or URL")
    route_set.add_argument("port", nargs="?", type=int, default=DEFAULT_PORT, help="route port")
    route_set.add_argument("--priority", type=int, default=100, help="lower values are preferred")
    route_set.add_argument("--tls-fingerprint", default="", help="optional route TLS fingerprint")
    route_remove = route_sp.add_parser("remove", help="remove local route preferences")
    route_remove.add_argument("node", help="node alias or host")
    route_remove.add_argument("--host", default="", help="only remove routes matching this host")
    route_remove.add_argument("--port", type=int, default=None, help="only remove routes matching this port")

    # --- end scaffold ---

    effective_argv = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(effective_argv)
    should_relaunch = args.command in ("slave", "master", "share", "open")
    if args.command == "daemon" and getattr(args, "daemon_command", "") == "serve":
        should_relaunch = True
    if args.command == "controller" and getattr(args, "controller_command", "") == "gui":
        should_relaunch = True
    if should_relaunch:
        if relaunch_in_console_if_needed(effective_argv, mode=getattr(args, "console", "auto")):
            return
    try:
        if args.command == "doctor":
            doctor(Path(args.root))
        elif args.command == "onboarding":
            print_onboarding_prompt(korean=getattr(args, "ko", False))
        elif args.command == "cleanup":
            print_json(cleanup_stale_partials(Path(args.root), older_than_hours=args.older_than_hours))
        elif args.command == "bootstrap":
            summary = run_bootstrap(
                Path(args.root),
                install=args.install,
                check_network=not args.no_network_check,
            )
            print(format_summary(summary))
        elif args.command == "install-work-mem":
            install_work_mem(Path(args.root))
            print(f"agent-work-mem installed in {Path(args.root).resolve() / 'AIMemory'}")
        elif args.command == "daemon" and daemon_command_skips_workmem(args):
            pass
        elif args.command in ("nodes", "topology", "policy", "route"):
            pass
        elif args.command in ("processes", "doctor", "stop-gui", "onboarding"):
            pass
        elif args.command in ("setup", "map", "status", "uninstall"):
            pass
        else:
            require_work_mem(command_root(args), prompt_install=True)
        if args.command == "slave":
                root = Path(args.root).resolve()
                process_record = register_process(root, "slave", os.getpid(), host=args.host, port=args.port)
                start_process_heartbeat(root, os.getpid(), process_record.get("id", ""))
                run_slave(
                    Path(args.root),
                    args.port,
                    daemon_password_arg(args),
                    args.host,
                model_id=args.model_id,
                firewall=args.firewall,
                max_concurrent=args.max_concurrent,
                authenticated_transfer_per_minute=args.authenticated_transfer_per_minute,
                panic_on_flood=args.panic_on_flood,
                tls=args.tls,
                cert_file=Path(args.cert_file) if args.cert_file else None,
                key_file=Path(args.key_file) if args.key_file else None,
                verbose=args.verbose,
                policy=args.policy,
                node_name=args.node_name,
                auto_worker=args.auto_worker,
                worker_execute=args.worker_execute,
                worker_include_manual=args.worker_include_manual,
                worker_report_to=args.worker_report_to,
                worker_from_name=args.worker_from_name,
                worker_timeout=args.worker_timeout,
                worker_interval=args.worker_interval,
                worker_agent_command=args.worker_agent_command,
                worker_agent_command_shell=args.worker_agent_command_shell,
            )
        elif args.command == "connect":
            host, port = split_host_port(args.host, args.port)
            client, tls_kwargs = connect_remote(host, port, password_arg(args.password), args)
            entry = set_connection(
                args.name,
                host,
                port,
                client.token,
                tls_fingerprint=tls_kwargs.get("tls_fingerprint", ""),
                tls_insecure=bool(tls_kwargs.get("tls_insecure", False)),
                ca_file=str(tls_kwargs.get("ca_file", "")),
                scopes=client.scopes,
            )
            record_host_event(
                Path.cwd(),
                entry["name"],
                host=host,
                port=port,
                event_type="CONNECTED",
                summary=f"Saved agent-remote-sync connection {entry['name']} -> {client.base_url}.",
            )
            print(f"connected: {entry['name']} -> {client.base_url}")
        elif args.command == "connections":
            for entry in iter_connections():
                print(f"{entry['name']}\t{entry['host']}:{entry['port']}")
        elif args.command == "disconnect":
            if remove_connection(args.name):
                print(f"removed: {normalize_alias(args.name)}")
            else:
                print(f"not found: {normalize_alias(args.name)}")
        elif args.command == "master":
            target = resolve_target(args.host, args.port, args.password, args)
            check_policy_alias(target.alias, args.policy, target.host)
            root = Path(args.local).resolve()
            process_record = register_process(root, "master", os.getpid(), ui_url=f"http://127.0.0.1:{args.ui_port}", extra={"target": target.host, "port": target.port})
            start_process_heartbeat(root, os.getpid(), process_record.get("id", ""))
            run_master(
                target.host,
                target.port,
                Path(args.local),
                target.password,
                token=target.token,
                ui_port=args.ui_port,
                open_browser=not args.no_browser,
                tls_fingerprint=target.tls_fingerprint,
                tls_insecure=target.tls_insecure,
                ca_file=target.ca_file,
                client_alias=target.alias,
            )
        elif args.command == "push":
            target = resolve_target(args.host, args.port, args.password, args)
            check_policy_alias(target.alias, args.policy, target.host)
            push(
                target.host,
                target.port,
                target.password,
                Path(args.local_path),
                args.remote_dir,
                token=target.token,
                overwrite=args.overwrite,
                alias=target.alias,
                tls_fingerprint=target.tls_fingerprint,
                tls_insecure=target.tls_insecure,
                ca_file=target.ca_file,
            )
        elif args.command == "pull":
            target = resolve_target(args.host, args.port, args.password, args)
            check_policy_alias(target.alias, args.policy, target.host)
            pull(
                target.host,
                target.port,
                target.password,
                args.remote_path,
                Path(args.local_dir),
                token=target.token,
                overwrite=args.overwrite,
                alias=target.alias,
                memory_root=Path.cwd(),
                tls_fingerprint=target.tls_fingerprint,
                tls_insecure=target.tls_insecure,
                ca_file=target.ca_file,
            )
        elif args.command == "sync":
            target = resolve_target(args.host, args.port, args.password, args)
            check_policy_alias(target.alias, args.policy, target.host)
            if args.sync_command == "plan":
                remote = RemoteClient(
                    target.host,
                    target.port,
                    target.password,
                    token=target.token,
                    tls_fingerprint=target.tls_fingerprint,
                    tls_insecure=target.tls_insecure,
                    ca_file=target.ca_file,
                    client_alias=target.alias,
                )
                if args.direction == "push":
                    plan = sync_plan_push(
                        Path.cwd(), Path(args.local_path), args.remote_dir, remote, compare_hash=args.compare_hash
                    )
                else:
                    plan = sync_plan_pull(
                        Path.cwd(), args.remote_dir, Path(args.local_path), remote, compare_hash=args.compare_hash
                    )
                write_plan(Path.cwd(), plan)
                print_json(plan)
            elif args.sync_command == "push":
                print_json(
                    sync_push(
                        target.host,
                        target.port,
                        target.password,
                        Path(args.local_path),
                        args.remote_dir,
                        token=target.token,
                        overwrite=args.overwrite,
                        delete=args.delete,
                        compare_hash=args.compare_hash,
                        alias=target.alias,
                        local_root=Path.cwd(),
                        tls_fingerprint=target.tls_fingerprint,
                        tls_insecure=target.tls_insecure,
                        ca_file=target.ca_file,
                    )
                )
            elif args.sync_command == "pull":
                print_json(
                    sync_pull(
                        target.host,
                        target.port,
                        target.password,
                        args.remote_dir,
                        Path(args.local_path),
                        token=target.token,
                        overwrite=args.overwrite,
                        delete=args.delete,
                        compare_hash=args.compare_hash,
                        alias=target.alias,
                        local_root=Path.cwd(),
                        tls_fingerprint=target.tls_fingerprint,
                        tls_insecure=target.tls_insecure,
                        ca_file=target.ca_file,
                    )
                )
        elif args.command == "tell":
            target = resolve_target(args.host, args.port, args.password, args)
            check_policy_alias(target.alias, args.policy, target.host)
            tell(
                target.host,
                target.port,
                target.password,
                args.task,
                token=target.token,
                local_root=Path.cwd(),
                from_name=args.from_name,
                to_name=args.host,
                paths=args.path,
                expect_report=args.expect_report,
                auto_run=args.auto_run,
                callback_alias=args.callback_alias,
                alias=target.alias,
                tls_fingerprint=target.tls_fingerprint,
                tls_insecure=target.tls_insecure,
                ca_file=target.ca_file,
            )
        elif args.command == "handoff":
            target = resolve_target(args.host, args.port, args.password, args)
            check_policy_alias(target.alias, args.policy, target.host)
            local_path, task = resolve_handoff_cli_args(args)
            result = send_handoff(
                target.host,
                target.port,
                target.password,
                Path(local_path),
                task,
                remote_dir=args.remote_dir,
                token=target.token,
                overwrite=args.overwrite,
                local_root=Path.cwd(),
                from_name=args.from_name,
                to_name=args.host,
                expect_report=args.expect_report,
                auto_run=args.auto_run,
                callback_alias=args.callback_alias,
                alias=target.alias,
                tls_fingerprint=target.tls_fingerprint,
                tls_insecure=target.tls_insecure,
                ca_file=target.ca_file,
            )
            transfer = result.get("transfer", {})
            instruction = result.get("instruction", {})
            remote_paths = list(transfer.get("remotePaths", []))
            call_record = save_call_record(
                target.alias,
                instruction.get("id", ""),
                instruction.get("handoffId", ""),
                remote_paths,
                "sent",
            )
            print(f"call sent: {call_record['callId']}")
            if args.wait_report and not args.callback_alias:
                print_wait_report_callback_warning()
            if args.wait_report:
                print(f"Waiting for report (timeout {args.timeout}s)...")
                result = wait_for_handoff_report(Path.cwd(), call_record["callId"], timeout=args.timeout, progress=True)
                print_wait_report_result(result)
        elif args.command == "report":
            target = resolve_target(args.host, args.port, args.password, args)
            check_policy_alias(target.alias, args.policy, target.host)
            report(
                target.host,
                target.port,
                target.password,
                args.parent_id,
                args.report,
                token=target.token,
                local_root=Path.cwd(),
                from_name=args.from_name,
                to_name=args.host,
                paths=args.path,
                alias=target.alias,
                tls_fingerprint=target.tls_fingerprint,
                tls_insecure=target.tls_insecure,
                ca_file=target.ca_file,
            )
        elif args.command == "inbox":
            root = Path(args.root)
            if args.claim:
                print_json(claim_instruction(root, args.claim))
            elif args.read:
                print_json(read_instruction(root, args.read))
            else:
                for item in list_instructions(root):
                    print(f"{item.get('id')}\t{item.get('state')}\t{item.get('task', '')[:80]}")
        elif args.command == "worker":
            if not args.once and not args.instruction_id:
                root = Path(args.root).resolve()
                process_record = register_process(root, "worker", os.getpid(), extra={"execute": args.execute, "interval": args.interval, "maxIterations": args.max_iterations})
                start_process_heartbeat(root, os.getpid(), process_record.get("id", ""))
                print_json(
                    run_worker_loop(
                        Path(args.root),
                        execute=args.execute,
                        include_manual=args.include_manual,
                        report_to=args.report_to,
                        from_name=args.from_name,
                        timeout=args.timeout,
                        interval=args.interval,
                        max_iterations=args.max_iterations or None,
                        agent_command=args.agent_command,
                        agent_command_shell=args.agent_command_shell,
                    )
                )
            else:
                print_json(
                    run_worker_once(
                        Path(args.root),
                        instruction_id=args.instruction_id,
                        execute=args.execute,
                        include_manual=args.include_manual,
                        report_to=args.report_to,
                        from_name=args.from_name,
                        timeout=args.timeout,
                        agent_command=args.agent_command,
                        agent_command_shell=args.agent_command_shell,
                    )
                )
        elif args.command == "daemon":
            if args.daemon_command == "serve":
                root = Path(args.root).resolve()
                process_record = register_process(root, "daemon-serve", os.getpid(), host=args.host, port=args.port)
                start_process_heartbeat(root, os.getpid(), process_record.get("id", ""))
                run_slave(
                    Path(args.root),
                    args.port,
                    daemon_password_arg(args),
                    args.host,
                    model_id=args.model_id,
                    firewall=args.firewall,
                    max_concurrent=args.max_concurrent,
                    authenticated_transfer_per_minute=args.authenticated_transfer_per_minute,
                    panic_on_flood=args.panic_on_flood,
                    tls=args.tls,
                    cert_file=Path(args.cert_file) if args.cert_file else None,
                    key_file=Path(args.key_file) if args.key_file else None,
                    verbose=args.verbose,
                    policy=args.policy,
                    node_name=args.node_name,
                    auto_worker=args.auto_worker,
                    worker_execute=args.worker_execute,
                    worker_include_manual=args.worker_include_manual,
                    worker_report_to=args.worker_report_to,
                    worker_from_name=args.worker_from_name,
                    worker_timeout=args.worker_timeout,
                    worker_interval=args.worker_interval,
                    worker_agent_command=args.worker_agent_command,
                    worker_agent_command_shell=args.worker_agent_command_shell,
                )
            elif args.daemon_command == "status":
                root = Path(args.root).resolve()
                aimemory_ok = is_installed(root)
                print(f"Daemon Status")
                print(f"=============")
                print(f"Root: {root}")
                print(f"AIMemory: {'installed' if aimemory_ok else 'missing'}")
                connections = iter_connections()
                print(f"Saved connections: {len(connections)}")
                profiles = load_daemon_profiles(root=root)
                all_profiles = load_daemon_profiles()
                profile_suffix = "" if len(all_profiles) == len(profiles) else f" ({len(all_profiles)} total)"
                print(f"Profiles: {len(profiles)} for this root{profile_suffix}")
                processes = list_process_registry(root)
                running = [p for p in processes if p.get("status") == "running"]
                print(f"Processes: {len(running)} running, {len(processes)} total")
                for profile in profiles:
                    status = daemon_profile_runtime_status(profile, processes)
                    print(f"  - {profile['name']} {profile['host']}:{profile['port']} {status}")
            elif args.daemon_command == "profile" and args.daemon_profile_command == "save":
                root = Path(args.root).resolve()
                profile = save_daemon_profile(args.name or root.name, root, args.host, args.port)
                print(f"saved: {profile['name']} -> {profile['host']}:{profile['port']}")
            elif args.daemon_command == "profile-save":
                root = Path(args.root).resolve()
                profile = save_daemon_profile(args.name or root.name, root, args.host, args.port)
                print(f"saved: {profile['name']} -> {profile['host']}:{profile['port']}")
            elif args.daemon_command == "profile" and args.daemon_profile_command == "list":
                root_filter = Path(args.root).resolve() if args.root else None
                profiles = load_daemon_profiles(root=root_filter)
                print_daemon_profiles(profiles)
            elif args.daemon_command == "profile-list":
                root_filter = Path(args.root).resolve() if args.root else None
                profiles = load_daemon_profiles(root=root_filter)
                print_daemon_profiles(profiles)
            elif args.daemon_command == "profile" and args.daemon_profile_command == "remove":
                root = Path(args.root).resolve()
                name = args.name or root.name
                ok = remove_daemon_profile(name)
                normalized = normalize_daemon_profile_name(name)
                print(f"removed: {normalized}" if ok else f"not found: {normalized}")
            elif args.daemon_command == "profile-remove":
                root = Path(args.root).resolve()
                name = args.name or root.name
                ok = remove_daemon_profile(name)
                normalized = normalize_daemon_profile_name(name)
                print(f"removed: {normalized}" if ok else f"not found: {normalized}")
            elif args.daemon_command == "install":
                root = Path(args.root).resolve()
                profile = select_daemon_profile(root, args.name)
                if not profile:
                    print("No profile found for this root. Use 'agentremote daemon profile save --root .' first.")
                else:
                    spec = render_service_spec(profile)
                    print(f"Service install dry-run for: {profile['name']}")
                    print(f"Platform: {platform.system()}")
                    print()
                    print(spec)
                    print()
                    print("Dry-run only. Review the spec, configure AGENTREMOTE_DAEMON_PASSWORD securely, then install with OS tools.")
            elif args.daemon_command == "uninstall":
                root = Path(args.root).resolve()
                profile = select_daemon_profile(root, args.name)
                name = profile["name"] if profile else normalize_daemon_profile_name(args.name or root.name)
                print(f"Service uninstall dry-run for: {name}")
                print(f"Root: {root}")
                print("Would stop and disable the matching user service if installed.")
                print("Dry-run only. Use OS-specific tools to stop/disable/remove the rendered service.")
        elif args.command == "controller":
            if args.controller_command == "gui":
                target = resolve_target(args.host, args.port, args.password, args)
                check_policy_alias(target.alias, args.policy, target.host)
                root = Path(args.local).resolve()
                process_record = register_process(root, "controller-gui", os.getpid(), ui_url=f"http://127.0.0.1:{args.ui_port}", extra={"target": target.host})
                start_process_heartbeat(root, os.getpid(), process_record.get("id", ""))
                run_master(
                    target.host,
                    target.port,
                    Path(args.local),
                    target.password,
                    token=target.token,
                    ui_port=args.ui_port,
                    open_browser=not args.no_browser,
                    tls_fingerprint=target.tls_fingerprint,
                    tls_insecure=target.tls_insecure,
                    ca_file=target.ca_file,
                    client_alias=target.alias,
                )
            elif args.controller_command == "pair":
                result = create_mobile_pairing(
                    Path(args.local),
                    args.name,
                    ttl=args.ttl,
                    scopes=args.scopes,
                )
                if args.json:
                    print_json(result)
                else:
                    device = result["device"]
                    print(f"paired: {device['id']}")
                    print(f"name: {device['name']}")
                    print(f"scopes: {','.join(device['scopes'])}")
                    print(f"expiresAt: {device['expiresAt']}")
                    print("pairing payload:")
                    print(result["payloadText"])
            elif args.controller_command == "devices":
                devices = list_mobile_devices(Path(args.local))
                if args.json:
                    print_json({"devices": devices})
                elif not devices:
                    print("No paired mobile devices.")
                else:
                    for device in devices:
                        print(
                            f"{device['id']}\t{device['name']}\t"
                            f"scopes={','.join(device.get('scopes', []))}\t"
                            f"revoked={device.get('revoked', False)}"
                        )
            elif args.controller_command == "revoke-device":
                ok = revoke_mobile_device(Path(args.local), args.id)
                print(f"revoked: {args.id}" if ok else f"not found: {args.id}")
        elif args.command == "mobile-api":
            root = Path(args.local).resolve()
            if args.mobile_api_command == "status":
                from .swarm import get_mobile_controller_data
                print_json(get_mobile_controller_data(root))
        elif args.command == "nodes":
            if args.nodes_command == "list":
                connections = iter_connections()
                if not connections:
                    print("No saved connection nodes.")
                    print("Use 'agentremote connect <name> <host> <port>' to add one.")
                else:
                    for entry in connections:
                        tls_info = ""
                        if entry.get("tlsFingerprint"):
                            tls_info = " tls"
                        scopes = entry.get("scopes", [])
                        scope_info = f" [{','.join(scopes)}]" if scopes else ""
                        print(f"{entry['name']}\t{entry['host']}:{entry['port']}{tls_info}{scope_info}")
            elif args.nodes_command == "status":
                _nodes_status(args)
        elif args.command == "topology":
            if args.topology_command == "show":
                root = Path(args.root).resolve()
                state = load_swarm_state()
                print("Topology")
                print("========")
                print(f"local-controller: {root}")
                nodes = topology_nodes(state)
                routes = merged_route_rows(state)
                if not nodes:
                    print("  (no remote nodes)")
                else:
                    for node in nodes:
                        ws = whitelist_status(state, node)
                        marker = ""
                        if ws == "denied":
                            marker = " [blocked: denied]"
                        elif ws == "unlisted":
                            marker = " [unlisted]"
                        node_record = state.get("nodes", {}).get(node, {})
                        node_meta = ""
                        if node_record:
                            status = node_record.get("lastStatus", "unknown")
                            model = node_record.get("modelId", "")
                            storage = node_record.get("storage", {})
                            storage_note = ""
                            if isinstance(storage, dict) and storage.get("freeBytes") is not None:
                                storage_note = f" free={storage.get('freeBytes')}"
                            model_note = f" model={model}" if model else ""
                            node_meta = f" status={status}{model_note}{storage_note}"
                        print(f"  -> {node} {ws}{marker}{node_meta}")
                        node_routes = [row for row in routes if row.get("name") == node]
                        if not node_routes:
                            print("     route none")
                        selected_route = select_best_route(node_routes)
                        for route in node_routes:
                            tls_mark = " tls" if route.get("tlsFingerprint") else ""
                            source = route.get("source", "explicit")
                            selected = ""
                            if selected_route and route.get("host") == selected_route.get("host") and int(route.get("port", 0)) == int(selected_route.get("port", 0)):
                                selected = " [selected]"
                            health = route_health_summary(route)
                            print(
                                "     "
                                f"route {route.get('routeType', 'direct')} "
                                f"{route['host']}:{route['port']} "
                                f"priority={int(route.get('priority', 100))} "
                                f"{source}{tls_mark}{selected}{health}"
                            )
        elif args.command == "policy":
            if args.policy_command == "list":
                state = load_swarm_state()
                print("Policy")
                print("======")
                print("Built-in session scopes:")
                for scope in sorted(SESSION_SCOPES):
                    print(f"  - {scope}")
                print()
                config = SecurityConfig()
                print("Security defaults:")
                print(f"  max-concurrent-requests: {config.max_concurrent_requests}")
                print(f"  unauthenticated-per-minute: {config.unauthenticated_per_minute}")
                print(f"  authenticated-per-minute: {config.authenticated_per_minute}")
                print(f"  authenticated-transfer-per-minute: {config.authenticated_transfer_per_minute}")
                print(f"  login-failures-per-minute: {config.login_failures_per_minute}")
                print(f"  login-block-seconds: {config.login_block_seconds}")
                print()
                print("Node whitelist:")
                whitelist = state.get("whitelist", {})
                if not whitelist:
                    print("  (empty)")
                for entry in sorted(whitelist.values(), key=lambda item: item.get("name", "")):
                    status = "allowed" if entry.get("allowed") else "denied"
                    kind = f" kind={entry.get('kind')}" if entry.get("kind") == "cidr" else ""
                    note = f" note={entry.get('note')}" if entry.get("note") else ""
                    print(f"  - {entry.get('name')} {status}{kind}{note}")
                print()
                print("Note: whitelist enforcement is available with --policy warn|strict|off.")
                print("      The default remains off for compatibility with existing connections.")
                print(f"      Tailscale helper ranges: {', '.join(TAILSCALE_CIDRS)}")
            elif args.policy_command in ("allow", "deny"):
                from .approval import require_approval
                require_approval(
                    Path.cwd(),
                    f"policy.{args.policy_command}",
                    risk="high",
                    origin_type="controller",
                    summary=f"{args.policy_command} whitelist node {args.node}",
                    details=f"node={args.node} note={args.note}",
                    timeout=300,
                    poll_interval=0.25,
                )
                entry = set_whitelist(
                    args.node,
                    args.policy_command == "allow",
                    note=args.note,
                )
                status = "allowed" if entry.get("allowed") else "denied"
                print(f"{status}: {entry['name']}")
                journal_policy_change(Path.cwd(), entry["name"], args.policy_command, note=args.note)
            elif args.policy_command == "remove":
                from .approval import require_approval
                name = normalize_node_name(args.node)
                require_approval(
                    Path.cwd(),
                    "policy.remove",
                    risk="high",
                    origin_type="controller",
                    summary=f"remove whitelist node {name}",
                    details=f"node={name}",
                    timeout=300,
                    poll_interval=0.25,
                )
                if remove_whitelist(args.node):
                    print(f"removed: {name}")
                    journal_policy_change(Path.cwd(), name, "remove")
                else:
                    print(f"not found: {name}")
            elif args.policy_command == "allow-tailscale":
                from .approval import require_approval
                require_approval(
                    Path.cwd(),
                    "policy.allow-tailscale",
                    risk="high",
                    origin_type="controller",
                    summary="allow built-in Tailscale CIDR ranges",
                    details=",".join(TAILSCALE_CIDRS),
                    timeout=300,
                    poll_interval=0.25,
                )
                entries = set_tailscale_whitelist(note=args.note)
                print("allowed Tailscale ranges:")
                for entry in entries:
                    print(f"  - {entry['name']}")
                    journal_policy_change(Path.cwd(), entry["name"], "allow", note=entry.get("note", ""))
            elif args.policy_command == "remove-tailscale":
                from .approval import require_approval
                require_approval(
                    Path.cwd(),
                    "policy.remove-tailscale",
                    risk="high",
                    origin_type="controller",
                    summary="remove built-in Tailscale CIDR ranges",
                    details=",".join(TAILSCALE_CIDRS),
                    timeout=300,
                    poll_interval=0.25,
                )
                removed = remove_tailscale_whitelist()
                print(f"removed {removed} Tailscale range(s)")
                for cidr in TAILSCALE_CIDRS:
                    journal_policy_change(Path.cwd(), cidr, "remove")
        elif args.command == "route":
            if args.route_command == "list":
                state = load_swarm_state()
                routes = merged_route_rows(state)
                if not routes:
                    print("No saved routes.")
                    print("Use 'agentremote route set <node> <host> [port]' or 'agentremote connect <name> <host> <port>' to add one.")
                else:
                    grouped: dict[str, list] = {}
                    for route in routes:
                        grouped.setdefault(route["name"], []).append(route)
                    for name in sorted(grouped):
                        node_routes = grouped[name]
                        selected_route = select_best_route(node_routes)
                        for idx, route in enumerate(node_routes):
                            tls_info = " tls" if route.get("tlsFingerprint") else ""
                            selected = ""
                            if selected_route and route.get("host") == selected_route.get("host") and int(route.get("port", 0)) == int(selected_route.get("port", 0)):
                                selected = " [selected]"
                            health = route_health_summary(route)
                            print(
                                f"{route['name']}\t{route['host']}:{route['port']}\t"
                                f"{route.get('routeType', 'direct')}\t"
                                f"priority={int(route.get('priority', 100))}\t"
                                f"{route.get('source', 'explicit')}{tls_info}{selected}{health}"
                            )
                print()
                print("Note: selected route is priority-first; health/latency is used only as a tie breaker.")
            elif args.route_command == "set":
                entry = set_route(
                    args.node,
                    args.host,
                    args.port,
                    priority=args.priority,
                    tls_fingerprint=args.tls_fingerprint,
                )
                tls_mark = " tls" if entry.get("tlsFingerprint") else ""
                print(
                    f"route set: {entry['name']} {entry['host']}:{entry['port']} "
                    f"priority={entry['priority']}{tls_mark}"
                )
            elif args.route_command == "remove":
                name = normalize_node_name(args.node)
                removed = remove_route(args.node, host=args.host, port=args.port)
                if removed:
                    print(f"removed {removed} route(s): {name}")
                else:
                    print(f"no matching routes: {name}")
            elif args.route_command == "probe":
                state = load_swarm_state()
                name = normalize_node_name(args.node)
                routes = state.get("routes", {}).get(name, [])
                if not routes:
                    connections = [e for e in iter_connections() if e["name"] == name]
                    if not connections:
                        print(f"No routes or connections for {name}")
                        return
                    routes = [
                        {
                            "host": c["host"],
                            "port": int(c["port"]),
                            "tlsFingerprint": c.get("tlsFingerprint", ""),
                            "tlsInsecure": bool(c.get("tlsInsecure", False)),
                            "caFile": c.get("caFile", ""),
                            "priority": 1000,
                        }
                        for c in connections
                    ]
                candidates = sort_swarm_routes(routes)
                if not args.all_routes:
                    best = select_best_route(candidates) or candidates[0]
                    candidates = [best]
                for route in candidates:
                    health = probe_route(
                        name,
                        route["host"],
                        int(route["port"]),
                        tls_fingerprint=route.get("tlsFingerprint", ""),
                        tls_insecure=bool(route.get("tlsInsecure", False)),
                        ca_file=str(route.get("caFile", "") or ""),
                        timeout=args.timeout,
                    )
                    health["host"] = route["host"]
                    health["port"] = int(route["port"])

                    save_route_health(name, route["host"], int(route["port"]), health)
                    saved_state = load_swarm_state()
                    saved_health = saved_state.get("routeHealth", {}).get(name, {}).get(
                        f"{route['host']}\t{int(route['port'])}",
                        health,
                    )
                    status = "OK" if health.get("lastOkAt") else "FAIL"
                    latency = f" {saved_health.get('lastLatencyMs')}ms" if saved_health.get("lastLatencyMs") else ""
                    error = f" error={saved_health.get('lastError')}" if saved_health.get("lastError") else ""
                    print(f"{route['host']}:{route['port']} {status}{latency}{error}")

                    journal_route_probe(Path.cwd(), name, route["host"], int(route["port"]), saved_health)
        elif args.command == "call":
            target = resolve_target(args.node, args.port, args.password, args)
            check_policy_alias(target.alias, args.policy, target.host)
            remote_paths: list[str] = []
            if args.path:
                result = send_handoff(
                    target.host,
                    target.port,
                    target.password,
                    Path(args.path),
                    args.task,
                    remote_dir=args.remote_dir,
                    token=target.token,
                    overwrite=args.overwrite,
                    local_root=Path.cwd(),
                    from_name=args.from_name,
                    alias=target.alias,
                    expect_report=args.expect_report,
                    auto_run=args.auto_run,
                    callback_alias=args.callback_alias,
                    tls_fingerprint=target.tls_fingerprint,
                    tls_insecure=target.tls_insecure,
                    ca_file=target.ca_file,
                )
                transfer = result.get("transfer", {})
                instruction = result.get("instruction", {})
                remote_paths = list(transfer.get("remotePaths", []))
            else:
                instruction = tell(
                    target.host, target.port, target.password, args.task,
                    token=target.token, local_root=Path.cwd(),
                    from_name=args.from_name, alias=target.alias,
                    paths=remote_paths, expect_report=args.expect_report,
                    auto_run=args.auto_run, callback_alias=args.callback_alias,
                    tls_fingerprint=target.tls_fingerprint, tls_insecure=target.tls_insecure, ca_file=target.ca_file,
                )
            call_record = save_call_record(
                target.alias, instruction.get("id"), instruction.get("handoffId", ""),
                remote_paths, "sent",
            )
            print(f"call sent: {call_record['callId']}")
            if remote_paths:
                print(f"paths: {', '.join(remote_paths)}")
        elif args.command == "calls":
            if args.calls_command == "list":
                records = list_call_records(Path(args.root))
                if not records:
                    print("No call records.")
                else:
                    for record in records:
                        paths = ", ".join(record.get("paths", []))
                        print(f"{record['callId']}\t{record['targetNode']}\t{record['state']}\t{paths}")
            elif args.calls_command == "show":
                record = read_call_record(args.call_id, root=Path(args.root))
                print_json(record)
            elif args.calls_command == "refresh":
                updated = refresh_call_records(Path(args.root))
                if updated:
                    print(f"Refreshed {len(updated)} call record(s)")
                    for rec in updated:
                        print(f"  {rec['callId']}: {rec['state']}")
                else:
                    print("No call records were updated")
            elif args.calls_command == "wait":
                result = wait_for_handoff_report(Path(args.root), args.call_id, timeout=args.timeout, progress=True)
                print_wait_report_result(result)
        elif args.command == "processes":
            if args.processes_command in (None, "list"):
                root = Path(args.root).resolve()
                print_process_registry(root)
            elif args.processes_command == "forget":
                root = Path(args.root).resolve()
                ok = forget_process(root, args.id)
                print(f"forgotten: {args.id}" if ok else f"not found: {args.id}")
            elif args.processes_command == "stop":
                root = Path(args.root).resolve()
                proc = get_process(root, args.id)
                if not proc:
                    print(f"not found: {args.id}")
                    return
                pid = process_pid(proc)
                if not process_stop_metadata_valid(root, proc):
                    print(f"PID mismatch; refusing to stop {args.id}")
                    return
                if not process_is_running(pid):
                    print(f"process is not running: {args.id}")
                    return
                try:
                    os.kill(pid, signal.SIGTERM)
                    print(f"stopped: {args.id}")
                except Exception as exc:
                    print(f"cannot stop: {exc}")
            elif args.processes_command == "stop-gui":
                stop_registered_gui_processes(Path(args.root).resolve())
        elif args.command == "stop-gui":
            stop_registered_gui_processes(Path(args.root).resolve())
        elif args.command == "approvals":
            from .approval import (
                decide_approval,
                list_approval_requests,
                load_approval_policy,
                sanitize_approval,
                save_approval_policy,
                wait_for_approval,
            )
            root = Path(args.root).resolve()
            if args.approvals_command == "list":
                items = list_approval_requests(root, status=args.status)
                if not items:
                    print("No matching approval requests.")
                else:
                    for item in items:
                        s = sanitize_approval(item)
                        print(f"{s['approvalId']} {s['status']} risk={s.get('risk','?')} {s.get('summary','')[:60]}")
            elif args.approvals_command == "approve":
                result = decide_approval(root, args.id, "approved", decided_by="cli")
                print(f"{result['status']}: {args.id}")
            elif args.approvals_command == "deny":
                result = decide_approval(root, args.id, "denied", decided_by="cli")
                print(f"{result['status']}: {args.id}")
            elif args.approvals_command == "wait":
                result = wait_for_approval(root, args.id, timeout=args.timeout)
                print(f"{result.get('status', 'unknown')}: {args.id}")
            elif args.approvals_command == "policy":
                if args.mode:
                    policy = save_approval_policy(root, args.mode)
                    print(f"approval mode: {policy['mode']}")
                else:
                    policy = load_approval_policy(root)
                    print(f"approval mode: {policy['mode']}")
        elif args.command == "worker-policy":
            from .worker_policy import init_policy, load_policy, list_rules, allow_rule, remove_rule
            root = Path(args.root).resolve()
            if args.worker_policy_command == "init":
                policy = init_policy(root)
                print(f"initialized: {root / '.agentremote/worker-policy.json'}")
            elif args.worker_policy_command == "list":
                rules = list_rules(root)
                if not rules:
                    print("No allowlist rules. Run 'agentremote worker-policy init' to create the policy file.")
                else:
                    for r in rules:
                        print(
                            f"{r['name']} {r['command']} args={r.get('argsPattern','*')} "
                            f"timeout={r.get('timeoutSeconds',600)}s shell={str(r.get('shell', False)).lower()}"
                        )
            elif args.worker_policy_command == "allow":
                rule = allow_rule(
                    root,
                    args.name,
                    args.allowed_command,
                    args_pattern=args.args_pattern,
                    timeout_seconds=args.timeout,
                    max_stdout_bytes=args.max_stdout,
                    network=args.network,
                    shell=args.shell,
                    description=args.description,
                )
                print(f"allowed: {rule['name']} -> {rule['command']}")
            elif args.worker_policy_command == "remove":
                ok = remove_rule(root, args.name)
                print(f"removed: {args.name}" if ok else f"not found: {args.name}")
            elif args.worker_policy_command == "templates":
                from .worker_policy import list_templates
                templates = list_templates()
                for name, tmpl in templates.items():
                    print(f"{name}: {tmpl.get('description', '')} shell={str(tmpl.get('shell', False)).lower()}")
            elif args.worker_policy_command == "apply-template":
                from .worker_policy import apply_template
                result = apply_template(root, args.template)
                if result:
                    print(f"applied template: {result['name']} -> {result['command']}")
                else:
                    print(f"unknown template: {args.template}")
        elif args.command == "setup":
            summary = run_bootstrap(Path(args.root), install=args.install, check_network=not args.no_network_check)
            print(format_summary(summary))
        elif args.command == "share":
            if args.verbose:
                print("Share will start; connect from another agent with:")
                print(f"  agentremote connect <name> {args.host}:{args.port} --password <your-password>")
                print(f"  agentremote open <name>")
                print()
            firewall = args.firewall
            if firewall == "auto":
                firewall = "no" if is_loopback_bind_host(args.host) else "ask"
            run_slave(
                Path(args.root),
                args.port,
                daemon_password_arg(args),
                args.host,
                model_id="agentremote-slave",
                firewall=firewall,
                authenticated_transfer_per_minute=args.authenticated_transfer_per_minute,
                policy=args.policy,
                node_name=args.node_name,
                verbose=args.verbose,
                auto_worker=args.auto_worker,
                worker_execute=args.worker_execute,
                worker_include_manual=args.worker_include_manual,
                worker_report_to=args.worker_report_to,
                worker_from_name=args.worker_from_name,
                worker_timeout=args.worker_timeout,
                worker_interval=args.worker_interval,
                worker_agent_command=args.worker_agent_command,
                worker_agent_command_shell=args.worker_agent_command_shell,
            )
        elif args.command == "open":
            target = resolve_target(args.name, args.port, args.password, args)
            check_policy_alias(target.alias, args.policy)
            run_master(target.host, target.port, Path(args.local), target.password, token=target.token, ui_port=args.ui_port, open_browser=not args.no_browser, tls_fingerprint=target.tls_fingerprint, tls_insecure=target.tls_insecure, ca_file=target.ca_file)
        elif args.command == "send":
            target = resolve_target(args.name, args.port, args.password, args)
            check_policy_alias(target.alias, args.policy)
            push(target.host, target.port, target.password, Path(args.path), args.remote_dir, token=target.token, overwrite=args.overwrite, alias=target.alias, tls_fingerprint=target.tls_fingerprint, tls_insecure=target.tls_insecure, ca_file=target.ca_file)
        elif args.command == "ask":
            target = resolve_target(args.name, args.port, args.password, args)
            check_policy_alias(target.alias, args.policy)
            instruction = tell(target.host, target.port, target.password, args.task, token=target.token, local_root=Path.cwd(), from_name=args.from_name, alias=target.alias, paths=args.path, expect_report=args.expect_report, auto_run=args.auto_run, callback_alias=args.callback_alias, tls_fingerprint=target.tls_fingerprint, tls_insecure=target.tls_insecure, ca_file=target.ca_file)
            call_record = save_call_record(target.alias, instruction.get("id"), instruction.get("handoffId", ""), args.path or [], "sent")
            print(f"ask sent: {call_record['callId']}")
            if args.wait_report and not args.callback_alias:
                print_wait_report_callback_warning()
            if args.wait_report:
                print(f"Waiting for report (timeout {args.timeout}s)...")
                result = wait_for_handoff_report(Path.cwd(), call_record["callId"], timeout=args.timeout, progress=True)
                print_wait_report_result(result)
        elif args.command == "sync-project":
            target = resolve_target(args.name, args.port, args.password, args)
            check_policy_alias(target.alias, args.policy)
            local_path = Path(args.local).resolve() if args.local else Path.cwd()
            remote = RemoteClient(target.host, target.port, target.password, token=target.token, tls_fingerprint=target.tls_fingerprint, tls_insecure=target.tls_insecure, ca_file=target.ca_file)
            excludes = sync_project_excludes(args)
            profiles = sync_project_profiles(args)
            plan = sync_plan_push(local_path, local_path, args.remote_dir, remote, exclude_patterns=excludes)
            write_plan(local_path, plan)
            transfer_bytes = sync_project_transfer_bytes(plan, overwrite=args.overwrite)
            print(f"Sync plan: {local_path.name} -> {args.remote_dir}")
            print(f"Profiles: {', '.join(profiles)}")
            if args.all_files:
                print("WARNING: default generated-folder, volatile-memory, and secret-pattern excludes are disabled.")
                print("         Only explicit --exclude rules and protocol-reserved state protection apply.")
            print(
                "Files: "
                f"{plan['summary']['copyFiles']} copy, "
                f"{len(plan.get('conflicts', []))} changed/conflict, "
                f"{plan['summary'].get('skipped', 0)} skipped"
            )
            print(f"Total: {format_bytes(transfer_bytes)}")
            print_sync_project_plan_details(plan, excludes)
            remote_storage = sync_project_remote_storage(remote)
            if remote_storage:
                print(f"Remote free: {format_bytes(int(remote_storage.get('freeBytes', 0) or 0))}")
                if args.yes and not args.dry_run:
                    ensure_storage_available(remote_storage, transfer_bytes, "remote project sync destination")
            if args.dry_run:
                print("Dry-run only. No files transferred.")
                return
            if not args.yes:
                if plan["conflicts"]:
                    print(f"{len(plan['conflicts'])} conflict(s). Use --overwrite to resolve.")
                print("Run again with --yes to execute.")
                return
            result = sync_push(target.host, target.port, target.password, local_path, args.remote_dir, token=target.token, overwrite=args.overwrite, delete=args.delete, alias=target.alias, local_root=local_path, tls_fingerprint=target.tls_fingerprint, tls_insecure=target.tls_insecure, ca_file=target.ca_file, exclude_patterns=excludes)
            print(f"Sync complete: {format_bytes(result.get('session', {}).get('totalBytes', result.get('totalBytes', 0)))}")
        elif args.command == "map":
            root = Path(args.root).resolve()
            state = load_swarm_state()
            rows = map_node_rows(root, state)
            print(f"Current project: {root.name}")
            print()
            print("You")
            for row in rows[:20]:
                bits = [row["status"]]
                if row.get("route"):
                    bits.append(f"route={row['route']}")
                if row.get("lastCall"):
                    bits.append(f"last call={row['lastCall']}")
                if row.get("approvals"):
                    bits.append(f"approvals={row['approvals']} pending")
                if row.get("policy") in ("denied", "allowed"):
                    bits.append(f"policy={row['policy']}")
                print(f"  {row['marker']} {row['name'].ljust(14)} {'  '.join(bits)}")
            if len(rows) > 20:
                print(f"  ... and {len(rows) - 20} more")
            if not rows:
                print("  (no connected nodes)")
        elif args.command == "status":
            root = Path(args.root).resolve()
            aimemory = is_installed(root)
            connections = iter_connections()
            state = load_swarm_state()
            processes = list_process_registry(root)
            calls = list_call_records(root)
            node_counts = node_status_counts(state)
            call_counts = call_state_counts(calls)
            process_counts = process_state_counts(processes)
            approvals_pending = len(local_approval_records(root, status="pending"))
            print(f"Project: {root.name}")
            print(f"AIMemory: {'installed' if aimemory else 'missing'}")
            print(f"Connections: {len(connections)}")
            print(
                "Nodes: "
                f"{node_counts.get('online', 0)} online, "
                f"{node_counts.get('offline', 0)} offline, "
                f"{node_counts.get('unknown', 0)} unknown"
            )
            print(
                "Processes: "
                f"{process_counts.get('running', 0)} running, "
                f"{process_counts.get('stale', 0)} stale, "
                f"{process_counts.get('stopped', 0)} stopped"
            )
            print(
                "Calls: "
                f"{call_counts.get('reported', 0)} reported, "
                f"{call_counts.get('sent', 0)} pending, "
                f"{call_counts.get('failed', 0)} failed"
            )
            print(f"Approvals: {approvals_pending} pending")
        elif args.command == "uninstall":
            root = Path(args.root).resolve()
            print("agent-remote-sync v0.1 uninstall assistant")
            print("================================")
            print(f"Project root: {root}")
            print()
            print("To remove the agentremote package:")
            print("  pip uninstall agentremote")
            if args.purge_memory:
                memory = root / "AIMemory"
                if memory.exists():
                    print(f"AIMemory would be removed from {memory}")
                    print("Dry-run only; remove AIMemory manually only if you truly want to erase handoff memory.")
            if args.project_state:
                for d in [".agentremote", ".agentremote_partial", ".agentremote_inbox"]:
                    p = root / d
                    if p.exists():
                        print(f"Would remove: {p}")
                print("Dry-run only. Remove these folders manually after reviewing the list.")
            else:
                print("Use --project-state to inspect/remove project-local agent-remote-sync state.")
                print("Use --purge-memory to remove AIMemory (not recommended by default).")

    except AgentRemoteError as exc:
        raise SystemExit(f"{exc.code}: {exc.message}") from exc


def split_host_port(host: str, port: int | None) -> tuple[str, int]:
    if "://" in host:
        parsed = urlparse(host)
        resolved_port = port or parsed.port or DEFAULT_PORT
        if parsed.hostname:
            hostname = parsed.hostname
            if ":" in hostname and not hostname.startswith("["):
                hostname = f"[{hostname}]"
            return f"{parsed.scheme}://{hostname}:{resolved_port}", resolved_port
        return host.rstrip("/"), resolved_port
    if port is None and host.count(":") == 1:
        maybe_host, maybe_port = host.rsplit(":", 1)
        if maybe_port.isdigit():
            return maybe_host, int(maybe_port)
    return host, port or DEFAULT_PORT


class Target:
    def __init__(
        self,
        host: str,
        port: int,
        password: str | None = None,
        token: str | None = None,
        alias: str = "",
        tls_fingerprint: str = "",
        tls_insecure: bool = False,
        ca_file: str = "",
    ):
        self.host = host
        self.port = port
        self.password = password
        self.token = token
        self.alias = alias
        self.tls_fingerprint = tls_fingerprint
        self.tls_insecure = tls_insecure
        self.ca_file = ca_file


def resolve_target(target: str, port: int | None, password: str | None, args: argparse.Namespace) -> Target:
    explicit_tls = tls_kwargs_from_args(args)
    explicit_token = getattr(args, "token", "") or ""
    swarm_state = load_swarm_state()
    saved = get_connection(target)
    alias = saved["name"] if saved else normalize_node_name(target)
    if not alias.startswith("::"):
        alias = ""
    selected_route = None
    swarm_routes = swarm_state.get("routes", {}).get(alias, []) if alias else []
    if port is None and isinstance(swarm_routes, list) and swarm_routes:
        valid_routes = [route for route in swarm_routes if isinstance(route, dict)]
        if valid_routes:
            selected_route = select_best_route(valid_routes)

    if saved and port is None:
        route_fingerprint = ""
        host = saved["host"]
        resolved_port = int(saved["port"])
        if selected_route:
            resolved_port = int(selected_route.get("port", resolved_port))
            host = route_target_host(str(selected_route.get("host", host)), resolved_port)
            route_fingerprint = str(selected_route.get("tlsFingerprint", "") or "")
        tls_kwargs = {
            "tls_fingerprint": explicit_tls.get("tls_fingerprint") or route_fingerprint or saved.get("tlsFingerprint", ""),
            "tls_insecure": bool(explicit_tls.get("tls_insecure", False) or saved.get("tlsInsecure", False)),
            "ca_file": explicit_tls.get("ca_file") or saved.get("caFile", ""),
        }
        if explicit_token:
            return Target(host, resolved_port, token=explicit_token, alias=saved["name"], **tls_kwargs)
        if password is not None:
            return Target(host, resolved_port, password=password, alias=saved["name"], **tls_kwargs)
        token = saved.get("token")
        if token:
            return Target(host, resolved_port, token=token, alias=saved["name"], **tls_kwargs)
        return Target(host, resolved_port, password=password_arg(None), alias=saved["name"], **tls_kwargs)

    if selected_route:
        route_fingerprint = str(selected_route.get("tlsFingerprint", "") or "")
        if route_fingerprint and not explicit_tls.get("tls_fingerprint"):
            explicit_tls["tls_fingerprint"] = route_fingerprint
        return Target(
            route_target_host(str(selected_route.get("host", target)), int(selected_route.get("port", DEFAULT_PORT))),
            int(selected_route.get("port", DEFAULT_PORT)),
            password=None if explicit_token else password_arg(password),
            token=explicit_token or None,
            alias=alias,
            **explicit_tls,
        )

    host, resolved_port = split_host_port(target, port)
    return Target(
        host,
        resolved_port,
        password=None if explicit_token else password_arg(password),
        token=explicit_token or None,
        alias=alias,
        **explicit_tls,
    )

def sort_swarm_routes(routes: list[dict]) -> list[dict]:
    return sorted(
        routes,
        key=lambda r: (int(r.get("priority", 100)), str(r.get("host", ""))),
    )

def route_target_host(host: str, port: int) -> str:
    if "://" not in host:
        return host
    resolved, _ = split_host_port(host, port)
    return resolved

def route_health_summary(route: dict) -> str:
    if route.get("lastOkAt"):
        latency = route.get("lastLatencyMs")
        return f" ok={latency}ms" if latency else " ok"
    if route.get("lastCheckedAt"):
        failures = int(route.get("failureCount", 0) or 0)
        return f" fail={failures}"
    return ""

def check_policy_alias(alias: str, policy: str, host: str = "") -> None:
    if policy == "off":
        return
    if not alias or not alias.startswith("::"):
        return
    state = load_swarm_state()
    known = state.get("whitelist", {}).get(alias)
    if not known:
        from .connections import get_connection
        known = get_connection(alias)
    if not known and not state.get("routes", {}).get(alias):
        return
    status = whitelist_status(state, alias)
    if status == "unlisted" and host:
        status = whitelist_status(state, host)
    if status == "denied":
        raise AgentRemoteError(403, "policy_denied", f"Node {alias} is denied by local whitelist policy. Use --policy off to bypass.")
    if status == "unlisted" and policy == "strict":
        raise AgentRemoteError(403, "policy_unlisted", f"Node {alias} is not whitelisted and --policy strict requires explicit allow. Use 'agentremote policy allow {alias}' or --policy warn/off.")
    if status == "unlisted" and policy == "warn":
        print(f"[policy] {alias} is not whitelisted. Proceeding (warn mode). Use --policy strict to block.")


def add_tls_client_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--tls-fingerprint",
        default="",
        help="pin the slave HTTPS certificate SHA-256 fingerprint",
    )
    parser.add_argument(
        "--tls-insecure",
        action="store_true",
        help="skip HTTPS certificate verification; use only on trusted test networks",
    )
    parser.add_argument("--ca-file", default="", help="CA bundle or certificate file for HTTPS verification")

def add_policy_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--policy",
        choices=["warn", "strict", "off"],
        default="warn",
        help="whitelist enforcement: warn for unlisted, strict fail-on-unlisted, off skip check",
    )


def add_embedded_worker_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--auto-worker", action="store_true", help="run a worker loop inside this slave/daemon process")
    parser.add_argument(
        "--worker-execute",
        choices=["never", "ask", "yes"],
        default="never",
        help="embedded worker execution mode; use yes only on trusted hosts",
    )
    parser.add_argument("--worker-include-manual", action="store_true", help="embedded worker may process non-autoRun instructions")
    parser.add_argument("--worker-report-to", default="", help="override callback alias for embedded worker STATUS_REPORTs")
    parser.add_argument("--worker-from-name", default="agentremote-auto-worker", help="sender name for embedded worker reports")
    parser.add_argument("--worker-timeout", type=int, default=600, help="embedded worker per-command/bridge timeout")
    parser.add_argument("--worker-interval", type=float, default=5.0, help="embedded worker polling interval")
    parser.add_argument(
        "--worker-agent-command",
        default="",
        help="local bridge command for natural-language handoffs with no agentremote-run lines",
    )
    parser.add_argument("--worker-agent-command-shell", action="store_true", help="run --worker-agent-command through the system shell")


def is_loopback_bind_host(host: str) -> bool:
    text = str(host or "").strip().lower().strip("[]")
    return text in {"127.0.0.1", "localhost", "::1"}


def tls_kwargs_from_args(args: argparse.Namespace) -> dict:
    fingerprint = getattr(args, "tls_fingerprint", "") or ""
    return {
        "tls_fingerprint": normalize_fingerprint(fingerprint) if fingerprint else "",
        "tls_insecure": bool(getattr(args, "tls_insecure", False)),
        "ca_file": getattr(args, "ca_file", "") or "",
    }


def connect_remote(
    host: str,
    port: int,
    password: str,
    args: argparse.Namespace,
) -> tuple[RemoteClient, dict]:
    tls_kwargs = tls_kwargs_from_args(args)
    scopes = parse_scopes(getattr(args, "scopes", ""))
    client_alias = normalize_alias(getattr(args, "name", "")) if getattr(args, "name", "") else ""
    try:
        return RemoteClient(host, port, password, scopes=scopes, client_alias=client_alias, **tls_kwargs), tls_kwargs
    except AgentRemoteError as exc:
        if not should_offer_tls_trust(host, tls_kwargs, exc):
            raise
    if not sys.stdin.isatty():
        raise AgentRemoteError(
            495,
            "tls_untrusted",
            "HTTPS certificate is not trusted. Re-run with --tls-fingerprint, --ca-file, or --tls-insecure.",
        )
    fingerprint = fetch_remote_fingerprint(host, port)
    print("The slave uses an untrusted HTTPS certificate.")
    print(f"SHA-256 fingerprint: {format_fingerprint(fingerprint)}")
    answer = input("Trust this certificate for this saved connection? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        raise AgentRemoteError(495, "tls_untrusted", "TLS certificate was not trusted")
    tls_kwargs["tls_fingerprint"] = fingerprint
    return RemoteClient(host, port, password, scopes=scopes, client_alias=client_alias, **tls_kwargs), tls_kwargs


def parse_scopes(value: str) -> list[str] | None:
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def should_offer_tls_trust(host: str, tls_kwargs: dict, exc: AgentRemoteError) -> bool:
    if not is_https_endpoint(host):
        return False
    if tls_kwargs.get("tls_fingerprint") or tls_kwargs.get("tls_insecure") or tls_kwargs.get("ca_file"):
        return False
    text = (exc.message or "").lower()
    return "certificate_verify_failed" in text or "certificate verify failed" in text

def doctor(root: Path | None = None) -> None:
    root = (root or Path.cwd()).resolve()
    package_file = Path(__file__).resolve()
    package_root = detect_checkout_root(package_file) or package_file.parent
    current_checkout = detect_checkout_root(root)
    command_path = shutil.which("agentremote") or ""
    print(f"agent-remote-sync {__version__}")
    print(f"Python {platform.python_version()}")
    print(f"Platform {platform.platform()}")
    print(f"Python executable: {sys.executable}")
    print(f"agentremote command: {command_path or 'not found on PATH'}")
    print(f"Imported package: {package_file}")
    print(f"Imported checkout: {package_root}")
    print(f"Project root: {root}")
    if current_checkout:
        print(f"Current checkout: {current_checkout}")
        if current_checkout != package_root:
            print("WARNING: current checkout differs from the imported agentremote package.")
            print("         Reinstall with `python -m pip install -e .` from the checkout you want to run.")
    if is_installed(root):
        print(f"agent-work-mem OK: {root / 'AIMemory'}")
    else:
        print("agent-work-mem MISSING in current project")
    process_counts = process_state_counts(list_process_registry(root))
    print(
        "Registered processes: "
        f"{process_counts.get('running', 0)} running, "
        f"{process_counts.get('stale', 0)} stale, "
        f"{process_counts.get('stopped', 0)} stopped"
    )
    try:
        import cryptography

        print(f"TLS self-signed support OK: cryptography {cryptography.__version__}")
    except ImportError:
        print("TLS self-signed support MISSING: install cryptography")
    print("Runtime OK")


def detect_checkout_root(start: Path) -> Path | None:
    current = start if start.is_dir() else start.parent
    for path in [current, *current.parents]:
        if (path / "pyproject.toml").exists() and (path / "src" / "agentremote").exists():
            return path.resolve()
    return None


def password_arg(value: str | None) -> str:
    if value is not None:
        return value
    import getpass

    return getpass.getpass("Slave password: ")


def print_json(value: object) -> None:
    import json

    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))


def command_root(args: argparse.Namespace) -> Path:
    if args.command == "slave":
        return Path(args.root)
    if args.command == "master":
        return Path(args.local)
    if args.command == "daemon":
        return Path(args.root)
    if args.command == "controller":
        return Path(args.local)
    if args.command == "inbox":
        return Path(args.root)
    if args.command == "worker":
        return Path(args.root)
    if args.command == "calls":
        return Path(getattr(args, "root", "."))
    if args.command == "cleanup":
        return Path(args.root)
    if args.command == "share":
        return Path(args.root)
    if args.command == "open":
        return Path(args.local)
    if args.command in ("send", "sync-project"):
        return Path.cwd()
    if args.command == "setup":
        return Path(args.root)
    return Path.cwd()


def daemon_command_skips_workmem(args: argparse.Namespace) -> bool:
    daemon_command = getattr(args, "daemon_command", "")
    if daemon_command in {"status", "profile-list", "profile-remove", "install", "uninstall"}:
        return True
    if daemon_command == "profile" and getattr(args, "daemon_profile_command", "") in {"list", "remove"}:
        return True
    return False


def _nodes_status(args: argparse.Namespace) -> None:
    state = load_swarm_state()
    nodes_dict = state.setdefault("nodes", {})
    routes = merged_route_rows(state)

    def selected_route_for(node: str) -> dict | None:
        name = normalize_node_name(node)
        node_routes = [row for row in routes if normalize_node_name(str(row.get("name", ""))) == name]
        return select_best_route(node_routes)

    def save_node_record(node: str, record: dict) -> None:
        nodes_dict[node] = record
        save_swarm_state(state)
        journal_node_status(Path.cwd(), node, record)
        journal_routes_summary(Path.cwd(), state)

    def unknown_node(node: str, error: str) -> dict:
        name = normalize_node_name(node)
        previous = nodes_dict.get(name, {})
        record = {
            "name": name,
            "nodeName": previous.get("nodeName", name),
            "lastCheckedAt": time.time(),
            "lastSeenAt": previous.get("lastSeenAt"),
            "lastStatus": "unknown",
            "lastError": error,
            "modelId": previous.get("modelId", ""),
            "policy": previous.get("policy", ""),
            "rootLabel": previous.get("rootLabel", ""),
            "capabilities": previous.get("capabilities", []),
            "storage": previous.get("storage", {}),
        }
        save_node_record(name, record)
        return {"nodeKey": name, "nodeName": record["nodeName"], "status": "unknown", "lastError": error}

    def fetch_node(node: str) -> dict:
        name = normalize_node_name(node)
        route = selected_route_for(name)
        if not route:
            return unknown_node(name, "no_route")
        host = str(route.get("host", ""))
        port = int(route.get("port", 0) or 0)
        if not host or not port:
            return unknown_node(name, "bad_route")
        try:
            secure = is_https_endpoint(host) or bool(
                route.get("tlsFingerprint") or route.get("tlsInsecure") or route.get("caFile")
            )
            url = probe_url(host, port, secure=secure).replace("/api/challenge", "/api/node")
            with open_url(
                Request(url, method="GET"),
                timeout=args.timeout,
                tls_fingerprint=str(route.get("tlsFingerprint", "") or ""),
                tls_insecure=bool(route.get("tlsInsecure", False)),
                ca_file=str(route.get("caFile", "") or ""),
            ) as resp:
                data = json.loads(resp.read().decode())
            now = time.time()
            record = {
                "name": name,
                "nodeName": data.get("nodeName", name),
                "lastCheckedAt": now,
                "lastSeenAt": now,
                "lastStatus": "online",
                "modelId": data.get("modelId", ""),
                "policy": data.get("policy", ""),
                "rootLabel": data.get("rootLabel", ""),
                "capabilities": data.get("capabilities", []),
                "storage": data.get("storage", {}),
                "route": {
                    "host": host,
                    "port": port,
                    "source": route.get("source", "explicit"),
                },
            }
            save_node_record(name, record)
            data["status"] = "online"
            data["nodeKey"] = name
            return data
        except Exception as exc:
            previous = nodes_dict.get(name, {})
            record = {
                "name": name,
                "nodeName": previous.get("nodeName", name),
                "lastCheckedAt": time.time(),
                "lastSeenAt": previous.get("lastSeenAt"),
                "lastStatus": "offline",
                "lastError": str(exc)[:200],
                "modelId": previous.get("modelId", ""),
                "policy": previous.get("policy", ""),
                "rootLabel": previous.get("rootLabel", ""),
                "capabilities": previous.get("capabilities", []),
                "storage": previous.get("storage", {}),
                "route": {
                    "host": host,
                    "port": port,
                    "source": route.get("source", "explicit"),
                },
            }
            save_node_record(name, record)
            return {
                "nodeKey": name,
                "nodeName": record["nodeName"],
                "status": "offline",
                "lastError": record["lastError"],
                "modelId": record.get("modelId", ""),
                "capabilities": record.get("capabilities", []),
                "storage": record.get("storage", {}),
            }

    if args.all_nodes:
        targets = topology_nodes(state)
    elif args.node:
        targets = [normalize_node_name(args.node)]
    else:
        print("Specify a node or use --all.")
        return

    results = []
    for node in sorted(set(targets)):
        info = fetch_node(node)
        results.append(info)

    if args.json:
        print_json(results if args.all_nodes or len(results) != 1 else results[0])
        return

    for info in results:
        node = info.get("nodeKey", info.get("nodeName", "unknown"))
        record = nodes_dict.get(node, {})
        status = info.get("status", record.get("lastStatus", "unknown"))
        model = info.get("modelId", record.get("modelId", "?"))
        storage_info_val = info.get("storage", record.get("storage", {}))
        storage_str = f"free={storage_info_val.get('freeBytes', '?')}" if storage_info_val else ""
        error = f" error={info.get('lastError')}" if info.get("lastError") else ""
        print(f"{node}: {status} model={model} {storage_str}{error}")

CALL_DIR_NAME = "calls"

def _calls_dir(root: Path | None = None, *, create: bool = True) -> Path:
    from .state import state_dir as _state_dir

    base = (root or Path.cwd()).resolve()
    if create:
        d = _state_dir(base) / CALL_DIR_NAME
    else:
        d = base / STATE_DIR_NAME / CALL_DIR_NAME
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d

def save_call_record(
    node: str, instruction_id: str, handoff_id: str, paths: list[str], state: str, *, root: Path | None = None,
) -> dict:
    call_id = f"call-{time.strftime('%Y%m%d-%H%M%S')}-{make_token()[:8]}"
    record = {
        "callId": call_id,
        "targetNode": node,
        "instructionId": instruction_id,
        "handoffId": handoff_id,
        "paths": paths,
        "state": state,
        "sentAt": time.time(),
        "reportedAt": None,
    }
    project_root = (root or Path.cwd()).resolve()
    path = _calls_dir(project_root) / f"{call_id}.json"
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp_path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
    journal_call_record(project_root, record)
    return record

def list_call_records(root: Path | None = None) -> list[dict]:
    records = []
    call_dir = _calls_dir(root, create=False)
    if not call_dir.exists():
        return records
    for f in sorted(call_dir.glob("call-*.json"), reverse=True):
        try:
            records.append(json.loads(f.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return records

def read_call_record(call_id: str, *, root: Path | None = None) -> dict:
    path = _calls_dir(root, create=False) / f"{call_id}.json"
    if not path.exists():
        raise AgentRemoteError(404, "call_not_found", f"Call record {call_id} not found")
    return json.loads(path.read_text(encoding="utf-8"))

def refresh_call_records(root: Path) -> list[dict]:
    updated = []
    calls = list_call_records(root)
    if not calls:
        return updated
    from .inbox import list_instructions as _list_instructions

    instructions = _list_instructions(root)
    for call in calls:
        if call.get("state") in ("reported", "failed", "completed"):
            continue
        match_ids = {str(call.get("handoffId", "")), str(call.get("instructionId", ""))}
        match_ids.discard("")
        for inst in instructions:
            if instruction_matches_call_report(root, inst, match_ids):
                call["state"] = report_state_from_instruction(root, inst)
                call["reportedAt"] = time.time()
                call["reportInstructionId"] = inst.get("id", "")
                call["reportHandoffId"] = inst.get("handoffId", "")
                updated.append(call)
                save_call_payload(call, root=root)
                break
    return updated

def instruction_matches_call_report(root: Path, instruction: dict, match_ids: set[str]) -> bool:
    if not match_ids:
        return False
    metadata = read_report_metadata(root, instruction)
    if metadata.get("type") == "STATUS_REPORT" and metadata.get("parentId") in match_ids:
        return True
    task = str(instruction.get("task", ""))
    lowered = task.lower()
    if not (
        lowered.startswith("status report")
        or lowered.startswith("report for")
        or lowered.startswith("report:")
        or lowered.startswith("status:")
    ):
        return False
    return any(identifier in task for identifier in match_ids)


def read_report_metadata(root: Path, instruction: dict) -> dict[str, str]:
    metadata: dict[str, str] = {}
    handoff_file = str(instruction.get("handoffFile", "") or "")
    if not handoff_file:
        return metadata
    path = root.resolve() / handoff_file
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return metadata
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("**Type**:"):
            metadata["type"] = line.split(":", 1)[1].strip()
        elif line.startswith("- parentId:"):
            metadata["parentId"] = line.split(":", 1)[1].strip().strip("`")
        elif line.startswith("- handoffId:"):
            metadata["handoffId"] = line.split(":", 1)[1].strip().strip("`")
    return metadata


def report_state_from_instruction(root: Path, instruction: dict) -> str:
    text = str(instruction.get("task", "")).lower()
    if any(word in text for word in ("failed", "failure", "error", "blocked", "blocker")):
        return "failed"
    metadata = read_report_metadata(root, instruction)
    if metadata.get("type") == "STATUS_REPORT":
        return "reported"
    return "reported"


def save_call_payload(record: dict, *, root: Path | None = None) -> None:
    project_root = (root or Path.cwd()).resolve()
    path = _calls_dir(project_root) / f"{record['callId']}.json"
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp_path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
    journal_call_record(project_root, record)

def start_process_heartbeat(root: Path, pid: int, process_id: str = "", interval: float = 20.0) -> None:
    root = root.resolve()
    pid = int(pid)
    process_id = str(process_id or "")
    sleep_interval = max(0.05, float(interval))

    def _beat():
        while True:
            time.sleep(sleep_interval)
            try:
                update_process_heartbeat(root, pid, process_id=process_id)
            except Exception:
                pass
    threading.Thread(target=_beat, daemon=True).start()


def map_node_rows(root: Path, state: dict) -> list[dict]:
    connections = {c["name"]: c for c in iter_connections()}
    routes = merged_route_rows(state)
    nodes_status = state.get("nodes", {}) if isinstance(state.get("nodes"), dict) else {}
    calls = list_call_records(root)
    approvals = local_approval_records(root, status="pending")
    names = set(topology_nodes(state))
    names.update(connections.keys())
    names.update(nodes_status.keys())
    names.update(str(call.get("targetNode", "")) for call in calls if call.get("targetNode"))
    rows = []
    for name in sorted(n for n in names if n):
        status = str(nodes_status.get(name, {}).get("lastStatus", "unknown"))
        marker = "+" if status == "online" else ("-" if status == "offline" else "?")
        selected_route = selected_route_for_node(routes, name)
        latest_call = latest_call_for_node(calls, name)
        rows.append(
            {
                "name": name,
                "status": status,
                "marker": marker,
                "route": format_route(selected_route),
                "lastCall": format_call_summary(latest_call),
                "approvals": approval_count_for_node(approvals, name),
                "policy": whitelist_status(state, name),
            }
        )
    return rows


def selected_route_for_node(routes: list[dict], node: str) -> dict | None:
    normalized = normalize_node_name(node)
    node_routes = [
        route
        for route in routes
        if normalize_node_name(str(route.get("name", ""))) == normalized
    ]
    return select_best_route(node_routes) if node_routes else None


def format_route(route: dict | None) -> str:
    if not route:
        return ""
    host = str(route.get("host", "") or "")
    port = int(route.get("port", 0) or 0)
    if not host:
        return ""
    text = f"{host}:{port}" if port else host
    source = str(route.get("source", "") or "")
    if source:
        text += f"({source})"
    if route.get("lastLatencyMs"):
        text += f" {route.get('lastLatencyMs')}ms"
    elif route.get("lastError"):
        text += " fail"
    return text


def latest_call_for_node(calls: list[dict], node: str) -> dict | None:
    normalized = normalize_node_name(node)
    matches = [
        call
        for call in calls
        if normalize_node_name(str(call.get("targetNode", ""))) == normalized
    ]
    if not matches:
        return None
    return max(matches, key=lambda call: float(call.get("sentAt", 0) or 0))


def format_call_summary(call: dict | None) -> str:
    if not call:
        return ""
    state = str(call.get("state", "unknown") or "unknown")
    stamp = call.get("reportedAt") or call.get("sentAt")
    age = human_age(stamp)
    return f"{state} {age}".strip()


def human_age(timestamp: object) -> str:
    try:
        seconds = max(0, int(time.time() - float(timestamp)))
    except (TypeError, ValueError):
        return ""
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def local_approval_records(root: Path, *, status: str = "") -> list[dict]:
    base = root.resolve() / ".agentremote" / "approvals"
    if not base.exists():
        return []
    records = []
    for path in sorted(base.glob("approval-*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if status and data.get("status") != status:
            continue
        records.append(data)
    return records


def approval_count_for_node(approvals: list[dict], node: str) -> int:
    normalized = normalize_node_name(node)
    total = 0
    for approval in approvals:
        target = normalize_node_name(str(approval.get("targetNode", "")))
        origin = normalize_node_name(str(approval.get("originNode", "")))
        if target == normalized or origin == normalized:
            total += 1
    return total


def call_state_counts(calls: list[dict]) -> dict[str, int]:
    counts = {"sent": 0, "reported": 0, "failed": 0}
    for call in calls:
        state = str(call.get("state", "") or "sent")
        if state in counts:
            counts[state] += 1
    return counts


def process_state_counts(processes: list[dict]) -> dict[str, int]:
    counts = {"running": 0, "stale": 0, "stopped": 0}
    for proc in processes:
        status = str(proc.get("status", "") or "stale")
        if status not in counts:
            status = "stale"
        counts[status] += 1
    return counts


def print_process_registry(root: Path) -> None:
    procs = list_process_registry(root)
    if not procs:
        print("No registered processes.")
        return
    counts = process_state_counts(procs)
    print(
        f"Processes for {root}: "
        f"{counts.get('running', 0)} running, {counts.get('stale', 0)} stale, {counts.get('stopped', 0)} stopped"
    )
    for p in procs:
        role = p.get("role", "?")
        pid = p.get("pid", "?")
        status = p.get("status", "?")
        host = p.get("host", "")
        port = p.get("port", "")
        addr = f"{host}:{port}" if host and port else ""
        ui = p.get("uiUrl", "")
        print(f"{p['id']} {role} pid={pid} {status} {addr} {ui}")


def stop_registered_gui_processes(root: Path) -> None:
    stopped = 0
    candidates = [
        proc for proc in list_process_registry(root)
        if proc.get("role") in ("master", "controller-gui") and proc.get("status") == "running"
    ]
    if not candidates:
        print("No running registered GUI processes.")
        return
    for proc in candidates:
        pid = process_pid(proc)
        proc_id = str(proc.get("id", ""))
        if not process_stop_metadata_valid(root, proc):
            print(f"PID mismatch; refusing to stop {proc_id}")
            continue
        if not process_is_running(pid):
            print(f"process is not running: {proc_id}")
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"stopped: {proc_id}")
            stopped += 1
        except Exception as exc:
            print(f"cannot stop {proc_id}: {exc}")
    if stopped == 0:
        print("No GUI process was stopped.")


def node_status_counts(state: dict) -> dict[str, int]:
    counts = {"online": 0, "offline": 0, "unknown": 0}
    nodes = topology_nodes(state)
    status_by_node = state.get("nodes", {}) if isinstance(state.get("nodes"), dict) else {}
    for node in nodes:
        status = str(status_by_node.get(node, {}).get("lastStatus", "unknown"))
        if status not in counts:
            status = "unknown"
        counts[status] += 1
    return counts


CORE_SYNC_EXCLUDES = {
    ".git/",
    ".agentremote/",
    ".agentremote_partial/",
    ".agentremote_handoff/",
    ".agentremote_inbox/",
    ".claude/",
    ".codex/",
    ".opencode/",
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.crt",
}

VOLATILE_MEMORY_EXCLUDES = {
    "AIMemory/agentremote_hosts/",
    "AIMemory/swarm/calls/",
    "AIMemory/swarm/events/",
    "AIMemory/swarm/nodes/",
    "AIMemory/swarm/routes.md",
}

SYNC_PROJECT_PROFILES: dict[str, set[str]] = {
    "standard": {
        ".venv/",
        "venv/",
        "node_modules/",
        "__pycache__/",
        ".pytest_cache/",
        ".mypy_cache/",
        ".ruff_cache/",
        "dist/",
        "build/",
        "logs/",
        "Logs/",
        "Library/",
        "Temp/",
        "Obj/",
        "UserSettings/",
    },
    "unity": {"Library/", "Logs/", "UserSettings/", "Temp/", "Obj/", "Build/", "Builds/"},
    "python": {"__pycache__/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", ".tox/", ".nox/", ".venv/", "venv/", "dist/", "build/", "*.egg-info/"},
    "node": {"node_modules/", ".next/", ".nuxt/", ".svelte-kit/", "coverage/", "dist/", "build/"},
    "llm": {"models/", "model/", "tts/", "audio_out/", "outputs/", "checkpoints/", "*.safetensors", "*.ckpt", "*.gguf"},
}
SYNC_PROJECT_PROFILES["unity-python-llm"] = (
    SYNC_PROJECT_PROFILES["unity"] | SYNC_PROJECT_PROFILES["python"] | SYNC_PROJECT_PROFILES["node"] | SYNC_PROJECT_PROFILES["llm"]
)
DEFAULT_SYNC_EXCLUDES = CORE_SYNC_EXCLUDES | SYNC_PROJECT_PROFILES["standard"] | VOLATILE_MEMORY_EXCLUDES


def sync_project_profiles(args: argparse.Namespace) -> list[str]:
    if getattr(args, "all_files", False):
        return ["all-files"]
    profiles = list(getattr(args, "profile", None) or [])
    return profiles or ["standard"]


def sync_project_excludes(args: argparse.Namespace) -> set[str]:
    explicit_excludes = set(getattr(args, "exclude", None) or [])
    if getattr(args, "all_files", False):
        return explicit_excludes
    excludes = CORE_SYNC_EXCLUDES.copy()
    for profile in sync_project_profiles(args):
        excludes.update(SYNC_PROJECT_PROFILES.get(profile, set()))
    if not getattr(args, "include_memory", False):
        excludes.add("AIMemory/")
    elif not getattr(args, "include_volatile_memory", False):
        excludes.update(VOLATILE_MEMORY_EXCLUDES)
    excludes.update(explicit_excludes)
    return excludes


def sync_project_transfer_bytes(plan: dict, *, overwrite: bool = False) -> int:
    total = sum(int(item.get("size", 0) or 0) for item in plan.get("copy", []))
    if overwrite:
        total += sum(int(item.get("size", 0) or 0) for item in plan.get("conflicts", []))
    return total


def sync_project_remote_storage(remote: RemoteClient) -> dict | None:
    try:
        storage = remote.storage()
    except Exception:
        return None
    return storage if isinstance(storage, dict) else None


def sync_project_exclusion_summary(plan: dict, *, limit: int = 8) -> list[str]:
    by_pattern: dict[str, dict[str, object]] = {}
    for item in plan.get("excluded", []) or []:
        pattern = str(item.get("pattern", "") or "?")
        bucket = by_pattern.setdefault(pattern, {"count": 0, "sample": ""})
        bucket["count"] = int(bucket["count"]) + 1
        if not bucket["sample"]:
            bucket["sample"] = str(item.get("rel", "") or "")
    rows = sorted(
        by_pattern.items(),
        key=lambda pair: (-int(pair[1]["count"]), str(pair[0])),
    )
    return [
        f"{pattern}: {data['count']} path(s)" + (f" e.g. {data['sample']}" if data.get("sample") else "")
        for pattern, data in rows[:limit]
    ]


def print_sync_project_plan_details(plan: dict, excludes: set[str], *, limit: int = 6) -> None:
    summary = plan.get("summary", {})
    create_dirs = int(summary.get("createDirs", 0) or 0)
    excluded_count = int(summary.get("excluded", len(plan.get("excluded", []) or [])) or 0)
    print(f"Dirs: {create_dirs} create")
    print(f"Excluded: {excluded_count} path(s), {len(excludes)} rule(s)")
    for line in sync_project_exclusion_summary(plan, limit=limit):
        print(f"  - {line}")
    conflicts = list(plan.get("conflicts", []) or [])
    if conflicts:
        print("Conflict samples:")
        for item in conflicts[:limit]:
            size = format_bytes(int(item.get("size", 0) or 0))
            print(f"  - {item.get('rel', '')}: {item.get('reason', 'changed')} ({size})")
    copies = list(plan.get("copy", []) or [])
    if copies:
        print("Upload samples:")
        for item in copies[:limit]:
            size = format_bytes(int(item.get("size", 0) or 0))
            print(f"  - {item.get('rel', '')}: {item.get('reason', 'missing')} ({size})")


def resolve_handoff_cli_args(args: argparse.Namespace) -> tuple[str, str]:
    explicit_path = str(getattr(args, "handoff_path", "") or "")
    local_path = explicit_path or str(getattr(args, "local_path", "") or "")
    task = str(getattr(args, "task_option", "") or getattr(args, "task", "") or "")
    if explicit_path and not getattr(args, "task_option", "") and getattr(args, "local_path", "") and not getattr(args, "task", ""):
        task = str(args.local_path)
    if not local_path:
        raise AgentRemoteError(400, "missing_handoff_path", "Handoff needs a local path. Use positional <path> or --path.")
    if not task:
        raise AgentRemoteError(400, "missing_handoff_task", "Handoff needs a task. Use positional <task> or --task.")
    return local_path, task


def wait_for_handoff_report(root: Path, call_id: str, *, timeout: int = 300, progress: bool = False) -> dict:
    import time as t
    root = root.resolve()
    deadline = t.time() + timeout
    next_notice = t.time() + min(15, max(1, timeout))
    call_path = root / ".agentremote" / "calls" / f"{call_id}.json"
    last_record: dict | None = read_call_record(call_id, root=root) if call_id and call_path.exists() else None
    while t.time() < deadline:
        refresh_call_records(root)
        record = read_call_record(call_id, root=root) if call_id and (root / ".agentremote" / "calls" / f"{call_id}.json").exists() else None
        if record:
            last_record = record
        if record and record.get("state") in ("reported", "failed"):
            result = dict(record)
            result["status"] = str(record.get("state", "unknown"))
            return result
        now = t.time()
        if progress and now >= next_notice:
            state = str((record or {}).get("state", "pending"))
            target = str((record or {}).get("targetNode", ""))
            remaining = max(0, int(deadline - now))
            print(f"Still waiting for report: call={call_id} state={state} target={target} remaining={remaining}s")
            next_notice = now + 30
        t.sleep(1)
    result = {"status": "timeout", "callId": call_id}
    if last_record:
        result.update(
            {
                "state": last_record.get("state"),
                "targetNode": last_record.get("targetNode"),
                "instructionId": last_record.get("instructionId"),
                "handoffId": last_record.get("handoffId"),
                "paths": last_record.get("paths", []),
            }
        )
    result["message"] = "No STATUS_REPORT arrived before timeout."
    result["nextSteps"] = wait_report_next_steps(result)
    return result


def print_wait_report_callback_warning() -> None:
    print("Note: --wait-report only observes reports that arrive back in this project.")
    print("      The receiver must run a local agent/worker and send a STATUS_REPORT back.")
    print("      For automatic return, the receiver usually needs a saved --callback-alias to this host.")


def print_wait_report_result(result: dict) -> None:
    status = result.get("status", "unknown")
    print(f"Result: {status}")
    if status == "timeout":
        if result.get("message"):
            print(str(result["message"]))
        for step in result.get("nextSteps", []):
            print(f"- {step}")


def wait_report_next_steps(result: dict) -> list[str]:
    call_id = str(result.get("callId", ""))
    instruction_id = str(result.get("instructionId", ""))
    handoff_id = str(result.get("handoffId", ""))
    paths = result.get("paths", [])
    first_path = str(paths[0]) if isinstance(paths, list) and paths else ""
    steps = [
        "On the receiver/slave host, ask the local agent to inspect the inbox and process the instruction.",
        "The receiver can run `agentremote worker --once --execute ask` from the project root that started slave/daemon.",
        "For unattended future work, restart the receiver with `--auto-worker --worker-execute yes`.",
        "For natural-language tasks, the receiver also needs a trusted `--worker-agent-command` bridge.",
    ]
    if instruction_id:
        steps.append(f"Receiver inbox instruction id: {instruction_id}")
    if handoff_id:
        steps.append(f"Expected report parent handoff id: {handoff_id}")
    if first_path:
        steps.append(f"Attached/related remote path: {first_path}")
    if call_id:
        steps.append(f"Resume waiting later with `agentremote calls wait {call_id} --root <sender-project>`.")
    return steps


AGENTREMOTE_ONBOARDING_PROMPT = """# agentremote LLM Onboarding Prompt

You are operating `agentremote`, a cross-host file/folder sync and remote-agent
handoff tool. Treat it as a safe transfer and coordination layer, not as remote
shell access.

## First checks

1. Run `agentremote doctor --root <project>` when starting in a repo or after install.
2. Run `agentremote connections` to find saved hosts. Saved aliases may be typed
   with or without the visible `::` prefix.
3. Run `agentremote status --root <project>` and `agentremote processes --root <project>`
   when behavior looks stuck or a GUI port is occupied.

## File and project sync

- For safe project sync, prefer:
  `agentremote sync-project <host> <remote-dir> --local <project> --dry-run --include-memory --profile unity-python-llm`
- Add `--yes` only after reviewing the plan.
- Use `--all-files` or `--no-default-excludes` only when the user explicitly wants
  default generated/secret/volatile excludes disabled.
- Use `--include-memory` to carry AIMemory context. Do not use
  `--include-volatile-memory` unless the user wants local connection/topology runtime state too.

## Handoff and reports

- `agentremote tell <host> "<task>"` sends instruction only.
- `agentremote handoff <host> <local-path> "<task>"` uploads files then sends instruction.
- Do not upload into `.agentremote_*` paths; those are protocol-reserved. Use a
  project path such as `/Project/AIMemory/incoming_handoffs` for human-readable attachments.
- `--auto-run` only marks the instruction eligible. The receiver still needs a
  local worker/agent to claim and process it.
- `--wait-report` is not remote execution. It only waits for a STATUS_REPORT to
  come back to this project. Before using it, confirm one of these is true:
  1. the receiver has a running worker and a saved `--callback-alias` back to this host, or
  2. a human/agent on the receiver will manually run the work and send `agentremote report`.
- If no report arrives, run `agentremote calls show <call-id> --root <project>`
  or `agentremote calls wait <call-id> --root <project>`, then ask the
  receiver-side agent to run `agentremote inbox`, `agentremote inbox --read <id>`,
  and `agentremote worker --once --execute ask` from the slave project root.
- For unattended receiver-side processing, the receiver must start slave/daemon
  with `--auto-worker --worker-execute yes`. This only runs explicit
  `agentremote-run:` lines unless a trusted local bridge is configured with
  `--worker-agent-command`.
- For natural-language handoffs, the bridge command receives
  `AGENTREMOTE_BRIDGE_INPUT` and writes a markdown report to
  `AGENTREMOTE_BRIDGE_OUTPUT` or stdout. `--auto-run` alone does not wake a
  remote LLM.

## Safety

- Never echo passwords, bearer tokens, or private key contents.
- Prefer Tailscale/private networks or HTTPS fingerprint pinning.
- Ask before delete/overwrite unless the user already gave explicit permission.
- For blocked permissions, storage problems, conflicts, or missing receiver worker,
  report the blocker clearly instead of silently waiting.
"""


AGENTREMOTE_ONBOARDING_KO_NOTES = """## 한국어 요약

- 먼저 `agentremote doctor --root <project>`로 실제 import 경로와 실행 파일을 확인하세요.
- 대형 프로젝트는 `sync-project --dry-run --include-memory --profile unity-python-llm`로 계획부터 봅니다.
- 사용자가 정말 원할 때만 `--all-files` 또는 `--no-default-excludes`를 사용합니다.
- `--wait-report`는 원격 실행이 아닙니다. 원격 host에서 worker/agent가 inbox를 처리하고 report를 다시 보내야 완료됩니다.
- 멈춘 것처럼 보이면 `agentremote calls show <call-id> --root <project>`와 `agentremote calls wait <call-id> --root <project>`를 사용하고, 원격 에이전트에게 `agentremote worker --once --execute ask`를 실행하게 하세요.
- 무인 처리를 원하면 받는 쪽 slave/daemon을 `--auto-worker --worker-execute yes`로 시작해야 합니다.
- 자연어 핸드오프는 `--worker-agent-command`로 신뢰된 로컬 에이전트 브리지를 연결해야 자동 처리됩니다. `--auto-run`만으로 원격 LLM이 깨어나지는 않습니다.
"""


def print_onboarding_prompt(*, korean: bool = False) -> None:
    print(AGENTREMOTE_ONBOARDING_PROMPT.rstrip())
    if korean:
        print()
        print(AGENTREMOTE_ONBOARDING_KO_NOTES.rstrip())


def daemon_password_arg(args: argparse.Namespace) -> str | None:
    env_name = str(getattr(args, "password_env", "") or "").strip()
    if env_name:
        if not valid_env_name(env_name):
            raise AgentRemoteError(400, "bad_password_env", "Password environment variable name is invalid")
        password = os.environ.get(env_name)
        if not password:
            raise AgentRemoteError(
                400,
                "missing_password_env",
                f"Environment variable {env_name} is not set or empty",
            )
        return password
    return getattr(args, "password", None)


def valid_env_name(value: str) -> bool:
    if not value:
        return False
    first = value[0]
    if not (first.isalpha() or first == "_"):
        return False
    return all(ch.isalnum() or ch == "_" for ch in value)


def select_daemon_profile(root: Path, name: str = "") -> dict | None:
    if name:
        safe_name = normalize_daemon_profile_name(name)
        return next((profile for profile in load_daemon_profiles() if profile["name"] == safe_name), None)
    profiles = load_daemon_profiles(root=root)
    return profiles[0] if profiles else None


def print_daemon_profiles(profiles: list[dict]) -> None:
    if not profiles:
        print("No saved daemon profiles.")
        return
    for profile in profiles:
        print(f"{profile['name']} {profile['host']}:{profile['port']} root={profile['root']}")


def daemon_service_args(profile: dict) -> list[str]:
    return [
        sys.executable,
        "-m",
        "agentremote",
        "daemon",
        "serve",
        "--root",
        str(profile.get("root", ".")),
        "--host",
        str(profile.get("host", "127.0.0.1")),
        "--port",
        str(int(profile.get("port", DEFAULT_PORT) or DEFAULT_PORT)),
        "--password-env",
        "AGENTREMOTE_DAEMON_PASSWORD",
        "--console",
        "no",
    ]


def render_service_spec(profile: dict, *, platform_name: str | None = None) -> str:
    profile = sanitize_daemon_profile(profile)
    name = profile.get("name", "agentremote")
    root = profile.get("root", ".")
    host = profile.get("host", "127.0.0.1")
    port = int(profile.get("port", DEFAULT_PORT) or DEFAULT_PORT)
    args = daemon_service_args(profile)
    plat = platform_name or sys.platform
    if plat == "win32":
        command = subprocess.list2cmdline(args)
        return f"""# Windows Task Scheduler command
# Password is read from AGENTREMOTE_DAEMON_PASSWORD at runtime; do not put secrets in the profile.
# Create task after configuring the task environment securely:
schtasks /create /tn "agentremote-{name}" /tr "{command}" /sc onlogon /rl limited
"""
    elif plat == "darwin":
        plist = f"~/Library/LaunchAgents/com.agentremote.{name}.plist"
        escaped_args = "\n".join(f"        <string>{xml_escape(arg)}</string>" for arg in args)
        return f"""# macOS launchd user agent
# Save to: {plist}
# Password is read from AGENTREMOTE_DAEMON_PASSWORD at runtime; configure it with launchctl setenv or a secure wrapper.
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.agentremote.{name}</string>
    <key>ProgramArguments</key>
    <array>
{escaped_args}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
"""
    else:
        command = shlex.join(args)
        return f"""# Linux systemd user unit
# Save to: ~/.config/systemd/user/agentremote-{name}.service
# Password is read from AGENTREMOTE_DAEMON_PASSWORD at runtime.
[Unit]
Description=agent-remote-sync daemon: {name}
After=network.target

[Service]
Type=simple
EnvironmentFile=-%h/.agentremote/daemon-secrets/{name}.env
ExecStart={command}
Restart=on-failure

[Install]
WantedBy=default.target

# Enable: systemctl --user enable agentremote-{name}
# Start:  systemctl --user start agentremote-{name}
"""


def xml_escape(value: object) -> str:
    text = str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
