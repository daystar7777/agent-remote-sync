from __future__ import annotations

import json
import re
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .common import AgentRemoteSyncError, read_json_body, send_error, send_json
from .connections import iter_connections
from .handoff import list_handoffs, read_handoff
from .inbox import list_instructions
from .registry import (
    list_instances,
    mark_instance_stopped,
    register_instance,
    start_heartbeat,
    stop_instance,
)
from .state import list_transfer_events, list_transfer_sessions
from .workmem import is_installed


DEFAULT_DASHBOARD_PORT = 7190


class DashboardState:
    def __init__(self, port: int):
        self.port = port
        self.instance_id = ""


class AgentRemoteSyncDashboardServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], state: DashboardState):
        super().__init__(server_address, DashboardHandler)
        self.state = state
        self.daemon_threads = True


class DashboardHandler(BaseHTTPRequestHandler):
    server: AgentRemoteSyncDashboardServer

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self.serve_index()
            elif parsed.path == "/api/status":
                send_json(self, 200, build_dashboard_status(dashboard_id=self.server.state.instance_id))
            elif parsed.path.startswith("/api/instances/") and parsed.path.endswith("/detail"):
                instance_id = parsed.path.removeprefix("/api/instances/").removesuffix("/detail")
                send_json(self, 200, build_instance_detail(instance_id))
            else:
                raise AgentRemoteSyncError(404, "not_found", "Endpoint not found")
        except Exception as exc:
            send_error(self, exc)

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            payload = read_json_body(self)
            if parsed.path.startswith("/api/instances/") and parsed.path.endswith("/stop"):
                instance_id = parsed.path.removeprefix("/api/instances/").removesuffix("/stop")
                send_json(self, 200, stop_instance(instance_id, confirm=bool(payload.get("confirm"))))
            else:
                raise AgentRemoteSyncError(404, "not_found", "Endpoint not found")
        except Exception as exc:
            send_error(self, exc)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def serve_index(self) -> None:
        data = files("agent_remote_sync.web").joinpath("dashboard.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def run_dashboard(port: int = DEFAULT_DASHBOARD_PORT, *, open_browser: bool = True) -> None:
    state = DashboardState(port)
    server = bind_dashboard_server(state, port)
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}"
    instance = register_instance(
        "dashboard",
        root=Path.cwd(),
        port=actual_port,
        url=url,
        name="agent-remote-sync dashboard",
    )
    state.instance_id = str(instance["id"])
    heartbeat = start_heartbeat(instance["id"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print()
    print("agent-remote-sync Dashboard")
    print("===========================")
    print(f"UI: {url}")
    if open_browser:
        webbrowser.open(url)
    print("Commands: [q] stop")
    try:
        while True:
            try:
                command = input("agent-remote-sync-dashboard> ").strip().lower()
            except EOFError:
                while True:
                    time.sleep(3600)
            if command in ("q", "quit", "exit"):
                break
            if command:
                print(f"UI: {url}")
    except KeyboardInterrupt:
        print()
    finally:
        heartbeat.set()
        mark_instance_stopped(instance["id"])
        server.shutdown()
        server.server_close()


def build_dashboard_status(*, dashboard_id: str = "") -> dict[str, Any]:
    instances = list_instances(include_stopped=False)
    projects = build_project_summaries(instances)
    return {
        "generatedAt": time.time(),
        "dashboardId": dashboard_id,
        "connections": public_connections(),
        "instances": instances,
        "channels": build_channels(instances),
        "projects": projects,
    }


def build_instance_detail(instance_id: str) -> dict[str, Any]:
    instances = list_instances(include_stopped=True)
    for item in instances:
        if item.get("id") == instance_id:
            return {"instance": item, "project": project_summary(Path(str(item.get("root", "."))))}
    raise AgentRemoteSyncError(404, "instance_not_found", "agent-remote-sync instance was not found")


def public_connections() -> list[dict[str, Any]]:
    public = []
    for entry in iter_connections():
        item = {key: value for key, value in entry.items() if key != "token"}
        item["hasToken"] = bool(entry.get("token"))
        public.append(item)
    return public


def build_channels(instances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    channels = []
    for connection in public_connections():
        alias = connection.get("name", "")
        remote = f"http://{connection.get('host')}:{connection.get('port')}"
        masters = [
            item
            for item in instances
            if item.get("role") == "master"
            and (item.get("alias") == alias or item.get("remote") == remote)
        ]
        slaves = [
            item
            for item in instances
            if item.get("role") == "slave"
            and int(item.get("port") or 0) == int(connection.get("port") or 0)
        ]
        channels.append(
            {
                "alias": alias,
                "name": connection.get("rawName") or alias,
                "host": connection.get("host"),
                "port": connection.get("port"),
                "remote": remote,
                "status": "connected" if masters or slaves else "saved",
                "masters": [item.get("id") for item in masters],
                "slaves": [item.get("id") for item in slaves],
                "updatedAt": connection.get("updatedAt"),
            }
        )
    return channels


def build_project_summaries(instances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    roots = []
    seen = set()
    for item in instances:
        root = str(item.get("root", ""))
        if root and root not in seen:
            roots.append(Path(root))
            seen.add(root)
    return [project_summary(root) for root in roots]


def project_summary(root: Path) -> dict[str, Any]:
    root = root.resolve()
    transfers = safe_transfer_sessions(root)
    handoffs = safe_handoffs(root)
    instructions = safe_instructions(root)
    return {
        "root": str(root),
        "name": root.name or str(root),
        "workMem": is_installed(root),
        "transfers": transfers,
        "handoffs": handoffs,
        "instructions": instructions,
        "latestTransfer": transfers[0] if transfers else None,
        "latestHandoff": handoffs[0] if handoffs else None,
        "latestInstruction": instructions[0] if instructions else None,
    }


def safe_transfer_sessions(root: Path) -> list[dict[str, Any]]:
    try:
        sessions = list_transfer_sessions(root, limit=8)
    except Exception:
        return []
    for session in sessions:
        session["events"] = list_transfer_events(root, session_id=str(session.get("id", "")), limit=12)
    return sessions


def safe_handoffs(root: Path) -> list[dict[str, Any]]:
    if not is_installed(root):
        return []
    items = []
    try:
        handoffs = list_handoffs(root)
    except Exception:
        return []
    for item in handoffs[:12]:
        try:
            text = read_handoff(root, item["filename"])
        except Exception:
            text = ""
        item = dict(item)
        item.update(parse_handoff_metadata(text))
        items.append(item)
    return items


def safe_instructions(root: Path) -> list[dict[str, Any]]:
    try:
        return list_instructions(root)[:12]
    except Exception:
        return []


def parse_handoff_metadata(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    title = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    if title:
        data["title"] = title.group(1).strip()
    for key in ("handoffId", "parentId", "direction", "autoRun", "executorModel", "callbackAlias"):
        match = re.search(rf"- {key}:\s+`([^`]*)`", text)
        if match:
            data[key] = match.group(1)
    type_match = re.search(r"^\*\*Type\*\*:\s+(.+)$", text, re.MULTILINE)
    if type_match:
        data["type"] = type_match.group(1).strip()
    content = re.search(r"## Content\s+(.+?)\s+Related paths:", text, re.DOTALL)
    if content:
        data["summary"] = " ".join(content.group(1).strip().split())[:220]
    return data


def bind_dashboard_server(state: DashboardState, start_port: int) -> AgentRemoteSyncDashboardServer:
    last_error: OSError | None = None
    for port in range(start_port, start_port + 50):
        try:
            return AgentRemoteSyncDashboardServer(("127.0.0.1", port), state)
        except OSError as exc:
            last_error = exc
    raise SystemExit(f"Could not bind a dashboard port: {last_error}")
