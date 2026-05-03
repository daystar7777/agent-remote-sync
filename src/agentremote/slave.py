from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .common import (
    AUTH_ITERATIONS,
    CHUNK_SIZE,
    DEFAULT_PORT,
    AgentRemoteError,
    MAX_DOWNLOAD_CHUNK,
    MAX_UPLOAD_CHUNK,
    b64,
    clean_rel_path,
    constant_time_equal,
    derive_key,
    detect_addresses,
    drain_request_body,
    file_info,
    list_dir,
    make_nonce,
    make_proof,
    make_salt,
    make_token,
    partial_paths,
    read_json_body,
    resolve_path,
    safe_name,
    send_error,
    send_json,
    sha256_file,
    stat_path,
    storage_info,
    tree_entries,
)
from .firewall import maybe_open_firewall
from .filenames import filename_policy
from .filenames import normalize_disk
from .inbox import create_instruction
from .security import SecurityConfig, SecurityState
from .tls import TLSFiles, certificate_fingerprint, ensure_self_signed_cert, format_fingerprint, wrap_server_socket


SESSION_SCOPES = {"read", "write", "delete", "handoff"}
DEFAULT_SESSION_SCOPES = sorted(SESSION_SCOPES)


class SlaveState:
    def __init__(
        self,
        root: Path,
        password: str,
        model_id: str = "agentremote-slave",
        security_config: SecurityConfig | None = None,
        quiet: bool = True,
        policy: str = "off",
        node_name: str = "",
    ):
        self.root = root.resolve()
        self.model_id = model_id
        self.quiet = quiet
        self.policy = policy
        self.node_name = node_name or self._default_node_name()
        self.started_at = time.time()
        self.security = SecurityState(security_config)
        self.salt = make_salt()
        self.iterations = AUTH_ITERATIONS
        self.password_key = derive_key(password, self.salt, self.iterations)
        self.nonces: dict[str, float] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.logs: list[str] = []
        self.lock = threading.Lock()

    def _default_node_name(self) -> str:
        try:
            return self.root.resolve().name or "agentremote-node"
        except Exception:
            return "agentremote-node"

    def node_info(self, *, authenticated: bool = False) -> dict[str, Any]:
        active = 0
        with self.lock:
            now = time.time()
            active = sum(1 for s in self.sessions.values() if float(s.get("expires", 0)) >= now)
        info = {
            "nodeName": self.node_name,
            "modelId": self.model_id,
            "policy": self.policy,
            "uptimeSeconds": round(time.time() - self.started_at),
            "startedAt": self.started_at,
            "storage": storage_info(self.root),
            "activeSessions": active,
            "capabilities": ["file-transfer", "handoff", "route-probe", "worker"],
        }
        if not authenticated:
            info.pop("activeSessions", None)
            root_info = info.get("storage", {})
            info["rootLabel"] = self.root.resolve().name or "root"
            info["storage"] = {
                "totalBytes": root_info.get("totalBytes", 0),
                "freeBytes": root_info.get("freeBytes", 0),
            }
        return info

    def log(self, message: str, *, important: bool = False) -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {message}"
        with self.lock:
            self.logs.append(line)
            self.logs[:] = self.logs[-200:]
        if important or not self.quiet:
            print(line)

    def challenge(self) -> dict[str, Any]:
        nonce = make_nonce()
        with self.lock:
            self.nonces[nonce] = time.time() + 120
        return {
            "nonce": nonce,
            "salt": b64(self.salt),
            "iterations": self.iterations,
            "algorithm": "PBKDF2-HMAC-SHA256+HMAC-SHA256",
        }

    def login(
        self,
        nonce: str,
        proof: str,
        client: str,
        scopes: list[str] | str | None = None,
        client_name: str = "",
        client_alias: str = "",
    ) -> dict[str, Any]:
        identity = client_alias or client_name or f"unknown:{client}"
        now = time.time()
        with self.lock:
            expires = self.nonces.pop(nonce, 0)
        if expires < now:
            raise AgentRemoteError(401, "bad_nonce", "Challenge expired or unknown")
        expected = make_proof(self.password_key, nonce)
        if not constant_time_equal(expected, proof):
            self.log(f"Rejected login from {client}")
            self.security.note_login_failure(client)
            raise AgentRemoteError(401, "bad_password", "Password proof rejected")
        self._check_client_policy(identity, client)
        token = make_token()
        granted_scopes = normalize_session_scopes(scopes)
        with self.lock:
            self.sessions[token] = {
                "expires": now + 12 * 60 * 60,
                "scopes": granted_scopes,
                "identity": identity,
                "clientIp": client,
            }
        self.log(f"Accepted master session from {client} scopes={','.join(granted_scopes)} identity={identity}")
        return {"token": token, "scopes": granted_scopes}

    def _check_client_policy(self, identity: str, client_ip: str) -> None:
        if self.policy == "off":
            return
        status = self._client_policy_status(identity, client_ip)
        if status == "denied":
            self.log(f"blocked denied client {identity} ({client_ip})", important=True)
            self._journal_policy_event(
                "slave-policy-denied",
                f"Slave policy denied: {identity}",
                f"Client: {identity}\nIP: {client_ip}\nPolicy: {self.policy}",
            )
            raise AgentRemoteError(403, "slave_policy_denied", f"Client {identity} is denied by slave whitelist policy")
        if status == "unlisted" and self.policy == "strict":
            self.log(f"blocked unlisted client {identity} ({client_ip}) under strict policy", important=True)
            self._journal_policy_event(
                "slave-policy-unlisted",
                f"Slave policy blocked unlisted client: {identity}",
                f"Client: {identity}\nIP: {client_ip}\nPolicy: strict",
            )
            raise AgentRemoteError(403, "slave_policy_unlisted", f"Client {identity} is not whitelisted and slave policy is strict. Use 'agentremote policy allow {identity}' on the slave.")
        if status == "unlisted" and self.policy == "warn":
            self.log(f"allowed unlisted client {identity} ({client_ip}) (warn mode)", important=True)
            self._journal_policy_event(
                "slave-policy-warn",
                f"Slave policy warned for unlisted client: {identity}",
                f"Client: {identity}\nIP: {client_ip}\nPolicy: warn",
            )

    def _client_policy_status(self, identity: str, client_ip: str = "") -> str:
        try:
            from .swarm import load_swarm_state, normalize_node_name, whitelist_status
            state = load_swarm_state()
            name = normalize_node_name(identity)
            status = whitelist_status(state, name)
            if status == "unlisted" and client_ip:
                return whitelist_status(state, client_ip)
            return status
        except ImportError:
            return "unlisted"

    def _journal_policy_event(self, event_type: str, title: str, body: str) -> None:
        try:
            from .swarm import journal_swarm_event
            journal_swarm_event(self.root, event_type, title, body)
        except ImportError:
            pass

    def _check_session_policy(self, identity: str, client_ip: str = "") -> None:
        if self.policy == "off" or not identity:
            return
        status = self._client_policy_status(identity, client_ip)
        if status == "denied":
            self.log(f"blocked denied session {identity}", important=True)
            self._journal_policy_event(
                "slave-policy-denied-session",
                f"Slave policy denied active session: {identity}",
                f"Client: {identity}\nPolicy: {self.policy}",
            )
            raise AgentRemoteError(403, "slave_policy_denied", f"Client {identity} is denied by slave whitelist policy")
        if status == "unlisted" and self.policy == "strict":
            self.log(f"blocked unlisted session {identity} under strict policy", important=True)
            self._journal_policy_event(
                "slave-policy-unlisted-session",
                f"Slave policy blocked active unlisted session: {identity}",
                f"Client: {identity}\nPolicy: strict",
            )
            raise AgentRemoteError(403, "slave_policy_unlisted", f"Client {identity} is not whitelisted and slave policy is strict.")

    def require_token(self, header_value: str | None, scope: str | None = None) -> None:
        if not header_value or not header_value.startswith("Bearer "):
            raise AgentRemoteError(401, "missing_token", "Missing bearer token")
        token = header_value.removeprefix("Bearer ").strip()
        now = time.time()
        with self.lock:
            session = self.sessions.get(token)
            if session and float(session.get("expires", 0)) >= now:
                identity = str(session.get("identity", ""))
                client_ip = str(session.get("clientIp", ""))
                granted = set(session.get("scopes", DEFAULT_SESSION_SCOPES))
            else:
                raise AgentRemoteError(401, "bad_token", "Session token is invalid or expired")
        self._check_session_policy(identity, client_ip)
        if scope and scope not in granted:
            raise AgentRemoteError(403, "scope_denied", f"Session does not allow {scope} operations")
        with self.lock:
            session = self.sessions.get(token)
            if session and float(session.get("expires", 0)) >= now:
                session["expires"] = now + 12 * 60 * 60
                return
        raise AgentRemoteError(401, "bad_token", "Session token is invalid or expired")


class AgentRemoteSlaveServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], state: SlaveState):
        super().__init__(server_address, SlaveHandler)
        self.state = state
        self.daemon_threads = True
        self.request_queue_size = 64


class SlaveHandler(BaseHTTPRequestHandler):
    server: AgentRemoteSlaveServer

    def setup(self) -> None:
        super().setup()
        self.request.settimeout(30)

    def run_guarded(self, handler: Any, *, authenticated: bool = False) -> None:
        ip = self.client_address[0]
        acquired = self.server.state.security.acquire_request()
        if not acquired:
            self.server.state.security.note_overload(ip)
            send_error(self, AgentRemoteError(503, "server_busy", "Server is busy"))
            return
        try:
            try:
                self.server.state.security.check_rate(
                    ip,
                    authenticated=authenticated,
                    transfer=is_transfer_endpoint(self.path),
                )
                handler()
            except Exception as exc:
                code = getattr(exc, "code", exc.__class__.__name__)
                message = getattr(exc, "message", str(exc))
                self.server.state.log(f"request error {code}: {message}", important=True)
                send_error(self, exc)
        finally:
            self.server.state.security.release_request()
            if self.server.state.security.flood_shutdown_requested:
                self.server.state.log("flood threshold reached; shutting down slave")
                threading.Thread(target=self.server.shutdown, daemon=True).start()

    def do_GET(self) -> None:
        self.run_guarded(self._do_GET, authenticated=self.path != "/api/challenge")

    def _do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path == "/api/challenge":
                send_json(self, 200, self.server.state.challenge())
                return
            if parsed.path == "/api/node":
                send_json(self, 200, self.server.state.node_info())
                return
            self.server.state.require_token(self.headers.get("Authorization"), "read")
            if parsed.path == "/api/list":
                send_json(self, 200, list_dir(self.server.state.root, first(query, "path", "/")))
            elif parsed.path == "/api/stat":
                self.handle_stat(query)
            elif parsed.path == "/api/tree":
                send_json(
                    self,
                    200,
                    {"entries": tree_entries(self.server.state.root, first(query, "path", "/"))},
                )
            elif parsed.path == "/api/storage":
                send_json(self, 200, storage_info(self.server.state.root))
            elif parsed.path == "/api/download":
                self.handle_download(query)
            else:
                raise AgentRemoteError(404, "not_found", "Endpoint not found")
        except Exception as exc:
            send_error(self, exc)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        self.run_guarded(self._do_POST, authenticated=parsed.path != "/api/login")

    def _do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/api/login":
                payload = read_json_body(self)
                session = self.server.state.login(
                    str(payload.get("nonce", "")),
                    str(payload.get("proof", "")),
                    self.client_address[0],
                    payload.get("scopes") if "scopes" in payload else None,
                    client_name=str(payload.get("clientName", "")),
                    client_alias=str(payload.get("clientAlias", "")),
                )
                send_json(
                    self,
                    200,
                    {
                        "token": session["token"],
                        "scopes": session["scopes"],
                        "root": str(self.server.state.root),
                        "slaveModel": self.server.state.model_id,
                        "executorModel": self.server.state.model_id,
                        "executionProfile": "slave-agent-default",
                        "filenameNormalization": filename_policy().__dict__,
                    },
                )
                return
            if parsed.path == "/api/mkdir":
                self.server.state.require_token(self.headers.get("Authorization"), "write")
                self.handle_mkdir(read_json_body(self))
            elif parsed.path == "/api/delete":
                self.server.state.require_token(self.headers.get("Authorization"), "delete")
                self.handle_delete(read_json_body(self))
            elif parsed.path == "/api/rename":
                self.server.state.require_token(self.headers.get("Authorization"), "write")
                self.handle_rename(read_json_body(self))
            elif parsed.path == "/api/move":
                self.server.state.require_token(self.headers.get("Authorization"), "write")
                self.handle_move(read_json_body(self))
            elif parsed.path == "/api/upload/status":
                self.server.state.require_token(self.headers.get("Authorization"), "write")
                self.handle_upload_status(read_json_body(self))
            elif parsed.path == "/api/upload/finish":
                self.server.state.require_token(self.headers.get("Authorization"), "write")
                self.handle_upload_finish(read_json_body(self))
            elif parsed.path == "/api/instructions":
                self.server.state.require_token(self.headers.get("Authorization"), "handoff")
                self.handle_instruction(read_json_body(self))
            else:
                raise AgentRemoteError(404, "not_found", "Endpoint not found")
        except Exception as exc:
            send_error(self, exc)

    def do_PUT(self) -> None:
        self.run_guarded(self._do_PUT, authenticated=True)

    def _do_PUT(self) -> None:
        try:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            self.server.state.require_token(self.headers.get("Authorization"), "write")
            if parsed.path == "/api/upload/chunk":
                self.handle_upload_chunk(query)
            else:
                raise AgentRemoteError(404, "not_found", "Endpoint not found")
        except Exception as exc:
            send_error(self, exc)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def handle_stat(self, query: dict[str, list[str]]) -> None:
        try:
            send_json(self, 200, stat_path(self.server.state.root, first(query, "path", "/")))
        except FileNotFoundError:
            send_json(self, 200, {"exists": False})

    def handle_download(self, query: dict[str, list[str]]) -> None:
        target = resolve_path(self.server.state.root, first(query, "path", "/"))
        if not target.is_file():
            raise AgentRemoteError(400, "not_file", "Only files can be downloaded")
        size = target.stat().st_size
        offset = parse_int(first(query, "offset", "0"), "offset")
        length = parse_int(first(query, "length", str(size)), "length")
        if length > MAX_DOWNLOAD_CHUNK:
            raise AgentRemoteError(413, "download_chunk_too_large", "Download chunk is too large")
        if offset < 0 or length < 0 or offset > size:
            raise AgentRemoteError(416, "bad_range", "Requested byte range is invalid")
        length = min(length, size - offset)
        with target.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(length)
        self.send_response(206)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-AgentRemote-Size", str(size))
        self.send_header("X-AgentRemote-Mtime", str(target.stat().st_mtime))
        self.end_headers()
        self.wfile.write(data)

    def handle_mkdir(self, payload: dict[str, Any]) -> None:
        if "path" in payload:
            target = resolve_path(self.server.state.root, str(payload["path"]), allow_missing=True)
        else:
            parent = resolve_path(self.server.state.root, str(payload.get("parent", "/")))
            target = parent / normalize_disk(safe_name(str(payload.get("name", ""))))
        if target.exists() and not target.is_dir():
            raise AgentRemoteError(409, "exists", "A non-directory already exists there")
        target.mkdir(parents=True, exist_ok=True)
        self.server.state.log(f"mkdir {clean_rel_path(str(payload.get('path') or target.name))}")
        send_json(self, 200, {"ok": True, "entry": file_info(self.server.state.root, target)})

    def handle_delete(self, payload: dict[str, Any]) -> None:
        path_text = str(payload.get("path", ""))
        if clean_rel_path(path_text) == "/":
            raise AgentRemoteError(400, "root_delete", "The root folder cannot be deleted")
        target = resolve_path(self.server.state.root, path_text)
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
        self.server.state.log(f"delete {path_text}")
        send_json(self, 200, {"ok": True})

    def handle_rename(self, payload: dict[str, Any]) -> None:
        path_text = str(payload.get("path", ""))
        if clean_rel_path(path_text) == "/":
            raise AgentRemoteError(400, "root_rename", "The root folder cannot be renamed")
        target = resolve_path(self.server.state.root, path_text)
        new_name = normalize_disk(safe_name(str(payload.get("newName", ""))))
        destination = target.with_name(new_name)
        if destination.exists():
            raise AgentRemoteError(409, "exists", "Destination already exists")
        target.rename(destination)
        self.server.state.log(f"rename {path_text} -> {new_name}")
        send_json(self, 200, {"ok": True, "entry": file_info(self.server.state.root, destination)})

    def handle_move(self, payload: dict[str, Any]) -> None:
        path_text = str(payload.get("path", ""))
        if clean_rel_path(path_text) == "/":
            raise AgentRemoteError(400, "root_move", "The root folder cannot be moved")
        target = resolve_path(self.server.state.root, path_text)
        destination_dir = resolve_path(self.server.state.root, str(payload.get("destDir", "/")))
        if not destination_dir.is_dir():
            raise AgentRemoteError(400, "not_directory", "Destination is not a directory")
        destination = destination_dir / target.name
        if destination.exists():
            raise AgentRemoteError(409, "exists", "Destination already exists")
        shutil.move(str(target), str(destination))
        self.server.state.log(f"move {path_text} -> {payload.get('destDir', '/')}")
        send_json(self, 200, {"ok": True, "entry": file_info(self.server.state.root, destination)})

    def handle_upload_status(self, payload: dict[str, Any]) -> None:
        path_text = str(payload.get("path", ""))
        declared_size = parse_int(str(payload.get("size", "0")), "size")
        target = resolve_path(self.server.state.root, path_text, allow_missing=True)
        part, meta = partial_paths(self.server.state.root, path_text)
        existing_type = None
        if target.exists():
            existing_type = "dir" if target.is_dir() else "file"
        partial_size = part.stat().st_size if part.exists() else 0
        partial_discarded = False
        if part.exists() and partial_size > declared_size:
            discard_upload_partial(part, meta)
            partial_size = 0
            partial_discarded = True
        send_json(
            self,
            200,
            {
                "exists": target.exists(),
                "type": existing_type,
                "partialSize": partial_size,
                "partialDiscarded": partial_discarded,
            },
        )

    def handle_upload_chunk(self, query: dict[str, list[str]]) -> None:
        path_text = first(query, "path", "")
        overwrite = first(query, "overwrite", "false").lower() == "true"
        offset = parse_int(first(query, "offset", "0"), "offset")
        total = parse_int(first(query, "total", "0"), "total")
        target = resolve_path(self.server.state.root, path_text, allow_missing=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not overwrite:
            raise AgentRemoteError(409, "exists", "Target exists and overwrite was not confirmed")
        part, meta = partial_paths(self.server.state.root, path_text)
        current = part.stat().st_size if part.exists() else 0
        if current != offset:
            send_json(self, 409, {"error": "offset_mismatch", "expectedOffset": current})
            return
        length = parse_int(self.headers.get("Content-Length", "0"), "Content-Length")
        if length <= 0:
            raise AgentRemoteError(400, "empty_chunk", "Upload chunk is empty")
        if length > MAX_UPLOAD_CHUNK:
            drain_request_body(self, length, MAX_UPLOAD_CHUNK + 1)
            raise AgentRemoteError(413, "upload_chunk_too_large", "Upload chunk is too large")
        if offset + length > total:
            raise AgentRemoteError(400, "too_much_data", "Chunk exceeds declared file size")
        with part.open("ab") as handle:
            remaining = length
            while remaining:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise AgentRemoteError(400, "short_read", "Request body ended early")
                handle.write(chunk)
                remaining -= len(chunk)
        meta.write_text(
            json.dumps({"path": clean_rel_path(path_text), "size": total, "updated": time.time()}),
            encoding="utf-8",
        )
        send_json(self, 200, {"ok": True, "received": offset + length})

    def handle_upload_finish(self, payload: dict[str, Any]) -> None:
        path_text = str(payload.get("path", ""))
        size = int(payload.get("size", 0))
        expected_hash = str(payload.get("sha256", ""))
        overwrite = bool(payload.get("overwrite", False))
        mtime = payload.get("mtime")
        target = resolve_path(self.server.state.root, path_text, allow_missing=True)
        part, meta = partial_paths(self.server.state.root, path_text)
        if not part.exists():
            if target.exists() and target.is_file() and target.stat().st_size == size:
                if not expected_hash or sha256_file(target) == expected_hash:
                    send_json(self, 200, {"ok": True, "entry": file_info(self.server.state.root, target)})
                    return
            raise AgentRemoteError(400, "missing_partial", "No partial upload exists")
        if part.stat().st_size != size:
            discard_upload_partial(part, meta)
            raise AgentRemoteError(400, "size_mismatch", "Partial upload size is incomplete")
        if expected_hash and sha256_file(part) != expected_hash:
            discard_upload_partial(part, meta)
            raise AgentRemoteError(400, "hash_mismatch", "Uploaded file hash did not match")
        if target.exists() and not overwrite:
            raise AgentRemoteError(409, "exists", "Target exists and overwrite was not confirmed")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(part, target)
        if meta.exists():
            meta.unlink()
        if isinstance(mtime, (int, float)):
            os.utime(target, (float(mtime), float(mtime)))
        self.server.state.log(f"upload finish {path_text}")
        send_json(self, 200, {"ok": True, "entry": file_info(self.server.state.root, target)})

    def handle_instruction(self, payload: dict[str, Any]) -> None:
        task = str(payload.get("task", "")).strip()
        if not task:
            raise AgentRemoteError(400, "missing_task", "Instruction task is required")
        raw_paths = payload.get("paths", [])
        paths = [clean_rel_path(str(path)) for path in raw_paths] if isinstance(raw_paths, list) else []
        manifest = create_instruction(
            self.server.state.root,
            task,
            from_name=str(payload.get("from", "")),
            expect_report=str(payload.get("expectedReport", "")),
            paths=paths,
            auto_run=bool(payload.get("autoRun", False)),
            handoff=payload.get("handoff") if isinstance(payload.get("handoff"), dict) else None,
            executor_model=self.server.state.model_id,
        )
        if payload.get("callbackAlias"):
            manifest["callbackAlias"] = str(payload.get("callbackAlias", ""))
            from .inbox import write_instruction

            write_instruction(self.server.state.root, manifest)
        self.server.state.log(f"instruction received {manifest['id']}: {task[:80]}")
        send_json(self, 200, {"ok": True, "instruction": manifest})


def normalize_session_scopes(scopes: list[str] | str | None) -> list[str]:
    if scopes is None or scopes == "":
        return list(DEFAULT_SESSION_SCOPES)
    if isinstance(scopes, str):
        raw_items = [item.strip() for item in scopes.split(",")]
    elif isinstance(scopes, list):
        raw_items = [str(item).strip() for item in scopes]
    else:
        raise AgentRemoteError(400, "bad_scopes", "Session scopes must be a list or comma-separated string")
    requested = {item for item in raw_items if item}
    if "all" in requested:
        return list(DEFAULT_SESSION_SCOPES)
    unknown = sorted(requested - SESSION_SCOPES)
    if unknown:
        raise AgentRemoteError(400, "bad_scopes", f"Unknown session scope: {', '.join(unknown)}")
    if not requested:
        return list(DEFAULT_SESSION_SCOPES)
    return sorted(requested)


def first(query: dict[str, list[str]], name: str, default: str) -> str:
    values = query.get(name)
    if not values:
        return default
    return values[0]


def parse_int(value: str | None, label: str) -> int:
    try:
        return int(value or "0")
    except ValueError as exc:
        raise AgentRemoteError(400, "bad_number", f"{label} must be an integer") from exc


def discard_upload_partial(part: Path, meta: Path) -> None:
    part.unlink(missing_ok=True)
    meta.unlink(missing_ok=True)


def is_transfer_endpoint(path_text: str) -> bool:
    parsed = urlparse(path_text)
    return parsed.path in {
        "/api/upload/status",
        "/api/upload/chunk",
        "/api/upload/finish",
        "/api/download",
    }


def run_slave(
    root: Path,
    port: int = DEFAULT_PORT,
    password: str | None = None,
    host: str = "0.0.0.0",
    model_id: str = "agentremote-slave",
    firewall: str = "ask",
    max_concurrent: int = 32,
    authenticated_transfer_per_minute: int = 30000,
    panic_on_flood: bool = False,
    tls: str = "off",
    cert_file: Path | None = None,
    key_file: Path | None = None,
    verbose: bool = False,
    policy: str = "off",
    node_name: str = "",
) -> None:
    if password is None:
        import getpass

        print("Set a pairing password for masters that connect to this slave.")
        print("The password is not stored; reconnecting masters use a session token.")
        password = getpass.getpass("Pairing password: ")
        confirm = getpass.getpass("Confirm pairing password: ")
        if password != confirm:
            raise SystemExit("Passwords did not match")
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    maybe_open_firewall(port, firewall)
    tls_files = prepare_tls(root, tls, cert_file, key_file)
    state = SlaveState(
        root,
        password,
        model_id=model_id,
        quiet=not verbose,
        policy=policy,
        node_name=node_name,
        security_config=SecurityConfig(
            max_concurrent_requests=max_concurrent,
            authenticated_transfer_per_minute=authenticated_transfer_per_minute,
            panic_on_flood=panic_on_flood,
        ),
    )
    server = AgentRemoteSlaveServer((host, port), state)
    if tls_files:
        wrap_server_socket(server, tls_files.cert_file, tls_files.key_file)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    scheme = "https" if tls_files else "http"

    print()
    print("agent-remote-sync Slave")
    print("=======================")
    print(f"Status: running")
    print(f"Root:   {root}")
    print(f"Port:   {port}")
    print(f"Model:  {model_id}")
    print("Pairing: started from this project root; handoffs are recorded in AIMemory")
    print(f"TLS:    {'enabled' if tls_files else 'off'}")
    if tls_files:
        print(f"Cert:   {tls_files.cert_file}")
        print(f"SHA256: {format_fingerprint(tls_files.fingerprint)}")
    print()
    addresses = advertised_addresses(host, port)
    print("Connect using:")
    for label, address in addresses:
        print(f"- {label:<9} {scheme}://{address}")
    if is_loopback_bind_host(host):
        print("  (local-only bind; use --host 0.0.0.0 or a trusted interface for cross-host access)")
    print()
    print("Commands: [i] info  [l] logs  [q] stop")
    state.log("slave started")

    try:
        if not input_available():
            wait_without_stdin("agent-remote-sync slave")
        else:
            while True:
                try:
                    command = input("agentremote-slave> ").strip().lower()
                except EOFError:
                    wait_without_stdin("agent-remote-sync slave")
                    break
                if command in ("q", "quit", "exit"):
                    break
                if command in ("i", "info"):
                    print(f"Root: {root}")
                    if tls_files:
                        print(f"TLS fingerprint: {format_fingerprint(tls_files.fingerprint)}")
                    for label, address in detect_addresses(port):
                        print(f"{label}: {scheme}://{address}")
                elif command in ("l", "log", "logs"):
                    with state.lock:
                        for line in state.logs[-25:]:
                            print(line)
                elif command:
                    print("Commands: [i] info  [l] logs  [q] stop")
    except KeyboardInterrupt:
        print()
    finally:
        state.log("stopping slave")
        server.shutdown()
        server.server_close()


def input_available() -> bool:
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def wait_without_stdin(label: str) -> None:
    print(f"{label}: stdin is not interactive; staying alive until the process is interrupted.")
    print("Use a visible console for [q] stop, or terminate the process from the host.")
    while True:
        time.sleep(3600)


def advertised_addresses(host: str, port: int) -> list[tuple[str, str]]:
    if is_loopback_bind_host(host):
        normalized = "::1" if str(host).strip().lower().strip("[]") == "::1" else "127.0.0.1"
        return [("Local", f"{normalized}:{port}")]
    normalized = str(host or "").strip()
    if normalized in ("", "0.0.0.0", "::"):
        return detect_addresses(port)
    return [("Bound", f"{normalized}:{port}")]


def is_loopback_bind_host(host: str) -> bool:
    text = str(host or "").strip().lower().strip("[]")
    return text in {"127.0.0.1", "localhost", "::1"}


def prepare_tls(
    root: Path,
    mode: str,
    cert_file: Path | None = None,
    key_file: Path | None = None,
) -> TLSFiles | None:
    if mode == "off":
        return None
    if mode == "self-signed":
        return ensure_self_signed_cert(root)
    if mode == "manual":
        if cert_file is None or key_file is None:
            raise AgentRemoteError(400, "missing_tls_files", "--cert-file and --key-file are required for --tls manual")
        cert = cert_file.expanduser().resolve()
        key = key_file.expanduser().resolve()
        if not cert.exists() or not key.exists():
            raise AgentRemoteError(404, "tls_file_not_found", "TLS certificate or key file was not found")
        return TLSFiles(cert, key, certificate_fingerprint(cert))
    raise AgentRemoteError(400, "bad_tls_mode", "TLS mode must be off, self-signed, or manual")
