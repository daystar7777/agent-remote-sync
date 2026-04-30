from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from typing import Any

from .common import (
    CHUNK_SIZE,
    AgentRemoteSyncError,
    RESERVED_DIR_NAMES,
    clean_rel_path,
    ensure_storage_available,
    format_bytes,
    join_rel,
    partial_paths,
    resolve_path,
    sha256_file,
    storage_info,
    storage_error,
)
from .headless import print_progress, resolve_conflicts
from .master import RemoteClient, upload_required_bytes
from .state import TransferLogger, plans_dir, rel_state_path
from .workmem import record_host_event


MTIME_TOLERANCE = 1.0


def sync_plan_push(
    local_root: Path,
    local_path: Path,
    remote_dir: str,
    remote: RemoteClient,
    *,
    compare_hash: bool = False,
) -> dict[str, Any]:
    source_root = resolve_local_sync_root(local_root, local_path)
    remote_dir = clean_rel_path(remote_dir)
    local_entries = local_index(source_root)
    remote_entries = remote_index(remote, remote_dir)
    local_dirs = local_dir_index(source_root)
    remote_dirs = remote_dir_index(remote, remote_dir)
    plan = build_sync_plan("push", local_entries, remote_entries, str(source_root), remote_dir)
    attach_create_dirs(plan, local_dirs, remote_dirs, remote_dir)
    if compare_hash:
        refine_hash_conflicts(plan, remote)
    return plan


def sync_plan_pull(
    local_root: Path,
    remote_dir: str,
    local_path: Path,
    remote: RemoteClient,
    *,
    compare_hash: bool = False,
) -> dict[str, Any]:
    target_root = (local_path if local_path.is_absolute() else local_root / local_path).resolve()
    remote_dir = clean_rel_path(remote_dir)
    remote_entries = remote_index(remote, remote_dir, missing_ok=False)
    local_entries = local_index(target_root) if target_root.exists() else {}
    remote_dirs = remote_dir_index(remote, remote_dir, missing_ok=False)
    local_dirs = local_dir_index(target_root) if target_root.exists() else {}
    plan = build_sync_plan("pull", remote_entries, local_entries, remote_dir, str(target_root))
    attach_create_dirs(plan, remote_dirs, local_dirs, str(target_root))
    if compare_hash:
        refine_hash_conflicts(plan, remote)
    return plan


def build_sync_plan(
    direction: str,
    source: dict[str, dict[str, Any]],
    target: dict[str, dict[str, Any]],
    source_root: str,
    target_root: str,
) -> dict[str, Any]:
    copy_files = []
    conflicts = []
    skipped = []
    for rel, source_item in sorted(source.items()):
        target_item = target.get(rel)
        action = {
            "rel": rel,
            "source": source_item["path"],
            "target": join_rel(target_root, rel) if target_root.startswith("/") else str(Path(target_root) / rel),
            "size": int(source_item["size"]),
            "mtime": source_item.get("mtime"),
            "reason": "missing",
        }
        if target_item is None:
            copy_files.append(action)
        elif int(target_item["size"]) != int(source_item["size"]) or not close_mtime(
            target_item.get("mtime"), source_item.get("mtime")
        ):
            action["reason"] = "changed"
            conflicts.append(action)
        else:
            skipped.append({"rel": rel, "reason": "same"})
    delete_candidates = []
    for rel, target_item in sorted(target.items()):
        if rel not in source:
            delete_candidates.append({"rel": rel, "path": target_item["path"], "size": int(target_item["size"])})
    total_copy_bytes = sum(item["size"] for item in copy_files)
    total_conflict_bytes = sum(item["size"] for item in conflicts)
    return {
        "direction": direction,
        "sourceRoot": source_root,
        "targetRoot": target_root,
        "copy": copy_files,
        "conflicts": conflicts,
        "deleteCandidates": delete_candidates,
        "createDirs": [],
        "skipped": skipped,
        "summary": {
            "sourceFiles": len(source),
            "targetFiles": len(target),
            "copyFiles": len(copy_files),
            "conflicts": len(conflicts),
            "deleteCandidates": len(delete_candidates),
            "createDirs": 0,
            "skipped": len(skipped),
            "copyBytes": total_copy_bytes,
            "conflictBytes": total_conflict_bytes,
        },
    }


def sync_push(
    host: str,
    port: int,
    password: str | None,
    local_path: Path,
    remote_dir: str,
    *,
    token: str | None = None,
    overwrite: bool = False,
    delete: bool = False,
    alias: str = "",
    local_root: Path | None = None,
    tls_fingerprint: str = "",
    tls_insecure: bool = False,
    ca_file: str = "",
    compare_hash: bool = False,
) -> dict[str, Any]:
    root = (local_root or Path.cwd()).resolve()
    remote = RemoteClient(
        host,
        port,
        password,
        token=token,
        tls_fingerprint=tls_fingerprint,
        tls_insecure=tls_insecure,
        ca_file=ca_file,
    )
    source_root = resolve_local_sync_root(root, local_path)
    plan = sync_plan_push(root, source_root, remote_dir, remote, compare_hash=compare_hash)
    write_plan(root, plan)
    conflicts = [item["target"] for item in plan["conflicts"]]
    overwrite = resolve_conflicts(conflicts, overwrite, "remote")
    apply_delete = resolve_delete_candidates(plan["deleteCandidates"], delete, "remote")
    items = list(plan["copy"]) + (list(plan["conflicts"]) if overwrite else [])
    logger = TransferLogger(root, "sync-push", remote=remote.base_url, alias=alias)
    total = sum(item["size"] for item in items)
    logger.start(
        total_files=len(items),
        total_bytes=total,
        source=str(source_root),
        remoteDir=clean_rel_path(remote_dir),
        overwrite=overwrite,
        deleteAllowed=delete,
        plan=plan.get("planFile", ""),
    )
    try:
        required_bytes = upload_required_bytes(remote, items)
        if required_bytes:
            ensure_storage_available(remote.storage(), required_bytes, "remote destination")
        ensure_remote_dirs(remote, [item["target"] for item in items])
        for directory in plan["createDirs"]:
            remote.mkdir(directory["target"])
        transfer_push_items(source_root, remote, items, overwrite, logger, total)
        if apply_delete:
            delete_remote_items(remote, plan["deleteCandidates"], logger)
    except OSError as exc:
        mapped = storage_error(exc, "local sync push read")
        logger.fail(mapped)
        raise mapped from exc
    except Exception as exc:
        logger.fail(exc)
        raise
    logger.complete(plan=plan.get("planFile", ""))
    session = logger.summary()
    if alias:
        record_host_event(
            root,
            alias,
            host=host,
            port=port,
            event_type="SYNC_PUSH",
            summary=f"Synced {source_root} to {remote_dir}.",
            extra={
                "files": len(items),
                "bytes": total,
                "conflicts": len(plan["conflicts"]),
                "deleteCandidates": len(plan["deleteCandidates"]),
                "deletesApplied": len(plan["deleteCandidates"]) if apply_delete else 0,
                "session": session["session"],
                "log": session["log"],
                "plan": plan.get("planFile", ""),
            },
        )
    print(f"sync push complete: {len(items)} file(s), {format_bytes(total)}")
    return {"plan": plan, "session": session, "transferred": items}


def sync_pull(
    host: str,
    port: int,
    password: str | None,
    remote_dir: str,
    local_path: Path,
    *,
    token: str | None = None,
    overwrite: bool = False,
    delete: bool = False,
    alias: str = "",
    local_root: Path | None = None,
    tls_fingerprint: str = "",
    tls_insecure: bool = False,
    ca_file: str = "",
    compare_hash: bool = False,
) -> dict[str, Any]:
    root = (local_root or Path.cwd()).resolve()
    target_root = (local_path if local_path.is_absolute() else root / local_path).resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    remote = RemoteClient(
        host,
        port,
        password,
        token=token,
        tls_fingerprint=tls_fingerprint,
        tls_insecure=tls_insecure,
        ca_file=ca_file,
    )
    plan = sync_plan_pull(root, remote_dir, target_root, remote, compare_hash=compare_hash)
    write_plan(root, plan)
    conflicts = [item["target"] for item in plan["conflicts"]]
    overwrite = resolve_conflicts(conflicts, overwrite, "local")
    apply_delete = resolve_delete_candidates(plan["deleteCandidates"], delete, "local")
    items = list(plan["copy"]) + (list(plan["conflicts"]) if overwrite else [])
    logger = TransferLogger(root, "sync-pull", remote=remote.base_url, alias=alias)
    total = sum(item["size"] for item in items)
    logger.start(
        total_files=len(items),
        total_bytes=total,
        remoteDir=clean_rel_path(remote_dir),
        localDir=str(target_root),
        overwrite=overwrite,
        deleteAllowed=delete,
        plan=plan.get("planFile", ""),
    )
    try:
        required_bytes = sync_download_required_bytes(target_root, items)
        if required_bytes:
            ensure_storage_available(storage_info(target_root), required_bytes, "local destination")
        create_local_dirs(plan["createDirs"])
        transfer_pull_items(target_root, remote, items, overwrite, logger, total)
        if apply_delete:
            delete_local_items(target_root, plan["deleteCandidates"], logger)
    except OSError as exc:
        mapped = storage_error(exc, "local sync pull write")
        logger.fail(mapped)
        raise mapped from exc
    except Exception as exc:
        logger.fail(exc)
        raise
    logger.complete(plan=plan.get("planFile", ""))
    session = logger.summary()
    if alias:
        record_host_event(
            root,
            alias,
            host=host,
            port=port,
            event_type="SYNC_PULL",
            summary=f"Synced {remote_dir} into {target_root}.",
            extra={
                "files": len(items),
                "bytes": total,
                "conflicts": len(plan["conflicts"]),
                "deleteCandidates": len(plan["deleteCandidates"]),
                "deletesApplied": len(plan["deleteCandidates"]) if apply_delete else 0,
                "session": session["session"],
                "log": session["log"],
                "plan": plan.get("planFile", ""),
            },
        )
    print(f"sync pull complete: {len(items)} file(s), {format_bytes(total)}")
    return {"plan": plan, "session": session, "transferred": items}


def resolve_local_sync_root(local_root: Path, local_path: Path) -> Path:
    target = local_path if local_path.is_absolute() else local_root / local_path
    target = target.resolve()
    if not target.exists():
        raise AgentRemoteSyncError(404, "not_found", f"Local sync path not found: {local_path}")
    if not target.is_dir():
        raise AgentRemoteSyncError(400, "sync_requires_directory", "sync currently requires a directory")
    return target


def local_index(root: Path) -> dict[str, dict[str, Any]]:
    root = root.resolve()
    entries: dict[str, dict[str, Any]] = {}
    if not root.exists():
        return entries
    def walk_error(exc: OSError) -> None:
        raise storage_error(exc, "local sync scan") from exc

    for current, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        dirs[:] = [name for name in dirs if name not in RESERVED_DIR_NAMES]
        current_path = Path(current)
        for filename in files:
            child = current_path / filename
            if child.is_symlink():
                continue
            rel = child.relative_to(root).as_posix()
            try:
                stat = child.stat()
            except OSError as exc:
                raise storage_error(exc, f"local sync scan {child}") from exc
            entries[rel] = {
                "rel": rel,
                "path": str(child),
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            }
    return entries


def local_dir_index(root: Path) -> dict[str, dict[str, Any]]:
    root = root.resolve()
    entries: dict[str, dict[str, Any]] = {}
    if not root.exists():
        return entries

    def walk_error(exc: OSError) -> None:
        raise storage_error(exc, "local sync directory scan") from exc

    for current, dirs, _files in os.walk(root, followlinks=False, onerror=walk_error):
        dirs[:] = [name for name in dirs if name not in RESERVED_DIR_NAMES]
        current_path = Path(current)
        for dirname in dirs:
            child = current_path / dirname
            if child.is_symlink():
                continue
            rel = child.relative_to(root).as_posix()
            entries[rel] = {"rel": rel, "path": str(child)}
    return entries


def remote_index(remote: RemoteClient, remote_dir: str, *, missing_ok: bool = True) -> dict[str, dict[str, Any]]:
    remote_dir = clean_rel_path(remote_dir)
    stat = remote.stat(remote_dir)
    if not stat.get("exists"):
        if not missing_ok:
            raise AgentRemoteSyncError(404, "not_found", f"Remote sync path not found: {remote_dir}")
        return {}
    if stat["entry"]["type"] != "dir":
        raise AgentRemoteSyncError(400, "sync_requires_directory", "remote sync path must be a directory")
    entries: dict[str, dict[str, Any]] = {}
    for entry in remote.tree(remote_dir):
        if entry["path"] == remote_dir or entry["type"] != "file":
            continue
        rel = remote_relative(remote_dir, entry["path"])
        entries[rel] = {
            "rel": rel,
            "path": entry["path"],
            "size": int(entry["size"]),
            "mtime": entry.get("modified"),
        }
    return entries


def remote_dir_index(remote: RemoteClient, remote_dir: str, *, missing_ok: bool = True) -> dict[str, dict[str, Any]]:
    remote_dir = clean_rel_path(remote_dir)
    stat = remote.stat(remote_dir)
    if not stat.get("exists"):
        if not missing_ok:
            raise AgentRemoteSyncError(404, "not_found", f"Remote sync path not found: {remote_dir}")
        return {}
    if stat["entry"]["type"] != "dir":
        raise AgentRemoteSyncError(400, "sync_requires_directory", "remote sync path must be a directory")
    entries: dict[str, dict[str, Any]] = {}
    for entry in remote.tree(remote_dir):
        if entry["path"] == remote_dir or entry["type"] != "dir":
            continue
        rel = remote_relative(remote_dir, entry["path"])
        entries[rel] = {"rel": rel, "path": entry["path"]}
    return entries


def attach_create_dirs(
    plan: dict[str, Any],
    source_dirs: dict[str, dict[str, Any]],
    target_dirs: dict[str, dict[str, Any]],
    target_root: str,
) -> None:
    create_dirs = []
    for rel in sorted(source_dirs):
        if rel in target_dirs:
            continue
        target = join_rel(target_root, rel) if target_root.startswith("/") else str(Path(target_root) / rel)
        create_dirs.append({"rel": rel, "target": target})
    plan["createDirs"] = create_dirs
    plan["summary"]["createDirs"] = len(create_dirs)


def create_local_dirs(items: list[dict[str, Any]]) -> None:
    for item in items:
        Path(item["target"]).mkdir(parents=True, exist_ok=True)


def remote_relative(base: str, child: str) -> str:
    base_clean = clean_rel_path(base).strip("/")
    child_clean = clean_rel_path(child).strip("/")
    if not base_clean:
        return child_clean
    prefix = base_clean + "/"
    if child_clean.startswith(prefix):
        return child_clean[len(prefix) :]
    raise AgentRemoteSyncError(400, "bad_tree", "Remote tree returned a path outside the requested sync root")


def close_mtime(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return True
    try:
        return abs(float(left) - float(right)) <= MTIME_TOLERANCE
    except (TypeError, ValueError):
        return True


def refine_hash_conflicts(plan: dict[str, Any], remote: RemoteClient) -> None:
    remaining = []
    for item in plan["conflicts"]:
        if item["reason"] != "changed":
            remaining.append(item)
            continue
        if remote_hash_matches_local(remote, item, str(plan.get("direction", ""))):
            plan["skipped"].append({"rel": item["rel"], "reason": "same_hash"})
        else:
            remaining.append(item)
    plan["conflicts"] = remaining
    plan["summary"]["conflicts"] = len(remaining)
    plan["summary"]["skipped"] = len(plan["skipped"])
    plan["summary"]["conflictBytes"] = sum(int(item["size"]) for item in remaining)
    plan["summary"]["compareHash"] = True


def remote_hash_matches_local(remote: RemoteClient, item: dict[str, Any], direction: str) -> bool:
    if direction == "push":
        remote_path = item["target"]
        local_path = Path(item["source"])
    elif direction == "pull":
        remote_path = item["source"]
        local_path = Path(item["target"])
    else:
        return False
    if not local_path.exists() or not local_path.is_file():
        return False
    return remote_sha256(remote, remote_path) == sha256_file(local_path)


def remote_sha256(remote: RemoteClient, path: str) -> str:
    import hashlib

    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = remote.download_chunk(path, offset, CHUNK_SIZE)
        if not chunk:
            break
        digest.update(chunk)
        offset += len(chunk)
        if len(chunk) < CHUNK_SIZE:
            break
    return digest.hexdigest()


def resolve_delete_candidates(candidates: list[dict[str, Any]], delete: bool, side: str) -> bool:
    if not candidates or not delete:
        return False
    print(f"{len(candidates)} {side} delete candidate(s):")
    for item in candidates[:20]:
        print(f"- {item['path']}")
    if len(candidates) > 20:
        print(f"- ... and {len(candidates) - 20} more")
    import sys

    if not sys.stdin.isatty():
        return True
    try:
        answer = input("Delete these files? [y/N] ").strip().lower()
    except EOFError:
        return False
    if answer not in ("y", "yes"):
        raise AgentRemoteSyncError(409, "delete_cancelled", "Sync delete was cancelled")
    return True


def delete_remote_items(remote: RemoteClient, candidates: list[dict[str, Any]], logger: TransferLogger) -> None:
    for item in candidates:
        remote.delete(item["path"])
        logger.event("delete_completed", target=item["path"], size=int(item.get("size", 0)))


def delete_local_items(target_root: Path, candidates: list[dict[str, Any]], logger: TransferLogger) -> None:
    root = target_root.resolve()
    for item in candidates:
        target = Path(item["path"]).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise AgentRemoteSyncError(403, "path_escape", "Delete candidate escapes sync root") from exc
        if target.is_dir():
            raise AgentRemoteSyncError(400, "delete_candidate_not_file", "Sync delete candidates must be files")
        target.unlink(missing_ok=True)
        logger.event("delete_completed", target=str(target), size=int(item.get("size", 0)))


def write_plan(root: Path, plan: dict[str, Any]) -> dict[str, Any]:
    from .state import make_session_id

    plan_id = make_session_id(f"sync-{plan['direction']}-plan")
    path = plans_dir(root) / f"{plan_id}.json"
    plan["planId"] = plan_id
    plan["planFile"] = rel_state_path(root, path)
    path.write_text(json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    return plan


def ensure_remote_dirs(remote: RemoteClient, targets: list[str]) -> None:
    dirs = sorted({parent_dir(path) for path in targets if parent_dir(path)}, key=lambda item: item.count("/"))
    for directory in dirs:
        remote.mkdir(directory)


def parent_dir(path: str) -> str:
    parent = PurePosixPath(clean_rel_path(path)).parent.as_posix()
    return clean_rel_path(parent)


def transfer_push_items(
    source_root: Path,
    remote: RemoteClient,
    items: list[dict[str, Any]],
    overwrite: bool,
    logger: TransferLogger,
    total: int,
) -> None:
    done = 0
    for item in items:
        source = source_root / item["rel"]
        digest = sha256_file(source)
        status = remote.upload_status(item["target"], item["size"])
        offset = int(status.get("partialSize", 0))
        if status.get("exists") and not overwrite:
            raise AgentRemoteSyncError(409, "exists", f"Remote file exists: {item['target']}")
        if offset > item["size"]:
            raise AgentRemoteSyncError(409, "bad_partial", f"Remote partial is larger than source: {item['target']}")
        logger.file_started(str(source), item["target"], item["size"], resume_offset=offset)
        print(f"sync upload {item['rel']} -> {item['target']}")
        done += offset
        with source.open("rb") as handle:
            handle.seek(offset)
            current_offset = offset
            while current_offset < item["size"]:
                chunk = handle.read(min(CHUNK_SIZE, item["size"] - current_offset))
                if not chunk:
                    break
                response = remote.upload_chunk(
                    item["target"],
                    current_offset,
                    item["size"],
                    chunk,
                    overwrite=overwrite,
                )
                current_offset = int(response.get("received", current_offset + len(chunk)))
                done += len(chunk)
                print_progress(done, total)
        remote.upload_finish(item["target"], item["size"], item["mtime"], digest, overwrite=overwrite)
        logger.file_completed(str(source), item["target"], item["size"])


def transfer_pull_items(
    target_root: Path,
    remote: RemoteClient,
    items: list[dict[str, Any]],
    overwrite: bool,
    logger: TransferLogger,
    total: int,
) -> None:
    done = 0
    for item in items:
        target = target_root / item["rel"]
        if target.exists() and not overwrite:
            raise AgentRemoteSyncError(409, "exists", f"Local file exists: {item['target']}")
        part, meta = partial_paths(target_root, "/" + item["rel"])
        offset = part.stat().st_size if part.exists() else 0
        if offset > item["size"]:
            part.unlink()
            offset = 0
        logger.file_started(item["source"], str(target), item["size"], resume_offset=offset)
        print(f"sync download {item['source']} -> {item['rel']}")
        done += offset
        with part.open("ab") as handle:
            current_offset = offset
            while current_offset < item["size"]:
                length = min(CHUNK_SIZE, item["size"] - current_offset)
                chunk = remote.download_chunk(item["source"], current_offset, length)
                if not chunk:
                    raise AgentRemoteSyncError(502, "empty_chunk", "Remote returned an empty chunk")
                handle.write(chunk)
                current_offset += len(chunk)
                done += len(chunk)
                print_progress(done, total)
        if part.stat().st_size != item["size"]:
            raise AgentRemoteSyncError(400, "size_mismatch", f"Downloaded size mismatch: {item['target']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not overwrite:
            raise AgentRemoteSyncError(409, "exists", f"Local file exists: {item['target']}")
        part.replace(target)
        if meta.exists():
            meta.unlink()
        if item.get("mtime"):
            os.utime(target, (float(item["mtime"]), float(item["mtime"])))
        logger.file_completed(item["source"], str(target), item["size"])


def sync_download_required_bytes(target_root: Path, items: list[dict[str, Any]]) -> int:
    required = 0
    for item in items:
        size = int(item["size"])
        part, _ = partial_paths(target_root, "/" + item["rel"])
        offset = part.stat().st_size if part.exists() else 0
        required += size if offset > size else size - offset
    return required
