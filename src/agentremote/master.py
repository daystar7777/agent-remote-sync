from __future__ import annotations

import json
import os
import shutil
import socket
import ssl
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from urllib.request import Request

from .common import (
    CHUNK_SIZE,
    DEFAULT_PORT,
    DEFAULT_UI_PORT,
    AgentRemoteError,
    RESERVED_DIR_NAMES,
    TransferJob,
    clean_rel_path,
    derive_key,
    ensure_storage_available,
    file_info,
    join_rel,
    list_dir,
    make_proof,
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
    to_agent_path,
    tree_entries,
    unb64,
)
from .connections import iter_connections
from .filenames import normalize_disk
from .swarm import (
    get_mobile_controller_data,
    journal_node_status,
    journal_routes_summary,
    load_swarm_state,
    merged_route_rows,
    normalize_node_name,
    probe_url,
    save_swarm_state,
    select_best_route,
    topology_nodes,
    verify_mobile_token,
    whitelist_status,
)
from .tls import is_https_endpoint, open_url


class RemoteClient:
    def __init__(
        self,
        host: str,
        port: int,
        password: str | None = None,
        *,
        token: str | None = None,
        tls_fingerprint: str = "",
        tls_insecure: bool = False,
        ca_file: str = "",
        scopes: list[str] | None = None,
        max_retries: int = 2,
        client_alias: str = "",
    ):
        if "://" in host:
            self.base_url = host.rstrip("/")
        else:
            self.base_url = f"http://{host}:{port}"
        self.token = ""
        self.slave_model = ""
        self.executor_model = ""
        self.filename_normalization: dict[str, Any] = {}
        self.tls_fingerprint = tls_fingerprint
        self.tls_insecure = tls_insecure
        self.ca_file = ca_file
        self.requested_scopes = scopes
        self.scopes: list[str] = []
        self.max_retries = max(0, max_retries)
        self.client_alias = client_alias
        if token:
            self.token = token
        elif password is not None:
            self.login(password)
        else:
            raise AgentRemoteError(401, "missing_auth", "Password or token is required")

    def login(self, password: str) -> None:
        challenge = self.request_json("GET", "/api/challenge", auth=False)
        key = derive_key(password, unb64(challenge["salt"]), int(challenge["iterations"]))
        proof = make_proof(key, challenge["nonce"])
        payload: dict[str, Any] = {"nonce": challenge["nonce"], "proof": proof}
        if self.requested_scopes:
            payload["scopes"] = self.requested_scopes
        if self.client_alias:
            payload["clientAlias"] = self.client_alias
        response = self.request_json(
            "POST",
            "/api/login",
            payload,
            auth=False,
            retryable=False,
        )
        self.token = response["token"]
        self.scopes = list(response.get("scopes", []))
        self.slave_model = response.get("slaveModel", "")
        self.executor_model = response.get("executorModel", self.slave_model)
        self.filename_normalization = response.get("filenameNormalization", {})

    def request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        auth: bool = True,
        retryable: bool | None = None,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        if retryable is None:
            retryable = method.upper() == "GET"
        raw = self.read_with_retries(request, timeout=60, retryable=retryable)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def read_with_retries(self, request: Request, *, timeout: int, retryable: bool) -> bytes:
        rate_limit_retryable = retryable and self.max_retries > 0 and is_transfer_request_url(request.full_url)
        attempts = self.max_retries + 1 if retryable else 1
        if rate_limit_retryable:
            attempts = max(attempts, 12)
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                with open_url(
                    request,
                    timeout=timeout,
                    tls_fingerprint=self.tls_fingerprint,
                    tls_insecure=self.tls_insecure,
                    ca_file=self.ca_file,
                ) as response:
                    return response.read()
            except HTTPError as exc:
                error = remote_http_error(exc)
                last = error
                if rate_limit_retryable and exc.code == 429 and attempt + 1 < attempts:
                    time.sleep(rate_limit_retry_delay(exc, attempt))
                    continue
                raise error from exc
            except (URLError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
                last = exc
                if attempt + 1 >= attempts:
                    break
                time.sleep(min(2.0, 0.25 * (2**attempt)))
        raise AgentRemoteError(502, "remote_unreachable", str(last)) from last

    def request_bytes(self, path: str) -> bytes:
        request = Request(
            self.base_url + path,
            headers={"Authorization": f"Bearer {self.token}"},
            method="GET",
        )
        return self.read_with_retries(request, timeout=120, retryable=True)

    def put_bytes(self, path: str, data: bytes) -> dict[str, Any]:
        request = Request(
            self.base_url + path,
            data=data,
            headers={"Authorization": f"Bearer {self.token}"},
            method="PUT",
        )
        try:
            raw = self.read_with_retries(request, timeout=120, retryable=True)
        except AgentRemoteError as exc:
            if exc.code == "offset_mismatch":
                expected = exc.details.get("expectedOffset")
                query = parse_qs(urlparse(path).query)
                sent_offset = int(first(query, "offset", "0"))
                if isinstance(expected, int) and expected == sent_offset + len(data):
                    return {"ok": True, "received": expected, "resumedByOffsetMismatch": True}
            raise
        return json.loads(raw.decode("utf-8")) if raw else {}

    def list(self, path: str) -> dict[str, Any]:
        return self.request_json("GET", "/api/list?" + urlencode({"path": path}))

    def stat(self, path: str) -> dict[str, Any]:
        return self.request_json("GET", "/api/stat?" + urlencode({"path": path}))

    def tree(self, path: str) -> list[dict[str, Any]]:
        return self.request_json("GET", "/api/tree?" + urlencode({"path": path}))["entries"]

    def storage(self) -> dict[str, Any]:
        return self.request_json("GET", "/api/storage")

    def mkdir(self, path: str) -> None:
        self.request_json("POST", "/api/mkdir", {"path": path}, retryable=True)

    def delete(self, path: str) -> None:
        self.request_json("POST", "/api/delete", {"path": path})

    def rename(self, path: str, new_name: str) -> None:
        self.request_json("POST", "/api/rename", {"path": path, "newName": new_name})

    def move(self, path: str, dest_dir: str) -> None:
        self.request_json("POST", "/api/move", {"path": path, "destDir": dest_dir})

    def upload_status(self, path: str, size: int) -> dict[str, Any]:
        return self.request_json("POST", "/api/upload/status", {"path": path, "size": size}, retryable=True)

    def upload_chunk(
        self,
        path: str,
        offset: int,
        total: int,
        data: bytes,
        *,
        overwrite: bool,
    ) -> dict[str, Any]:
        query = urlencode(
            {
                "path": path,
                "offset": str(offset),
                "total": str(total),
                "overwrite": "true" if overwrite else "false",
            }
        )
        return self.put_bytes("/api/upload/chunk?" + query, data)

    def upload_finish(
        self,
        path: str,
        size: int,
        mtime: float,
        digest: str,
        *,
        overwrite: bool,
    ) -> None:
        self.request_json(
            "POST",
            "/api/upload/finish",
            {
                "path": path,
                "size": size,
                "mtime": mtime,
                "sha256": digest,
                "overwrite": overwrite,
            },
            retryable=True,
        )

    def download_chunk(self, path: str, offset: int, length: int) -> bytes:
        return self.request_bytes(
            "/api/download?"
            + urlencode({"path": path, "offset": str(offset), "length": str(length)})
        )

    def send_instruction(
        self,
        task: str,
        *,
        from_name: str = "",
        paths: list[str] | None = None,
        expect_report: str = "",
        auto_run: bool = False,
        handoff: dict[str, Any] | None = None,
        callback_alias: str = "",
    ) -> dict[str, Any]:
        return self.request_json(
            "POST",
            "/api/instructions",
            {
                "task": task,
                "from": from_name,
                "paths": paths or [],
                "expectedReport": expect_report,
                "autoRun": auto_run,
                "handoff": handoff,
                "callbackAlias": callback_alias,
            },
        )


class MasterState:
    def __init__(self, local_root: Path, remote: RemoteClient):
        self.local_root = local_root.resolve()
        self.remote = remote
        self.jobs: dict[str, TransferJob] = {}
        self.plans: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def start_job(self, kind: str, runner: Callable[[TransferJob], None]) -> TransferJob:
        job = TransferJob(id=make_token(), kind=kind)
        with self.lock:
            self.jobs[job.id] = job

        def run() -> None:
            job.state = "running"
            try:
                job.raise_if_cancelled()
                runner(job)
                if job.cancel_requested:
                    job.state = "cancelled"
                    job.error = "Transfer was cancelled"
                else:
                    job.state = "done"
            except AgentRemoteError as exc:
                if exc.code == "cancelled":
                    job.state = "cancelled"
                    job.error = exc.message
                else:
                    job.state = "error"
                    job.error = exc.message
            except Exception as exc:
                job.state = "error"
                job.error = str(exc)
            finally:
                job.finished_at = time.time()

        threading.Thread(target=run, daemon=True).start()
        return job

    def get_job(self, job_id: str) -> TransferJob:
        with self.lock:
            job = self.jobs.get(job_id)
        if job is None:
            raise AgentRemoteError(404, "job_not_found", "Transfer job was not found")
        return job

    def cancel_job(self, job_id: str) -> TransferJob:
        job = self.get_job(job_id)
        if job.state in ("done", "error", "cancelled"):
            return job
        job.cancel_requested = True
        return job

    def save_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        plan = dict(plan)
        plan_id = make_token()
        plan["planId"] = plan_id
        plan["createdAt"] = time.time()
        with self.lock:
            self.plans[plan_id] = plan
            if len(self.plans) > 100:
                oldest = sorted(self.plans.items(), key=lambda item: item[1].get("createdAt", 0))[:20]
                for key, _ in oldest:
                    self.plans.pop(key, None)
        return plan

    def get_plan(self, plan_id: str, direction: str) -> dict[str, Any]:
        with self.lock:
            plan = self.plans.get(plan_id)
        if plan is None:
            raise AgentRemoteError(404, "plan_not_found", "Transfer plan was not found")
        if plan.get("direction") != direction:
            raise AgentRemoteError(400, "wrong_plan_type", "Transfer plan direction does not match this job")
        return dict(plan)


class AgentRemoteMasterServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], state: MasterState):
        super().__init__(server_address, MasterHandler)
        self.state = state
        self.daemon_threads = True


class MasterHandler(BaseHTTPRequestHandler):
    server: AgentRemoteMasterServer

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path == "/":
                self.serve_index()
            elif parsed.path == "/api/bootstrap":
                send_json(
                    self,
                    200,
                    {
                        "localRoot": str(self.server.state.local_root),
                        "remoteBase": self.server.state.remote.base_url,
                    },
                )
            elif parsed.path == "/api/local/list":
                send_json(
                    self, 200, list_dir(self.server.state.local_root, first(query, "path", "/"))
                )
            elif parsed.path == "/api/remote/list":
                send_json(self, 200, self.server.state.remote.list(first(query, "path", "/")))
            elif parsed.path == "/api/local/storage":
                send_json(self, 200, storage_info(self.server.state.local_root))
            elif parsed.path == "/api/remote/storage":
                send_json(self, 200, self.server.state.remote.storage())
            elif parsed.path == "/api/dashboard":
                from .swarm import get_dashboard_data
                send_json(self, 200, get_dashboard_data(self.server.state.local_root))
            elif parsed.path == "/api/approvals":
                from .approval import list_approval_requests, sanitize_approval
                items = [sanitize_approval(r) for r in list_approval_requests(self.server.state.local_root)]
                send_json(self, 200, {"approvals": items})
            elif parsed.path == "/api/dashboard/processes":
                from .swarm import list_process_registry
                send_json(self, 200, {"processes": list_process_registry(self.server.state.local_root)})
            elif parsed.path == "/api/mobile/controller":
                _handle_mobile_controller(self)
            elif parsed.path == "/api/worker-policy":
                from .worker_policy import worker_policy_summary

                send_json(self, 200, worker_policy_summary(self.server.state.local_root))
            elif parsed.path == "/api/inbox":
                _handle_inbox(self)
            elif parsed.path == "/api/inbox/read":
                _handle_inbox_read(self, {"filename": first(query, "filename", "")})
            elif parsed.path.startswith("/api/dashboard/call/"):
                call_id = unquote(parsed.path.removeprefix("/api/dashboard/call/"))
                _handle_dashboard_call_detail(self, call_id)
            elif parsed.path.startswith("/api/dashboard/node/"):
                node_name = unquote(parsed.path.removeprefix("/api/dashboard/node/"))
                _handle_dashboard_node_detail(self, node_name)
            elif parsed.path.startswith("/api/jobs/"):
                job_id = parsed.path.removeprefix("/api/jobs/")
                send_json(self, 200, self.server.state.get_job(job_id).as_dict())
            else:
                raise AgentRemoteError(404, "not_found", "Endpoint not found")
        except Exception as exc:
            send_error(self, exc)

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            payload = read_json_body(self)
            if parsed.path == "/api/local/mkdir":
                self.handle_local_mkdir(payload)
            elif parsed.path == "/api/local/delete":
                self.handle_local_delete(payload)
            elif parsed.path == "/api/local/rename":
                self.handle_local_rename(payload)
            elif parsed.path == "/api/local/move":
                self.handle_local_move(payload)
            elif parsed.path == "/api/remote/mkdir":
                self.server.state.remote.mkdir(mkdir_path_from_payload(payload))
                send_json(self, 200, {"ok": True})
            elif parsed.path == "/api/remote/delete":
                from .approval import require_approval
                remote_path = path_from_payload(payload)
                require_approval(
                    self.server.state.local_root,
                    "remote.delete",
                    risk="high",
                    origin_type="controller",
                    target_node=getattr(self.server.state.remote, "client_alias", "") or self.server.state.remote.base_url,
                    summary=f"Delete remote path {clean_rel_path(remote_path)}",
                    details=f"path={clean_rel_path(remote_path)}",
                    timeout=300,
                    poll_interval=0.25,
                )
                self.server.state.remote.delete(remote_path)
                send_json(self, 200, {"ok": True})
            elif parsed.path == "/api/remote/rename":
                self.server.state.remote.rename(
                    path_from_payload(payload), str(payload.get("newName", ""))
                )
                send_json(self, 200, {"ok": True})
            elif parsed.path == "/api/remote/move":
                self.server.state.remote.move(
                    path_from_payload(payload), str(payload.get("destDir", "/"))
                )
                send_json(self, 200, {"ok": True})
            elif parsed.path == "/api/conflicts/upload":
                send_json(self, 200, {"conflicts": self.upload_conflicts(payload)})
            elif parsed.path == "/api/conflicts/download":
                send_json(self, 200, {"conflicts": self.download_conflicts(payload)})
            elif parsed.path == "/api/plan/upload":
                send_json(self, 200, self.server.state.save_plan(self.build_upload_transfer_plan(payload)))
            elif parsed.path == "/api/plan/download":
                send_json(self, 200, self.server.state.save_plan(self.build_download_transfer_plan(payload)))
            elif parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/cancel"):
                job_id = parsed.path.removeprefix("/api/jobs/").removesuffix("/cancel")
                send_json(self, 200, self.server.state.cancel_job(job_id).as_dict())
            elif parsed.path == "/api/jobs/upload":
                job = self.server.state.start_job(
                    "upload", lambda current: self.run_upload_job(current, payload)
                )
                send_json(self, 202, job.as_dict())
            elif parsed.path == "/api/jobs/download":
                job = self.server.state.start_job(
                    "download", lambda current: self.run_download_job(current, payload)
                )
                send_json(self, 202, job.as_dict())
            elif parsed.path == "/api/dashboard/refresh-all":
                _handle_dashboard_refresh_all(self)
            elif parsed.path == "/api/dashboard/refresh-node":
                _handle_dashboard_refresh_node(self, payload)
            elif parsed.path == "/api/dashboard/calls-refresh":
                _handle_dashboard_calls_refresh(self)
            elif parsed.path == "/api/dashboard/process/stop":
                _handle_process_stop(self, payload)
            elif parsed.path == "/api/dashboard/process/forget":
                _handle_process_forget(self, payload)
            elif parsed.path == "/api/dashboard/profile/forget":
                _handle_daemon_profile_forget(self, payload)
            elif parsed.path == "/api/approvals":
                from .approval import list_approval_requests, sanitize_approval
                items = [sanitize_approval(r) for r in list_approval_requests(self.server.state.local_root)]
                send_json(self, 200, {"approvals": items})
            elif parsed.path == "/api/approvals/decide":
                from .approval import decide_approval
                result = decide_approval(self.server.state.local_root, str(payload.get("id", "")), str(payload.get("decision", "denied")), decided_by="controller")
                send_json(self, 200, {"ok": True, "status": result.get("status")})
            elif parsed.path == "/api/worker-policy/init":
                _handle_worker_policy_init(self)
            elif parsed.path == "/api/worker-policy/apply-template":
                _handle_worker_policy_apply_template(self, payload)
            elif parsed.path == "/api/worker-policy/remove":
                _handle_worker_policy_remove(self, payload)
            elif parsed.path == "/api/inbox/read":
                _handle_inbox_read(self, payload)
            elif parsed.path == "/api/mobile/process/stop":
                _require_mobile_scope(self, "process-control")
                _handle_process_stop(self, payload)
            elif parsed.path == "/api/mobile/process/forget":
                _require_mobile_scope(self, "process-control")
                _handle_process_forget(self, payload)
            else:
                raise AgentRemoteError(404, "not_found", "Endpoint not found")
        except Exception as exc:
            send_error(self, exc)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def serve_index(self) -> None:
        data = files("agentremote.web").joinpath("index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_local_mkdir(self, payload: dict[str, Any]) -> None:
        if "path" in payload:
            target = resolve_path(self.server.state.local_root, str(payload["path"]), allow_missing=True)
        else:
            parent = resolve_path(self.server.state.local_root, str(payload.get("parent", "/")))
            target = parent / normalize_disk(safe_name(str(payload.get("name", ""))))
        if target.exists() and not target.is_dir():
            raise AgentRemoteError(409, "exists", "A non-directory already exists there")
        target.mkdir(parents=True, exist_ok=True)
        send_json(self, 200, {"ok": True, "entry": file_info(self.server.state.local_root, target)})

    def handle_local_delete(self, payload: dict[str, Any]) -> None:
        path_text = path_from_payload(payload)
        if clean_rel_path(path_text) == "/":
            raise AgentRemoteError(400, "root_delete", "The local root cannot be deleted")
        from .approval import require_approval
        require_approval(
            self.server.state.local_root,
            "local.delete",
            risk="high",
            origin_type="controller",
            summary=f"Delete local path {clean_rel_path(path_text)}",
            details=f"path={clean_rel_path(path_text)}",
            timeout=300,
            poll_interval=0.25,
        )
        target = resolve_path(self.server.state.local_root, path_text)
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
        send_json(self, 200, {"ok": True})

    def handle_local_rename(self, payload: dict[str, Any]) -> None:
        path_text = path_from_payload(payload)
        if clean_rel_path(path_text) == "/":
            raise AgentRemoteError(400, "root_rename", "The local root cannot be renamed")
        target = resolve_path(self.server.state.local_root, path_text)
        new_name = normalize_disk(safe_name(str(payload.get("newName", ""))))
        destination = target.with_name(new_name)
        if destination.exists():
            raise AgentRemoteError(409, "exists", "Destination already exists")
        target.rename(destination)
        send_json(self, 200, {"ok": True, "entry": file_info(self.server.state.local_root, destination)})

    def handle_local_move(self, payload: dict[str, Any]) -> None:
        path_text = path_from_payload(payload)
        if clean_rel_path(path_text) == "/":
            raise AgentRemoteError(400, "root_move", "The local root cannot be moved")
        target = resolve_path(self.server.state.local_root, path_text)
        destination_dir = resolve_path(self.server.state.local_root, str(payload.get("destDir", "/")))
        if not destination_dir.is_dir():
            raise AgentRemoteError(400, "not_directory", "Destination is not a directory")
        destination = destination_dir / target.name
        if destination.exists():
            raise AgentRemoteError(409, "exists", "Destination already exists")
        shutil.move(str(target), str(destination))
        send_json(self, 200, {"ok": True, "entry": file_info(self.server.state.local_root, destination)})

    def upload_conflicts(self, payload: dict[str, Any]) -> list[str]:
        plan = build_upload_plan(
            self.server.state.local_root,
            list(payload.get("paths", [])),
            str(payload.get("remoteDir", "/")),
        )
        conflicts = []
        for item in plan["files"]:
            stat = self.server.state.remote.stat(item["target"])
            if stat.get("exists"):
                conflicts.append(item["target"])
        return conflicts

    def download_conflicts(self, payload: dict[str, Any]) -> list[str]:
        plan = build_download_plan(
            self.server.state.remote,
            list(payload.get("paths", [])),
            str(payload.get("localDir", "/")),
        )
        conflicts = []
        for item in plan["files"]:
            target = resolve_path(self.server.state.local_root, item["target"], allow_missing=True)
            if target.exists():
                conflicts.append(item["target"])
        return conflicts

    def build_upload_transfer_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        return build_upload_transfer_plan(
            self.server.state.remote,
            self.server.state.local_root,
            list(payload.get("paths", [])),
            str(payload.get("remoteDir", "/")),
        )

    def build_download_transfer_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        return build_download_transfer_plan(
            self.server.state.remote,
            self.server.state.local_root,
            list(payload.get("paths", [])),
            str(payload.get("localDir", "/")),
        )

    def plan_from_payload(self, payload: dict[str, Any], direction: str) -> dict[str, Any]:
        plan_id = str(payload.get("planId", ""))
        if plan_id:
            return self.server.state.get_plan(plan_id, direction)
        if direction == "upload":
            return self.build_upload_transfer_plan(payload)
        return self.build_download_transfer_plan(payload)

    def run_upload_job(self, job: TransferJob, payload: dict[str, Any]) -> None:
        overwrite = bool(payload.get("overwrite", False))
        plan = self.plan_from_payload(payload, "upload")
        job.total_bytes = int(plan["totalBytes"])
        required_bytes = int(plan["requiredBytes"])
        if required_bytes:
            ensure_storage_available(plan["destinationStorage"], required_bytes, "remote destination")
        if plan["conflicts"] and not overwrite:
            raise AgentRemoteError(409, "exists", "Remote file exists and overwrite was not confirmed")
        for directory in plan["dirs"]:
            job.raise_if_cancelled()
            self.server.state.remote.mkdir(directory)
        for item in plan["files"]:
            job.raise_if_cancelled()
            job.current = f"{item['source']} -> {item['target']}"
            source = resolve_path(self.server.state.local_root, item["source"])
            digest = sha256_file(source)
            attempts = 0
            file_done = 0
            while True:
                job.raise_if_cancelled()
                if file_done:
                    job.done_bytes = max(0, job.done_bytes - file_done)
                    file_done = 0
                status = self.server.state.remote.upload_status(item["target"], item["size"])
                if status.get("exists") and not overwrite:
                    raise AgentRemoteError(409, "exists", f"Remote file exists: {item['target']}")
                offset = int(status.get("partialSize", 0))
                if offset > item["size"]:
                    raise AgentRemoteError(409, "bad_partial", f"Remote partial is larger than source: {item['target']}")
                job.done_bytes += offset
                file_done += offset
                with source.open("rb") as handle:
                    handle.seek(offset)
                    current_offset = offset
                    while current_offset < item["size"]:
                        job.raise_if_cancelled()
                        chunk = handle.read(min(CHUNK_SIZE, item["size"] - current_offset))
                        if not chunk:
                            break
                        response = self.server.state.remote.upload_chunk(
                            item["target"],
                            current_offset,
                            item["size"],
                            chunk,
                            overwrite=overwrite,
                        )
                        current_offset = int(response.get("received", current_offset + len(chunk)))
                        job.done_bytes += len(chunk)
                        file_done += len(chunk)
                try:
                    self.server.state.remote.upload_finish(
                        item["target"],
                        item["size"],
                        item["mtime"],
                        digest,
                        overwrite=overwrite,
                    )
                    break
                except AgentRemoteError as exc:
                    if attempts == 0 and exc.code in {"hash_mismatch", "size_mismatch"}:
                        attempts += 1
                        job.current = f"retrying {item['source']} -> {item['target']}"
                        continue
                    raise

    def run_download_job(self, job: TransferJob, payload: dict[str, Any]) -> None:
        overwrite = bool(payload.get("overwrite", False))
        plan = self.plan_from_payload(payload, "download")
        job.total_bytes = int(plan["totalBytes"])
        required_bytes = int(plan["requiredBytes"])
        if required_bytes:
            ensure_storage_available(plan["destinationStorage"], required_bytes, "local destination")
        if plan["conflicts"] and not overwrite:
            raise AgentRemoteError(409, "exists", "Local file exists and overwrite was not confirmed")
        for directory in plan["dirs"]:
            job.raise_if_cancelled()
            resolve_path(self.server.state.local_root, directory, allow_missing=True).mkdir(
                parents=True, exist_ok=True
            )
        for item in plan["files"]:
            job.raise_if_cancelled()
            job.current = f"{item['source']} -> {item['target']}"
            target = resolve_path(self.server.state.local_root, item["target"], allow_missing=True)
            if target.exists() and not overwrite:
                raise AgentRemoteError(409, "exists", f"Local file exists: {item['target']}")
            part, meta = partial_paths(self.server.state.local_root, item["target"])
            offset = part.stat().st_size if part.exists() else 0
            if offset > item["size"]:
                part.unlink()
                offset = 0
            job.done_bytes += offset
            with part.open("ab") as handle:
                current_offset = offset
                while current_offset < item["size"]:
                    job.raise_if_cancelled()
                    length = min(CHUNK_SIZE, item["size"] - current_offset)
                    chunk = self.server.state.remote.download_chunk(
                        item["source"], current_offset, length
                    )
                    if not chunk:
                        raise AgentRemoteError(502, "empty_chunk", "Remote returned an empty chunk")
                    handle.write(chunk)
                    current_offset += len(chunk)
                    job.done_bytes += len(chunk)
            if part.stat().st_size != item["size"]:
                raise AgentRemoteError(400, "size_mismatch", f"Downloaded size mismatch: {item['target']}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and not overwrite:
                raise AgentRemoteError(409, "exists", f"Local file exists: {item['target']}")
            os.replace(part, target)
            if meta.exists():
                meta.unlink()
            if item.get("mtime"):
                os.utime(target, (float(item["mtime"]), float(item["mtime"])))


def build_upload_plan(local_root: Path, paths: list[str], remote_dir: str) -> dict[str, Any]:
    dirs: set[str] = set()
    files: list[dict[str, Any]] = []
    target_sources: dict[str, str] = {}
    remote_dir = clean_rel_path(remote_dir)

    def add_file(source: Path, target: str) -> None:
        normalized_target = clean_rel_path(target)
        source_key = str(source.resolve())
        source_agent = to_agent_path(local_root, source)
        previous = target_sources.get(normalized_target)
        if previous and previous != source_key:
            raise AgentRemoteError(
                409,
                "filename_normalization_collision",
                "Multiple local files map to the same remote path after Unicode normalization: "
                f"{Path(previous).name} and {source.name} -> {normalized_target}",
            )
        target_sources[normalized_target] = source_key
        files.append(upload_item(local_root, source, normalized_target))

    for raw_path in paths:
        source_agent = clean_rel_path(str(raw_path))
        source = resolve_path(local_root, source_agent)
        base_name = source.name or "local-root"
        if source.is_dir():
            root_target = join_rel(remote_dir, base_name)
            dirs.add(root_target)
            for current, dirnames, filenames in os.walk(source, followlinks=False):
                dirnames[:] = [name for name in dirnames if name not in RESERVED_DIR_NAMES]
                current_path = Path(current)
                rel_dir = current_path.relative_to(source).as_posix()
                current_target = root_target if rel_dir == "." else join_rel(root_target, rel_dir)
                dirs.add(current_target)
                for filename in filenames:
                    child = current_path / filename
                    if child.is_symlink():
                        continue
                    rel_file = child.relative_to(source).as_posix()
                    target = join_rel(root_target, rel_file)
                    add_file(child, target)
        else:
            add_file(source, join_rel(remote_dir, base_name))
    return {"dirs": sorted(dirs), "files": files}


def build_upload_transfer_plan(
    remote: RemoteClient,
    local_root: Path,
    paths: list[str],
    remote_dir: str,
) -> dict[str, Any]:
    raw = build_upload_plan(local_root, paths, remote_dir)
    files = []
    conflicts = []
    required = 0
    for item in raw["files"]:
        enriched = dict(item)
        size = int(item["size"])
        status = remote.upload_status(item["target"], size)
        partial_size = int(status.get("partialSize", 0))
        exists = bool(status.get("exists", False))
        required_bytes = size if partial_size > size else size - partial_size
        enriched.update(
            {
                "exists": exists,
                "partialSize": partial_size,
                "requiredBytes": required_bytes,
            }
        )
        if exists:
            conflicts.append(item["target"])
        required += required_bytes
        files.append(enriched)
    storage = remote.storage()
    return transfer_plan(
        direction="upload",
        source="local",
        destination="remote",
        dirs=raw["dirs"],
        files=files,
        conflicts=conflicts,
        required_bytes=required,
        destination_storage=storage,
        destination_label="remote destination",
    )


def build_download_transfer_plan(
    remote: RemoteClient,
    local_root: Path,
    paths: list[str],
    local_dir: str,
) -> dict[str, Any]:
    raw = build_download_plan(remote, paths, local_dir)
    files = []
    conflicts = []
    required = 0
    for item in raw["files"]:
        enriched = dict(item)
        size = int(item["size"])
        target = resolve_path(local_root, item["target"], allow_missing=True)
        exists = target.exists()
        part, _ = partial_paths(local_root, item["target"])
        partial_size = part.stat().st_size if part.exists() else 0
        required_bytes = size if partial_size > size else size - partial_size
        enriched.update(
            {
                "exists": exists,
                "partialSize": partial_size,
                "requiredBytes": required_bytes,
            }
        )
        if exists:
            conflicts.append(item["target"])
        required += required_bytes
        files.append(enriched)
    storage = storage_info(local_root)
    return transfer_plan(
        direction="download",
        source="remote",
        destination="local",
        dirs=raw["dirs"],
        files=files,
        conflicts=conflicts,
        required_bytes=required,
        destination_storage=storage,
        destination_label="local destination",
    )


def transfer_plan(
    *,
    direction: str,
    source: str,
    destination: str,
    dirs: list[str],
    files: list[dict[str, Any]],
    conflicts: list[str],
    required_bytes: int,
    destination_storage: dict[str, Any],
    destination_label: str,
) -> dict[str, Any]:
    total_bytes = sum(int(item["size"]) for item in files)
    warnings = []
    try:
        ensure_storage_available(destination_storage, required_bytes, destination_label)
    except AgentRemoteError as exc:
        warnings.append({"code": exc.code, "message": exc.message})
    return {
        "direction": direction,
        "source": source,
        "destination": destination,
        "dirs": dirs,
        "files": files,
        "conflicts": conflicts,
        "totalFiles": len(files),
        "totalDirs": len(dirs),
        "totalBytes": total_bytes,
        "requiredBytes": required_bytes,
        "destinationStorage": destination_storage,
        "canStart": not warnings,
        "warnings": warnings,
    }


def upload_required_bytes(remote: RemoteClient, files: list[dict[str, Any]]) -> int:
    required = 0
    for item in files:
        if "requiredBytes" in item:
            required += int(item["requiredBytes"])
            continue
        size = int(item["size"])
        status = remote.upload_status(item["target"], size)
        offset = int(status.get("partialSize", 0))
        required += size if offset > size else size - offset
    return required


def download_required_bytes(local_root: Path, files: list[dict[str, Any]]) -> int:
    required = 0
    for item in files:
        if "requiredBytes" in item:
            required += int(item["requiredBytes"])
            continue
        size = int(item["size"])
        part, _ = partial_paths(local_root, item["target"])
        offset = part.stat().st_size if part.exists() else 0
        required += size if offset > size else size - offset
    return required


def upload_item(local_root: Path, source: Path, target: str) -> dict[str, Any]:
    stat = source.stat()
    return {
        "source": clean_rel_path(to_local_agent_path(local_root, source)),
        "target": clean_rel_path(target),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    }


def build_download_plan(remote: RemoteClient, paths: list[str], local_dir: str) -> dict[str, Any]:
    dirs: set[str] = set()
    files: list[dict[str, Any]] = []
    local_dir = clean_rel_path(local_dir)
    for raw_path in paths:
        source_agent = clean_rel_path(str(raw_path))
        stat = remote.stat(source_agent)
        if not stat.get("exists"):
            raise AgentRemoteError(404, "not_found", f"Remote path not found: {source_agent}")
        entry = stat["entry"]
        base_name = PurePosixPath(source_agent).name or "remote-root"
        if entry["type"] == "dir":
            root_target = join_rel(local_dir, base_name)
            dirs.add(root_target)
            for remote_entry in remote.tree(source_agent):
                remote_path = clean_rel_path(remote_entry["path"])
                if remote_path == source_agent:
                    continue
                rel = posix_relative(source_agent, remote_path)
                target = join_rel(root_target, rel)
                if remote_entry["type"] == "dir":
                    dirs.add(target)
                elif remote_entry["type"] == "file":
                    files.append(
                        {
                            "source": remote_path,
                            "target": target,
                            "size": int(remote_entry["size"]),
                            "mtime": remote_entry.get("modified"),
                        }
                    )
        elif entry["type"] == "file":
            files.append(
                {
                    "source": source_agent,
                    "target": join_rel(local_dir, base_name),
                    "size": int(entry["size"]),
                    "mtime": entry.get("modified"),
                }
            )
        else:
            raise AgentRemoteError(400, "unsupported_type", "Symlinks are not transferred")
    return {"dirs": sorted(dirs), "files": files}


def posix_relative(base: str, child: str) -> str:
    base_clean = clean_rel_path(base).strip("/")
    child_clean = clean_rel_path(child).strip("/")
    if not base_clean:
        return child_clean
    prefix = base_clean + "/"
    if child_clean.startswith(prefix):
        return child_clean[len(prefix) :]
    raise AgentRemoteError(400, "bad_tree", "Remote tree returned a path outside the requested base")


def to_local_agent_path(root: Path, target: Path) -> str:
    root = root.resolve()
    resolved = target.resolve()
    try:
        rel = resolved.relative_to(root)
    except ValueError as exc:
        raise AgentRemoteError(403, "path_escape", "Local path escapes root") from exc
    return "/" + rel.as_posix() if rel.as_posix() != "." else "/"


def path_from_payload(payload: dict[str, Any]) -> str:
    return clean_rel_path(str(payload.get("path", "/")))


def mkdir_path_from_payload(payload: dict[str, Any]) -> str:
    if "path" in payload:
        return path_from_payload(payload)
    parent = clean_rel_path(str(payload.get("parent", "/")))
    name = safe_name(str(payload.get("name", "")))
    return join_rel(parent, name)


def first(query: dict[str, list[str]], name: str, default: str) -> str:
    values = query.get(name)
    if not values:
        return default
    return values[0]


def remote_http_error(exc: HTTPError) -> AgentRemoteError:
    raw = exc.read()
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except json.JSONDecodeError:
        payload = {}
    return AgentRemoteError(
        exc.code,
        str(payload.get("error", "remote_error")),
        str(payload.get("message", exc.reason)),
        details=payload,
    )


def rate_limit_retry_delay(exc: HTTPError, attempt: int) -> float:
    retry_after = ""
    try:
        retry_after = exc.headers.get("Retry-After", "")
    except AttributeError:
        retry_after = ""
    try:
        if retry_after:
            return min(60.0, max(0.5, float(retry_after)))
    except ValueError:
        pass
    return min(15.0, 1.0 * (2**attempt))


def is_transfer_request_url(url: str) -> bool:
    return urlparse(url).path in {
        "/api/upload/status",
        "/api/upload/chunk",
        "/api/upload/finish",
        "/api/download",
    }


def run_master(
    host: str,
    port: int = DEFAULT_PORT,
    local_root: Path | None = None,
    password: str | None = None,
    token: str | None = None,
    ui_port: int = DEFAULT_UI_PORT,
    open_browser: bool = True,
    tls_fingerprint: str = "",
    tls_insecure: bool = False,
    ca_file: str = "",
    client_alias: str = "",
) -> None:
    if password is None and token is None:
        import getpass

        password = getpass.getpass("Slave password: ")
    root = (local_root or Path.cwd()).resolve()
    remote = RemoteClient(
        host,
        port,
        password,
        token=token,
        tls_fingerprint=tls_fingerprint,
        tls_insecure=tls_insecure,
        ca_file=ca_file,
        client_alias=client_alias,
    )
    state = MasterState(root, remote)
    server = bind_master_server(state, ui_port)
    actual_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{actual_port}"
    print()
    print("agent-remote-sync Master")
    print("========================")
    print(f"Remote: {remote.base_url}")
    print(f"Local:  {root}")
    print(f"UI:     {url}")
    if open_browser:
        webbrowser.open(url)
    print("Commands: [q] stop")
    try:
        if not input_available():
            wait_without_stdin("agent-remote-sync master")
        else:
            while True:
                try:
                    command = input("agentremote-master> ").strip().lower()
                except EOFError:
                    wait_without_stdin("agent-remote-sync master")
                    break
                if command in ("q", "quit", "exit"):
                    break
                if command:
                    print(f"UI: {url}")
    except KeyboardInterrupt:
        print()
    finally:
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


def bind_master_server(state: MasterState, start_port: int) -> AgentRemoteMasterServer:
    last_error: OSError | None = None
    for port in range(start_port, start_port + 50):
        try:
            return AgentRemoteMasterServer(("127.0.0.1", port), state)
        except OSError as exc:
            last_error = exc
    raise SystemExit(f"Could not bind a master UI port: {last_error}")


def _handle_dashboard_refresh_all(handler: Any) -> None:
    state = load_swarm_state()
    results = {}
    for node in topology_nodes(state):
        results[node] = _dashboard_probe_node(node, root=handler.server.state.local_root)
    send_json(handler, 200, {"ok": True, "results": results})


def _handle_dashboard_refresh_node(handler: Any, payload: dict[str, Any]) -> None:
    node = str(payload.get("node", ""))
    if not node:
        raise AgentRemoteError(400, "missing_node", "Node name is required")
    name = normalize_node_name(node)
    state = load_swarm_state()
    if name not in topology_nodes(state):
        raise AgentRemoteError(404, "node_not_found", f"Node {name} is not known")
    result = _dashboard_probe_node(name, root=handler.server.state.local_root)
    send_json(handler, 200, result)


def _handle_dashboard_calls_refresh(handler: Any) -> None:
    from .cli import refresh_call_records
    from .swarm import sanitize_call_record

    updated = refresh_call_records(handler.server.state.local_root)
    send_json(
        handler,
        200,
        {
            "ok": True,
            "refreshed": len(updated),
            "calls": [sanitize_call_record(call) for call in updated],
        },
    )


def _mobile_token_from_request(handler: Any) -> str:
    authorization = str(handler.headers.get("Authorization", "") or "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return str(handler.headers.get("X-AgentRemote-Mobile-Token", "") or "").strip()


def _require_mobile_scope(handler: Any, scope: str) -> dict[str, Any]:
    return verify_mobile_token(handler.server.state.local_root, _mobile_token_from_request(handler), scope)


def _handle_mobile_controller(handler: Any) -> None:
    device = _require_mobile_scope(handler, "read")
    data = get_mobile_controller_data(handler.server.state.local_root)
    with handler.server.state.lock:
        jobs = [job.as_dict() for job in handler.server.state.jobs.values()]
    jobs = sorted(jobs, key=lambda item: item.get("startedAt", 0), reverse=True)[:30]
    data["device"] = device
    data["transfers"] = {
        "active": sum(1 for job in jobs if job.get("state") in ("queued", "running")),
        "jobs": jobs,
    }
    send_json(handler, 200, data)


def _handle_dashboard_call_detail(handler: Any, call_id: str) -> None:
    call_id = _safe_dashboard_id(call_id, "call")
    from .cli import read_call_record
    from .swarm import sanitize_call_record

    data = read_call_record(call_id, root=handler.server.state.local_root)
    send_json(handler, 200, sanitize_call_record(data))


def _handle_dashboard_node_detail(handler: Any, node_name: str) -> None:
    alias = normalize_node_name(node_name)
    state = load_swarm_state()
    if alias not in topology_nodes(state):
        raise AgentRemoteError(404, "node_not_found", f"Node {alias} is not known")
    nodes_dict = state.get("nodes", {}).get(alias, {})
    connections = {c["name"]: c for c in iter_connections()}
    conn = connections.get(alias)
    routes = [r for r in merged_route_rows(state) if r.get("name") == alias]
    policy = whitelist_status(state, alias)
    send_json(handler, 200, {
        "name": alias,
        "status": nodes_dict.get("lastStatus", "unknown"),
        "lastSeenAt": nodes_dict.get("lastSeenAt"),
        "modelId": nodes_dict.get("modelId", conn.get("modelId", "") if conn else ""),
        "capabilities": nodes_dict.get("capabilities", []),
        "storage": nodes_dict.get("storage", {}),
        "routes": [{"host": r.get("host"), "port": int(r.get("port", 0)), "priority": int(r.get("priority", 100)), "tls": bool(r.get("tlsFingerprint")), "source": r.get("source", "explicit")} for r in routes],
        "policy": policy,
    })

def _handle_process_stop(handler: Any, payload: dict[str, Any]) -> None:
    root = handler.server.state.local_root
    process_id = str(payload.get("id", ""))
    if not process_id:
        send_json(handler, 400, {"ok": False, "error": "missing process id"})
        return
    from .swarm import get_process, process_is_running, process_stop_metadata_valid
    import signal
    proc = get_process(root, process_id)
    if not proc:
        send_json(handler, 404, {"ok": False, "error": "process not found"})
        return
    pid = int(proc.get("pid", 0))
    if not process_stop_metadata_valid(root, proc):
        send_json(handler, 403, {"ok": False, "error": "PID mismatch, refusing to stop unrelated process"})
        return
    if not process_is_running(pid):
        send_json(handler, 409, {"ok": False, "error": "process is not running"})
        return
    from .approval import require_approval
    require_approval(
        root,
        "process.stop",
        risk="high",
        origin_type="controller",
        summary=f"Stop local process {process_id}",
        details=f"role={proc.get('role', '')} pid={pid} root={proc.get('root', '')}",
        timeout=300,
        poll_interval=0.25,
    )
    try:
        os.kill(pid, signal.SIGTERM)
        send_json(handler, 200, {"ok": True, "stopped": process_id})
    except Exception as exc:
        send_json(handler, 500, {"ok": False, "error": str(exc)})

def _handle_process_forget(handler: Any, payload: dict[str, Any]) -> None:
    root = handler.server.state.local_root
    process_id = str(payload.get("id", ""))
    if not process_id:
        send_json(handler, 400, {"ok": False, "error": "missing process id"})
        return
    from .approval import require_approval
    require_approval(
        root,
        "process.forget",
        risk="medium",
        origin_type="controller",
        summary=f"Forget local process registry record {process_id}",
        details=f"processId={process_id}",
        timeout=300,
        poll_interval=0.25,
    )
    from .swarm import forget_process
    ok = forget_process(root, process_id)
    send_json(handler, 200, {"ok": ok, "forgotten": process_id if ok else None})


def _handle_daemon_profile_forget(handler: Any, payload: dict[str, Any]) -> None:
    root = handler.server.state.local_root
    name = str(payload.get("name", "") or "").strip()
    if not name:
        raise AgentRemoteError(400, "missing_profile", "Daemon profile name is required")
    from .approval import require_approval
    from .daemon_profiles import normalize_daemon_profile_name, remove_daemon_profile

    normalized = normalize_daemon_profile_name(name)
    require_approval(
        root,
        "daemon-profile.forget",
        risk="medium",
        origin_type="controller",
        summary=f"Forget daemon profile {normalized}",
        details=f"profile={normalized}",
        timeout=300,
        poll_interval=0.25,
    )
    ok = remove_daemon_profile(normalized)
    send_json(handler, 200, {"ok": ok, "forgotten": normalized if ok else None})


def _handle_worker_policy_init(handler: Any) -> None:
    root = handler.server.state.local_root
    from .approval import require_approval
    from .worker_policy import init_policy, worker_policy_summary

    require_approval(
        root,
        "worker-policy.init",
        risk="high",
        origin_type="controller",
        summary="Initialize worker command policy",
        details="create .agentremote/worker-policy.json",
        timeout=300,
        poll_interval=0.25,
    )
    init_policy(root)
    send_json(handler, 200, {"ok": True, "policy": worker_policy_summary(root)})


def _handle_worker_policy_apply_template(handler: Any, payload: dict[str, Any]) -> None:
    root = handler.server.state.local_root
    template = str(payload.get("template", "") or "").strip()
    if not template:
        raise AgentRemoteError(400, "missing_template", "Template name is required")
    from .approval import require_approval
    from .worker_policy import apply_template, sanitize_policy_text, worker_policy_summary

    require_approval(
        root,
        "worker-policy.apply-template",
        risk="high",
        origin_type="controller",
        summary=f"Apply worker policy template {sanitize_policy_text(template)}",
        details=f"template={sanitize_policy_text(template)}",
        timeout=300,
        poll_interval=0.25,
    )
    rule = apply_template(root, template)
    if not rule:
        raise AgentRemoteError(404, "template_not_found", f"Worker policy template not found: {sanitize_policy_text(template)}")
    send_json(handler, 200, {"ok": True, "rule": rule.get("name", ""), "policy": worker_policy_summary(root)})


def _handle_worker_policy_remove(handler: Any, payload: dict[str, Any]) -> None:
    root = handler.server.state.local_root
    rule_name = str(payload.get("name", "") or "").strip()
    if not rule_name:
        raise AgentRemoteError(400, "missing_rule", "Rule name is required")
    from .approval import require_approval
    from .worker_policy import remove_rule, sanitize_policy_text, worker_policy_summary

    require_approval(
        root,
        "worker-policy.remove",
        risk="high",
        origin_type="controller",
        summary=f"Remove worker policy rule {sanitize_policy_text(rule_name)}",
        details=f"rule={sanitize_policy_text(rule_name)}",
        timeout=300,
        poll_interval=0.25,
    )
    ok = remove_rule(root, rule_name)
    if not ok:
        raise AgentRemoteError(404, "rule_not_found", f"Worker policy rule not found: {sanitize_policy_text(rule_name)}")
    send_json(handler, 200, {"ok": True, "removed": sanitize_policy_text(rule_name), "policy": worker_policy_summary(root)})


def _dashboard_probe_node(node: str, *, root: Path, timeout: float = 3.0) -> dict[str, Any]:
    state = load_swarm_state()
    name = normalize_node_name(node)
    route = select_best_route([row for row in merged_route_rows(state) if row.get("name") == name])
    if not route:
        _save_dashboard_node_record(root, state, name, _dashboard_unknown_node(state, name, "no_route"))
        return {"ok": False, "node": name, "status": "unknown", "error": "no_route"}

    host = str(route.get("host", "") or "")
    port = int(route.get("port", 0) or 0)
    if not host or not port:
        _save_dashboard_node_record(root, state, name, _dashboard_unknown_node(state, name, "bad_route"))
        return {"ok": False, "node": name, "status": "unknown", "error": "bad_route"}

    try:
        secure = is_https_endpoint(host) or bool(
            route.get("tlsFingerprint") or route.get("tlsInsecure") or route.get("caFile")
        )
        url = probe_url(host, port, secure=secure).replace("/api/challenge", "/api/node")
        with open_url(
            Request(url, method="GET"),
            timeout=timeout,
            tls_fingerprint=str(route.get("tlsFingerprint", "") or ""),
            tls_insecure=bool(route.get("tlsInsecure", False)),
            ca_file=str(route.get("caFile", "") or ""),
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
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
        _save_dashboard_node_record(root, state, name, record)
        return {
            "ok": True,
            "node": name,
            "status": "online",
            "modelId": record["modelId"],
            "capabilities": record["capabilities"],
            "storage": record["storage"],
        }
    except Exception as exc:
        error = str(exc)[:200]
        previous = state.get("nodes", {}).get(name, {})
        record = {
            "name": name,
            "nodeName": previous.get("nodeName", name),
            "lastCheckedAt": time.time(),
            "lastSeenAt": previous.get("lastSeenAt"),
            "lastStatus": "offline",
            "lastError": error,
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
        _save_dashboard_node_record(root, state, name, record)
        return {
            "ok": False,
            "node": name,
            "status": "offline",
            "error": error,
            "modelId": record.get("modelId", ""),
        }


def _dashboard_unknown_node(state: dict[str, Any], node: str, error: str) -> dict[str, Any]:
    previous = state.get("nodes", {}).get(node, {})
    return {
        "name": node,
        "nodeName": previous.get("nodeName", node),
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


def _save_dashboard_node_record(root: Path, state: dict[str, Any], node: str, record: dict[str, Any]) -> None:
    state.setdefault("nodes", {})[node] = record
    save_swarm_state(state)
    journal_node_status(root, node, record)
    journal_routes_summary(root, state)


def _safe_dashboard_id(value: str, label: str) -> str:
    text = str(value or "").strip()
    if not text or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:" for ch in text):
        raise AgentRemoteError(400, f"bad_{label}_id", f"Bad {label} id")
    return text


def _sanitize_dashboard_route(route: dict[str, Any]) -> dict[str, Any]:
    return {
        "host": str(route.get("host", "")),
        "port": int(route.get("port", 0) or 0),
        "priority": int(route.get("priority", 100)),
        "tls": bool(route.get("tlsFingerprint")),
        "source": str(route.get("source", "explicit")),
        "routeType": str(route.get("routeType", "direct")),
        "lastLatencyMs": route.get("lastLatencyMs"),
        "lastError": route.get("lastError", ""),
    }


def _handle_inbox(handler: Any) -> None:
    root = handler.server.state.local_root
    from .handoff import list_handoffs
    from .inbox import list_instructions

    handoffs = list_handoffs(root)
    instructions = list_instructions(root)
    items = []
    for h in handoffs:
        items.append({
            "type": "handoff",
            "filename": h.get("filename", ""),
            "path": h.get("path", ""),
        })
    for inst in instructions:
        items.append({
            "type": "instruction",
            "id": inst.get("id", ""),
            "task": str(inst.get("task", ""))[:200],
            "from": inst.get("from", ""),
            "state": inst.get("state", "received"),
            "autoRun": bool(inst.get("autoRun", False)),
            "createdAt": inst.get("createdAt", 0),
            "handoffFile": inst.get("handoffFile", ""),
            "expectedReport": inst.get("expectedReport", ""),
            "callbackAlias": inst.get("callbackAlias", ""),
        })
    items.sort(key=lambda x: x.get("createdAt", 0) or 0, reverse=True)
    send_json(handler, 200, {"items": items})


def _handle_inbox_read(handler: Any, payload: dict[str, Any]) -> None:
    root = handler.server.state.local_root
    filename = str(payload.get("filename", "") or "").strip()
    if not filename:
        raise AgentRemoteError(400, "missing_filename", "Filename is required")
    from .handoff import read_handoff

    try:
        content = read_handoff(root, filename)
    except FileNotFoundError as exc:
        raise AgentRemoteError(404, "handoff_not_found", "Handoff file was not found") from exc
    send_json(handler, 200, {"filename": filename, "content": content})
