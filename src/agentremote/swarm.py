from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse
from urllib.request import Request

from .common import AgentRemoteError, make_token
from .connections import config_home, iter_connections, normalize_alias
from .daemon_profiles import load_daemon_profiles, summarize_daemon_profiles
from .state import STATE_DIR_NAME
from .tls import is_https_endpoint, normalize_fingerprint, open_url


SWARM_STATE_VERSION = 1
TAILSCALE_CIDRS = ("100.64.0.0/10", "fd7a:115c:a1e0::/48")
MOBILE_TOKEN_SCOPES = ("read", "transfer", "handoff", "process-control", "policy-control")
MOBILE_PAIRING_VERSION = 1


def swarm_path() -> Path:
    return config_home() / "swarm.json"


def empty_swarm_state() -> dict[str, Any]:
    return {
        "version": SWARM_STATE_VERSION,
        "whitelist": {},
        "routes": {},
        "routeHealth": {},
        "nodes": {},
    }


def load_swarm_state() -> dict[str, Any]:
    path = swarm_path()
    if not path.exists():
        return empty_swarm_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return empty_swarm_state()
    if not isinstance(data, dict):
        return empty_swarm_state()
    if not isinstance(data.get("whitelist"), dict):
        data["whitelist"] = {}
    if not isinstance(data.get("routes"), dict):
        data["routes"] = {}
    if not isinstance(data.get("routeHealth"), dict):
        data["routeHealth"] = {}
    if not isinstance(data.get("nodes"), dict):
        data["nodes"] = {}
    data["version"] = int(data.get("version", SWARM_STATE_VERSION) or SWARM_STATE_VERSION)
    return data


def save_swarm_state(data: dict[str, Any]) -> None:
    path = swarm_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": SWARM_STATE_VERSION,
        "whitelist": data.get("whitelist", {}),
        "routes": data.get("routes", {}),
        "routeHealth": data.get("routeHealth", {}),
        "nodes": data.get("nodes", {}),
    }
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def normalize_node_name(node: str) -> str:
    text = str(node).strip()
    if not text:
        return normalize_alias("")
    if text.startswith("::"):
        return normalize_alias(text)
    if "://" in text or "." in text or ":" in text or "/" in text or "\\" in text:
        return text
    return normalize_alias(text)


def set_whitelist(node: str, allowed: bool, *, note: str = "") -> dict[str, Any]:
    state = load_swarm_state()
    name = normalize_node_name(node)
    kind = "cidr" if is_cidr(name) else ("alias" if name.startswith("::") else "host")
    now = time.time()
    current = state["whitelist"].get(name, {})
    entry = {
        "name": name,
        "kind": kind,
        "allowed": bool(allowed),
        "note": note or current.get("note", ""),
        "createdAt": current.get("createdAt", now),
        "updatedAt": now,
    }
    state["whitelist"][name] = entry
    save_swarm_state(state)
    return entry


def set_tailscale_whitelist(*, note: str = "") -> list[dict[str, Any]]:
    label = note or "Tailscale tailnet address range"
    return [set_whitelist(cidr, True, note=label) for cidr in TAILSCALE_CIDRS]


def remove_whitelist(node: str) -> bool:
    state = load_swarm_state()
    name = normalize_node_name(node)
    existed = name in state["whitelist"]
    state["whitelist"].pop(name, None)
    save_swarm_state(state)
    return existed


def remove_tailscale_whitelist() -> int:
    removed = 0
    for cidr in TAILSCALE_CIDRS:
        if remove_whitelist(cidr):
            removed += 1
    return removed


def whitelist_status(state: dict[str, Any], node: str) -> str:
    normalized = normalize_node_name(node)
    whitelist = state.get("whitelist", {})
    entry = whitelist.get(normalized)
    if entry:
        return "allowed" if entry.get("allowed") else "denied"
    ip = extract_ip_address(normalized)
    if ip is None:
        return "unlisted"
    matches: list[tuple[int, bool]] = []
    for candidate in whitelist.values():
        if not isinstance(candidate, dict):
            continue
        network_text = str(candidate.get("name", ""))
        if not is_cidr(network_text):
            continue
        try:
            network = ip_network(network_text, strict=False)
        except ValueError:
            continue
        if ip.version == network.version and ip in network:
            matches.append((int(network.prefixlen), bool(candidate.get("allowed"))))
    if not matches:
        return "unlisted"
    matches.sort(key=lambda item: item[0], reverse=True)
    return "allowed" if matches[0][1] else "denied"


def is_cidr(value: str) -> bool:
    text = str(value).strip()
    if "/" not in text:
        return False
    try:
        ip_network(text, strict=False)
    except ValueError:
        return False
    return True


def extract_ip_address(value: str):
    text = str(value).strip()
    if not text:
        return None
    candidates = [text]
    if text.startswith("unknown:"):
        candidates.append(text.split(":", 1)[1])
    if "://" in text:
        parsed = urlparse(text)
        if parsed.hostname:
            candidates.append(parsed.hostname)
    if text.startswith("[") and "]" in text:
        candidates.append(text[1:text.index("]")])
    if text.count(":") == 1:
        host, maybe_port = text.rsplit(":", 1)
        if maybe_port.isdigit():
            candidates.append(host)
    for candidate in candidates:
        cleaned = candidate.strip().strip("[]")
        try:
            return ip_address(cleaned)
        except ValueError:
            continue
    return None


def set_route(
    node: str,
    host: str,
    port: int,
    *,
    priority: int = 100,
    tls_fingerprint: str = "",
) -> dict[str, Any]:
    state = load_swarm_state()
    name = normalize_node_name(node)
    fingerprint = normalize_fingerprint(tls_fingerprint) if tls_fingerprint else ""
    now = time.time()
    routes = [dict(item) for item in state["routes"].get(name, []) if isinstance(item, dict)]
    current = next(
        (item for item in routes if item.get("host") == host and int(item.get("port", 0)) == int(port)),
        None,
    )
    entry = {
        "name": name,
        "host": host,
        "port": int(port),
        "priority": int(priority),
        "tlsFingerprint": fingerprint,
        "routeType": "direct",
        "createdAt": (current or {}).get("createdAt", now),
        "updatedAt": now,
    }
    routes = [
        item
        for item in routes
        if not (item.get("host") == host and int(item.get("port", 0)) == int(port))
    ]
    routes.append(entry)
    state["routes"][name] = sort_routes(routes)
    save_swarm_state(state)
    return entry


def remove_route(node: str, *, host: str = "", port: int | None = None) -> int:
    state = load_swarm_state()
    name = normalize_node_name(node)
    routes = [dict(item) for item in state["routes"].get(name, []) if isinstance(item, dict)]
    if not routes:
        return 0
    if not host and port is None:
        removed = len(routes)
        state["routes"].pop(name, None)
        save_swarm_state(state)
        return removed
    kept = []
    removed = 0
    for route in routes:
        host_matches = not host or route.get("host") == host
        port_matches = port is None or int(route.get("port", 0)) == int(port)
        if host_matches and port_matches:
            removed += 1
        else:
            kept.append(route)
    if kept:
        state["routes"][name] = sort_routes(kept)
    else:
        state["routes"].pop(name, None)
    save_swarm_state(state)
    return removed


def sort_routes(routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        routes,
        key=lambda item: (
            int(item.get("priority", 100)),
            str(item.get("name", "")),
            str(item.get("host", "")),
            int(item.get("port", 0)),
        ),
    )


def explicit_route_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for node, routes in state.get("routes", {}).items():
        if not isinstance(routes, list):
            continue
        for route in routes:
            if not isinstance(route, dict):
                continue
            row = dict(route)
            row.setdefault("name", node)
            row.setdefault("priority", 100)
            row.update(route_health(state, node, row.get("host", ""), int(row.get("port", 0))))
            row["source"] = "explicit"
            rows.append(row)
    return sort_routes(rows)


def saved_route_rows(state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    state = state or load_swarm_state()
    rows = []
    for entry in iter_connections():
        row = {
            "name": entry["name"],
            "host": entry["host"],
            "port": int(entry["port"]),
            "priority": 1000,
            "tlsFingerprint": entry.get("tlsFingerprint", ""),
            "tlsInsecure": bool(entry.get("tlsInsecure", False)),
            "caFile": entry.get("caFile", ""),
            "routeType": "direct",
            "source": "saved",
        }
        row.update(route_health(state, row["name"], row["host"], int(row["port"])))
        rows.append(row)
    return sort_routes(rows)


def merged_route_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    seen = set()
    for row in explicit_route_rows(state) + saved_route_rows(state):
        key = (row.get("name"), row.get("host"), int(row.get("port", 0)))
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return sort_routes(rows)


def topology_nodes(state: dict[str, Any]) -> list[str]:
    nodes = set(state.get("whitelist", {}).keys())
    nodes.update(state.get("routes", {}).keys())
    nodes.update(state.get("nodes", {}).keys())
    nodes.update(entry["name"] for entry in iter_connections())
    return sorted(nodes)

def route_key(host: str, port: int) -> str:
    return f"{host}\t{int(port)}"


def route_health(state: dict[str, Any], node: str, host: str, port: int) -> dict[str, Any]:
    name = normalize_node_name(node)
    health_by_node = state.get("routeHealth", {}).get(name, {})
    if not isinstance(health_by_node, dict):
        return {}
    value = health_by_node.get(route_key(str(host), int(port)), {})
    return dict(value) if isinstance(value, dict) else {}


def probe_url(host: str, port: int, *, secure: bool) -> str:
    text = str(host).strip().rstrip("/")
    if "://" in text:
        parsed = urlparse(text)
        scheme = parsed.scheme or ("https" if secure else "http")
        netloc = parsed.netloc
        if parsed.hostname and parsed.port is None:
            hostname = parsed.hostname
            if ":" in hostname and not hostname.startswith("["):
                hostname = f"[{hostname}]"
            netloc = f"{hostname}:{int(port)}"
        return urlunparse((scheme, netloc, "/api/challenge", "", "", ""))
    hostname = text
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    scheme = "https" if secure else "http"
    return f"{scheme}://{hostname}:{int(port)}/api/challenge"


def probe_route(
    node: str,
    host: str,
    port: int,
    *,
    tls_fingerprint: str = "",
    tls_insecure: bool = False,
    ca_file: str = "",
    timeout: float = 3.0,
) -> dict[str, Any]:
    _ = node

    result = {
        "host": host,
        "port": int(port),
        "lastCheckedAt": time.time(),
    }
    secure = is_https_endpoint(str(host)) or bool(tls_fingerprint or tls_insecure or ca_file)
    url = probe_url(host, port, secure=secure)
    try:
        started = time.time()
        request = Request(url, method="GET")
        with open_url(
            request,
            timeout=timeout,
            tls_fingerprint=tls_fingerprint,
            tls_insecure=tls_insecure,
            ca_file=ca_file,
        ) as resp:
            if resp.status == 200:
                latency = (time.time() - started) * 1000
                result["lastOkAt"] = time.time()
                result["lastLatencyMs"] = round(latency, 1)
                result["failureCount"] = 0
                result["lastError"] = ""
                return result
    except Exception as exc:
        result["lastError"] = str(exc)[:200]
        result["failureCount"] = 1
        return result
    result["lastError"] = "unexpected_response"
    result["failureCount"] = 1
    return result

def save_route_health(node: str, host: str, port: int, health: dict[str, Any]) -> None:
    state = load_swarm_state()
    name = normalize_node_name(node)
    key = route_key(str(host), int(port))
    previous = route_health(state, name, host, port)
    saved_health = {
        "lastCheckedAt": health.get("lastCheckedAt"),
        "lastOkAt": health.get("lastOkAt", 0),
        "lastLatencyMs": health.get("lastLatencyMs", 0),
        "lastError": health.get("lastError", ""),
        "failureCount": 0,
    }
    if saved_health["lastError"] or not saved_health["lastOkAt"]:
        saved_health["failureCount"] = int(previous.get("failureCount", 0)) + 1
    state.setdefault("routeHealth", {}).setdefault(name, {})[key] = saved_health
    routes = [dict(item) for item in state.get("routes", {}).get(name, []) if isinstance(item, dict)]
    for route in routes:
        if route.get("host") == host and int(route.get("port", 0)) == int(port):
            route.update(saved_health)
            break
    if routes:
        state["routes"][name] = sort_routes(routes)
    save_swarm_state(state)

def select_best_route(routes: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not routes:
        return None
    sorted_routes = sort_routes(routes)
    best_priority = int(sorted_routes[0].get("priority", 100))
    candidates = [route for route in sorted_routes if int(route.get("priority", 100)) == best_priority]
    healthy = [r for r in candidates if r.get("lastOkAt") and not r.get("lastError")]
    if healthy:
        return min(healthy, key=lambda r: (int(r.get("priority", 100)), r.get("lastLatencyMs", 999999)))
    recent_ok = [r for r in candidates if r.get("lastOkAt")]
    if recent_ok:
        return min(recent_ok, key=lambda r: (int(r.get("priority", 100)), r.get("lastLatencyMs", 999999)))
    return candidates[0]


def journal_swarm_event(root: Path, event_type: str, title: str, body: str) -> Path | None:
    try:
        from .workmem import is_installed
        if not is_installed(root):
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe_type = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in event_type.lower()).strip("-")
        swarm_dir = root.resolve() / "AIMemory" / "swarm" / "events"
        swarm_dir.mkdir(parents=True, exist_ok=True)
        path = swarm_dir / f"{stamp}-{time.time_ns()}-{safe_type or 'event'}.md"
        lines = [
            f"# {title}",
            "",
            f"- date: {stamp}",
            f"- type: {event_type}",
            "",
            body.rstrip(),
            "",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    except Exception:
        return None


def journal_route_probe(root: Path, node: str, host: str, port: int, health: dict[str, Any]) -> Path | None:
    if health.get("lastOkAt"):
        status = f"Status: OK\nLatency: {health.get('lastLatencyMs', '?')}ms"
    else:
        status = f"Status: FAIL\nError: {health.get('lastError', '?')}"
    body = (
        f"Node: {node}\n"
        f"Host: {host}:{int(port)}\n"
        f"{status}\n"
        f"Failures: {health.get('failureCount', 0)}"
    )
    return journal_swarm_event(root, "route-probe", f"Route probe: {node} {host}:{int(port)}", body)


def journal_policy_change(root: Path, node: str, action: str, note: str = "") -> Path | None:
    body = f"Node: {node}\nAction: {action}"
    if note:
        body += f"\nNote: {note}"
    return journal_swarm_event(root, f"policy-{action}", f"Policy {action}: {node}", body)


def journal_node_status(root: Path, node: str, record: dict[str, Any]) -> Path | None:
    try:
        from .workmem import is_installed

        if not is_installed(root):
            return None
        node_dir = root.resolve() / "AIMemory" / "swarm" / "nodes"
        node_dir.mkdir(parents=True, exist_ok=True)
        path = node_dir / f"{safe_slug(node, 'node')}.md"
        storage = record.get("storage", {}) if isinstance(record.get("storage"), dict) else {}
        capabilities = record.get("capabilities", [])
        if not isinstance(capabilities, list):
            capabilities = []
        lines = [
            f"# Node {node}",
            "",
            f"- node: `{node}`",
            f"- status: {record.get('lastStatus', 'unknown')}",
            f"- lastSeenAt: {format_unix_time(record.get('lastSeenAt'))}",
            f"- modelId: `{record.get('modelId', '')}`",
            f"- policy: `{record.get('policy', '')}`",
            f"- rootLabel: `{record.get('rootLabel', '')}`",
            f"- storageFreeBytes: {storage.get('freeBytes', '')}",
            f"- storageTotalBytes: {storage.get('totalBytes', '')}",
            f"- capabilities: {', '.join(str(item) for item in capabilities)}",
            "",
            "This file is generated from `agentremote nodes status` and intentionally omits credentials.",
            "",
        ]
        if record.get("lastError"):
            lines.insert(-2, f"- lastError: `{record.get('lastError')}`")
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    except Exception:
        return None


def journal_routes_summary(root: Path, state: dict[str, Any]) -> Path | None:
    try:
        from .workmem import is_installed

        if not is_installed(root):
            return None
        swarm_dir = root.resolve() / "AIMemory" / "swarm"
        swarm_dir.mkdir(parents=True, exist_ok=True)
        path = swarm_dir / "routes.md"
        routes = merged_route_rows(state)
        nodes = topology_nodes(state)
        lines = [
            "# Swarm Routes",
            "",
            f"- updatedAt: {format_unix_time(time.time())}",
            f"- nodes: {len(nodes)}",
            "",
        ]
        if not routes:
            lines.append("No known routes.")
        else:
            for route in routes:
                health = route_health_summary_for_journal(route)
                lines.append(
                    f"- `{route.get('name')}` -> `{route.get('host')}:{int(route.get('port', 0))}` "
                    f"priority={int(route.get('priority', 100))} source={route.get('source', 'explicit')} {health}".rstrip()
                )
        lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    except Exception:
        return None


def journal_call_record(root: Path, record: dict[str, Any]) -> Path | None:
    try:
        from .workmem import is_installed

        if not is_installed(root):
            return None
        calls_dir = root.resolve() / "AIMemory" / "swarm" / "calls"
        calls_dir.mkdir(parents=True, exist_ok=True)
        call_id = str(record.get("callId") or f"call-{time.time_ns()}")
        path = calls_dir / f"{safe_file_stem(call_id, 'call')}.md"
        paths = record.get("paths", [])
        if not isinstance(paths, list):
            paths = []
        lines = [
            f"# Call {call_id}",
            "",
            f"- callId: `{call_id}`",
            f"- targetNode: `{record.get('targetNode', '')}`",
            f"- state: {record.get('state', '')}",
            f"- sentAt: {format_unix_time(record.get('sentAt'))}",
            f"- reportedAt: {format_unix_time(record.get('reportedAt'))}",
            f"- instructionId: `{record.get('instructionId', '')}`",
            f"- handoffId: `{record.get('handoffId', '')}`",
            "",
            "## Paths",
            "",
        ]
        if paths:
            lines.extend(f"- `{item}`" for item in paths)
        else:
            lines.append("- none")
        lines.extend(
            [
                "",
                "This file is generated from the local call record and intentionally omits credentials.",
                "",
            ]
        )
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    except Exception:
        return None


def safe_slug(value: str, fallback: str) -> str:
    text = str(value or "").strip()
    if text.startswith("::"):
        text = text[2:]
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in text.lower())
    slug = "-".join(part for part in cleaned.split("-") if part)
    return slug or fallback


def safe_file_stem(value: str, fallback: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(value or "").strip())
    cleaned = cleaned.strip(".")
    return cleaned or fallback


def format_unix_time(value: Any) -> str:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return ""
    if timestamp <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def route_health_summary_for_journal(route: dict[str, Any]) -> str:
    if route.get("lastOkAt"):
        latency = route.get("lastLatencyMs")
        return f"ok={latency}ms" if latency else "ok"
    if route.get("lastCheckedAt"):
        failures = int(route.get("failureCount", 0) or 0)
        error = str(route.get("lastError", "") or "")
        return f"failures={failures} error={error[:80]}".strip()
    return ""

def get_dashboard_data(root: Path | None = None) -> dict[str, Any]:
    state = load_swarm_state()
    project_root = (root or Path.cwd()).resolve()
    connections = {c["name"]: c for c in iter_connections()}
    routes = merged_route_rows(state)
    nodes_status = state.get("nodes", {})
    recent_calls_raw = read_dashboard_call_records(project_root, limit=20)
    recent_calls = [sanitize_call_record(record) for record in recent_calls_raw]
    pending_approvals = read_dashboard_approval_records(project_root, status="pending", limit=20)
    processes = list_process_registry(project_root)
    daemon_profiles = summarize_daemon_profiles(load_daemon_profiles(root=project_root), processes)

    nodes = []
    for node in dashboard_node_names(state, recent_calls, pending_approvals):
        conn = connections.get(node)
        status_entry = nodes_status.get(node, {})

        route_info = None
        node_routes = [row for row in routes if row.get("name") == node]
        best = select_best_route(node_routes) if node_routes else None
        if best:
            route_info = {
                "host": best.get("host", ""),
                "port": int(best.get("port", 0)),
                "priority": int(best.get("priority", 100)),
                "tls": bool(best.get("tlsFingerprint")),
                "selected": True,
                "source": best.get("source", "explicit"),
                "lastLatencyMs": best.get("lastLatencyMs"),
                "lastError": best.get("lastError"),
            }

        policy_state = whitelist_status(state, node)
        latest_call = latest_call_for_dashboard_node(recent_calls, node)
        approval_count = approval_count_for_dashboard_node(pending_approvals, node)

        nodes.append({
            "name": node,
            "status": status_entry.get("lastStatus", "unknown"),
            "modelId": status_entry.get("modelId", conn.get("modelId", "") if conn else ""),
            "lastSeenAt": status_entry.get("lastSeenAt"),
            "storage": status_entry.get("storage", {}),
            "route": route_info,
            "policy": policy_state,
            "capabilities": status_entry.get("capabilities", []),
            "latestCall": latest_call,
            "pendingApprovals": approval_count,
        })

    return {
        "nodes": nodes,
        "recentCalls": recent_calls,
        "pendingApprovals": pending_approvals,
        "processes": processes,
        "daemonProfiles": daemon_profiles,
        "summaries": {
            "nodes": summarize_dashboard_nodes(nodes),
            "calls": summarize_dashboard_calls(recent_calls),
            "processes": summarize_dashboard_processes(processes),
            "approvals": summarize_dashboard_approvals(pending_approvals),
            "profiles": summarize_dashboard_profiles(daemon_profiles),
        },
        "connectionCount": len(connections),
        "activeSessions": sum(1 for n in nodes_status.values() if n.get("lastStatus") == "online"),
    }


def dashboard_node_names(
    state: dict[str, Any],
    calls: list[dict[str, Any]],
    approvals: list[dict[str, Any]],
) -> list[str]:
    names = {normalize_node_name(node) for node in topology_nodes(state)}
    for call in calls:
        target = normalize_node_name(str(call.get("targetNode", "")))
        if target:
            names.add(target)
    for approval in approvals:
        origin = normalize_node_name(str(approval.get("originNode", "")))
        target = normalize_node_name(str(approval.get("targetNode", "")))
        if origin:
            names.add(origin)
        if target:
            names.add(target)
    return sorted(name for name in names if name)


def read_dashboard_call_records(root: Path, *, limit: int = 20) -> list[dict[str, Any]]:
    calls_dir = root.resolve() / STATE_DIR_NAME / "calls"
    if not calls_dir.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(calls_dir.glob("call-*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            records.append(data)
        if len(records) >= limit:
            break
    return records


def read_dashboard_approval_records(
    root: Path,
    *,
    status: str = "",
    limit: int = 20,
) -> list[dict[str, Any]]:
    base = root.resolve() / STATE_DIR_NAME / "approvals"
    if not base.exists():
        return []
    now = time.time()
    records: list[dict[str, Any]] = []
    for path in sorted(base.glob("approval-*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        visible_status = dashboard_approval_status(data, now=now)
        if status and visible_status != status:
            continue
        sanitized = sanitize_dashboard_approval(data)
        sanitized["status"] = visible_status
        records.append(sanitized)
        if len(records) >= limit:
            break
    return records


def dashboard_approval_status(record: dict[str, Any], *, now: float | None = None) -> str:
    status = str(record.get("status", "pending") or "pending")
    if status == "pending":
        try:
            expires_at = float(record.get("expiresAt", 0) or 0)
        except (TypeError, ValueError):
            expires_at = 0
        current = time.time() if now is None else now
        if expires_at and expires_at < current:
            return "expired"
    return status


def sanitize_dashboard_approval(record: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "approvalId",
        "status",
        "createdAt",
        "expiresAt",
        "originType",
        "originNode",
        "targetNode",
        "risk",
        "summary",
        "requestedAction",
        "decidedAt",
        "decidedBy",
        "agentId",
        "modelId",
        "callId",
        "handoffId",
    )
    sanitized: dict[str, Any] = {}
    for key in allowed:
        if key in record:
            sanitized[key] = record[key]
    sanitized["approvalId"] = str(record.get("approvalId", ""))
    sanitized["status"] = str(record.get("status", "pending") or "pending")
    sanitized["risk"] = str(record.get("risk", "medium") or "medium")
    sanitized["summary"] = dashboard_safe_text(record.get("summary", ""), 500)
    sanitized["requestedAction"] = dashboard_safe_text(record.get("requestedAction", ""), 160)
    sanitized["originType"] = str(record.get("originType", "") or "")
    sanitized["originNode"] = str(record.get("originNode", "") or "")
    sanitized["targetNode"] = str(record.get("targetNode", "") or "")
    return sanitized


def dashboard_safe_text(value: object, limit: int) -> str:
    text = str(value or "")[:limit]
    lowered = text.lower()
    if any(fragment in lowered for fragment in PROCESS_SECRET_FRAGMENTS):
        return "[redacted]"
    return text


def sanitize_call_record(record: dict[str, Any]) -> dict[str, Any]:
    paths = record.get("paths", [])
    if not isinstance(paths, list):
        paths = []
    return {
        "callId": str(record.get("callId", "")),
        "targetNode": str(record.get("targetNode", "")),
        "instructionId": str(record.get("instructionId", "")),
        "handoffId": str(record.get("handoffId", "")),
        "paths": [str(item) for item in paths],
        "state": str(record.get("state", "")),
        "sentAt": record.get("sentAt"),
        "reportedAt": record.get("reportedAt"),
    }


def latest_call_for_dashboard_node(calls: list[dict[str, Any]], node: str) -> dict[str, Any] | None:
    normalized = normalize_node_name(node)
    matches = [
        call
        for call in calls
        if normalize_node_name(str(call.get("targetNode", ""))) == normalized
    ]
    if not matches:
        return None
    return max(matches, key=lambda call: float(call.get("reportedAt") or call.get("sentAt") or 0))


def approval_count_for_dashboard_node(approvals: list[dict[str, Any]], node: str) -> int:
    normalized = normalize_node_name(node)
    count = 0
    for approval in approvals:
        origin = normalize_node_name(str(approval.get("originNode", "")))
        target = normalize_node_name(str(approval.get("targetNode", "")))
        if origin == normalized or target == normalized:
            count += 1
    return count


def summarize_dashboard_nodes(nodes: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"online": 0, "offline": 0, "unknown": 0}
    for node in nodes:
        status = str(node.get("status", "") or "unknown")
        if status not in counts:
            status = "unknown"
        counts[status] += 1
    counts["total"] = len(nodes)
    return counts


def summarize_dashboard_calls(calls: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"sent": 0, "reported": 0, "failed": 0, "completed": 0, "other": 0}
    for call in calls:
        state = str(call.get("state", "") or "sent")
        if state in counts:
            counts[state] += 1
        else:
            counts["other"] += 1
    counts["pending"] = counts["sent"]
    counts["total"] = len(calls)
    return counts


def summarize_dashboard_processes(processes: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"running": 0, "stale": 0, "stopped": 0, "other": 0}
    for proc in processes:
        status = str(proc.get("status", "") or "stale")
        if status in counts:
            counts[status] += 1
        else:
            counts["other"] += 1
    counts["total"] = len(processes)
    return counts


def summarize_dashboard_approvals(approvals: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"pending": 0, "approved": 0, "denied": 0, "expired": 0, "other": 0}
    for approval in approvals:
        status = str(approval.get("status", "") or "pending")
        if status in counts:
            counts[status] += 1
        else:
            counts["other"] += 1
    counts["total"] = len(approvals)
    return counts


def summarize_dashboard_profiles(profiles: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"running": 0, "not-running": 0, "other": 0}
    for profile in profiles:
        status = str(profile.get("status", "") or "not-running")
        if status in counts:
            counts[status] += 1
        else:
            counts["other"] += 1
    counts["total"] = len(profiles)
    return counts


# --- Mobile Controller Pairing ---

def _mobile_devices_path(root: Path, *, create: bool = False) -> Path:
    d = root.resolve() / STATE_DIR_NAME
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d / "mobile_devices.json"


def _load_mobile_devices(root: Path) -> dict[str, Any]:
    path = _mobile_devices_path(root)
    if not path.exists():
        return {"version": MOBILE_PAIRING_VERSION, "devices": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": MOBILE_PAIRING_VERSION, "devices": {}}
    if not isinstance(data, dict):
        return {"version": MOBILE_PAIRING_VERSION, "devices": {}}
    devices = data.get("devices", {})
    if not isinstance(devices, dict):
        devices = {}
    return {"version": MOBILE_PAIRING_VERSION, "devices": devices}


def _save_mobile_devices(root: Path, data: dict[str, Any]) -> None:
    path = _mobile_devices_path(root, create=True)
    payload = {
        "version": MOBILE_PAIRING_VERSION,
        "devices": data.get("devices", {}),
    }
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def normalize_mobile_scopes(scopes: list[str] | tuple[str, ...] | str | None) -> list[str]:
    if scopes is None or scopes == "":
        requested = ["read"]
    elif isinstance(scopes, str):
        requested = [item.strip() for item in scopes.split(",") if item.strip()]
    else:
        requested = [str(item).strip() for item in scopes if str(item).strip()]
    if not requested:
        requested = ["read"]
    unknown = [scope for scope in requested if scope not in MOBILE_TOKEN_SCOPES]
    if unknown:
        raise AgentRemoteError(400, "bad_mobile_scope", f"Unknown mobile scope: {', '.join(unknown)}")
    ordered = [scope for scope in MOBILE_TOKEN_SCOPES if scope in set(requested)]
    return ordered or ["read"]


def mobile_token_hash(token: str) -> str:
    return hashlib.sha256(f"agent-remote-sync-mobile:{token}".encode("utf-8")).hexdigest()


def sanitize_mobile_device(record: dict[str, Any]) -> dict[str, Any]:
    scopes = record.get("scopes", [])
    if not isinstance(scopes, list):
        scopes = []
    return {
        "id": str(record.get("id", "")),
        "name": str(record.get("name", "")),
        "scopes": [str(scope) for scope in scopes],
        "createdAt": record.get("createdAt"),
        "expiresAt": record.get("expiresAt"),
        "lastSeenAt": record.get("lastSeenAt"),
        "revokedAt": record.get("revokedAt"),
        "tokenPrefix": str(record.get("tokenPrefix", "")),
        "status": mobile_device_status(record),
    }


def mobile_device_status(record: dict[str, Any]) -> str:
    if record.get("revokedAt"):
        return "revoked"
    try:
        expires_at = float(record.get("expiresAt", 0) or 0)
    except (TypeError, ValueError):
        expires_at = 0
    if expires_at and expires_at < time.time():
        return "expired"
    return "active"


def list_mobile_devices(root: Path) -> list[dict[str, Any]]:
    data = _load_mobile_devices(root)
    records = [record for record in data.get("devices", {}).values() if isinstance(record, dict)]
    return [sanitize_mobile_device(record) for record in sorted(records, key=lambda item: item.get("createdAt", 0), reverse=True)]


def create_mobile_pairing(
    root: Path,
    name: str,
    *,
    ttl: int = 600,
    scopes: list[str] | tuple[str, ...] | str | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    label = str(name or "").strip() or "mobile"
    normalized_scopes = normalize_mobile_scopes(scopes)
    now = time.time()
    expires_at = now + max(60, int(ttl or 600))
    token = make_token()
    device_id = f"mobile-{safe_slug(label, 'device')}-{time.strftime('%Y%m%d%H%M%S')}-{safe_slug(make_token()[:8], 'token')}"
    record = {
        "id": device_id,
        "name": label,
        "scopes": normalized_scopes,
        "tokenHash": mobile_token_hash(token),
        "tokenPrefix": token[:8],
        "createdAt": now,
        "expiresAt": expires_at,
        "lastSeenAt": None,
        "revokedAt": None,
    }
    data = _load_mobile_devices(root)
    data.setdefault("devices", {})[device_id] = record
    _save_mobile_devices(root, data)
    journal_swarm_event(
        root,
        "MOBILE_DEVICE_PAIRED",
        "Mobile device paired",
        (
            f"Device: {device_id}\n"
            f"Name: {label}\n"
            f"Scopes: {', '.join(normalized_scopes)}\n"
            f"Expires: {format_unix_time(expires_at)}"
        ),
    )
    payload = {
        "type": "agent-remote-sync-mobile-pairing",
        "version": MOBILE_PAIRING_VERSION,
        "deviceId": device_id,
        "name": label,
        "token": token,
        "scopes": normalized_scopes,
        "expiresAt": expires_at,
    }
    return {
        "device": sanitize_mobile_device(record),
        "token": token,
        "payload": payload,
        "payloadText": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    }


def revoke_mobile_device(root: Path, device_id: str) -> dict[str, Any] | None:
    root = root.resolve()
    data = _load_mobile_devices(root)
    devices = data.setdefault("devices", {})
    record = devices.get(str(device_id))
    if not isinstance(record, dict):
        return None
    if not record.get("revokedAt"):
        record["revokedAt"] = time.time()
        _save_mobile_devices(root, data)
        journal_swarm_event(
            root,
            "MOBILE_DEVICE_REVOKED",
            "Mobile device revoked",
            f"Device: {record.get('id', device_id)}\nName: {record.get('name', '')}",
        )
    return sanitize_mobile_device(record)


def verify_mobile_token(root: Path, token: str, required_scope: str = "read") -> dict[str, Any]:
    token = str(token or "").strip()
    if not token:
        raise AgentRemoteError(401, "missing_mobile_token", "Mobile controller token is required")
    if required_scope and required_scope not in MOBILE_TOKEN_SCOPES:
        raise AgentRemoteError(500, "bad_required_scope", f"Unknown required mobile scope: {required_scope}")
    token_hash = mobile_token_hash(token)
    data = _load_mobile_devices(root)
    devices = data.setdefault("devices", {})
    for device_id, record in devices.items():
        if not isinstance(record, dict):
            continue
        if not hmac.compare_digest(str(record.get("tokenHash", "")), token_hash):
            continue
        status = mobile_device_status(record)
        if status == "revoked":
            raise AgentRemoteError(403, "mobile_token_revoked", "Mobile controller token was revoked")
        if status == "expired":
            raise AgentRemoteError(401, "mobile_token_expired", "Mobile controller token expired")
        scopes = normalize_mobile_scopes(record.get("scopes", []))
        if required_scope and required_scope not in scopes:
            raise AgentRemoteError(403, "mobile_scope_denied", f"Mobile token lacks required scope: {required_scope}")
        record["lastSeenAt"] = time.time()
        record["id"] = str(record.get("id", device_id))
        _save_mobile_devices(root, data)
        return sanitize_mobile_device(record)
    raise AgentRemoteError(401, "invalid_mobile_token", "Mobile controller token is invalid")


def get_mobile_controller_data(root: Path | None = None) -> dict[str, Any]:
    project_root = (root or Path.cwd()).resolve()
    dashboard = get_dashboard_data(project_root)
    return {
        "controller": {
            "apiVersion": MOBILE_PAIRING_VERSION,
            "projectRoot": str(project_root),
            "generatedAt": time.time(),
        },
        "topology": dashboard.get("nodes", []),
        "processes": dashboard.get("processes", []),
        "recentCalls": dashboard.get("recentCalls", []),
        "pendingApprovals": dashboard.get("pendingApprovals", []),
        "daemonProfiles": dashboard.get("daemonProfiles", []),
        "summaries": dashboard.get("summaries", {}),
        "connectionCount": dashboard.get("connectionCount", 0),
        "activeSessions": dashboard.get("activeSessions", 0),
    }

# --- Process Registry ---

PROCESS_SECRET_FRAGMENTS = ("password", "token", "secret", "credential", "proof", "key")
PROCESS_CORE_FIELDS = {
    "id",
    "role",
    "pid",
    "root",
    "host",
    "port",
    "uiUrl",
    "startedAt",
    "lastSeenAt",
    "status",
    "commandFingerprint",
    "extra",
}


def _process_registry_path(root: Path, *, create: bool = False) -> Path:
    d = root.resolve() / STATE_DIR_NAME
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d / "processes.json"


def register_process(
    root: Path,
    role: str,
    pid: int,
    host: str = "",
    port: int = 0,
    ui_url: str = "",
    extra: dict | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    path = _process_registry_path(root, create=True)
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    key = f"proc-{safe_slug(str(role), 'process')}-{int(pid)}-{time.time_ns()}"
    now = time.time()
    entry = {
        "role": str(role),
        "pid": int(pid),
        "root": str(root),
        "host": str(host or ""),
        "port": int(port or 0),
        "uiUrl": str(ui_url or ""),
        "startedAt": now,
        "lastSeenAt": now,
        "status": "running",
        "commandFingerprint": process_fingerprint(role, pid, root, host=host, port=port, ui_url=ui_url),
    }
    if extra:
        entry["extra"] = sanitize_process_extra(extra)
    data[key] = entry
    _atomic_save_processes(path, data)
    return sanitize_process_record(key, entry)

def update_process_heartbeat(root: Path, pid: int, *, process_id: str = "") -> None:
    path = _process_registry_path(root)
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    for key, v in data.items():
        if process_id and str(key) != str(process_id):
            continue
        if isinstance(v, dict) and process_pid(v) == int(pid):
            v["lastSeenAt"] = time.time()
            v["status"] = "running"
    _atomic_save_processes(path, data)

def list_process_registry(root: Path) -> list[dict]:
    path = _process_registry_path(root)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [sanitize_process_record(k, v) for k, v in data.items() if isinstance(v, dict)]

def get_process(root: Path, process_id: str) -> dict | None:
    path = _process_registry_path(root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    entry = data.get(process_id)
    if not isinstance(entry, dict):
        return None
    raw = dict(entry)
    raw["id"] = process_id
    raw["status"] = process_runtime_status(raw)
    return raw

def forget_process(root: Path, process_id: str) -> bool:
    path = _process_registry_path(root)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if process_id not in data:
        return False
    data.pop(process_id)
    _atomic_save_processes(path, data)
    return True

def _atomic_save_processes(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def sanitize_process_extra(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            lowered = text_key.lower()
            if text_key in PROCESS_CORE_FIELDS or any(fragment in lowered for fragment in PROCESS_SECRET_FRAGMENTS):
                continue
            result[text_key] = sanitize_process_extra(item)
        return result
    if isinstance(value, list):
        return [sanitize_process_extra(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def sanitize_process_record(process_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    pid = process_pid(entry)
    return {
        "id": str(process_id),
        "role": str(entry.get("role", "")),
        "pid": pid,
        "root": str(entry.get("root", "")),
        "host": str(entry.get("host", "")),
        "port": int(entry.get("port", 0) or 0),
        "uiUrl": str(entry.get("uiUrl", "")),
        "startedAt": entry.get("startedAt"),
        "lastSeenAt": entry.get("lastSeenAt"),
        "status": process_runtime_status(entry),
        "extra": sanitize_process_extra(entry.get("extra", {})),
    }


def process_pid(entry: dict[str, Any]) -> int:
    try:
        return int(entry.get("pid", 0) or 0)
    except (TypeError, ValueError):
        return 0


def process_fingerprint(
    role: str,
    pid: int,
    root: Path,
    *,
    host: str = "",
    port: int = 0,
    ui_url: str = "",
) -> str:
    material = {
        "role": str(role),
        "pid": int(pid),
        "root": str(root.resolve()),
        "host": str(host or ""),
        "port": int(port or 0),
        "uiUrl": str(ui_url or ""),
        "python": Path(sys.executable).name,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def process_stop_metadata_valid(root: Path, entry: dict[str, Any]) -> bool:
    role = str(entry.get("role", ""))
    pid = process_pid(entry)
    if not role or pid <= 0:
        return False
    try:
        entry_root = Path(str(entry.get("root", ""))).resolve()
    except OSError:
        return False
    if entry_root != root.resolve():
        return False
    expected = process_fingerprint(
        role,
        pid,
        entry_root,
        host=str(entry.get("host", "") or ""),
        port=int(entry.get("port", 0) or 0),
        ui_url=str(entry.get("uiUrl", "") or ""),
    )
    return str(entry.get("commandFingerprint", "")) == expected


def process_runtime_status(entry: dict[str, Any]) -> str:
    stored = str(entry.get("status", "") or "")
    if stored and stored != "running":
        return stored
    return "running" if process_is_running(process_pid(entry)) else "stale"


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True
                return int(code.value) == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
