from __future__ import annotations

import argparse
import platform
import sys
from pathlib import Path
from urllib.parse import urlparse

from . import __version__
from .bootstrap import format_summary, run_bootstrap
from .cleanup import cleanup_stale_partials
from .common import AgentFTPError
from .common import DEFAULT_PORT, DEFAULT_UI_PORT
from .connections import get_connection, iter_connections, normalize_alias, remove_connection, set_connection
from .headless import handoff as send_handoff
from .headless import pull, push, report, tell
from .inbox import claim_instruction, list_instructions, read_instruction
from .master import RemoteClient, run_master
from .slave import run_slave
from .sync import sync_plan_pull, sync_plan_push, sync_pull, sync_push, write_plan
from .tls import fetch_remote_fingerprint, format_fingerprint, is_https_endpoint, normalize_fingerprint
from .worker import run_worker_loop, run_worker_once
from .workmem import install_work_mem, is_installed, record_host_event, require_work_mem


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agentftp")
    parser.add_argument("--version", action="version", version=f"agentFTP {__version__}")
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
    slave.add_argument("--model-id", default="agentftp-slave", help="model/profile used by this slave agent")
    slave.add_argument(
        "--firewall",
        choices=["ask", "yes", "no"],
        default="ask",
        help="ask/open/skip local firewall rule for the slave port",
    )
    slave.add_argument("--max-concurrent", type=int, default=32, help="maximum concurrent requests")
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
    master.add_argument("--ui-port", type=int, default=DEFAULT_UI_PORT, help="local browser UI port")
    master.add_argument("--no-browser", action="store_true", help="print the UI URL without opening it")
    add_tls_client_args(master)

    push_parser = subcommands.add_parser("push", help="headless upload to a slave")
    push_parser.add_argument("host", help="slave host, IP, host:port, URL, or saved alias")
    push_parser.add_argument("local_path", help="local file or folder to upload")
    push_parser.add_argument("remote_dir", help="remote destination folder")
    push_parser.add_argument("--port", type=int, default=None, help="slave port")
    push_parser.add_argument("--password", help="slave password; omit to prompt")
    push_parser.add_argument("--overwrite", action="store_true", help="overwrite conflicts")
    add_tls_client_args(push_parser)

    pull_parser = subcommands.add_parser("pull", help="headless download from a slave")
    pull_parser.add_argument("host", help="slave host, IP, host:port, URL, or saved alias")
    pull_parser.add_argument("remote_path", help="remote file or folder to download")
    pull_parser.add_argument("local_dir", help="local destination folder")
    pull_parser.add_argument("--port", type=int, default=None, help="slave port")
    pull_parser.add_argument("--password", help="slave password; omit to prompt")
    pull_parser.add_argument("--overwrite", action="store_true", help="overwrite conflicts")
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
    add_tls_client_args(tell_parser)

    handoff_parser = subcommands.add_parser("handoff", help="push a file/folder and send an instruction")
    handoff_parser.add_argument("host", help="slave host, IP, host:port, URL, or saved alias")
    handoff_parser.add_argument("local_path", help="local file or folder to upload before sending the instruction")
    handoff_parser.add_argument("task", help="instruction text")
    handoff_parser.add_argument("--remote-dir", default="/incoming", help="remote destination folder")
    handoff_parser.add_argument("--port", type=int, default=None, help="slave port")
    handoff_parser.add_argument("--password", help="slave password; omit to prompt")
    handoff_parser.add_argument("--overwrite", action="store_true", help="overwrite remote conflicts")
    handoff_parser.add_argument("--from-name", default="", help="sender name for the manifest")
    handoff_parser.add_argument("--expect-report", default="", help="report requested from the receiver")
    handoff_parser.add_argument("--auto-run", action="store_true", help="mark instruction as eligible for receiver auto mode")
    handoff_parser.add_argument("--callback-alias", default="", help="receiver-side saved alias for sending a report back")
    add_tls_client_args(handoff_parser)

    report_parser = subcommands.add_parser("report", help="send a STATUS_REPORT handoff")
    report_parser.add_argument("host", help="slave host, IP, host:port, URL, or saved alias")
    report_parser.add_argument("parent_id", help="handoff id being answered")
    report_parser.add_argument("report", help="report text")
    report_parser.add_argument("--port", type=int, default=None, help="slave port")
    report_parser.add_argument("--password", help="slave password; omit to prompt")
    report_parser.add_argument("--from-name", default="", help="sender name for the manifest")
    report_parser.add_argument("--path", action="append", default=[], help="path related to the report")
    add_tls_client_args(report_parser)

    inbox = subcommands.add_parser("inbox", help="list or read local received instructions")
    inbox.add_argument("--root", default=".", help="slave root containing .agentftp_inbox")
    inbox.add_argument("--read", help="instruction id to print")
    inbox.add_argument("--claim", help="instruction id to claim for local worker execution")

    worker = subcommands.add_parser("worker", help="claim and optionally execute received autoRun handoffs")
    worker.add_argument("--root", default=".", help="project/slave root containing .agentftp_inbox")
    worker.add_argument("--once", action="store_true", help="process one instruction and exit")
    worker.add_argument("--instruction-id", default="", help="specific instruction id to process")
    worker.add_argument(
        "--execute",
        choices=["never", "ask", "yes"],
        default="never",
        help="show plan only, ask before running, or run explicit agentftp-run commands",
    )
    worker.add_argument("--include-manual", action="store_true", help="allow instructions without autoRun")
    worker.add_argument("--report-to", default="", help="override callback alias for the STATUS_REPORT")
    worker.add_argument("--from-name", default="agentftp-worker", help="sender name for worker reports")
    worker.add_argument("--timeout", type=int, default=600, help="per-command timeout in seconds")
    worker.add_argument("--interval", type=float, default=5.0, help="daemon polling interval in seconds")
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

    subcommands.add_parser("doctor", help="check local runtime")

    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            doctor()
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
        else:
            require_work_mem(command_root(args), prompt_install=True)
        if args.command == "slave":
            run_slave(
                Path(args.root),
                args.port,
                args.password,
                args.host,
                model_id=args.model_id,
                firewall=args.firewall,
                max_concurrent=args.max_concurrent,
                panic_on_flood=args.panic_on_flood,
                tls=args.tls,
                cert_file=Path(args.cert_file) if args.cert_file else None,
                key_file=Path(args.key_file) if args.key_file else None,
                verbose=args.verbose,
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
                summary=f"Saved agentFTP connection {entry['name']} -> {client.base_url}.",
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
            )
        elif args.command == "push":
            target = resolve_target(args.host, args.port, args.password, args)
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
            if args.sync_command == "plan":
                remote = RemoteClient(
                    target.host,
                    target.port,
                    target.password,
                    token=target.token,
                    tls_fingerprint=target.tls_fingerprint,
                    tls_insecure=target.tls_insecure,
                    ca_file=target.ca_file,
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
            send_handoff(
                target.host,
                target.port,
                target.password,
                Path(args.local_path),
                args.task,
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
        elif args.command == "report":
            target = resolve_target(args.host, args.port, args.password, args)
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
            if not args.once:
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
                    )
                )
    except AgentFTPError as exc:
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
    saved = get_connection(target)
    if saved and port is None:
        tls_kwargs = {
            "tls_fingerprint": explicit_tls.get("tls_fingerprint") or saved.get("tlsFingerprint", ""),
            "tls_insecure": bool(explicit_tls.get("tls_insecure", False) or saved.get("tlsInsecure", False)),
            "ca_file": explicit_tls.get("ca_file") or saved.get("caFile", ""),
        }
        if password is not None:
            return Target(saved["host"], int(saved["port"]), password=password, alias=saved["name"], **tls_kwargs)
        token = saved.get("token")
        if token:
            return Target(saved["host"], int(saved["port"]), token=token, alias=saved["name"], **tls_kwargs)
        return Target(saved["host"], int(saved["port"]), password=password_arg(None), alias=saved["name"], **tls_kwargs)
    host, resolved_port = split_host_port(target, port)
    return Target(
        host,
        resolved_port,
        password=password_arg(password),
        alias=normalize_alias(host),
        **explicit_tls,
    )


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
    try:
        return RemoteClient(host, port, password, scopes=scopes, **tls_kwargs), tls_kwargs
    except AgentFTPError as exc:
        if not should_offer_tls_trust(host, tls_kwargs, exc):
            raise
    if not sys.stdin.isatty():
        raise AgentFTPError(
            495,
            "tls_untrusted",
            "HTTPS certificate is not trusted. Re-run with --tls-fingerprint, --ca-file, or --tls-insecure.",
        )
    fingerprint = fetch_remote_fingerprint(host, port)
    print("The slave uses an untrusted HTTPS certificate.")
    print(f"SHA-256 fingerprint: {format_fingerprint(fingerprint)}")
    answer = input("Trust this certificate for this saved connection? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        raise AgentFTPError(495, "tls_untrusted", "TLS certificate was not trusted")
    tls_kwargs["tls_fingerprint"] = fingerprint
    return RemoteClient(host, port, password, scopes=scopes, **tls_kwargs), tls_kwargs


def parse_scopes(value: str) -> list[str] | None:
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def should_offer_tls_trust(host: str, tls_kwargs: dict, exc: AgentFTPError) -> bool:
    if not is_https_endpoint(host):
        return False
    if tls_kwargs.get("tls_fingerprint") or tls_kwargs.get("tls_insecure") or tls_kwargs.get("ca_file"):
        return False
    text = (exc.message or "").lower()
    return "certificate_verify_failed" in text or "certificate verify failed" in text


def doctor() -> None:
    print(f"agentFTP {__version__}")
    print(f"Python {platform.python_version()}")
    print(f"Platform {platform.platform()}")
    print(f"Executable {sys.executable}")
    if is_installed(Path.cwd()):
        print(f"agent-work-mem OK: {Path.cwd().resolve() / 'AIMemory'}")
    else:
        print("agent-work-mem MISSING in current project")
    try:
        import cryptography

        print(f"TLS self-signed support OK: cryptography {cryptography.__version__}")
    except ImportError:
        print("TLS self-signed support MISSING: install cryptography")
    print("Runtime OK")


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
    if args.command == "inbox":
        return Path(args.root)
    if args.command == "worker":
        return Path(args.root)
    if args.command == "cleanup":
        return Path(args.root)
    return Path.cwd()
