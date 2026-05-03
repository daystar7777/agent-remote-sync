from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import string
import threading
import time
from pathlib import Path
from typing import Any

from .common import AgentRemoteError, make_token

APPROVALS_DIR = ".agentremote/approvals"
APPROVAL_POLICY_FILE = ".agentremote/approval_policy.json"
APPROVAL_ID_CHARS = set(string.ascii_letters + string.digits + "-_.:")
APPROVAL_STATUSES = {"pending", "approved", "denied", "expired"}
APPROVAL_DECISIONS = {"approve": "approved", "approved": "approved", "deny": "denied", "denied": "denied"}
APPROVAL_RISKS = {"low", "medium", "high", "critical"}
APPROVAL_MODES = {"auto", "ask", "strict", "deny"}
RISK_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
SENSITIVE_ACTION_FRAGMENTS = (
    "delete",
    "remove",
    "rmdir",
    "unlink",
    "execute",
    "run",
    "shell",
    "command",
    "process.stop",
    "process.forget",
    "policy",
    "handoff.execute",
)
SECRET_PATTERN = re.compile(
    r"(?i)(password|passwd|pwd|token|secret|credential|api[_-]?key|private[_-]?key|proof)"
    r"(\s*[:=]\s*)?([^\s,;&]+)?"
)
_LOCK = threading.RLock()

def _approvals_root(root: Path) -> Path:
    d = root.resolve() / APPROVALS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d

def _safe_approval_id(approval_id: str) -> str:
    value = str(approval_id or "").strip()
    if not value or any(ch not in APPROVAL_ID_CHARS for ch in value) or "/" in value or "\\" in value:
        raise AgentRemoteError(400, "bad_approval_id", "Approval id contains unsafe characters")
    return value

def _approval_path(root: Path, approval_id: str) -> Path:
    return _approvals_root(root) / f"{_safe_approval_id(approval_id)}.json"

def _read_approval_file(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None

def _write_approval_file(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

def _approval_policy_path(root: Path) -> Path:
    return root.resolve() / APPROVAL_POLICY_FILE

def _sanitize_text(value: str, limit: int) -> str:
    text = str(value or "")[:limit]
    return SECRET_PATTERN.sub(lambda m: f"{m.group(1)}=[redacted]", text)

def _normalize_risk(value: str) -> str:
    risk = str(value or "medium").strip().lower()
    return risk if risk in APPROVAL_RISKS else "medium"

def _normalize_mode(value: str) -> str:
    mode = str(value or "auto").strip().lower()
    if mode not in APPROVAL_MODES:
        raise AgentRemoteError(400, "bad_approval_mode", f"Approval mode must be one of: {', '.join(sorted(APPROVAL_MODES))}")
    return mode

def _request_hash(record: dict[str, Any]) -> str:
    material = {
        "requestedAction": str(record.get("requestedAction", "")),
        "summary": str(record.get("summary", "")),
        "details": str(record.get("details", "")),
        "risk": str(record.get("risk", "")),
        "originType": str(record.get("originType", "")),
        "originNode": str(record.get("originNode", "")),
        "targetNode": str(record.get("targetNode", "")),
        "projectRoot": str(record.get("projectRoot", "")),
        "agentId": str(record.get("agentId", "")),
        "modelId": str(record.get("modelId", "")),
        "callId": str(record.get("callId", "")),
        "handoffId": str(record.get("handoffId", "")),
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

def _token_hash(token: str, request_hash: str) -> str:
    return hashlib.sha256(f"approval-token:{token}:{request_hash}".encode("utf-8")).hexdigest()

def _journal(root: Path, event_type: str, record: dict[str, Any]) -> None:
    try:
        from .swarm import journal_swarm_event

        title = f"Approval {event_type.lower().replace('_', ' ')}"
        body = (
            f"Approval: {record.get('approvalId', '')}\n"
            f"Status: {record.get('status', '')}\n"
            f"Origin: {record.get('originType', '')} {record.get('originNode', '')}\n"
            f"Action: {record.get('requestedAction', '')}\n"
            f"Risk: {record.get('risk', '')}\n"
            f"Summary: {record.get('summary', '')}"
        )
        journal_swarm_event(root, event_type, title, body)
    except Exception:
        pass

def _mark_expired_if_needed(root: Path, path: Path, record: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    current = time.time() if now is None else now
    try:
        expires_at = float(record.get("expiresAt", 0) or 0)
    except (TypeError, ValueError):
        expires_at = 0
    if record.get("status") == "pending" and expires_at and expires_at < current:
        record["status"] = "expired"
        record["decidedAt"] = current
        record["decidedBy"] = "system-expiry"
        _write_approval_file(path, record)
        _journal(root, "APPROVAL_EXPIRED", record)
    return record

def create_approval_request(
    root: Path,
    requested_action: str,
    *,
    summary: str = "",
    details: str = "",
    risk: str = "medium",
    origin_type: str = "local-agent",
    origin_node: str = "",
    target_node: str = "",
    agent_id: str = "",
    model_id: str = "",
    call_id: str = "",
    handoff_id: str = "",
    expires_in: float = 300.0,
) -> dict[str, Any]:
    now = time.time()
    approval_id = f"approval-{time.strftime('%Y%m%d-%H%M%S')}-{make_token()[:8]}"
    record = {
        "approvalId": approval_id,
        "createdAt": now,
        "expiresAt": now + max(0.001, float(expires_in)),
        "status": "pending",
        "originType": str(origin_type or "local-agent"),
        "originNode": str(origin_node or ""),
        "targetNode": str(target_node or ""),
        "projectRoot": str(root.resolve()),
        "agentId": str(agent_id or ""),
        "modelId": str(model_id or ""),
        "callId": str(call_id or ""),
        "handoffId": str(handoff_id or ""),
        "requestedAction": str(requested_action or ""),
        "risk": _normalize_risk(risk),
        "summary": _sanitize_text(summary, 500),
        "details": _sanitize_text(details, 2000),
        "requestHash": "",
        "approvalTokenHash": "",
        "approvalTokenExpiresAt": None,
        "approvalTokenUsedAt": None,
        "decidedAt": None,
        "decidedBy": "",
    }
    record["requestHash"] = _request_hash(record)
    path = _approval_path(root, approval_id)
    with _LOCK:
        _write_approval_file(path, record)
    _journal(root, "APPROVAL_REQUESTED", record)
    return record

def list_approval_requests(root: Path, status: str = "") -> list[dict[str, Any]]:
    base = _approvals_root(root)
    now = time.time()
    items = []
    for f in sorted(base.glob("approval-*.json"), key=lambda p: p.name, reverse=True):
        data = _read_approval_file(f)
        if not data:
            continue
        data = _mark_expired_if_needed(root, f, data, now=now)
        if status and data.get("status") != status:
            continue
        items.append(data)
    return items

def decide_approval(
    root: Path,
    approval_id: str,
    decision: str,
    *,
    decided_by: str = "",
) -> dict[str, Any]:
    path = _approval_path(root, approval_id)
    if not path.exists():
        raise AgentRemoteError(404, "approval_not_found", f"Approval {approval_id} not found")
    data = _read_approval_file(path)
    if not data:
        raise AgentRemoteError(400, "bad_approval_record", f"Approval {approval_id} is unreadable")
    data = _mark_expired_if_needed(root, path, data)
    if data.get("status") != "pending":
        raise AgentRemoteError(409, "approval_already_decided", f"Approval is already {data.get('status')}")
    now = time.time()
    normalized_decision = APPROVAL_DECISIONS.get(str(decision or "").strip().lower())
    if not normalized_decision:
        raise AgentRemoteError(400, "bad_decision", "Decision must be approved or denied")
    data["status"] = normalized_decision
    data["decidedAt"] = now
    data["decidedBy"] = _sanitize_text(decided_by, 120)
    if normalized_decision == "approved":
        token = make_token()
        data["approvalTokenHash"] = _token_hash(token, str(data.get("requestHash", "")))
        data["approvalTokenExpiresAt"] = now + 300
        result = dict(data)
        result["_approvalToken"] = token
    else:
        result = dict(data)
    with _LOCK:
        _write_approval_file(path, data)
    _journal(root, "APPROVAL_APPROVED" if normalized_decision == "approved" else "APPROVAL_DENIED", data)
    return result

def wait_for_approval(
    root: Path,
    approval_id: str,
    *,
    timeout: float = 300.0,
    poll_interval: float = 1.0,
) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        path = _approval_path(root, approval_id)
        if not path.exists():
            time.sleep(poll_interval)
            continue
        data = _read_approval_file(path)
        if not data:
            time.sleep(poll_interval)
            continue
        data = _mark_expired_if_needed(root, path, data)
        status = data.get("status", "pending")
        if status in ("approved", "denied", "expired"):
            return data
        time.sleep(poll_interval)
    return {"approvalId": approval_id, "status": "timeout"}

def verify_approval_token(
    root: Path,
    approval_id: str,
    token: str,
    *,
    action_hash: str = "",
) -> bool:
    path = _approval_path(root, approval_id)
    if not path.exists():
        return False
    data = _read_approval_file(path)
    if not data:
        return False
    data = _mark_expired_if_needed(root, path, data)
    if data.get("status") != "approved":
        return False
    if data.get("approvalTokenUsedAt"):
        return False
    try:
        token_expires_at = float(data.get("approvalTokenExpiresAt", 0) or 0)
    except (TypeError, ValueError):
        token_expires_at = 0
    if token_expires_at and token_expires_at < time.time():
        return False
    stored = data.get("approvalTokenHash", "")
    if not stored:
        return False
    expected = _token_hash(token, str(data.get("requestHash", "")))
    if action_hash:
        if action_hash != data.get("requestHash", ""):
            return False
    if not secrets.compare_digest(str(stored), expected):
        return False
    data["approvalTokenUsedAt"] = time.time()
    _write_approval_file(path, data)
    return True

def get_approval_count(root: Path, status: str = "pending") -> int:
    return len(list_approval_requests(root, status=status))

def sanitize_approval(record: dict[str, Any]) -> dict[str, Any]:
    allowed = {"approvalId", "status", "createdAt", "expiresAt", "originType", "originNode", "targetNode", "risk", "summary", "requestedAction", "requestHash", "decidedAt", "decidedBy", "agentId", "modelId"}
    sanitized = {}
    for k in allowed:
        if k in record:
            sanitized[k] = record[k]
    sanitized["approvalId"] = str(record.get("approvalId", ""))
    sanitized["status"] = str(record.get("status", "pending"))
    return sanitized

def cleanup_expired_approvals(root: Path) -> int:
    base = _approvals_root(root)
    now = time.time()
    marked = 0
    for path in base.glob("approval-*.json"):
        record = _read_approval_file(path)
        if not record or record.get("status") != "pending":
            continue
        try:
            expires_at = float(record.get("expiresAt", 0) or 0)
        except (TypeError, ValueError):
            expires_at = 0
        if expires_at and expires_at < now:
            _mark_expired_if_needed(root, path, record, now=now)
            marked += 1
    return marked


# --- Approval Policy and Enforcement ---

def load_approval_policy(root: Path) -> dict[str, Any]:
    path = _approval_policy_path(root)
    if not path.exists():
        return {"mode": "auto", "rules": {}, "updatedAt": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"mode": "auto", "rules": {}, "updatedAt": None}
    if not isinstance(data, dict):
        return {"mode": "auto", "rules": {}, "updatedAt": None}
    try:
        mode = _normalize_mode(str(data.get("mode", "auto")))
    except AgentRemoteError:
        mode = "auto"
    rules = data.get("rules", {})
    if not isinstance(rules, dict):
        rules = {}
    return {
        "mode": mode,
        "rules": rules,
        "updatedAt": data.get("updatedAt"),
    }


def save_approval_policy(root: Path, mode: str, rules: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = {
        "mode": _normalize_mode(mode),
        "rules": rules if isinstance(rules, dict) else {},
        "updatedAt": time.time(),
    }
    _write_json_file(_approval_policy_path(root), policy)
    try:
        from .swarm import journal_swarm_event

        journal_swarm_event(
            root,
            "APPROVAL_POLICY_CHANGED",
            "Approval policy changed",
            f"Mode: {policy['mode']}",
        )
    except Exception:
        pass
    return policy


def sensitive_action(requested_action: str, risk: str = "medium") -> bool:
    normalized_risk = _normalize_risk(risk)
    if RISK_RANK[normalized_risk] >= RISK_RANK["high"]:
        return True
    action = str(requested_action or "").lower()
    return any(fragment in action for fragment in SENSITIVE_ACTION_FRAGMENTS)


def approval_required(root: Path, requested_action: str, risk: str = "medium", origin_type: str = "local-agent") -> bool:
    mode = load_approval_policy(root)["mode"]
    if mode == "auto":
        return False
    if mode == "deny":
        return True
    if mode == "strict":
        return True
    return sensitive_action(requested_action, risk)


def require_approval(
    root: Path,
    requested_action: str,
    *,
    summary: str = "",
    details: str = "",
    risk: str = "medium",
    origin_type: str = "local-agent",
    origin_node: str = "",
    target_node: str = "",
    agent_id: str = "",
    model_id: str = "",
    call_id: str = "",
    handoff_id: str = "",
    timeout: float = 300.0,
    poll_interval: float = 1.0,
    decided_by: str = "approval-broker",
) -> dict[str, Any]:
    policy = load_approval_policy(root)
    mode = policy["mode"]
    normalized_risk = _normalize_risk(risk)
    if mode == "auto" or (mode == "ask" and not sensitive_action(requested_action, normalized_risk)):
        return {"allowed": True, "mode": mode, "status": "auto"}
    if mode == "deny":
        raise AgentRemoteError(403, "approval_denied", f"Approval policy denied {requested_action}")

    request = create_approval_request(
        root,
        requested_action,
        summary=summary,
        details=details,
        risk=normalized_risk,
        origin_type=origin_type,
        origin_node=origin_node,
        target_node=target_node,
        agent_id=agent_id,
        model_id=model_id,
        call_id=call_id,
        handoff_id=handoff_id,
        expires_in=max(1.0, timeout),
    )
    decision = wait_for_approval(
        root,
        request["approvalId"],
        timeout=timeout,
        poll_interval=poll_interval,
    )
    status = str(decision.get("status", "timeout"))
    if status != "approved":
        raise AgentRemoteError(403, f"approval_{status}", f"Approval for {requested_action} was {status}")
    token = str(decision.get("_approvalToken", "") or "")
    if token and not verify_approval_token(root, request["approvalId"], token, action_hash=request["requestHash"]):
        raise AgentRemoteError(403, "approval_token_invalid", "Approval token could not be verified")
    return {
        "allowed": True,
        "mode": mode,
        "status": status,
        "approvalId": request["approvalId"],
    }
