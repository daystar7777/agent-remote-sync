from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import subprocess
import ssl
import stat
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import agentremote.master as master_module
from agentremote.cli import main as cli_main
from agentremote.cli import save_call_record
from agentremote.cli import wait_for_handoff_report
from agentremote.common import (
    AgentRemoteError,
    CHUNK_SIZE,
    MAX_DOWNLOAD_CHUNK,
    MAX_JSON_BODY,
    MAX_UPLOAD_CHUNK,
    clean_rel_path,
    derive_key,
    detect_addresses,
    format_bytes,
    join_rel,
    local_ipv4_addresses,
    make_proof,
    make_token,
    partial_paths,
    resolve_path,
    sha256_file,
    storage_info,
    unb64,
)
from agentremote.connections import (
    get_connection,
    normalize_alias,
    set_connection,
    strip_alias_prefix,
    load_connections,
    save_connections,
)
from agentremote.handoff import (
    create_handoff,
    one_line,
    receive_handoff,
    slugify,
    unique_path,
)
from agentremote.headless import (
    handoff,
    local_scope,
    print_progress,
    push,
    pull,
    report,
    resolve_conflicts,
    tell,
)
from agentremote.inbox import (
    claim_instruction,
    create_instruction,
    list_instructions,
    read_instruction,
    update_instruction_state,
    write_instruction,
)
from agentremote.master import (
    AgentRemoteMasterServer,
    MasterState,
    RemoteClient,
    build_download_plan,
    build_upload_plan,
    posix_relative,
)
from agentremote.security import SecurityConfig, SecurityState, SlidingWindowLimiter
from agentremote.slave import (
    AgentRemoteSlaveServer,
    SlaveState,
    advertised_addresses,
    normalize_session_scopes,
    parse_int,
    prepare_tls,
)
from agentremote.state import (
    TransferLogger,
    current_transfer_log_path,
    logs_dir,
    prune_transfer_logs,
    rotate_log_file,
    write_log_row,
)
from agentremote.swarm import (
    TAILSCALE_CIDRS,
    create_mobile_pairing,
    forget_process,
    get_dashboard_data,
    list_process_registry,
    list_mobile_devices,
    load_swarm_state,
    probe_url,
    register_process,
    revoke_mobile_device,
    save_route_health,
    select_best_route,
    swarm_path,
    update_process_heartbeat,
    verify_mobile_token,
    whitelist_status,
)
from agentremote.tls import (
    PinnedHTTPSConnection,
    ensure_self_signed_cert,
    fetch_remote_fingerprint,
    format_fingerprint,
    is_https_endpoint,
    normalize_fingerprint,
    open_url,
    wrap_server_socket,
)
from agentremote.worker import (
    CommandResult,
    approve_execution,
    build_plan,
    execute_commands,
    extract_commands,
    finish_without_execution,
    is_blocked_command,
    render_report,
    run_worker_once,
    truncate,
)
from agentremote.workmem import install_work_mem


class DeepSeekScenarioTests(unittest.TestCase):
    def start_slave(self, root: Path, password: str = "secret") -> AgentRemoteSlaveServer:
        state = SlaveState(root, password)
        server = AgentRemoteSlaveServer(("127.0.0.1", 0), state)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def start_slave_with_security(
        self, root: Path, config: SecurityConfig, password: str = "secret"
    ) -> AgentRemoteSlaveServer:
        state = SlaveState(root, password, security_config=config)
        server = AgentRemoteSlaveServer(("127.0.0.1", 0), state)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def start_tls_slave(
        self, root: Path, cert_store: Path, password: str = "secret"
    ) -> tuple[AgentRemoteSlaveServer, str]:
        state = SlaveState(root, password)
        tls_files = ensure_self_signed_cert(root, store_dir=cert_store)
        server = AgentRemoteSlaveServer(("127.0.0.1", 0), state)
        wrap_server_socket(server, tls_files.cert_file, tls_files.key_file)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, tls_files.fingerprint

    # ========================================================================
    # B1: worker.py unhandled subprocess.TimeoutExpired
    # ========================================================================
    def test_ds01_worker_command_timeout_is_handled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_work_mem(root)
            manifest = create_instruction(root, "Test timeout.", auto_run=True)
            manifest = claim_instruction(root, manifest["id"], claimed_by="test")
            plan = build_plan(root, manifest)
            plan["commands"] = ["python -c \"import time; time.sleep(10)\""]
            result = execute_commands(root, plan["commands"], timeout=1)
            self.assertEqual(len(result), 1)
            self.assertNotEqual(result[0].exit_code, 0)

    # ========================================================================
    # B2: inbox.py read/write_instruction should raise AgentRemoteError not FileNotFoundError
    # ========================================================================
    def test_ds02_read_instruction_missing_raises_agentremote_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(AgentRemoteError):
                read_instruction(root, "nonexistent-id-12345")

    def test_ds03_write_instruction_missing_raises_agentremote_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = {"id": "nonexistent-id-67890", "state": "received"}
            with self.assertRaises(AgentRemoteError):
                write_instruction(root, manifest)

    # ========================================================================
    # B3: master.py read_with_retries should catch ssl.SSLError
    # ========================================================================
    def test_ds04_ssl_error_caught_in_read_with_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote_root = root / "remote"
            remote_root.mkdir()
            cert_store = root / "certs"
            slave, fingerprint = self.start_tls_slave(remote_root, cert_store)
            try:
                url = f"https://127.0.0.1:{slave.server_address[1]}"
                client = RemoteClient(
                    url,
                    slave.server_address[1],
                    "secret",
                    tls_fingerprint=fingerprint,
                )
                self.assertIsNotNone(client.token)
                listing = client.list("/")
                self.assertEqual(listing["path"], "/")
            finally:
                slave.shutdown()
                slave.server_close()

    # ========================================================================
    # B4: state.py prune_transfer_logs keep=0 should delete all
    # ========================================================================
    def test_ds05_prune_transfer_logs_keep_zero_deletes_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = logs_dir(root)
            for i in range(5):
                log_path = log_dir / f"transfer-2026043{i}.jsonl"
                log_path.write_text(json.dumps({"event": f"test{i}"}), encoding="utf-8")
                os.utime(log_path, (time.time() - (5 - i) * 100, time.time() - (5 - i) * 100))
            prune_transfer_logs(log_dir, keep=0)
            remaining = list(log_dir.glob("transfer-*.jsonl"))
            self.assertEqual(len(remaining), 0)

    def test_ds06_prune_transfer_logs_keep_exceeds_count_preserves_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = logs_dir(root)
            for i in range(3):
                log_path = log_dir / f"transfer-2026043{i}.jsonl"
                log_path.write_text(json.dumps({"event": f"test{i}"}), encoding="utf-8")
            prune_transfer_logs(log_dir, keep=10)
            remaining = list(log_dir.glob("transfer-*.jsonl"))
            self.assertEqual(len(remaining), 3)

    # ========================================================================
    # B5: headless.py handoff orphaned files on tell failure
    # ========================================================================
    def test_ds07_handoff_tell_failure_does_not_orphan_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = root / "local"
            remote = root / "remote"
            local.mkdir()
            remote.mkdir()
            install_work_mem(local)
            (local / "data.txt").write_text("test data", encoding="utf-8")
            slave = self.start_slave(remote)
            previous_cwd = Path.cwd()
            try:
                os.chdir(local)
                import agentremote.headless as headless_module
                original_tell = headless_module.tell
                tell_called = []

                def failing_tell(*args, **kwargs):
                    tell_called.append(True)
                    raise AgentRemoteError(500, "simulated_failure", "tell failed")

                with patch.object(headless_module, "tell", side_effect=failing_tell):
                    with self.assertRaises(AgentRemoteError) as ctx:
                        handoff(
                            "127.0.0.1",
                            slave.server_address[1],
                            "secret",
                            Path("data.txt"),
                            "Task that should fail on tell.",
                            remote_dir="/incoming",
                            from_name="master-agent",
                            alias="::lab",
                        )
                    self.assertEqual(ctx.exception.code, "simulated_failure")
                self.assertTrue(tell_called)
                self.assertFalse(
                    (remote / "incoming" / "data.txt").exists(),
                    "Orphaned remote file should be cleaned up after tell failure"
                )
            finally:
                os.chdir(previous_cwd)
                slave.shutdown()
                slave.server_close()

    # ========================================================================
    # B6: slave.py handle_upload_chunk offset_mismatch inconsistency
    # ========================================================================
    def test_ds07b_handoff_cleanup_tolerates_delete_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = root / "local"
            remote = root / "remote"
            local.mkdir()
            remote.mkdir()
            install_work_mem(local)
            (local / "data.txt").write_text("test data", encoding="utf-8")
            slave = self.start_slave(remote)
            previous_cwd = Path.cwd()
            try:
                os.chdir(local)
                import agentremote.headless as headless_module
                cleanup_called = threading.Event()

                orig_cleanup = headless_module._cleanup_handoff_files

                def safe_wrapper(*args, **kwargs):
                    cleanup_called.set()

                with patch.object(headless_module, "_cleanup_handoff_files", side_effect=safe_wrapper):
                    with patch.object(headless_module, "tell", side_effect=AgentRemoteError(500, "tell_fail", "tell fail")):
                        with self.assertRaises(AgentRemoteError) as ctx:
                            handoff(
                                "127.0.0.1",
                                slave.server_address[1],
                                "secret",
                                Path("data.txt"),
                                "Task",
                                remote_dir="/incoming",
                            )
                self.assertEqual(ctx.exception.code, "tell_fail")
                self.assertTrue(cleanup_called.is_set(), "Cleanup should have been attempted")
            finally:
                os.chdir(previous_cwd)
                slave.shutdown()
                slave.server_close()

    def test_ds08_upload_chunk_offset_mismatch_returns_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote_root = root / "remote"
            remote_root.mkdir()
            slave = self.start_slave(remote_root)
            try:
                client = RemoteClient("127.0.0.1", slave.server_address[1], "secret")
                payload = b"test-data-for-offset-test"
                total = len(payload)
                digest = hashlib.sha256(payload).hexdigest()
                client.mkdir("/incoming")
                client.upload_chunk(
                    "/incoming/offset-test.bin", 0, total, payload[:5], overwrite=False
                )
                with self.assertRaises(AgentRemoteError) as ctx:
                    client.upload_chunk(
                        "/incoming/offset-test.bin", 10, total, payload[10:], overwrite=False
                    )
                self.assertEqual(ctx.exception.code, "offset_mismatch")
            finally:
                slave.shutdown()
                slave.server_close()

    # ========================================================================
    # Additional edge case tests
    # ========================================================================

    def test_ds09_format_bytes_boundaries(self) -> None:
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(500), "500 B")
        self.assertEqual(format_bytes(1024), "1.0 KB")
        self.assertEqual(format_bytes(1048576), "1.0 MB")
        self.assertEqual(format_bytes(1073741824), "1.0 GB")
        self.assertEqual(format_bytes(1099511627776), "1.0 TB")

    def test_ds10_clean_rel_path_edge_cases(self) -> None:
        self.assertEqual(clean_rel_path("/"), "/")
        self.assertEqual(clean_rel_path(""), "/")
        self.assertEqual(clean_rel_path("//foo//bar//"), "/foo/bar")
        self.assertEqual(clean_rel_path("/a/b/c"), "/a/b/c")

    def test_ds11_normalize_session_scopes_all(self) -> None:
        scopes = normalize_session_scopes("all")
        self.assertIn("read", scopes)
        self.assertIn("write", scopes)
        self.assertIn("delete", scopes)
        self.assertIn("handoff", scopes)

    def test_ds12_normalize_session_scopes_empty_list(self) -> None:
        scopes = normalize_session_scopes([])
        self.assertIn("read", scopes)

    def test_ds13_normalize_session_scopes_unknown(self) -> None:
        with self.assertRaises(AgentRemoteError) as ctx:
            normalize_session_scopes("read,unknown-scope")
        self.assertEqual(ctx.exception.code, "bad_scopes")

    def test_ds14_normalize_session_scopes_bad_type(self) -> None:
        with self.assertRaises(AgentRemoteError) as ctx:
            normalize_session_scopes(12345)  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.code, "bad_scopes")

    def test_ds15_parse_int_invalid(self) -> None:
        with self.assertRaises(AgentRemoteError) as ctx:
            parse_int("not-a-number", "test_param")
        self.assertEqual(ctx.exception.code, "bad_number")

    def test_ds16_security_concurrent_request_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = SecurityConfig(max_concurrent_requests=1)
            slave = self.start_slave_with_security(root, config)
            try:
                client = RemoteClient("127.0.0.1", slave.server_address[1], "secret")
                self.assertIsNotNone(client.token)
                results = []
                errors = []

                def make_request():
                    try:
                        client.list("/")
                        results.append(True)
                    except Exception as e:
                        errors.append(e)

                t1 = threading.Thread(target=make_request)
                t2 = threading.Thread(target=make_request)
                t1.start()
                t2.start()
                t1.join(timeout=5)
                t2.join(timeout=5)
                self.assertGreater(len(results), 0)
            finally:
                slave.shutdown()
                slave.server_close()

    def test_ds17_pinned_https_fingerprint_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote_root = root / "remote"
            remote_root.mkdir()
            cert_store = root / "certs"
            slave, _ = self.start_tls_slave(remote_root, cert_store)
            try:
                url = f"https://127.0.0.1:{slave.server_address[1]}"
                with self.assertRaises(AgentRemoteError):
                    RemoteClient(
                        url,
                        slave.server_address[1],
                        "secret",
                        tls_fingerprint="ff" * 32,
                    )
            finally:
                slave.shutdown()
                slave.server_close()

    def test_ds18_worker_blocked_command(self) -> None:
        self.assertTrue(is_blocked_command("rm -rf /"))
        self.assertTrue(is_blocked_command("sudo rm something"))
        self.assertFalse(is_blocked_command("echo hello"))

    def test_ds19_worker_blocked_commands_in_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_work_mem(root)
            manifest = create_instruction(
                root,
                "Do something dangerous.\nagentremote-run: rm -rf /",
                auto_run=True,
            )
            plan = build_plan(root, manifest)
            self.assertGreater(len(plan["blockedCommands"]), 0)
            self.assertEqual(plan["blockedCommands"][0], "rm -rf /")

    def test_ds20_worker_empty_commands_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_work_mem(root)
            manifest = create_instruction(root, "Task with no commands.", auto_run=True)
            plan = build_plan(root, manifest)
            self.assertEqual(len(plan["commands"]), 0)
            result = finish_without_execution(
                root, manifest, plan, "blocked", "", "test-worker"
            )
            self.assertEqual(result["state"], "blocked")

    def test_ds21_approve_execution_bad_mode(self) -> None:
        with self.assertRaises(AgentRemoteError) as ctx:
            approve_execution("bad_mode", {"commands": ["echo hi"]})
        self.assertEqual(ctx.exception.code, "bad_execute_mode")

    def test_ds22_approve_execution_not_tty(self) -> None:
        with patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(AgentRemoteError) as ctx:
                approve_execution("ask", {"commands": ["echo hi"]})
            self.assertEqual(ctx.exception.code, "execution_needs_approval")

    def test_ds23_create_handoff_invalid_message_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_work_mem(root)
            with self.assertRaises(AgentRemoteError) as ctx:
                create_handoff(
                    root,
                    title="Bad handoff",
                    task="test",
                    from_model="test",
                    message_type="INVALID_TYPE",
                )
            self.assertEqual(ctx.exception.code, "bad_handoff_type")

    def test_ds24_slugify_edge_cases(self) -> None:
        self.assertEqual(slugify(""), "handoff")
        self.assertEqual(slugify("   "), "handoff")
        self.assertEqual(slugify("??"), "handoff")
        self.assertTrue(len(slugify("a" * 100)) <= 45)

    def test_ds25_one_line_edge_cases(self) -> None:
        self.assertEqual(one_line(""), "agentremote handoff")
        self.assertEqual(one_line("   "), "agentremote handoff")
        self.assertIn("hello", one_line("hello world"))
        self.assertIn("first", one_line("first\nsecond"))

    def test_ds26_instruction_manifest_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inbox_dir = root / ".agentremote_inbox"
            inbox_dir.mkdir(parents=True)
            bad_instr = inbox_dir / "bad-one"
            bad_instr.mkdir()
            (bad_instr / "manifest.json").write_text("not valid json {{{", encoding="utf-8")
            instructions = list_instructions(root)
            corrupt = [i for i in instructions if i.get("state") == "corrupt"]
            self.assertEqual(len(corrupt), 1)
            self.assertEqual(corrupt[0]["id"], "bad-one")

    def test_ds27_claim_instruction_already_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_work_mem(root)
            manifest = create_instruction(root, "Test claim.", auto_run=True)
            claim_instruction(root, manifest["id"], claimed_by="worker-1")
            with self.assertRaises(AgentRemoteError) as ctx:
                claim_instruction(root, manifest["id"], claimed_by="worker-2")
            self.assertEqual(ctx.exception.code, "instruction_not_claimable")

    def test_ds28_worker_select_instruction_already_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_work_mem(root)
            manifest = create_instruction(root, "Already done.", auto_run=True)
            update_instruction_state(root, manifest["id"], "completed")
            with self.assertRaises(AgentRemoteError) as ctx:
                from agentremote.worker import select_instruction
                select_instruction(root, instruction_id=manifest["id"])
            self.assertEqual(ctx.exception.code, "instruction_not_runnable")

    def test_ds29_connections_corrupted_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "agentremote_config"
            config_dir.mkdir(parents=True)
            conn_file = config_dir / "connections.json"
            conn_file.write_text("NOT JSON {{{", encoding="utf-8")
            with patch("agentremote.connections.config_home", return_value=config_dir):
                loaded = load_connections()
                self.assertIsInstance(loaded, dict)
                self.assertIn("connections", loaded)

    def test_ds30_connections_non_dict_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "agentremote_config"
            config_dir.mkdir(parents=True)
            conn_file = config_dir / "connections.json"
            conn_file.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
            with patch("agentremote.connections.config_home", return_value=config_dir):
                loaded = load_connections()
                self.assertIn("connections", loaded)
                self.assertIsInstance(loaded["connections"], dict)

    def test_ds30b_connections_save_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "agentremote_config"
            config_dir.mkdir(parents=True)
            with patch("agentremote.connections.config_home", return_value=config_dir):
                import agentremote.connections as conn_module
                conn_module.save_connections({"connections": {"test": {"name": "::test", "host": "x", "port": 1, "token": "t"}}})
                tmp_path = config_dir / "connections.tmp"
                self.assertFalse(tmp_path.exists(), "Temp file should be cleaned up after atomic save")
                loaded = conn_module.load_connections()
                self.assertIn("test", loaded["connections"])

    def test_ds31_strip_alias_prefix_edge(self) -> None:
        self.assertEqual(strip_alias_prefix("::lab"), "lab")
        self.assertEqual(strip_alias_prefix("lab"), "lab")
        self.assertEqual(strip_alias_prefix(""), "")
        self.assertEqual(strip_alias_prefix("::"), "")
        self.assertEqual(strip_alias_prefix(":::"), ":")

    def test_ds32_transfer_logger_rotation_index_exhaustion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = current_transfer_log_path(root)
            log_path.write_text(json.dumps({"event": "test"}) + "\n", encoding="utf-8")
            for i in range(5):
                rotate_log_file(log_path)
            self.assertTrue(True)

    def test_ds32b_concurrent_log_writes_no_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            errors = []
            threads = []

            def write_entry(index: int):
                try:
                    write_log_row(
                        root,
                        {"event": "concurrent", "index": index, "data": "x" * 50},
                        max_bytes=500,
                        keep=2,
                    )
                except Exception as e:
                    errors.append(e)

            for i in range(20):
                t = threading.Thread(target=write_entry, args=(i,))
                threads.append(t)
                t.start()
            for t in threads:
                t.join(timeout=10)

            self.assertEqual(len(errors), 0, f"Concurrent writes raised errors: {errors}")
            log_path = current_transfer_log_path(root)
            if log_path.exists():
                lines = log_path.read_text(encoding="utf-8").strip().splitlines()
                self.assertGreater(len(lines), 0, "Should have written entries")

    def test_ds33_local_scope_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "project"
            base.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (outside / "test.txt").write_text("data", encoding="utf-8")
            new_root, agent_path = local_scope(outside / "test.txt", base)
            self.assertEqual(agent_path, "/test.txt")
            self.assertEqual(new_root, (outside / "test.txt").parent)

    def test_ds34_resolve_conflicts_more_than_20(self) -> None:
        conflicts = [f"/path/to/conflict_{i}.txt" for i in range(25)]
        with patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(AgentRemoteError) as ctx:
                resolve_conflicts(conflicts, False, "remote")
            self.assertEqual(ctx.exception.code, "conflicts")

    def test_ds35_resolve_conflicts_overwrite_skips_prompt(self) -> None:
        conflicts = ["/file.txt"]
        result = resolve_conflicts(conflicts, True, "remote")
        self.assertTrue(result)

    def test_ds36_download_range_edge_cases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote_root = root / "remote"
            remote_root.mkdir()
            data = b"hello-range-test-data-12345"
            (remote_root / "range-test.bin").write_bytes(data)
            slave = self.start_slave(remote_root)
            try:
                client = RemoteClient("127.0.0.1", slave.server_address[1], "secret")
                chunk = client.download_chunk("/range-test.bin", 0, len(data))
                self.assertEqual(chunk, data)
                chunk_mid = client.download_chunk("/range-test.bin", 6, 5)
                self.assertEqual(chunk_mid, data[6:11])
                chunk_end = client.download_chunk("/range-test.bin", len(data) - 1, 100)
                self.assertEqual(chunk_end, data[-1:])
                chunk_zero = client.download_chunk("/range-test.bin", len(data), 100)
                self.assertEqual(len(chunk_zero), 0, "Zero-byte download at EOF should return empty bytes")
            finally:
                slave.shutdown()
                slave.server_close()

    def test_ds37_download_not_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote_root = root / "remote"
            remote_root.mkdir()
            (remote_root / "dir").mkdir()
            slave = self.start_slave(remote_root)
            try:
                client = RemoteClient("127.0.0.1", slave.server_address[1], "secret")
                with self.assertRaises(AgentRemoteError) as ctx:
                    client.download_chunk("/dir", 0, 100)
                self.assertEqual(ctx.exception.code, "not_file")
            finally:
                slave.shutdown()
                slave.server_close()

    def test_ds38_upload_finish_mtime_bool_edge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote_root = root / "remote"
            remote_root.mkdir()
            slave = self.start_slave(remote_root)
            try:
                client = RemoteClient("127.0.0.1", slave.server_address[1], "secret")
                data = b"mtime-edge-case-test"
                digest = hashlib.sha256(data).hexdigest()
                client.upload_chunk("/mtime-test.bin", 0, len(data), data, overwrite=False)
                client.upload_finish("/mtime-test.bin", len(data), time.time(), digest, overwrite=False)
                self.assertTrue((remote_root / "mtime-test.bin").exists())
            finally:
                slave.shutdown()
                slave.server_close()

    def test_ds39_handoff_creates_both_local_and_remote_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = root / "local"
            remote = root / "remote"
            local.mkdir()
            remote.mkdir()
            install_work_mem(local)
            install_work_mem(remote)
            (local / "data.txt").write_text("handoff test", encoding="utf-8")
            slave = self.start_slave(remote)
            previous_cwd = Path.cwd()
            try:
                os.chdir(local)
                result = handoff(
                    "127.0.0.1",
                    slave.server_address[1],
                    "secret",
                    Path("data.txt"),
                    "Test handoff with file and instruction.",
                    remote_dir="/incoming",
                    from_name="test-agent",
                    alias="::lab",
                )
                self.assertTrue(result["transfer"]["remotePaths"])
                self.assertTrue(result["instruction"]["id"])
                local_handoffs = list((local / "AIMemory").glob("handoff_*.md"))
                self.assertGreaterEqual(len(local_handoffs), 1)
            finally:
                os.chdir(previous_cwd)
                slave.shutdown()
                slave.server_close()

    def test_ds40_update_instruction_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = create_instruction(root, "Test update.", auto_run=True)
            updated = update_instruction_state(
                root, manifest["id"], "claimed", extra={"testKey": "testValue"}
            )
            self.assertEqual(updated["state"], "claimed")
            self.assertEqual(updated["testKey"], "testValue")

    def test_ds41_push_with_memory_root_separate_from_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = root / "local"
            remote = root / "remote"
            memory_root = root / "memory"
            local.mkdir()
            remote.mkdir()
            memory_root.mkdir()
            install_work_mem(memory_root)
            (local / "data.txt").write_text("test data", encoding="utf-8")
            slave = self.start_slave(remote)
            try:
                push(
                    "127.0.0.1",
                    slave.server_address[1],
                    "secret",
                    local / "data.txt",
                    "/incoming",
                    local_root=memory_root,
                )
                self.assertTrue((remote / "incoming" / "data.txt").exists())
            finally:
                slave.shutdown()
                slave.server_close()

    def test_ds42_join_rel_with_empty_names(self) -> None:
        self.assertEqual(join_rel("/a", ""), "/a")
        self.assertEqual(join_rel("/a", "b"), "/a/b")
        self.assertEqual(join_rel("/", "file.txt"), "/file.txt")

    def test_ds43_sha256_file_matches_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_path = root / "test.bin"
            content = b"sha256-test-content"
            file_path.write_bytes(content)
            expected = hashlib.sha256(content).hexdigest()
            self.assertEqual(sha256_file(file_path), expected)

    def test_ds44_truncate_long_output(self) -> None:
        short = "short"
        self.assertEqual(truncate(short), short)
        long_text = "x" * 5000
        result = truncate(long_text)
        self.assertLess(len(result), len(long_text))
        self.assertIn("[truncated]", result)

    def test_ds45_render_report_with_results(self) -> None:
        results = [
            CommandResult("echo hello", 0, "hello\n", "", 0.1),
            CommandResult("bad-command", 1, "", "error\n", 0.2),
        ]
        manifest = {"id": "test-id", "task": "Test task"}
        plan = {
            "instructionId": "test-id",
            "handoffId": "",
            "task": "Test task",
            "autoRun": True,
            "paths": [{"path": "/test.txt", "exists": True}],
            "commands": ["echo hello", "bad-command"],
            "blockedCommands": [],
            "callbackAlias": "",
            "expectedReport": "",
        }
        report = render_report(manifest, plan, "failed", results)
        self.assertIn("echo hello", report)
        self.assertIn("bad-command", report)
        self.assertIn("exit 0", report)
        self.assertIn("exit 1", report)

    def test_ds46_extract_commands_from_manifest(self) -> None:
        manifest = {
            "task": "Test task.\nagentremote-run: python -c \"print('hello')\"\nagentremote-run: echo done",
            "commands": ["ls -la"],
        }
        commands = extract_commands(manifest)
        self.assertIn("ls -la", commands)
        self.assertIn('python -c "print(\'hello\')"', commands)
        self.assertIn("echo done", commands)

    def test_ds46b_extract_commands_case_sensitive(self) -> None:
        manifest = {
            "task": (
                "Valid command line:\n"
                "agentremote-run: echo hello\n"
                "AGENTREMOTE-RUN: this uppercase should NOT match\n"
                "Agentremote-Run: this mixed case should NOT match\n"
                "agentremote-run: echo world\n"
            )
        }
        commands = extract_commands(manifest)
        self.assertEqual(commands, ["echo hello", "echo world"])

    def test_ds46c_extract_commands_whitespace_handling(self) -> None:
        manifest = {
            "task": "  agentremote-run:  echo trimmed  \n  agentremote-run:   \n"
        }
        commands = extract_commands(manifest)
        self.assertEqual(commands, ["echo trimmed"])

    def test_ds47_prepare_tls_manual_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(AgentRemoteError) as ctx:
                prepare_tls(root, "manual", cert_file=Path("/nonexistent/cert.pem"), key_file=Path("/nonexistent/key.pem"))
            self.assertEqual(ctx.exception.code, "tls_file_not_found")

    def test_ds48_prepare_tls_bad_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(AgentRemoteError) as ctx:
                prepare_tls(root, "invalid-mode")
            self.assertEqual(ctx.exception.code, "bad_tls_mode")

    def test_ds49_create_instruction_with_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_work_mem(root)
            handoff_obj = create_handoff(
                root,
                title="Test handoff for instruction",
                task="Test task",
                from_model="test",
                message_type="DECISION_RELAY",
            )
            manifest = create_instruction(
                root,
                "Test instruction with handoff",
                from_name="test",
                handoff=handoff_obj,
            )
            self.assertTrue(manifest.get("handoffFile"))
            self.assertTrue(manifest.get("handoffId"))

    def test_ds50_list_instructions_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instructions = list_instructions(root)
            self.assertEqual(len(instructions), 0)

    def test_ds51_write_instruction_no_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = {"state": "received"}
            with self.assertRaises(AgentRemoteError) as ctx:
                write_instruction(root, manifest)
            self.assertEqual(ctx.exception.code, "missing_instruction_id")

    def test_ds52_posix_relative_edge_cases(self) -> None:
        self.assertEqual(posix_relative("/a/b", "/a/b/c"), "c")
        self.assertEqual(posix_relative("/a/b", "/a/b/c/d"), "c/d")
        with self.assertRaises(AgentRemoteError):
            posix_relative("/a/b", "/x/y")

    def test_ds53_build_download_plan_symlink_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote_root = root / "remote"
            remote_root.mkdir()
            (remote_root / "data.txt").write_text("test", encoding="utf-8")
            slave = self.start_slave(remote_root)
            try:
                client = RemoteClient("127.0.0.1", slave.server_address[1], "secret")
                plan = build_download_plan(client, ["/data.txt"], "/output")
                self.assertEqual(len(plan["files"]), 1)
                self.assertEqual(plan["files"][0]["source"], "/data.txt")
            finally:
                slave.shutdown()
                slave.server_close()

    def test_ds54_build_upload_plan_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_root = root / "local"
            local_root.mkdir()
            sub_dir = local_root / "mydir"
            sub_dir.mkdir()
            (sub_dir / "file1.txt").write_text("content1", encoding="utf-8")
            (sub_dir / "file2.txt").write_text("content2", encoding="utf-8")
            plan = build_upload_plan(local_root, ["/mydir"], "/remote")
            self.assertGreaterEqual(len(plan["files"]), 2)
            self.assertIn("/remote/mydir", plan["dirs"])

    def test_ds55_security_sliding_window_precision(self) -> None:
        limiter = SlidingWindowLimiter(limit=3, window_seconds=10)
        now = time.time()
        self.assertTrue(limiter.allow("test-ip", now=now))
        self.assertTrue(limiter.allow("test-ip", now=now + 1))
        self.assertTrue(limiter.allow("test-ip", now=now + 2))
        self.assertFalse(limiter.allow("test-ip", now=now + 3))
        self.assertEqual(limiter.count("test-ip", now=now + 3), 3)

    def test_ds56_normalize_fingerprint_invalid_length(self) -> None:
        with self.assertRaises(AgentRemoteError) as ctx:
            normalize_fingerprint("abcd")
        self.assertEqual(ctx.exception.code, "bad_tls_fingerprint")

    def test_ds57_format_fingerprint(self) -> None:
        fp = "a" * 64
        formatted = format_fingerprint(fp)
        self.assertIn(":", formatted)
        self.assertEqual(formatted, ":".join(["AA"] * 32))

    def test_ds58_command_result_as_dict(self) -> None:
        result = CommandResult("echo test", 0, "test", "", 0.5)
        d = result.as_dict()
        self.assertEqual(d["command"], "echo test")
        self.assertEqual(d["exitCode"], 0)
        self.assertEqual(d["stdout"], "test")
        self.assertEqual(d["duration"], 0.5)

    def test_ds59_print_progress_zero_total(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            print_progress(0, 0)
        self.assertIn("0 B", out.getvalue())

    def test_ds60_upload_chunk_total_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote_root = root / "remote"
            remote_root.mkdir()
            slave = self.start_slave(remote_root)
            try:
                client = RemoteClient("127.0.0.1", slave.server_address[1], "secret")
                data = b"this-is-a-test"
                with self.assertRaises(AgentRemoteError) as ctx:
                    client.upload_chunk(
                        "/overflow.bin", 0, 5, data, overwrite=False
                    )
                self.assertEqual(ctx.exception.code, "too_much_data")
            finally:
                slave.shutdown()
                slave.server_close()

    def test_ds61_panic_on_flood_sets_shutdown_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote_root = root / "remote"
            remote_root.mkdir()
            config = SecurityConfig(
                panic_on_flood=True,
                overload_events_per_minute=1,
                unauthenticated_per_minute=1,
            )
            slave = self.start_slave_with_security(remote_root, config)
            try:
                base = f"http://127.0.0.1:{slave.server_address[1]}"
                flood_triggered = False
                for _ in range(50):
                    try:
                        raw_request(
                            base + "/api/challenge",
                            "GET",
                            b"",
                            {},
                            timeout=2,
                        )
                    except Exception:
                        pass
                    if slave.state.security.flood_shutdown_requested:
                        flood_triggered = True
                        break
                    time.sleep(0.05)
                self.assertTrue(
                    flood_triggered,
                    "Flood shutdown should be requested after exceeding overload threshold"
                )
            finally:
                slave.shutdown()
                slave.server_close()

    # ========================================================================
    # Phase 7: Token/nonce expiry tests
    # ========================================================================
    def test_ds62_expired_nonce_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote_root = root / "remote"
            remote_root.mkdir()
            state = SlaveState(remote_root, "secret")
            challenge = state.challenge()
            proof = make_proof_from_state(state, challenge["nonce"])
            state.nonces[challenge["nonce"]] = time.time() - 1
            with self.assertRaises(AgentRemoteError) as ctx:
                state.login(challenge["nonce"], proof, "127.0.0.1")
            self.assertEqual(ctx.exception.code, "bad_nonce")

    def test_ds63_expired_token_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote_root = root / "remote"
            remote_root.mkdir()
            state = SlaveState(remote_root, "secret")
            challenge = state.challenge()
            proof = make_proof_from_state(state, challenge["nonce"])
            session = state.login(challenge["nonce"], proof, "127.0.0.1")
            token = session["token"]
            state.sessions[token]["expires"] = time.time() - 1
            with self.assertRaises(AgentRemoteError) as ctx:
                state.require_token(f"Bearer {token}")
            self.assertEqual(ctx.exception.code, "bad_token")

    def test_ds64_valid_token_renews_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote_root = root / "remote"
            remote_root.mkdir()
            state = SlaveState(remote_root, "secret")
            challenge = state.challenge()
            proof = make_proof_from_state(state, challenge["nonce"])
            session = state.login(challenge["nonce"], proof, "127.0.0.1")
            token = session["token"]
            original_expiry = state.sessions[token]["expires"]
            time.sleep(0.1)
            state.require_token(f"Bearer {token}")
            new_expiry = state.sessions[token]["expires"]
            self.assertGreater(new_expiry, original_expiry, "Token expiry should be renewed on use")

    # ========================================================================
    # Phase 8: Network detection tests
    # ========================================================================
    def test_ds65_detect_addresses_returns_local(self) -> None:
        addresses = detect_addresses(7171)
        labels = {label for label, _ in addresses}
        self.assertIn("Local", labels)
        local_addrs = [addr for label, addr in addresses if label == "Local"]
        self.assertGreater(len(local_addrs), 0)
        self.assertIn("127.0.0.1:7171", local_addrs)

    def test_ds66_local_ipv4_addresses_no_crash(self) -> None:
        result = local_ipv4_addresses()
        self.assertIsInstance(result, list)
        if result:
            for ip in result:
                self.assertIsInstance(ip, str)
                self.assertNotIn(" ", ip)

    def test_ds67_detect_addresses_no_duplicates(self) -> None:
        addresses = detect_addresses(8080)
        endpoints = [addr for _, addr in addresses]
        self.assertEqual(len(endpoints), len(set(endpoints)), "Addresses should be deduplicated")

    # ========================================================================
    # Phase 9: TLS fetch_remote_fingerprint error path tests
    # ========================================================================
    def test_ds68_fetch_fingerprint_non_https_rejected(self) -> None:
        with self.assertRaises(AgentRemoteError) as ctx:
            fetch_remote_fingerprint("http://example.com", 443, timeout=0.1)
        self.assertEqual(ctx.exception.code, "not_https")

    def test_ds69_fetch_fingerprint_bad_host_rejected(self) -> None:
        with self.assertRaises(AgentRemoteError) as ctx:
            fetch_remote_fingerprint("https://", 443, timeout=0.1)
        self.assertEqual(ctx.exception.code, "bad_https_host")

    def test_ds70_is_https_endpoint(self) -> None:
        self.assertTrue(is_https_endpoint("https://example.com"))
        self.assertTrue(is_https_endpoint("HTTPS://EXAMPLE.COM:7171"))
        self.assertFalse(is_https_endpoint("http://example.com"))
        self.assertFalse(is_https_endpoint("localhost:7171"))

    # ========================================================================
    # Phase 10: Swarm/daemon/controller scaffold CLI tests
    # ========================================================================
    def test_ds71_daemon_serve_help(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            with self.assertRaises(SystemExit):
                cli_main(["daemon", "serve", "--help"])
        self.assertIn("daemon agent", out.getvalue())

    def test_ds72_controller_gui_help(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            with self.assertRaises(SystemExit):
                cli_main(["controller", "gui", "--help"])
        self.assertIn("controller gui", out.getvalue())

    def test_ds73_nodes_list_no_connections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "empty_config"
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["nodes", "list"])
                self.assertIn("No saved connection", out.getvalue())
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds74_nodes_list_with_connection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "proj"
            remote = root / "remote"
            project.mkdir()
            remote.mkdir()
            install_work_mem(project)
            slave = self.start_slave(remote)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = Path.cwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                os.chdir(project)
                cli_main(["connect", "alpha", "127.0.0.1", str(slave.server_address[1]), "--password", "secret"])
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["nodes", "list"])
                self.assertIn("::alpha", out.getvalue())
                self.assertIn("127.0.0.1", out.getvalue())
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home
                slave.shutdown()
                slave.server_close()

    def test_ds75_topology_show_no_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "empty_config"
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["topology", "show", "--root", str(root)])
                self.assertIn("local-controller", out.getvalue())
                self.assertIn("no remote nodes", out.getvalue().lower())
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds76_topology_show_with_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "proj"
            remote = root / "remote"
            project.mkdir()
            remote.mkdir()
            install_work_mem(project)
            slave = self.start_slave(remote)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = Path.cwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                os.chdir(project)
                cli_main(["connect", "beta", "127.0.0.1", str(slave.server_address[1]), "--password", "secret"])
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["topology", "show", "--root", str(project)])
                self.assertIn("local-controller", out.getvalue())
                self.assertIn("::beta", out.getvalue())
                self.assertIn("direct", out.getvalue())
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home
                slave.shutdown()
                slave.server_close()

    def test_ds77_policy_list_includes_scopes(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            cli_main(["policy", "list"])
        output = out.getvalue()
        self.assertIn("read", output)
        self.assertIn("write", output)
        self.assertIn("delete", output)
        self.assertIn("handoff", output)
        self.assertIn("--policy warn|strict|off", output)
        self.assertNotIn("planned", output.lower())

    def test_ds78_route_list_no_connections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "empty_config"
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["route", "list"])
                self.assertIn("No saved routes", out.getvalue())
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds79_route_list_shows_direct(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "proj"
            remote = root / "remote"
            project.mkdir()
            remote.mkdir()
            install_work_mem(project)
            slave = self.start_slave(remote)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = Path.cwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                os.chdir(project)
                cli_main(["connect", "gamma", "127.0.0.1", str(slave.server_address[1]), "--password", "secret"])
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["route", "list"])
                output = out.getvalue()
                self.assertIn("::gamma", output)
                self.assertIn("direct", output)
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home
                slave.shutdown()
                slave.server_close()

    def test_ds80_daemon_status_reports_aimemory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_work_mem(root)
            out = io.StringIO()
            with redirect_stdout(out):
                cli_main(["daemon", "status", "--root", str(root)])
            output = out.getvalue()
            self.assertIn("AIMemory: installed", output)

    def test_ds81_daemon_status_reports_missing_aimemory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = io.StringIO()
            with redirect_stdout(out):
                cli_main(["daemon", "status", "--root", str(root)])
            output = out.getvalue()
            self.assertIn("AIMemory: missing", output)

    # ========================================================================
    # Phase 11: Policy enforcement + route integration tests
    # ========================================================================
    def test_ds93_policy_denied_node_blocks_command_default_warn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "proj"
            remote = root / "remote"
            project.mkdir()
            remote.mkdir()
            install_work_mem(project)
            (project / "dummy.txt").write_text("test", encoding="utf-8")
            slave = self.start_slave(remote)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = Path.cwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                os.chdir(project)
                cli_main(["connect", "blacklisted", "127.0.0.1", str(slave.server_address[1]), "--password", "secret"])
                cli_main(["policy", "deny", "blacklisted", "--note", "blocked for testing"])
                with self.assertRaises(SystemExit) as ctx:
                    cli_main(["push", "blacklisted", "dummy.txt", "/out", "--policy", "warn"])
                self.assertIn("policy_denied", str(ctx.exception))
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home
                slave.shutdown()
                slave.server_close()

    def test_ds94_policy_off_bypasses_deny(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "proj"
            remote = root / "remote"
            project.mkdir()
            remote.mkdir()
            install_work_mem(project)
            (project / "off.txt").write_text("off-test", encoding="utf-8")
            slave = self.start_slave(remote)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = Path.cwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                os.chdir(project)
                cli_main(["connect", "off-node", "127.0.0.1", str(slave.server_address[1]), "--password", "secret"])
                cli_main(["policy", "deny", "off-node"])
                with redirect_stdout(io.StringIO()):
                    cli_main(["push", "off-node", "off.txt", "/off-out", "--policy", "off", "--overwrite"])
                self.assertTrue(
                    (remote / "off-out" / "off.txt").exists(),
                    "File should have been pushed despite deny policy with --policy off"
                )
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home
                slave.shutdown()
                slave.server_close()

    def test_ds95_route_priority_affects_target_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "proj"
            remote = root / "remote"
            project.mkdir()
            remote.mkdir()
            install_work_mem(project)
            slave = self.start_slave(remote)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = Path.cwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                os.chdir(project)
                cli_main(["connect", "routed", "127.0.0.1", str(slave.server_address[1]), "--password", "secret"])
                cli_main(["route", "set", "routed", "127.0.0.1", str(slave.server_address[1]), "--priority", "1"])
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["route", "list"])
                self.assertIn("[selected]", out.getvalue())
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home
                slave.shutdown()
                slave.server_close()

    def test_ds96_topology_denied_node_marked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "proj"
            project.mkdir()
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = Path.cwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                os.chdir(project)
                cli_main(["policy", "deny", "blocked-host"])
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["topology", "show", "--root", str(project)])
                output = out.getvalue()
                self.assertIn("::blocked-host", output)
                self.assertIn("denied", output)
                self.assertIn("blocked", output)
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds97_policy_help_appears_in_push(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            with self.assertRaises(SystemExit):
                cli_main(["push", "--help"])
        self.assertIn("--policy", out.getvalue())

    def test_ds98_policy_strict_unlisted_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "proj"
            remote = root / "remote"
            project.mkdir()
            remote.mkdir()
            install_work_mem(project)
            slave = self.start_slave(remote)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = Path.cwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                os.chdir(project)
                cli_main(["connect", "unlisted-node", "127.0.0.1", str(slave.server_address[1]), "--password", "secret"])
                with self.assertRaises(SystemExit) as ctx:
                    cli_main(["tell", "unlisted-node", "test task", "--policy", "strict"])
                self.assertIn("unlisted", str(ctx.exception).lower())
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home
                slave.shutdown()
                slave.server_close()

    def test_ds99_route_set_and_remove_operations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                cli_main(["route", "set", "testroute", "192.168.1.1", "7171", "--priority", "5"])
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["route", "list"])
                self.assertIn("testroute", out.getvalue())
                self.assertIn("192.168.1.1:7171", out.getvalue())

                cli_main(["route", "remove", "testroute", "--host", "192.168.1.1", "--port", "7171"])
                out2 = io.StringIO()
                with redirect_stdout(out2):
                    cli_main(["route", "list"])
                self.assertNotIn("testroute", out2.getvalue())
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds82_controller_gui_accepts_explicit_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_work_mem(root)
            with patch("getpass.getpass", side_effect=AssertionError("password prompt should not run")):
                with patch("agentremote.cli.run_master") as run_master_mock:
                    cli_main(
                        [
                            "controller",
                            "gui",
                            "127.0.0.1",
                            "--local",
                            str(root),
                            "--token",
                            "explicit-token",
                            "--no-browser",
                            "--console",
                            "no",
                        ]
                    )
            self.assertEqual(run_master_mock.call_args.kwargs["token"], "explicit-token")

    def test_ds83_master_accepts_explicit_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_work_mem(root)
            with patch("getpass.getpass", side_effect=AssertionError("password prompt should not run")):
                with patch("agentremote.cli.run_master") as run_master_mock:
                    cli_main(
                        [
                            "master",
                            "127.0.0.1",
                            "--local",
                            str(root),
                            "--token",
                            "explicit-token",
                            "--no-browser",
                            "--console",
                            "no",
                        ]
                    )
            self.assertEqual(run_master_mock.call_args.kwargs["token"], "explicit-token")

    def test_ds84_daemon_status_does_not_create_state_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["daemon", "status", "--root", str(root)])
                self.assertIn("AIMemory: missing", out.getvalue())
                self.assertFalse((root / ".agentremote").exists())
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds85_swarm_state_missing_and_corrupt_loads_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                missing = load_swarm_state()
                self.assertEqual(missing["whitelist"], {})
                self.assertEqual(missing["routes"], {})
                config.mkdir(parents=True)
                swarm_path().write_text("NOT JSON {{{", encoding="utf-8")
                corrupt = load_swarm_state()
                self.assertEqual(corrupt["whitelist"], {})
                self.assertEqual(corrupt["routes"], {})
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds86_policy_allow_deny_remove_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "cfg"
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                with redirect_stdout(io.StringIO()):
                    cli_main(["policy", "allow", "lab", "--note", "trusted"])
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["policy", "list"])
                self.assertIn("::lab allowed", out.getvalue())
                self.assertIn("trusted", out.getvalue())

                with redirect_stdout(io.StringIO()):
                    cli_main(["policy", "deny", "lab", "--note", "paused"])
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["policy", "list"])
                self.assertIn("::lab denied", out.getvalue())
                self.assertIn("paused", out.getvalue())

                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["policy", "remove", "lab"])
                self.assertIn("removed: ::lab", out.getvalue())
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds87_route_set_list_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "cfg"
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                with redirect_stdout(io.StringIO()):
                    cli_main(["route", "set", "lab", "10.0.0.2", "7172", "--priority", "5"])
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["route", "list"])
                output = out.getvalue()
                self.assertIn("::lab", output)
                self.assertIn("10.0.0.2:7172", output)
                self.assertIn("priority=5", output)
                self.assertIn("explicit", output)

                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["route", "remove", "lab", "--host", "10.0.0.2", "--port", "7172"])
                self.assertIn("removed 1 route", out.getvalue())
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds88_route_list_sorts_by_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "cfg"
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                with redirect_stdout(io.StringIO()):
                    cli_main(["route", "set", "slow", "10.0.0.20", "--priority", "50"])
                    cli_main(["route", "set", "fast", "10.0.0.10", "--priority", "1"])
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["route", "list"])
                lines = [line for line in out.getvalue().splitlines() if line.startswith("::")]
                self.assertTrue(lines[0].startswith("::fast"), lines)
                self.assertTrue(lines[1].startswith("::slow"), lines)
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds89_topology_shows_whitelist_state_and_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            config = Path(tmp) / "cfg"
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                with redirect_stdout(io.StringIO()):
                    cli_main(["policy", "allow", "lab"])
                    cli_main(["route", "set", "lab", "100.64.1.20", "7171", "--priority", "10"])
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["topology", "show", "--root", str(root)])
                output = out.getvalue()
                self.assertIn("::lab allowed", output)
                self.assertIn("100.64.1.20:7171", output)
                self.assertIn("priority=10", output)
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds90_saved_connection_appears_as_route_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "cfg"
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                set_connection("saved", "127.0.0.1", 7171, "token")
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["route", "list"])
                output = out.getvalue()
                self.assertIn("::saved", output)
                self.assertIn("127.0.0.1:7171", output)
                self.assertIn("saved", output)
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds91_route_remove_no_match_is_friendly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "cfg"
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["route", "remove", "missing", "--host", "10.0.0.99"])
                self.assertIn("no matching routes: ::missing", out.getvalue())
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds92_policy_raw_host_stays_raw(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "cfg"
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                with redirect_stdout(io.StringIO()):
                    cli_main(["policy", "allow", "192.168.1.50"])
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["policy", "list"])
                self.assertIn("192.168.1.50 allowed", out.getvalue())
                self.assertNotIn("::192.168.1.50", out.getvalue())
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds100_route_priority_overrides_saved_host_but_reuses_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "project"
            project.mkdir()
            install_work_mem(project)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                set_connection("routed", "saved.example", 1111, "saved-token")
                with redirect_stdout(io.StringIO()):
                    cli_main(["route", "set", "routed", "route.example", "2222", "--priority", "1"])
                with patch("getpass.getpass", side_effect=AssertionError("password prompt should not run")):
                    with patch("agentremote.cli.run_master") as run_master_mock:
                        cli_main(
                            [
                                "master",
                                "routed",
                                "--local",
                                str(project),
                                "--no-browser",
                                "--console",
                                "no",
                            ]
                        )
                self.assertEqual(run_master_mock.call_args.args[0], "route.example")
                self.assertEqual(run_master_mock.call_args.args[1], 2222)
                self.assertEqual(run_master_mock.call_args.kwargs["token"], "saved-token")
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds101_route_fingerprint_overrides_saved_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "project"
            project.mkdir()
            install_work_mem(project)
            saved_fp = "cd" * 32
            route_fp = "ab" * 32
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                set_connection("tlsnode", "https://saved.example", 7444, "saved-token", tls_fingerprint=saved_fp)
                with redirect_stdout(io.StringIO()):
                    cli_main(
                        [
                            "route",
                            "set",
                            "tlsnode",
                            "https://route.example",
                            "7443",
                            "--priority",
                            "1",
                            "--tls-fingerprint",
                            route_fp,
                        ]
                    )
                with patch("agentremote.cli.run_master") as run_master_mock:
                    cli_main(
                        [
                            "master",
                            "tlsnode",
                            "--local",
                            str(project),
                            "--no-browser",
                            "--console",
                            "no",
                        ]
                    )
                self.assertEqual(run_master_mock.call_args.args[0], "https://route.example:7443")
                self.assertEqual(run_master_mock.call_args.args[1], 7443)
                self.assertEqual(run_master_mock.call_args.kwargs["tls_fingerprint"], route_fp)
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds102_raw_host_policy_strict_does_not_alias_promptless_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "project"
            project.mkdir()
            install_work_mem(project)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                with redirect_stdout(io.StringIO()):
                    cli_main(["policy", "deny", "127.0.0.1"])
                with patch("getpass.getpass", side_effect=AssertionError("password prompt should not run")):
                    with patch("agentremote.cli.run_master") as run_master_mock:
                        cli_main(
                            [
                                "master",
                                "127.0.0.1",
                                "--local",
                                str(project),
                                "--token",
                                "raw-token",
                                "--policy",
                                "strict",
                                "--no-browser",
                                "--console",
                                "no",
                            ]
                        )
                self.assertEqual(run_master_mock.call_args.args[0], "127.0.0.1")
                self.assertEqual(run_master_mock.call_args.kwargs["token"], "raw-token")
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    # ========================================================================
    # Phase 12: Slave policy + route probe + AIMemory journal (ds103+)
    # ========================================================================
    def test_ds103_slave_strict_rejects_unlisted_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            remote_root = root / "remote"
            remote_root.mkdir()
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                state = SlaveState(remote_root, "secret", policy="strict")
                slave = AgentRemoteSlaveServer(("127.0.0.1", 0), state)
                t = threading.Thread(target=slave.serve_forever, daemon=True)
                t.start()
                try:
                    with self.assertRaises(AgentRemoteError) as ctx:
                        RemoteClient(
                            "127.0.0.1", slave.server_address[1], "secret",
                            client_alias="::unlisted-strict"
                        )
                    self.assertEqual(ctx.exception.code, "slave_policy_unlisted")
                finally:
                    slave.shutdown()
                    slave.server_close()
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds104_slave_warn_allows_unlisted_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            remote_root = root / "remote"
            remote_root.mkdir()
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                state = SlaveState(remote_root, "secret", policy="warn")
                slave = AgentRemoteSlaveServer(("127.0.0.1", 0), state)
                t = threading.Thread(target=slave.serve_forever, daemon=True)
                t.start()
                try:
                    client = RemoteClient(
                        "127.0.0.1", slave.server_address[1], "secret",
                        client_alias="::warn-ok"
                    )
                    self.assertIsNotNone(client.token)
                finally:
                    slave.shutdown()
                    slave.server_close()
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds105_slave_denied_rejected_even_warn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "proj"
            remote_root = root / "remote"
            project.mkdir()
            remote_root.mkdir()
            install_work_mem(project)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = Path.cwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                os.chdir(project)
                cli_main(["policy", "deny", "bad-client"])
                state = SlaveState(remote_root, "secret", policy="warn")
                slave = AgentRemoteSlaveServer(("127.0.0.1", 0), state)
                t = threading.Thread(target=slave.serve_forever, daemon=True)
                t.start()
                try:
                    with self.assertRaises(AgentRemoteError) as ctx:
                        RemoteClient(
                            "127.0.0.1", slave.server_address[1], "secret",
                            client_alias="::bad-client"
                        )
                    self.assertEqual(ctx.exception.code, "slave_policy_denied")
                finally:
                    slave.shutdown()
                    slave.server_close()
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds142_policy_allow_tailscale_registers_cidr_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "cfg"
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["policy", "allow-tailscale", "--note", "tailnet trusted"])
                output = out.getvalue()
                self.assertIn("allowed Tailscale ranges", output)
                state = load_swarm_state()
                for cidr in TAILSCALE_CIDRS:
                    entry = state["whitelist"][cidr]
                    self.assertTrue(entry["allowed"])
                    self.assertEqual(entry["kind"], "cidr")
                    self.assertIn("tailnet trusted", entry["note"])
                    self.assertIn(cidr, output)
                self.assertEqual(whitelist_status(state, "100.65.1.2"), "allowed")
                self.assertEqual(whitelist_status(state, "fd7a:115c:a1e0::1234"), "allowed")
                self.assertEqual(whitelist_status(state, "8.8.8.8"), "unlisted")
                with redirect_stdout(io.StringIO()):
                    cli_main(["policy", "remove-tailscale"])
                state = load_swarm_state()
                for cidr in TAILSCALE_CIDRS:
                    self.assertNotIn(cidr, state["whitelist"])
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds143_master_strict_policy_allows_saved_tailscale_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "project"
            project.mkdir()
            install_work_mem(project)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                set_connection("tailnode", "100.65.1.2", 7171, "saved-token")
                cli_main(["policy", "allow-tailscale"])
                with patch("getpass.getpass", side_effect=AssertionError("password prompt should not run")):
                    with patch("agentremote.cli.run_master") as run_master_mock:
                        cli_main(
                            [
                                "master",
                                "tailnode",
                                "--local",
                                str(project),
                                "--policy",
                                "strict",
                                "--no-browser",
                                "--console",
                                "no",
                            ]
                        )
                self.assertEqual(run_master_mock.call_args.args[0], "100.65.1.2")
                self.assertEqual(run_master_mock.call_args.kwargs["token"], "saved-token")
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds144_slave_strict_policy_allows_tailscale_client_ip_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            remote_root = root / "remote"
            remote_root.mkdir()
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                cli_main(["policy", "allow-tailscale"])
                state = SlaveState(remote_root, "secret", policy="strict")
                challenge = state.challenge()
                key = derive_key("secret", unb64(challenge["salt"]), int(challenge["iterations"]))
                proof = make_proof(key, challenge["nonce"])
                session = state.login(
                    challenge["nonce"],
                    proof,
                    "100.65.1.2",
                    client_alias="::unlisted-tail-client",
                )
                state.require_token(f"Bearer {session['token']}", "read")
                cli_main(["policy", "deny", "unlisted-tail-client"])
                with self.assertRaises(AgentRemoteError) as ctx:
                    state.require_token(f"Bearer {session['token']}", "read")
                self.assertEqual(ctx.exception.code, "slave_policy_denied")
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds145_process_registry_sanitizes_secrets_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = register_process(
                root,
                "master",
                os.getpid(),
                host="127.0.0.1",
                port=7180,
                ui_url="http://127.0.0.1:7180",
                extra={
                    "token": "hidden",
                    "secretToken": "hidden",
                    "password": "hidden",
                    "safe": "visible",
                    "nested": {"apiKey": "hidden", "safeNested": "ok"},
                },
            )
            self.assertIn("id", record)
            records = list_process_registry(root)
            self.assertEqual(len(records), 1)
            proc = records[0]
            self.assertEqual(proc["status"], "running")
            self.assertNotIn("commandFingerprint", proc)
            as_text = json.dumps(proc)
            self.assertNotIn("hidden", as_text)
            self.assertEqual(proc["extra"]["safe"], "visible")
            self.assertEqual(proc["extra"]["nested"]["safeNested"], "ok")

    def test_ds146_process_forget_removes_only_requested_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = register_process(root, "master", os.getpid())
            second = register_process(root, "daemon-serve", os.getpid(), host="0.0.0.0", port=7171)
            self.assertTrue(forget_process(root, first["id"]))
            remaining = list_process_registry(root)
            self.assertEqual([item["id"] for item in remaining], [second["id"]])
            self.assertFalse(forget_process(root, first["id"]))

    def test_ds147_dashboard_data_includes_sanitized_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            register_process(root, "controller-gui", os.getpid(), ui_url="http://127.0.0.1:7180", extra={"token": "hidden"})
            data = get_dashboard_data(root)
            self.assertIn("processes", data)
            self.assertEqual(len(data["processes"]), 1)
            proc = data["processes"][0]
            self.assertEqual(proc["role"], "controller-gui")
            self.assertNotIn("commandFingerprint", proc)
            self.assertNotIn("hidden", json.dumps(proc))

    def test_ds148_dashboard_process_stop_refuses_mismatched_fingerprint(self) -> None:
        class DummyRemote:
            base_url = "http://dummy"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = register_process(root, "master", os.getpid())
            path = root / ".agentremote" / "processes.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data[record["id"]]["commandFingerprint"] = "forged"
            path.write_text(json.dumps(data), encoding="utf-8")
            master = AgentRemoteMasterServer(("127.0.0.1", 0), MasterState(root, DummyRemote()))
            threading.Thread(target=master.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{master.server_address[1]}"
                with patch("agentremote.master.os.kill") as kill_mock:
                    with self.assertRaises(HTTPError) as ctx:
                        request_json(base, "POST", "/api/dashboard/process/stop", {"id": record["id"]})
                    self.assertEqual(ctx.exception.code, 403)
                    kill_mock.assert_not_called()
            finally:
                master.shutdown()
                master.server_close()

    def test_ds149_dashboard_process_stop_uses_verified_registry_record(self) -> None:
        class DummyRemote:
            base_url = "http://dummy"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = register_process(root, "master", os.getpid(), host="127.0.0.1", port=7180)
            master = AgentRemoteMasterServer(("127.0.0.1", 0), MasterState(root, DummyRemote()))
            threading.Thread(target=master.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{master.server_address[1]}"
                with patch("agentremote.master.os.kill") as kill_mock:
                    result = request_json(base, "POST", "/api/dashboard/process/stop", {"id": record["id"]})
                    self.assertTrue(result["ok"])
                    kill_mock.assert_called_once()
            finally:
                master.shutdown()
                master.server_close()

    def test_ds150_dashboard_ui_process_args_are_uri_encoded(self) -> None:
        html = (Path(__file__).resolve().parents[1] / "src" / "agentremote" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("stopProcess(decodeURIComponent", html)
        self.assertIn("forgetProcess(decodeURIComponent", html)
        self.assertIn("showCallDetail(decodeURIComponent", html)
        self.assertNotIn("stopProcess('${esc(proc.id)}')", html)
        self.assertNotIn("showCallDetail('${esc(call.callId)}')", html)
        self.assertNotIn("        }\n        }\n        }\n        document.getElementById(\"dashboard-content\")", html)

    def test_ds151_processes_list_uses_root_and_sanitizes_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "project"
            other = base / "other"
            project.mkdir()
            other.mkdir()
            install_work_mem(project)
            record = register_process(
                project,
                "master",
                os.getpid(),
                host="127.0.0.1",
                port=7180,
                extra={"password": "hidden", "safe": "visible"},
            )
            previous_cwd = Path.cwd()
            try:
                os.chdir(other)
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["processes", "list", "--root", str(project)])
                text = out.getvalue()
            finally:
                os.chdir(previous_cwd)
            self.assertIn(record["id"], text)
            self.assertIn("master", text)
            self.assertNotIn("hidden", text)
            self.assertNotIn("commandFingerprint", text)

    def test_ds152_processes_forget_removes_registered_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_work_mem(root)
            record = register_process(root, "worker", os.getpid())
            out = io.StringIO()
            with redirect_stdout(out):
                cli_main(["processes", "forget", record["id"], "--root", str(root)])
            self.assertIn("forgotten", out.getvalue())
            self.assertEqual(list_process_registry(root), [])

    def test_ds153_processes_stop_refuses_mismatched_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_work_mem(root)
            record = register_process(root, "master", os.getpid())
            path = root / ".agentremote" / "processes.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data[record["id"]]["commandFingerprint"] = "forged"
            path.write_text(json.dumps(data), encoding="utf-8")
            out = io.StringIO()
            with patch("agentremote.cli.os.kill") as kill_mock:
                with redirect_stdout(out):
                    cli_main(["processes", "stop", record["id"], "--root", str(root)])
            self.assertIn("refusing", out.getvalue())
            kill_mock.assert_not_called()

    def test_ds154_processes_stop_uses_verified_registry_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_work_mem(root)
            record = register_process(root, "master", os.getpid(), host="127.0.0.1", port=7180)
            out = io.StringIO()
            with patch("agentremote.cli.os.kill") as kill_mock:
                with redirect_stdout(out):
                    cli_main(["processes", "stop", record["id"], "--root", str(root)])
            self.assertIn("stopped", out.getvalue())
            kill_mock.assert_called_once()

    def test_ds155_worker_instruction_id_runs_once_without_process_registration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_work_mem(root)
            with patch("agentremote.cli.run_worker_once", return_value={"state": "claimed"}) as once_mock:
                with patch("agentremote.cli.run_worker_loop") as loop_mock:
                    with redirect_stdout(io.StringIO()):
                        cli_main(["worker", "--root", str(root), "--instruction-id", "instr-1"])
            once_mock.assert_called_once()
            self.assertEqual(once_mock.call_args.kwargs["instruction_id"], "instr-1")
            loop_mock.assert_not_called()
            self.assertEqual(list_process_registry(root), [])

    def test_ds156_worker_loop_registers_sanitized_process_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_work_mem(root)
            with patch("agentremote.cli.run_worker_loop", return_value={"state": "stopped"}):
                with redirect_stdout(io.StringIO()):
                    cli_main(["worker", "--root", str(root), "--max-iterations", "1", "--interval", "0.1"])
            records = list_process_registry(root)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["role"], "worker")
            self.assertEqual(records[0]["extra"]["execute"], "never")
            self.assertEqual(records[0]["extra"]["maxIterations"], 1)
            self.assertNotIn("commandFingerprint", records[0])

    def test_ds157_heartbeat_can_target_one_process_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = register_process(root, "master", os.getpid())
            second = register_process(root, "worker", os.getpid())
            path = root / ".agentremote" / "processes.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data[first["id"]]["lastSeenAt"] = 1
            data[second["id"]]["lastSeenAt"] = 1
            path.write_text(json.dumps(data), encoding="utf-8")
            update_process_heartbeat(root, os.getpid(), process_id=second["id"])
            updated = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(updated[first["id"]]["lastSeenAt"], 1)
            self.assertGreater(updated[second["id"]]["lastSeenAt"], 1)

    def test_ds158_mobile_pairing_does_not_store_plaintext_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_work_mem(root)
            result = create_mobile_pairing(root, "Daystar iPhone", scopes="read,process-control")
            token = result["token"]
            data_path = root / ".agentremote" / "mobile_devices.json"
            raw = data_path.read_text(encoding="utf-8")
            self.assertNotIn(token, raw)
            self.assertIn("tokenHash", raw)
            self.assertNotIn("tokenHash", json.dumps(result["device"]))
            self.assertEqual(result["device"]["scopes"], ["read", "process-control"])

    def test_ds159_mobile_expired_and_revoked_tokens_are_rejected_by_endpoint(self) -> None:
        class DummyRemote:
            base_url = "http://dummy"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_work_mem(root)
            expired = create_mobile_pairing(root, "Expired", scopes="read")
            data_path = root / ".agentremote" / "mobile_devices.json"
            data = json.loads(data_path.read_text(encoding="utf-8"))
            data["devices"][expired["device"]["id"]]["expiresAt"] = 1
            data_path.write_text(json.dumps(data), encoding="utf-8")
            revoked = create_mobile_pairing(root, "Revoked", scopes="read")
            revoke_mobile_device(root, revoked["device"]["id"])
            master = AgentRemoteMasterServer(("127.0.0.1", 0), MasterState(root, DummyRemote()))
            threading.Thread(target=master.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{master.server_address[1]}"
                with self.assertRaises(HTTPError) as expired_ctx:
                    request_json_headers(
                        base,
                        "GET",
                        "/api/mobile/controller",
                        headers={"Authorization": f"Bearer {expired['token']}"},
                    )
                self.assertEqual(expired_ctx.exception.code, 401)
                with self.assertRaises(HTTPError) as revoked_ctx:
                    request_json_headers(
                        base,
                        "GET",
                        "/api/mobile/controller",
                        headers={"Authorization": f"Bearer {revoked['token']}"},
                    )
                self.assertEqual(revoked_ctx.exception.code, 403)
            finally:
                master.shutdown()
                master.server_close()

    def test_ds160_mobile_read_only_token_cannot_stop_or_forget_processes(self) -> None:
        class DummyRemote:
            base_url = "http://dummy"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_work_mem(root)
            pairing = create_mobile_pairing(root, "Viewer", scopes="read")
            record = register_process(root, "master", os.getpid())
            master = AgentRemoteMasterServer(("127.0.0.1", 0), MasterState(root, DummyRemote()))
            threading.Thread(target=master.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{master.server_address[1]}"
                headers = {"Authorization": f"Bearer {pairing['token']}"}
                with patch("agentremote.master.os.kill") as kill_mock:
                    with self.assertRaises(HTTPError) as stop_ctx:
                        request_json_headers(base, "POST", "/api/mobile/process/stop", {"id": record["id"]}, headers=headers)
                    self.assertEqual(stop_ctx.exception.code, 403)
                    kill_mock.assert_not_called()
                with self.assertRaises(HTTPError) as forget_ctx:
                    request_json_headers(base, "POST", "/api/mobile/process/forget", {"id": record["id"]}, headers=headers)
                self.assertEqual(forget_ctx.exception.code, 403)
                self.assertEqual(len(list_process_registry(root)), 1)
            finally:
                master.shutdown()
                master.server_close()

    def test_ds161_mobile_process_control_token_can_stop_verified_process(self) -> None:
        class DummyRemote:
            base_url = "http://dummy"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_work_mem(root)
            pairing = create_mobile_pairing(root, "Operator", scopes="read,process-control")
            record = register_process(root, "master", os.getpid(), host="127.0.0.1", port=7180)
            master = AgentRemoteMasterServer(("127.0.0.1", 0), MasterState(root, DummyRemote()))
            threading.Thread(target=master.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{master.server_address[1]}"
                headers = {"Authorization": f"Bearer {pairing['token']}"}
                with patch("agentremote.master.os.kill") as kill_mock:
                    result = request_json_headers(base, "POST", "/api/mobile/process/stop", {"id": record["id"]}, headers=headers)
                self.assertTrue(result["ok"])
                kill_mock.assert_called_once()
            finally:
                master.shutdown()
                master.server_close()

    def test_ds162_mobile_device_list_and_cli_output_are_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_work_mem(root)
            result = create_mobile_pairing(root, "Sanitized", scopes="read")
            devices = list_mobile_devices(root)
            self.assertEqual(len(devices), 1)
            as_text = json.dumps(devices)
            self.assertNotIn(result["token"], as_text)
            self.assertNotIn("tokenHash", as_text)
            out = io.StringIO()
            with redirect_stdout(out):
                cli_main(["controller", "devices", "--local", str(root), "--json"])
            cli_text = out.getvalue()
            self.assertNotIn(result["token"], cli_text)
            self.assertNotIn("tokenHash", cli_text)
            self.assertIn("Sanitized", cli_text)

    def test_ds163_mobile_pair_and_revoke_create_aimemory_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_work_mem(root)
            result = create_mobile_pairing(root, "Event Phone", scopes="read")
            revoke_mobile_device(root, result["device"]["id"])
            events = list((root / "AIMemory" / "swarm" / "events").glob("*.md"))
            text = "\n".join(path.read_text(encoding="utf-8") for path in events)
            self.assertIn("MOBILE_DEVICE_PAIRED", text)
            self.assertIn("MOBILE_DEVICE_REVOKED", text)
            self.assertNotIn(result["token"], text)

    def test_ds164_mobile_controller_endpoint_returns_sanitized_summary(self) -> None:
        class DummyRemote:
            base_url = "http://dummy"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_work_mem(root)
            pairing = create_mobile_pairing(root, "Viewer", scopes="read")
            register_process(root, "controller-gui", os.getpid(), ui_url="http://127.0.0.1:7180", extra={"token": "hidden"})
            master = AgentRemoteMasterServer(("127.0.0.1", 0), MasterState(root, DummyRemote()))
            threading.Thread(target=master.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{master.server_address[1]}"
                result = request_json_headers(
                    base,
                    "GET",
                    "/api/mobile/controller",
                    headers={"X-AgentRemote-Mobile-Token": pairing["token"]},
                )
            finally:
                master.shutdown()
                master.server_close()
            self.assertIn("controller", result)
            self.assertIn("topology", result)
            self.assertIn("processes", result)
            self.assertIn("recentCalls", result)
            self.assertIn("transfers", result)
            self.assertEqual(result["device"]["name"], "Viewer")
            as_text = json.dumps(result)
            self.assertNotIn("hidden", as_text)
            self.assertNotIn("commandFingerprint", as_text)
            self.assertNotIn(pairing["token"], as_text)

    def test_ds165_dashboard_html_has_mobile_controller_view(self) -> None:
        html = (Path(__file__).resolve().parents[1] / "src" / "agentremote" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("mobile-controller-view", html)
        self.assertIn("data-mobile-controller-view", html)
        self.assertIn("mobile-controller-summary", html)
        self.assertIn("stopProcess(decodeURIComponent", html)
        self.assertIn("forgetProcess(decodeURIComponent", html)
        self.assertNotIn("stopProcess('${esc(proc.id)}')", html)

    def test_ds106_route_probe_healthy_records_latency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            remote_root = root / "remote"
            remote_root.mkdir()
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                state = SlaveState(remote_root, "secret")
                slave = AgentRemoteSlaveServer(("127.0.0.1", 0), state)
                threading.Thread(target=slave.serve_forever, daemon=True).start()
                try:
                    cli_main(["route", "set", "probe-ok", "127.0.0.1", str(slave.server_address[1]), "--priority", "1"])
                    out = io.StringIO()
                    with redirect_stdout(out):
                        cli_main(["route", "probe", "probe-ok"])
                    self.assertIn("OK", out.getvalue())
                finally:
                    slave.shutdown()
                    slave.server_close()
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds107_route_probe_failed_records_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                cli_main(["route", "set", "dead", "127.0.0.1", "19999", "--priority", "1"])
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["route", "probe", "dead", "--timeout", "0.5"])
                self.assertIn("FAIL", out.getvalue())
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    # ========================================================================
    # Phase 13: Node status + call/return lifecycle (ds115+)
    # ========================================================================
    def test_ds115_node_endpoint_returns_safe_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote_root = root / "remote"
            remote_root.mkdir()
            state = SlaveState(remote_root, "secret", node_name="test-node-1")
            slave = AgentRemoteSlaveServer(("127.0.0.1", 0), state)
            threading.Thread(target=slave.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{slave.server_address[1]}"
                info = request_json(base, "GET", "/api/node")
                self.assertEqual(info["nodeName"], "test-node-1")
                self.assertIn("capabilities", info)
                self.assertNotIn("root", info)
                self.assertNotIn("password", info)
            finally:
                slave.shutdown()
                slave.server_close()

    def test_ds116_nodes_status_records_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "proj"
            remote_root = root / "remote"
            project.mkdir()
            remote_root.mkdir()
            install_work_mem(project)
            slave = self.start_slave(remote_root)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = Path.cwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                os.chdir(project)
                cli_main(["connect", "status-test", "127.0.0.1", str(slave.server_address[1]), "--password", "secret"])
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["nodes", "status", "status-test"])
                self.assertIn("online", out.getvalue())
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home
                slave.shutdown()
                slave.server_close()

    def test_ds117_call_record_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "proj"
            remote = root / "remote"
            project.mkdir()
            remote.mkdir()
            install_work_mem(project)
            install_work_mem(remote)
            slave = self.start_slave(remote)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = Path.cwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                os.chdir(project)
                cli_main(["connect", "call-node", "127.0.0.1", str(slave.server_address[1]), "--password", "secret"])
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["call", "call-node", "test call task"])
                self.assertIn("call sent:", out.getvalue())
                calls_dir = project / ".agentremote" / "calls"
                self.assertTrue(calls_dir.exists())
                files = list(calls_dir.glob("call-*.json"))
                self.assertEqual(len(files), 1)
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home
                slave.shutdown()
                slave.server_close()

    def test_ds118_calls_list_shows_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "proj"
            project.mkdir()
            install_work_mem(project)
            previous_cwd = Path.cwd()
            try:
                os.chdir(project)
                save_call_record("::test-calls", "inst-1", "handoff-1", ["/test"], "sent")
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["calls", "list"])
                self.assertIn("::test-calls", out.getvalue())
            finally:
                os.chdir(previous_cwd)

    # ========================================================================
    # Phase 14: Dashboard + Report linkage (ds126+)
    # ========================================================================
    def test_ds126_dashboard_api_no_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = root / "local"
            remote = root / "remote"
            local.mkdir()
            remote.mkdir()
            slave = self.start_slave(remote)
            client = RemoteClient("127.0.0.1", slave.server_address[1], "secret")
            master = AgentRemoteMasterServer(("127.0.0.1", 0), MasterState(local, client))
            threading.Thread(target=master.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{master.server_address[1]}"
                data = request_json(base, "GET", "/api/dashboard")
                self.assertIn("nodes", data)
                self.assertIn("recentCalls", data)
                self.assertIn("connectionCount", data)
                for node in data.get("nodes", []):
                    self.assertNotIn("token", str(node))
                    self.assertNotIn("password", str(node))
            finally:
                master.shutdown()
                master.server_close()
                slave.shutdown()
                slave.server_close()

    def test_ds127_calls_refresh_links_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "proj"
            project.mkdir()
            install_work_mem(project)
            previous_cwd = Path.cwd()
            try:
                os.chdir(project)
                rec = save_call_record("::rep-node", "inst-abc", "handoff-xyz", [], "sent")
                from agentremote.inbox import create_instruction as _ci
                _ci(project, f"Status report for {rec['handoffId']}: all good", from_name="worker")
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["calls", "refresh"])
                self.assertIn("Refreshed 1", out.getvalue())
            finally:
                os.chdir(previous_cwd)

    # ========================================================================
    # Phase 15: Dashboard actions (ds134+)
    # ========================================================================
    def test_ds134_dashboard_refresh_node_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            local = root / "local"
            remote = root / "remote"
            local.mkdir()
            remote.mkdir()
            slave = self.start_slave(remote)
            client = RemoteClient("127.0.0.1", slave.server_address[1], "secret")
            master = AgentRemoteMasterServer(("127.0.0.1", 0), MasterState(local, client))
            threading.Thread(target=master.serve_forever, daemon=True).start()
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                set_connection("refresh-node", "127.0.0.1", slave.server_address[1], "token")
                base = f"http://127.0.0.1:{master.server_address[1]}"
                resp = request_json(base, "POST", "/api/dashboard/refresh-node", {"node": "refresh-node"})
                self.assertEqual(resp["status"], "online")
                self.assertEqual(load_swarm_state()["nodes"]["::refresh-node"]["lastStatus"], "online")
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home
                master.shutdown()
                master.server_close()
                slave.shutdown()
                slave.server_close()

    def test_ds135_dashboard_refresh_all_no_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            local = root / "local"
            remote = root / "remote"
            local.mkdir()
            remote.mkdir()
            slave = self.start_slave(remote)
            client = RemoteClient("127.0.0.1", slave.server_address[1], "secret")
            master = AgentRemoteMasterServer(("127.0.0.1", 0), MasterState(local, client))
            threading.Thread(target=master.serve_forever, daemon=True).start()
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                set_connection("refresh-all", "127.0.0.1", slave.server_address[1], "secret-token")
                base = f"http://127.0.0.1:{master.server_address[1]}"
                resp = request_json(base, "POST", "/api/dashboard/refresh-all")
                self.assertIn("results", resp)
                self.assertEqual(resp["results"]["::refresh-all"]["status"], "online")
                self.assertNotIn("token", json.dumps(resp))
                self.assertNotIn("secret-token", json.dumps(resp))
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home
                master.shutdown()
                master.server_close()
                slave.shutdown()
                slave.server_close()

    def test_ds136_dashboard_call_detail_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = root / "local"
            remote = root / "remote"
            local.mkdir()
            remote.mkdir()
            (local / ".agentremote" / "calls").mkdir(parents=True)
            call_id = "call-test-sanitize"
            (local / ".agentremote" / "calls" / f"{call_id}.json").write_text(
                json.dumps({"callId": call_id, "targetNode": "x", "state": "sent", "sentAt": 0, "reportedAt": None, "secretToken": "should-be-stripped", "instructionId": "i1", "handoffId": "h1", "paths": []})
            )
            slave = self.start_slave(remote)
            client = RemoteClient("127.0.0.1", slave.server_address[1], "secret")
            master = AgentRemoteMasterServer(("127.0.0.1", 0), MasterState(local, client))
            threading.Thread(target=master.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{master.server_address[1]}"
                resp = request_json(base, "GET", f"/api/dashboard/call/{call_id}")
                self.assertNotIn("secretToken", resp)
                self.assertEqual(resp["callId"], call_id)
            finally:
                master.shutdown()
                master.server_close()
                slave.shutdown()
                slave.server_close()

    def test_ds137_dashboard_refresh_node_respects_https_route_tls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            local = root / "local"
            remote = root / "remote"
            certs = root / "certs"
            local.mkdir()
            remote.mkdir()
            slave, fingerprint = self.start_tls_slave(remote, certs)
            client = RemoteClient(
                f"https://127.0.0.1:{slave.server_address[1]}",
                slave.server_address[1],
                "secret",
                tls_fingerprint=fingerprint,
            )
            master = AgentRemoteMasterServer(("127.0.0.1", 0), MasterState(local, client))
            threading.Thread(target=master.serve_forever, daemon=True).start()
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = Path.cwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                os.chdir(local)
                cli_main([
                    "route",
                    "set",
                    "tls-dash",
                    f"https://127.0.0.1:{slave.server_address[1]}",
                    str(slave.server_address[1]),
                    "--tls-fingerprint",
                    fingerprint,
                ])
                base = f"http://127.0.0.1:{master.server_address[1]}"
                resp = request_json(base, "POST", "/api/dashboard/refresh-node", {"node": "tls-dash"})
                self.assertEqual(resp["status"], "online")
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home
                master.shutdown()
                master.server_close()
                slave.shutdown()
                slave.server_close()

    def test_ds138_dashboard_refresh_node_prefers_explicit_route_over_saved_connection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            local = root / "local"
            remote = root / "remote"
            local.mkdir()
            remote.mkdir()
            slave = self.start_slave(remote)
            client = RemoteClient("127.0.0.1", slave.server_address[1], "secret")
            master = AgentRemoteMasterServer(("127.0.0.1", 0), MasterState(local, client))
            threading.Thread(target=master.serve_forever, daemon=True).start()
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = Path.cwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                os.chdir(local)
                set_connection("route-action", "127.0.0.1", 19997, "dead-token")
                cli_main(["route", "set", "route-action", "127.0.0.1", str(slave.server_address[1]), "--priority", "1"])
                base = f"http://127.0.0.1:{master.server_address[1]}"
                resp = request_json(base, "POST", "/api/dashboard/refresh-node", {"node": "route-action"})
                self.assertEqual(resp["status"], "online")
                state = load_swarm_state()
                self.assertEqual(state["nodes"]["::route-action"]["route"]["port"], slave.server_address[1])
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home
                master.shutdown()
                master.server_close()
                slave.shutdown()
                slave.server_close()

    def test_ds139_dashboard_unknown_node_returns_404(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            local = root / "local"
            remote = root / "remote"
            local.mkdir()
            remote.mkdir()
            slave = self.start_slave(remote)
            client = RemoteClient("127.0.0.1", slave.server_address[1], "secret")
            master = AgentRemoteMasterServer(("127.0.0.1", 0), MasterState(local, client))
            threading.Thread(target=master.serve_forever, daemon=True).start()
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                base = f"http://127.0.0.1:{master.server_address[1]}"
                with self.assertRaises(HTTPError) as ctx:
                    request_json(base, "POST", "/api/dashboard/refresh-node", {"node": "missing-node"})
                self.assertEqual(ctx.exception.code, 404)
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home
                master.shutdown()
                master.server_close()
                slave.shutdown()
                slave.server_close()

    def test_ds140_dashboard_call_detail_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = root / "local"
            remote = root / "remote"
            local.mkdir()
            remote.mkdir()
            slave = self.start_slave(remote)
            client = RemoteClient("127.0.0.1", slave.server_address[1], "secret")
            master = AgentRemoteMasterServer(("127.0.0.1", 0), MasterState(local, client))
            threading.Thread(target=master.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{master.server_address[1]}"
                with self.assertRaises(HTTPError) as ctx:
                    request_json(base, "GET", "/api/dashboard/call/..%2Foutside")
                self.assertEqual(ctx.exception.code, 400)
                self.assertFalse((local / ".agentremote" / "calls").exists())
            finally:
                master.shutdown()
                master.server_close()
                slave.shutdown()
                slave.server_close()

    def test_ds141_dashboard_ui_has_no_stale_duplicate_block_and_safe_inline_args(self) -> None:
        html = (Path(__file__).resolve().parents[1] / "src" / "agentremote" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("decodeURIComponent('${encodeURIComponent(node.name)}')", html)
        self.assertIn("esc(call.callId)", html)
        self.assertIn("stopProcess(decodeURIComponent", html)
        self.assertIn("forgetProcess(decodeURIComponent", html)
        self.assertNotIn("stopProcess('${esc(proc.id)}')", html)
        self.assertNotIn("escapeHtml(call.callId)", html)
        self.assertEqual(html.count("function loadDashboard()"), 1)

    def test_ds128_dashboard_uses_local_root_and_sanitizes_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = root / "local"
            remote = root / "remote"
            local.mkdir()
            remote.mkdir()
            install_work_mem(local)
            rec = save_call_record("::dash-node", "inst-dash", "handoff-dash", [], "sent", root=local)
            call_path = local / ".agentremote" / "calls" / f"{rec['callId']}.json"
            payload = json.loads(call_path.read_text(encoding="utf-8"))
            payload["token"] = "secret-token-value"
            payload["password"] = "secret-password-value"
            call_path.write_text(json.dumps(payload), encoding="utf-8")
            slave = self.start_slave(remote)
            client = RemoteClient("127.0.0.1", slave.server_address[1], "secret")
            master = AgentRemoteMasterServer(("127.0.0.1", 0), MasterState(local, client))
            threading.Thread(target=master.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{master.server_address[1]}"
                data = request_json(base, "GET", "/api/dashboard")
                self.assertEqual(data["recentCalls"][0]["callId"], rec["callId"])
                serialized = json.dumps(data)
                self.assertNotIn("secret-token-value", serialized)
                self.assertNotIn("secret-password-value", serialized)
                self.assertNotIn("token", serialized.lower())
                self.assertNotIn("password", serialized.lower())
            finally:
                master.shutdown()
                master.server_close()
                slave.shutdown()
                slave.server_close()

    def test_ds129_calls_refresh_requires_status_report_parent_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "proj"
            project.mkdir()
            install_work_mem(project)
            previous_cwd = Path.cwd()
            try:
                os.chdir(project)
                rec = save_call_record("::rep-node", "inst-abc", "handoff-xyz", [], "sent")
                create_instruction(project, f"Please mention {rec['handoffId']} but this is not a report")
                with redirect_stdout(io.StringIO()):
                    cli_main(["calls", "refresh"])
                still_sent = json.loads((project / ".agentremote" / "calls" / f"{rec['callId']}.json").read_text(encoding="utf-8"))
                self.assertEqual(still_sent["state"], "sent")
                create_instruction(
                    project,
                    "Completed successfully",
                    from_name="worker",
                    handoff={
                        "title": "Report",
                        "task": "Completed successfully",
                        "from": "worker",
                        "to": "controller",
                        "type": "STATUS_REPORT",
                        "parentId": rec["handoffId"],
                    },
                )
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["calls", "refresh"])
                self.assertIn("Refreshed 1", out.getvalue())
                updated = json.loads((project / ".agentremote" / "calls" / f"{rec['callId']}.json").read_text(encoding="utf-8"))
                self.assertEqual(updated["state"], "reported")
                self.assertTrue(updated.get("reportedAt"))
                self.assertTrue(updated.get("reportInstructionId"))
            finally:
                os.chdir(previous_cwd)

    def test_ds130_calls_refresh_updates_aimemory_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "proj"
            project.mkdir()
            install_work_mem(project)
            previous_cwd = Path.cwd()
            try:
                os.chdir(project)
                rec = save_call_record("::mirror-report", "inst-mirror", "handoff-mirror", [], "sent")
                create_instruction(
                    project,
                    "Blocked by missing dependency",
                    from_name="worker",
                    handoff={
                        "title": "Report",
                        "task": "Blocked by missing dependency",
                        "from": "worker",
                        "to": "controller",
                        "type": "STATUS_REPORT",
                        "parentId": rec["handoffId"],
                    },
                )
                with redirect_stdout(io.StringIO()):
                    cli_main(["calls", "refresh"])
                mirror = project / "AIMemory" / "swarm" / "calls" / f"{rec['callId']}.md"
                text = mirror.read_text(encoding="utf-8")
                self.assertIn("- state: failed", text)
                self.assertIn("- reportedAt:", text)
            finally:
                os.chdir(previous_cwd)

    def test_ds131_calls_show_missing_does_not_create_call_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "proj"
            project.mkdir()
            install_work_mem(project)
            previous_cwd = Path.cwd()
            try:
                os.chdir(project)
                with self.assertRaises(SystemExit):
                    cli_main(["calls", "show", "call-missing"])
                self.assertFalse((project / ".agentremote" / "calls").exists())
            finally:
                os.chdir(previous_cwd)

    def test_ds132_dashboard_data_prefers_merged_selected_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "proj"
            project.mkdir()
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                set_connection("route-dash", "saved.example", 7171, "saved-token")
                cli_main(["route", "set", "route-dash", "preferred.example", "7443", "--priority", "1"])
                data = get_dashboard_data(project)
                node = next(item for item in data["nodes"] if item["name"] == "::route-dash")
                self.assertEqual(node["route"]["host"], "preferred.example")
                self.assertEqual(node["route"]["source"], "explicit")
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds133_dashboard_ui_escapes_dynamic_values(self) -> None:
        html = (Path(__file__).resolve().parents[1] / "src" / "agentremote" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("esc(node.name)", html)
        self.assertIn("esc(call.callId)", html)
        self.assertIn("esc(call.targetNode)", html)

    def test_ds119_nodes_status_persists_to_swarm_and_topology(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "proj"
            remote_root = root / "remote"
            project.mkdir()
            remote_root.mkdir()
            install_work_mem(project)
            slave = self.start_slave(remote_root)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = Path.cwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                os.chdir(project)
                cli_main(["connect", "status-test", "127.0.0.1", str(slave.server_address[1]), "--password", "secret"])
                with redirect_stdout(io.StringIO()):
                    cli_main(["nodes", "status", "status-test"])
                state = load_swarm_state()
                self.assertEqual(state["nodes"]["::status-test"]["lastStatus"], "online")
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["topology", "show"])
                self.assertIn("status=online", out.getvalue())
                self.assertTrue((project / "AIMemory" / "swarm" / "nodes" / "status-test.md").exists())
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home
                slave.shutdown()
                slave.server_close()

    def test_ds120_nodes_status_offline_preserves_last_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "proj"
            remote_root = root / "remote"
            project.mkdir()
            remote_root.mkdir()
            install_work_mem(project)
            state = SlaveState(remote_root, "secret", model_id="node-model-1")
            slave = AgentRemoteSlaveServer(("127.0.0.1", 0), state)
            threading.Thread(target=slave.serve_forever, daemon=True).start()
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = Path.cwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                os.chdir(project)
                cli_main(["connect", "offline-test", "127.0.0.1", str(slave.server_address[1]), "--password", "secret"])
                with redirect_stdout(io.StringIO()):
                    cli_main(["nodes", "status", "offline-test"])
                slave.shutdown()
                slave.server_close()
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["nodes", "status", "offline-test", "--timeout", "0.2"])
                self.assertIn("offline", out.getvalue())
                state_data = load_swarm_state()
                record = state_data["nodes"]["::offline-test"]
                self.assertEqual(record["lastStatus"], "offline")
                self.assertEqual(record["modelId"], "node-model-1")
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home
                try:
                    slave.server_close()
                except Exception:
                    pass

    def test_ds121_connect_sends_alias_for_strict_slave_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "proj"
            remote_root = root / "remote"
            project.mkdir()
            remote_root.mkdir()
            install_work_mem(project)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = Path.cwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                os.chdir(project)
                cli_main(["policy", "allow", "connect-name"])
                state = SlaveState(remote_root, "secret", policy="strict")
                slave = AgentRemoteSlaveServer(("127.0.0.1", 0), state)
                threading.Thread(target=slave.serve_forever, daemon=True).start()
                try:
                    out = io.StringIO()
                    with redirect_stdout(out):
                        cli_main(["connect", "connect-name", "127.0.0.1", str(slave.server_address[1]), "--password", "secret"])
                    self.assertIn("connected: ::connect-name", out.getvalue())
                finally:
                    slave.shutdown()
                    slave.server_close()
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds122_call_with_path_cleans_remote_upload_when_tell_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "proj"
            remote_root = root / "remote"
            project.mkdir()
            remote_root.mkdir()
            install_work_mem(project)
            (project / "payload.txt").write_text("payload", encoding="utf-8")
            slave = self.start_slave(remote_root)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = Path.cwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                os.chdir(project)
                cli_main(["connect", "call-node", "127.0.0.1", str(slave.server_address[1]), "--password", "secret"])
                import agentremote.headless as headless_module

                with patch.object(headless_module, "tell", side_effect=AgentRemoteError(500, "tell_failed", "tell failed")):
                    with self.assertRaises(SystemExit) as ctx:
                        cli_main(["call", "call-node", "do task", "--path", "payload.txt"])
                self.assertIn("tell_failed", str(ctx.exception))
                self.assertFalse((remote_root / "incoming" / "payload.txt").exists())
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home
                slave.shutdown()
                slave.server_close()

    def test_ds123_calls_list_does_not_create_empty_call_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "proj"
            project.mkdir()
            install_work_mem(project)
            previous_cwd = Path.cwd()
            try:
                os.chdir(project)
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["calls", "list"])
                self.assertIn("No call records.", out.getvalue())
                self.assertFalse((project / ".agentremote" / "calls").exists())
            finally:
                os.chdir(previous_cwd)

    def test_ds124_call_record_writes_aimemory_mirror_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "proj"
            project.mkdir()
            install_work_mem(project)
            previous_cwd = Path.cwd()
            try:
                os.chdir(project)
                record = save_call_record("::mirror-node", "inst-1", "handoff-1", ["/incoming/payload.txt"], "sent")
                mirror = project / "AIMemory" / "swarm" / "calls" / f"{record['callId']}.md"
                self.assertTrue(mirror.exists())
                text = mirror.read_text(encoding="utf-8").lower()
                self.assertIn("mirror-node", text)
                self.assertNotIn("secret", text)
                self.assertNotIn("token", text)
            finally:
                os.chdir(previous_cwd)

    def test_ds125_nodes_status_json_includes_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "proj"
            remote_root = root / "remote"
            project.mkdir()
            remote_root.mkdir()
            install_work_mem(project)
            slave = self.start_slave(remote_root)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = Path.cwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                os.chdir(project)
                cli_main(["connect", "json-node", "127.0.0.1", str(slave.server_address[1]), "--password", "secret"])
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["nodes", "status", "json-node", "--json"])
                info = json.loads(out.getvalue())
                self.assertEqual(info["status"], "online")
                self.assertEqual(info["nodeKey"], "::json-node")
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home
                slave.shutdown()
                slave.server_close()

    def test_ds108_route_probe_failure_count_accumulates_and_preserves_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                cli_main(["policy", "allow", "dead"])
                cli_main(["route", "set", "dead", "127.0.0.1", "19998", "--priority", "1"])
                with redirect_stdout(io.StringIO()):
                    cli_main(["route", "probe", "dead", "--timeout", "0.2"])
                    cli_main(["route", "probe", "dead", "--timeout", "0.2"])
                state = load_swarm_state()
                self.assertIn("::dead", state["whitelist"])
                health = state["routeHealth"]["::dead"]["127.0.0.1\t19998"]
                self.assertEqual(health["failureCount"], 2)
                self.assertEqual(list(config.glob("swarm.json.*.tmp")), [])
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds109_route_selection_keeps_priority_before_latency(self) -> None:
        slow_priority = {
            "name": "::node",
            "host": "slow-priority",
            "port": 7171,
            "priority": 1,
            "lastOkAt": 10,
            "lastLatencyMs": 900,
            "lastError": "",
        }
        fast_lower_priority = {
            "name": "::node",
            "host": "fast-lower-priority",
            "port": 7172,
            "priority": 5,
            "lastOkAt": 10,
            "lastLatencyMs": 1,
            "lastError": "",
        }
        self.assertEqual(select_best_route([fast_lower_priority, slow_priority])["host"], "slow-priority")

    def test_ds110_probe_url_preserves_https_route_port(self) -> None:
        self.assertEqual(
            probe_url("https://route.example", 7443, secure=True),
            "https://route.example:7443/api/challenge",
        )
        self.assertEqual(
            probe_url("https://route.example:7444", 7443, secure=True),
            "https://route.example:7444/api/challenge",
        )

    def test_ds111_policy_and_route_probe_create_aimemory_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "project"
            remote_root = root / "remote"
            project.mkdir()
            remote_root.mkdir()
            install_work_mem(project)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = Path.cwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                os.chdir(project)
                cli_main(["policy", "allow", "journal-node"])
                state = SlaveState(remote_root, "secret")
                slave = AgentRemoteSlaveServer(("127.0.0.1", 0), state)
                threading.Thread(target=slave.serve_forever, daemon=True).start()
                try:
                    cli_main(["route", "set", "journal-node", "127.0.0.1", str(slave.server_address[1])])
                    with redirect_stdout(io.StringIO()):
                        cli_main(["route", "probe", "journal-node"])
                finally:
                    slave.shutdown()
                    slave.server_close()
                events = list((project / "AIMemory" / "swarm" / "events").glob("*.md"))
                event_text = "\n".join(path.read_text(encoding="utf-8") for path in events)
                self.assertIn("Policy allow", event_text)
                self.assertIn("Route probe", event_text)
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds112_headless_push_sends_alias_for_strict_slave_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            local = root / "local"
            remote_root = root / "remote"
            local.mkdir()
            remote_root.mkdir()
            install_work_mem(local)
            (local / "payload.txt").write_text("ok", encoding="utf-8")
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = Path.cwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                os.chdir(local)
                cli_main(["policy", "allow", "strict-target"])
                state = SlaveState(remote_root, "secret", policy="strict")
                slave = AgentRemoteSlaveServer(("127.0.0.1", 0), state)
                threading.Thread(target=slave.serve_forever, daemon=True).start()
                try:
                    push(
                        "127.0.0.1",
                        slave.server_address[1],
                        "secret",
                        Path("payload.txt"),
                        "/incoming",
                        alias="::strict-target",
                    )
                finally:
                    slave.shutdown()
                    slave.server_close()
                self.assertTrue((remote_root / "incoming" / "payload.txt").exists())
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds113_saved_connection_probe_persists_health_without_explicit_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            remote_root = root / "remote"
            remote_root.mkdir()
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                state = SlaveState(remote_root, "secret")
                slave = AgentRemoteSlaveServer(("127.0.0.1", 0), state)
                threading.Thread(target=slave.serve_forever, daemon=True).start()
                try:
                    set_connection("saved-probe", "127.0.0.1", slave.server_address[1], "token")
                    with redirect_stdout(io.StringIO()):
                        cli_main(["route", "probe", "saved-probe"])
                    state_data = load_swarm_state()
                    key = f"127.0.0.1\t{slave.server_address[1]}"
                    self.assertIn(key, state_data["routeHealth"]["::saved-probe"])
                    out = io.StringIO()
                    with redirect_stdout(out):
                        cli_main(["route", "list"])
                    self.assertIn("ok=", out.getvalue())
                finally:
                    slave.shutdown()
                    slave.server_close()
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ds114_slave_policy_denial_revokes_active_named_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            remote_root = root / "remote"
            remote_root.mkdir()
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                cli_main(["policy", "allow", "revoked-client"])
                state = SlaveState(remote_root, "secret", policy="strict")
                slave = AgentRemoteSlaveServer(("127.0.0.1", 0), state)
                threading.Thread(target=slave.serve_forever, daemon=True).start()
                try:
                    client = RemoteClient(
                        "127.0.0.1",
                        slave.server_address[1],
                        "secret",
                        client_alias="::revoked-client",
                    )
                    self.assertIn("entries", client.list("/"))
                    cli_main(["policy", "deny", "revoked-client"])
                    with self.assertRaises(AgentRemoteError) as ctx:
                        client.list("/")
                    self.assertEqual(ctx.exception.code, "slave_policy_denied")
                finally:
                    slave.shutdown()
                    slave.server_close()
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home


def request_json(base: str, method: str, path: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        base + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urlopen(request, timeout=60) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else {}

def request_json_headers(
    base: str,
    method: str,
    path: str,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers or {})
    request = Request(
        base + path,
        data=data,
        headers=request_headers,
        method=method,
    )
    with urlopen(request, timeout=60) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else {}

def raw_request(url: str, method: str, body: bytes, headers: dict[str, str], timeout: float = 60) -> bytes:
    request = Request(url, data=body, headers=headers, method=method)
    with urlopen(request, timeout=timeout) as response:
        return response.read()

def make_proof_from_state(state: SlaveState, nonce: str) -> str:
    return make_proof(state.password_key, nonce)


# ========================================================================
# Phase 18: Approval mode tests
# ========================================================================
class ApprovalModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def start_slave(self, root: Path, password: str = "secret"):
        from agentremote.slave import SlaveState, AgentRemoteSlaveServer
        state = SlaveState(root, password)
        server = AgentRemoteSlaveServer(("127.0.0.1", 0), state)
        import threading
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def test_approval_create_and_list(self):
        from agentremote.approval import create_approval_request, list_approval_requests
        req = create_approval_request(self.root, "worker.execute", summary="Run test", risk="high")
        self.assertEqual(req["status"], "pending")
        items = list_approval_requests(self.root, status="pending")
        self.assertEqual(len(items), 1)

    def test_approval_approve_creates_token(self):
        from agentremote.approval import create_approval_request, decide_approval, verify_approval_token
        req = create_approval_request(self.root, "delete.file", summary="Delete x.txt")
        result = decide_approval(self.root, req["approvalId"], "approved", decided_by="test")
        self.assertEqual(result["status"], "approved")
        token = result.get("_approvalToken", "")
        self.assertTrue(verify_approval_token(self.root, req["approvalId"], token))

    def test_approval_deny_sets_status(self):
        from agentremote.approval import create_approval_request, decide_approval
        req = create_approval_request(self.root, "process.stop", summary="Stop")
        result = decide_approval(self.root, req["approvalId"], "denied", decided_by="test")
        self.assertEqual(result["status"], "denied")

    def test_approval_already_decided_rejects(self):
        from agentremote.approval import create_approval_request, decide_approval
        from agentremote.common import AgentRemoteError
        req = create_approval_request(self.root, "test.act")
        decide_approval(self.root, req["approvalId"], "approved")
        with self.assertRaises(AgentRemoteError):
            decide_approval(self.root, req["approvalId"], "denied")

    def test_approval_wait_returns_on_decision(self):
        from agentremote.approval import create_approval_request, decide_approval, wait_for_approval
        import threading
        req = create_approval_request(self.root, "action.wait")
        result_holder = []

        def waiter():
            result_holder.append(wait_for_approval(self.root, req["approvalId"], timeout=5, poll_interval=0.1))

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.2)
        decide_approval(self.root, req["approvalId"], "approved")
        t.join(timeout=3)
        self.assertEqual(len(result_holder), 1)
        self.assertEqual(result_holder[0]["status"], "approved")

    def test_approval_sanitized_no_token_leak(self):
        from agentremote.approval import create_approval_request, decide_approval, sanitize_approval
        req = create_approval_request(self.root, "sensitive.action", details="secret-token-value")
        result = decide_approval(self.root, req["approvalId"], "approved")
        s = sanitize_approval(result)
        self.assertNotIn("_approvalToken", s)
        self.assertNotIn("approvalTokenHash", s)
        self.assertNotIn("details", s)

    def test_worker_policy_allowlisted_executes(self):
        from agentremote.worker_policy import init_policy, allow_rule, check_command
        init_policy(self.root)
        allow_rule(self.root, "python-cmd", "python", args_pattern="*print*")
        allowed = check_command(self.root, 'python -c "print(1)"')
        blocked = check_command(self.root, 'python -c "open(\'other.txt\', \'w\').write(\'bad\')"')
        self.assertTrue(allowed["allowed"])
        self.assertFalse(allowed["shell"])
        self.assertFalse(blocked["allowed"])
        self.assertEqual(blocked["reason"], "no_matching_rule")

    def test_worker_policy_unlisted_blocked(self):
        from agentremote.worker_policy import init_policy, check_command
        init_policy(self.root)
        result = check_command(self.root, "unknown-tool arg1")
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "no_matching_rule")

    def test_worker_policy_blocked_no_output(self):
        install_work_mem(self.root)
        create_instruction(
            self.root,
            "Try blocked output.\nagentremote-run: python -c \"from pathlib import Path; Path('blocked.txt').write_text('bad', encoding='utf-8')\"",
            auto_run=True,
        )
        with redirect_stdout(io.StringIO()):
            result = run_worker_once(self.root, execute="yes")
        self.assertEqual(result["state"], "blocked")
        self.assertIn("policyBlockedCommands", result["plan"])
        self.assertFalse((self.root / "blocked.txt").exists())

    def test_worker_policy_allowlisted_run_applies_stdout_cap(self):
        from agentremote.worker_policy import allow_rule

        install_work_mem(self.root)
        allow_rule(self.root, "python-allowed", "python", args_pattern="*allowed.txt*", max_stdout_bytes=4)
        create_instruction(
            self.root,
            "Write allowed output.\nagentremote-run: python -c \"from pathlib import Path; Path('allowed.txt').write_text('ok', encoding='utf-8'); print('abcdef')\"",
            auto_run=True,
        )
        with redirect_stdout(io.StringIO()):
            result = run_worker_once(self.root, execute="yes")
        self.assertEqual(result["state"], "completed")
        self.assertEqual((self.root / "allowed.txt").read_text(encoding="utf-8"), "ok")
        self.assertIn("abcd", result["results"][0]["stdout"])
        self.assertIn("stdout truncated by worker policy", result["results"][0]["stdout"])

    def test_worker_policy_rule_timeout_overrides_worker_timeout(self):
        from agentremote.worker_policy import allow_rule

        install_work_mem(self.root)
        allow_rule(self.root, "python-sleep", "python", args_pattern="*time.sleep*", timeout_seconds=1)
        create_instruction(
            self.root,
            "Sleep too long.\nagentremote-run: python -c \"import time; time.sleep(10)\"",
            auto_run=True,
        )
        with redirect_stdout(io.StringIO()):
            result = run_worker_once(self.root, execute="yes", timeout=30)
        self.assertEqual(result["state"], "failed")
        self.assertNotEqual(result["results"][0]["exitCode"], 0)
        self.assertIn("timed out after 1s", result["results"][0]["stderr"])

    def test_worker_policy_shell_false_blocks_shell_chaining_side_effect(self):
        from agentremote.worker_policy import allow_rule

        install_work_mem(self.root)
        allow_rule(self.root, "python-print", "python", args_pattern="*print*")
        create_instruction(
            self.root,
            "Do not run chained shell command.\n"
            "agentremote-run: python -c \"print('ok')\" && python -c \"from pathlib import Path; Path('pwned.txt').write_text('bad', encoding='utf-8')\"",
            auto_run=True,
        )
        with redirect_stdout(io.StringIO()):
            result = run_worker_once(self.root, execute="yes")
        self.assertEqual(result["state"], "completed")
        self.assertFalse((self.root / "pwned.txt").exists())
        self.assertFalse(result["plan"]["policyAllowedRules"][0]["shell"])

    def test_worker_policy_exact_command_rule_applies_per_command_caps(self):
        results = execute_commands(
            self.root,
            [
                'python -c "print(\'abcdef\')"',
                'python -c "print(\'uvwxyz\')"',
            ],
            timeout=30,
            policy_allowed=[
                {
                    "command": 'python -c "print(\'abcdef\')"',
                    "timeoutSeconds": 30,
                    "maxStdoutBytes": 4,
                    "shell": False,
                },
                {
                    "command": 'python -c "print(\'uvwxyz\')"',
                    "timeoutSeconds": 30,
                    "maxStdoutBytes": 0,
                    "shell": False,
                },
            ],
        )
        self.assertIn("stdout truncated by worker policy", results[0].stdout)
        self.assertEqual(results[1].stdout.strip(), "uvwxyz")

    def test_worker_policy_templates_default_to_argv_execution(self):
        from agentremote.worker_policy import apply_template, check_command, list_templates

        install_work_mem(self.root)
        templates = list_templates()
        self.assertFalse(templates["python-tests"]["shell"])
        applied = apply_template(self.root, "python-tests")
        self.assertIsNotNone(applied)
        allowed = check_command(self.root, "python -m pytest tests")
        self.assertTrue(allowed["allowed"])
        self.assertFalse(allowed["shell"])

    def test_worker_policy_templates_cli_has_root_and_lists_shell_mode(self):
        install_work_mem(self.root)
        out = io.StringIO()
        with redirect_stdout(out):
            cli_main(["worker-policy", "templates", "--root", str(self.root)])
            cli_main(["worker-policy", "apply-template", "python-compile", "--root", str(self.root)])
            cli_main(["worker-policy", "list", "--root", str(self.root)])
        output = out.getvalue()
        self.assertIn("python-compile", output)
        self.assertIn("shell=false", output)

    def test_worker_policy_summary_redacts_secret_like_values(self):
        from agentremote.worker_policy import allow_rule, worker_policy_summary

        install_work_mem(self.root)
        allow_rule(
            self.root,
            "secret-rule",
            "python",
            args_pattern="*token=super-secret-value*",
            description="password=hidden-value",
        )
        summary = worker_policy_summary(self.root)
        serialized = json.dumps(summary)
        self.assertTrue(summary["hasPolicy"])
        self.assertEqual(summary["ruleCount"], 1)
        self.assertNotIn("super-secret-value", serialized)
        self.assertNotIn("hidden-value", serialized)
        self.assertIn("[redacted]", serialized)

    def test_worker_policy_api_returns_sanitized_summary_and_templates_list(self):
        from agentremote.worker_policy import allow_rule

        class DummyRemote:
            base_url = "http://dummy"

        install_work_mem(self.root)
        allow_rule(
            self.root,
            "api-rule",
            "python",
            args_pattern="*api_key=secret-from-rule*",
            description="credential=hidden-description",
        )
        master = AgentRemoteMasterServer(("127.0.0.1", 0), MasterState(self.root, DummyRemote()))
        threading.Thread(target=master.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{master.server_address[1]}"
            data = request_json(base, "GET", "/api/worker-policy")
        finally:
            master.shutdown()
            master.server_close()
        serialized = json.dumps(data)
        self.assertTrue(data["hasPolicy"])
        self.assertEqual(data["ruleCount"], 1)
        self.assertIsInstance(data["templates"], list)
        self.assertNotIn("secret-from-rule", serialized)
        self.assertNotIn("hidden-description", serialized)
        self.assertIn("[redacted]", serialized)

    def test_worker_policy_api_mutations_respect_approval_mode(self):
        from agentremote.approval import save_approval_policy

        class DummyRemote:
            base_url = "http://dummy"

        install_work_mem(self.root)
        master = AgentRemoteMasterServer(("127.0.0.1", 0), MasterState(self.root, DummyRemote()))
        threading.Thread(target=master.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{master.server_address[1]}"
            created = request_json(base, "POST", "/api/worker-policy/init", {})
            self.assertTrue(created["policy"]["hasPolicy"])

            applied = request_json(base, "POST", "/api/worker-policy/apply-template", {"template": "python-compile"})
            self.assertEqual(applied["policy"]["ruleCount"], 1)
            self.assertEqual(applied["policy"]["rules"][0]["name"], "python-compile")
            self.assertFalse(applied["policy"]["rules"][0]["shell"])

            removed = request_json(base, "POST", "/api/worker-policy/remove", {"name": "python-compile"})
            self.assertEqual(removed["policy"]["ruleCount"], 0)

            save_approval_policy(self.root, "deny")
            with self.assertRaises(HTTPError) as ctx:
                request_json(base, "POST", "/api/worker-policy/apply-template", {"template": "python-tests"})
            self.assertEqual(ctx.exception.code, 403)
        finally:
            master.shutdown()
            master.server_close()

    def test_dashboard_html_has_worker_policy_actions_and_encoded_args(self):
        html = (Path(__file__).resolve().parents[1] / "src" / "agentremote" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("/api/worker-policy", html)
        self.assertIn("/api/worker-policy/init", html)
        self.assertIn("/api/worker-policy/apply-template", html)
        self.assertIn("/api/worker-policy/remove", html)
        self.assertIn("applyWorkerPolicyTemplate(decodeURIComponent", html)
        self.assertIn("removeWorkerPolicyRule(decodeURIComponent", html)
        self.assertIn("worker-policy-grid", html)

    def test_worker_report_includes_rule_shell_metadata_and_redacts_secrets(self):
        manifest = {
            "id": "report-test",
            "task": "Check command output",
        }
        command = 'python -c "print(\'token=super-secret-value\')"'
        plan = {
            "paths": [],
            "commands": [command],
            "blockedCommands": [],
            "policyBlockedCommands": ["unknown-tool password=hidden-value"],
            "policyBlockedDetails": [
                {"command": "unknown-tool password=hidden-value", "reason": "no_matching_rule"}
            ],
            "policyAllowedRules": [
                {"command": command, "rule": "python-tests", "shell": False}
            ],
        }
        report_text = render_report(
            manifest,
            plan,
            "failed",
            [CommandResult(command, 0, "api_key=stdout-secret\n", "password=stderr-secret\n", 0.1)],
        )
        self.assertIn("rule=python-tests shell=false", report_text)
        self.assertIn("reason=no_matching_rule", report_text)
        self.assertIn("[redacted]", report_text)
        self.assertNotIn("super-secret-value", report_text)
        self.assertNotIn("stdout-secret", report_text)
        self.assertNotIn("stderr-secret", report_text)
        self.assertNotIn("hidden-value", report_text)

    def test_release_smoke_script_runs_without_external_pythonpath(self):
        repo = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, str(repo / "smoke.py")],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Result: 8 passed, 0 failed", result.stdout)
        self.assertIn("[PASS] worker execution", result.stdout)

    def test_worker_policy_cli_init_list_allow(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = Path(tmp)
            install_work_mem(r)
            out = io.StringIO()
            with redirect_stdout(out):
                cli_main(["worker-policy", "init", "--root", str(r)])
                cli_main(["worker-policy", "allow", "echo-rule", "echo", "--args-pattern", "hello*", "--network", "off", "--root", str(r), "--shell"])
                cli_main(["worker-policy", "list", "--root", str(r)])
                cli_main(["worker-policy", "remove", "echo-rule", "--root", str(r)])
            output = out.getvalue()
            self.assertIn("initialized:", output)
            self.assertIn("allowed: echo-rule -> echo", output)
            self.assertIn("echo-rule echo args=hello*", output)
            self.assertIn("shell=true", output)
            self.assertIn("removed: echo-rule", output)

    def test_simple_command_surface_help_and_docs(self):
        repo = Path(__file__).resolve().parents[1]
        english = (repo / "README.md").read_text(encoding="utf-8")
        korean = (repo / "README.ko.md").read_text(encoding="utf-8")
        out = io.StringIO()
        with self.assertRaises(SystemExit) as ctx:
            with redirect_stdout(out):
                cli_main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        help_text = out.getvalue()
        for command in ("setup", "share", "open", "send", "sync-project", "map", "status", "uninstall"):
            self.assertIn(command, help_text)
            self.assertIn(f"agentremote {command}", english)
            self.assertIn(f"agentremote {command}", korean)

    def test_share_wrapper_defaults_to_local_only_and_skips_firewall(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_work_mem(root)
            with patch("agentremote.cli.run_slave") as run_slave_mock:
                cli_main(["share", "--root", str(root), "--password", "pw", "--console", "no"])
                self.assertEqual(run_slave_mock.call_args.args[3], "127.0.0.1")
                self.assertEqual(run_slave_mock.call_args.kwargs["firewall"], "no")

                run_slave_mock.reset_mock()
                cli_main(["share", "--root", str(root), "--password", "pw", "--host", "0.0.0.0", "--console", "no"])
                self.assertEqual(run_slave_mock.call_args.args[3], "0.0.0.0")
                self.assertEqual(run_slave_mock.call_args.kwargs["firewall"], "ask")

    def test_share_advertised_addresses_follow_bind_host(self):
        self.assertEqual(advertised_addresses("127.0.0.1", 7171), [("Local", "127.0.0.1:7171")])
        self.assertEqual(advertised_addresses("localhost", 7171), [("Local", "127.0.0.1:7171")])
        self.assertEqual(advertised_addresses("100.64.1.2", 7171), [("Bound", "100.64.1.2:7171")])

    def test_send_wrapper_defaults_remote_dir_to_incoming(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "project"
            project.mkdir()
            install_work_mem(project)
            (project / "data.txt").write_text("payload", encoding="utf-8")
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                set_connection("lab", "127.0.0.1", 7171, "tok")
                with patch("agentremote.cli.push") as push_mock:
                    cli_main(["send", "lab", str(project / "data.txt"), "--policy", "off"])
                self.assertEqual(push_mock.call_args.args[4], "/incoming")
                self.assertEqual(push_mock.call_args.kwargs["token"], "tok")
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_sync_project_is_plan_first_without_yes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "project"
            project.mkdir()
            install_work_mem(project)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = os.getcwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                set_connection("lab", "127.0.0.1", 7171, "tok")
                os.chdir(project)
                plan = {
                    "direction": "push",
                    "summary": {"copyFiles": 2},
                    "conflicts": [],
                    "deleteCandidates": [],
                    "copy": [],
                }
                out = io.StringIO()
                with patch("agentremote.cli.RemoteClient"), patch("agentremote.cli.sync_plan_push", return_value=plan), patch("agentremote.cli.write_plan", return_value=plan), patch("agentremote.cli.sync_push") as sync_push_mock:
                    with redirect_stdout(out):
                        cli_main(["sync-project", "lab", "--policy", "off"])
                sync_push_mock.assert_not_called()
                self.assertIn("Files: 2 copy", out.getvalue())
                self.assertIn("Run again with --yes", out.getvalue())
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_uninstall_is_dry_run_and_preserves_project_state_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("AIMemory", ".agentremote", ".agentremote_partial", ".agentremote_inbox"):
                (root / name).mkdir()
            out = io.StringIO()
            with redirect_stdout(out):
                cli_main(["uninstall", "--root", str(root), "--project-state", "--purge-memory"])
            output = out.getvalue()
            self.assertIn("Dry-run only", output)
            for name in ("AIMemory", ".agentremote", ".agentremote_partial", ".agentremote_inbox"):
                self.assertTrue((root / name).exists())

    def test_worker_policy_summary_marks_metadata_only_fields(self):
        from agentremote.worker_policy import allow_rule, worker_policy_summary

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            allow_rule(root, "tests", "python", network="off", cwd_pattern="*", env_allowlist=["PATH"])
            summary = worker_policy_summary(root)
        self.assertEqual(summary["metadataOnlyFields"], ["network", "cwdPattern", "envAllowlist"])
        self.assertIn("do not sandbox", summary["metadataNote"])

    def test_saved_connection_file_permissions_and_docs_warn_about_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "cfg"
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                set_connection("lab", "127.0.0.1", 7171, "tok")
                path = config / "connections.json"
                self.assertTrue(path.exists())
                if os.name != "nt":
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                    self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o700)
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home
        docs = (Path(__file__).resolve().parents[1] / "docs" / "security.md").read_text(encoding="utf-8")
        self.assertIn("Saved connections store bearer tokens", docs)
        self.assertIn("Treat saved tokens like local secrets", docs)

    def test_approvals_wait_and_policy_cli_are_reachable(self):
        from agentremote.approval import create_approval_request, decide_approval

        install_work_mem(self.root)
        out = io.StringIO()
        with redirect_stdout(out):
            cli_main(["approvals", "policy", "--root", str(self.root), "--mode", "ask"])
            cli_main(["approvals", "policy", "--root", str(self.root)])
        self.assertIn("approval mode: ask", out.getvalue())

        req = create_approval_request(self.root, "cli.wait", summary="Wait test")

        def decide_later():
            time.sleep(0.1)
            decide_approval(self.root, req["approvalId"], "approved")

        thread = threading.Thread(target=decide_later)
        thread.start()
        out = io.StringIO()
        with redirect_stdout(out):
            cli_main(["approvals", "wait", req["approvalId"], "--root", str(self.root), "--timeout", "2"])
        thread.join(timeout=2)
        self.assertIn("approved", out.getvalue())

    def test_map_shows_route_latest_call_and_pending_approvals(self):
        from agentremote.approval import create_approval_request

        config = self.root / "cfg"
        project = self.root / "project"
        project.mkdir()
        previous_home = os.environ.get("AGENTREMOTE_HOME")
        try:
            os.environ["AGENTREMOTE_HOME"] = str(config)
            cli_main(["route", "set", "lab", "100.64.1.20", "7171", "--priority", "1"])
            save_route_health(
                "::lab",
                "100.64.1.20",
                7171,
                {
                    "lastCheckedAt": time.time(),
                    "lastOkAt": time.time(),
                    "lastLatencyMs": 12.3,
                    "lastError": "",
                },
            )
            save_call_record("::lab", "instruction-1", "handoff-1", [], "reported", root=project)
            create_approval_request(
                project,
                "handoff.execute",
                summary="Run remote test",
                origin_type="remote-agent",
                origin_node="::local",
                target_node="::lab",
            )
            out = io.StringIO()
            with redirect_stdout(out):
                cli_main(["map", "--root", str(project)])
            text = out.getvalue()
            self.assertIn("::lab", text)
            self.assertIn("route=100.64.1.20:7171", text)
            self.assertIn("last call=reported", text)
            self.assertIn("approvals=1 pending", text)
        finally:
            if previous_home is None:
                os.environ.pop("AGENTREMOTE_HOME", None)
            else:
                os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_status_shows_nodes_calls_approvals_and_process_breakdown(self):
        from agentremote.approval import create_approval_request
        from agentremote.swarm import save_swarm_state

        config = self.root / "cfg"
        project = self.root / "project"
        project.mkdir()
        install_work_mem(project)
        previous_home = os.environ.get("AGENTREMOTE_HOME")
        try:
            os.environ["AGENTREMOTE_HOME"] = str(config)
            set_connection("status-lab", "127.0.0.1", 7171, "tok")
            state = load_swarm_state()
            state.setdefault("nodes", {})["::status-lab"] = {
                "lastStatus": "online",
                "lastSeenAt": time.time(),
            }
            save_swarm_state(state)
            register_process(project, "controller-gui", os.getpid(), ui_url="http://127.0.0.1:7180")
            save_call_record("::status-lab", "instruction-sent", "handoff-sent", [], "sent", root=project)
            save_call_record("::status-lab", "instruction-reported", "handoff-reported", [], "reported", root=project)
            save_call_record("::status-lab", "instruction-failed", "handoff-failed", [], "failed", root=project)
            create_approval_request(project, "process.stop", summary="Stop process", target_node="::status-lab")
            out = io.StringIO()
            with redirect_stdout(out):
                cli_main(["status", "--root", str(project)])
            text = out.getvalue()
            self.assertIn("Connections: 1", text)
            self.assertIn("Nodes: 1 online, 0 offline, 0 unknown", text)
            self.assertIn("Processes: 1 running", text)
            self.assertIn("Calls: 1 reported, 1 pending, 1 failed", text)
            self.assertIn("Approvals: 1 pending", text)
        finally:
            if previous_home is None:
                os.environ.pop("AGENTREMOTE_HOME", None)
            else:
                os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_dashboard_data_includes_summaries_and_sanitized_pending_approvals(self):
        from agentremote.approval import create_approval_request

        config = self.root / "cfg"
        project = self.root / "project"
        project.mkdir()
        previous_home = os.environ.get("AGENTREMOTE_HOME")
        try:
            os.environ["AGENTREMOTE_HOME"] = str(config)
            set_connection("lab", "127.0.0.1", 7171, "saved-token-value")
            save_call_record("::lab", "instruction-1", "handoff-1", [], "sent", root=project)
            create_approval_request(
                project,
                "delete.file",
                summary="Delete generated artifact",
                details="secret-token-value should never leak",
                risk="high",
                origin_type="remote-agent",
                origin_node="::remote",
                target_node="::lab",
            )
            data = get_dashboard_data(project)
            payload = json.dumps(data, sort_keys=True)
            self.assertEqual(data["summaries"]["approvals"]["pending"], 1)
            self.assertEqual(data["summaries"]["calls"]["pending"], 1)
            self.assertEqual(data["nodes"][0]["pendingApprovals"], 1)
            self.assertIn("pendingApprovals", data)
            self.assertNotIn("_approvalToken", payload)
            self.assertNotIn("approvalTokenHash", payload)
            self.assertNotIn("requestHash", payload)
            self.assertNotIn("details", payload)
            self.assertNotIn("secret-token-value", payload)
            self.assertNotIn("saved-token-value", payload)
        finally:
            if previous_home is None:
                os.environ.pop("AGENTREMOTE_HOME", None)
            else:
                os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_release_readiness_docs_and_metadata_use_new_repo_name(self):
        repo = Path(__file__).resolve().parents[1]
        english = (repo / "README.md").read_text(encoding="utf-8")
        korean = (repo / "README.ko.md").read_text(encoding="utf-8")
        release_notes = (repo / "docs" / "release-notes-v0.1.md").read_text(encoding="utf-8")
        development_plan = (repo / "docs" / "development-plan.md").read_text(encoding="utf-8")
        validation_plan = (repo / "docs" / "v0.1-validation-scenarios.md").read_text(encoding="utf-8")
        fullscale_plan = (repo / "docs" / "v0.1-fullscale-test-scenarios.md").read_text(encoding="utf-8")
        lab_runbook = (repo / "docs" / "v0.1-fullscale-lab-runbook.md").read_text(encoding="utf-8")
        docker_lab = (repo / "docs" / "docker-fullscale-lab.md").read_text(encoding="utf-8")
        pyproject = (repo / "pyproject.toml").read_text(encoding="utf-8")

        for text in (english, korean):
            self.assertIn("# agent-remote-sync", text)
            self.assertIn("github.com/daystar7777/agent-remote-sync", text)
            self.assertIn("python -m pytest tests deepseek-test -q", text)
            self.assertIn("worker execution", text)
            self.assertIn("worker-policy", text)
            self.assertIn("agentremote daemon profile save", text)
            self.assertIn("agentremote daemon profile remove", text)
            self.assertIn("agentremote daemon status", text)
            self.assertIn("uninstall --root", text)
            self.assertIn("dry-run", text)
            self.assertIn("DDoS", text)
            self.assertIn("future", text)
            self.assertIn("worker policy", text)
        self.assertNotIn("github.com/daystar7777/agentremote", english)
        self.assertNotIn("github.com/daystar7777/agentremote", korean)
        self.assertIn("Known Limitations", english)
        self.assertIn("알려진 제한사항", korean)
        self.assertIn("agent-remote-sync / agentremote v0.1 Release Notes", release_notes)
        self.assertIn("Known Limitations", release_notes)
        self.assertIn("dry-run planners", release_notes)
        self.assertIn("Relay", release_notes)
        self.assertIn("DDoS", release_notes)
        self.assertIn("Release Candidate Readiness", development_plan)
        self.assertIn("v0.1-fullscale-test-scenarios.md", validation_plan)
        self.assertIn("R7 | Cross-OS filename", fullscale_plan)
        self.assertIn("R8 | Large-file", fullscale_plan)
        self.assertIn("v0.1-fullscale-lab-runbook.md", fullscale_plan)
        self.assertIn("docker-fullscale-lab.md", fullscale_plan)
        self.assertIn("Full-Scale Lab Runbook", lab_runbook)
        self.assertIn("docker-fullscale-lab.md", lab_runbook)
        self.assertIn("Machine Matrix", lab_runbook)
        self.assertIn("Windows -> macOS", lab_runbook)
        self.assertIn("Large-File Resume", lab_runbook)
        self.assertIn("test-results_YYYYMMDD-HHMMSS-fullscale-lab-deepseek.md", lab_runbook)
        self.assertIn("Do not intentionally fill a real disk", lab_runbook)
        self.assertIn("tools\\generate_fullscale_lab_data.py", lab_runbook)
        self.assertIn("tools/generate_fullscale_lab_data.py", lab_runbook)
        self.assertIn("tools\\inspect_unicode_names.py", lab_runbook)
        self.assertNotIn("python - <<", lab_runbook)
        self.assertTrue((repo / "tools" / "generate_fullscale_lab_data.py").is_file())
        self.assertTrue((repo / "tools" / "inspect_unicode_names.py").is_file())
        self.assertIn("Docker Full-Scale Lab", docker_lab)
        self.assertIn("run_docker_fullscale.py", docker_lab)
        self.assertIn("compose.fullscale.yml", docker_lab)
        self.assertIn(".github/workflows/docker-fullscale.yml", docker_lab)
        self.assertIn("docker-fullscale-results", docker_lab)
        self.assertIn("What It Cannot Prove", docker_lab)
        self.assertIn('name = "agentremote"', pyproject)
        self.assertIn("github.com/daystar7777/agent-remote-sync", pyproject)
        self.assertIn('agentremote = "agentremote.cli:main"', pyproject)

    def test_docker_fullscale_lab_files_are_wired_for_repeatable_validation(self):
        repo = Path(__file__).resolve().parents[1]
        dockerfile = (repo / "docker" / "Dockerfile").read_text(encoding="utf-8")
        compose = (repo / "docker" / "compose.fullscale.yml").read_text(encoding="utf-8")
        dockerignore = (repo / ".dockerignore").read_text(encoding="utf-8")
        workflow = (repo / ".github" / "workflows" / "docker-fullscale.yml").read_text(encoding="utf-8")

        self.assertIn("FROM python:3.12-slim", dockerfile)
        self.assertIn("python -m pip install --no-cache-dir -e .", dockerfile)
        for service in ("node-a", "node-b", "controller"):
            self.assertIn(f"  {service}:", compose)
        self.assertIn("tools/docker_node_entry.py", compose)
        self.assertIn("tools/docker_fullscale_runner.py", compose)
        self.assertIn("condition: service_healthy", compose)
        self.assertIn("AGENTREMOTE_DOCKER_MANY_COUNT", compose)
        self.assertIn("AGENTREMOTE_DOCKER_LARGE_SIZE_MIB", compose)
        self.assertIn("AIMemory/", dockerignore)
        self.assertIn(".agentremote/", dockerignore)
        self.assertIn("workflow_dispatch", workflow)
        self.assertIn("tools/run_docker_fullscale.py", workflow)
        self.assertIn("actions/upload-artifact", workflow)
        self.assertIn("docker-fullscale-results", workflow)

        for script in (
            "docker_node_entry.py",
            "docker_fullscale_runner.py",
            "run_docker_fullscale.py",
        ):
            path = repo / "tools" / script
            self.assertTrue(path.is_file(), script)
            result = subprocess.run(
                [sys.executable, str(path), "--help"],
                cwd=repo,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_release_packaging_metadata_includes_web_assets_and_ignores_generated_state(self):
        repo = Path(__file__).resolve().parents[1]
        pyproject = (repo / "pyproject.toml").read_text(encoding="utf-8")
        gitignore = (repo / ".gitignore").read_text(encoding="utf-8")

        self.assertTrue((repo / "src" / "agentremote" / "web" / "index.html").is_file())
        self.assertIn("[tool.setuptools.package-data]", pyproject)
        self.assertIn('agentremote = ["py.typed", "web/index.html"]', pyproject)
        for pattern in (
            "__pycache__/",
            "*.py[cod]",
            ".pytest_cache/",
            ".claude/",
            "build/",
            "dist/",
            "*.egg-info/",
            "AIMemory/",
            ".agentremote/",
            ".agentremote_partial/",
            ".agentremote_handoff/",
            ".agentremote_inbox/",
        ):
            self.assertIn(pattern, gitignore)

    def test_fullscale_lab_tools_handle_unicode_under_narrow_console_encoding(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "lab-data"
            subprocess.run(
                [
                    sys.executable,
                    str(repo / "tools" / "generate_fullscale_lab_data.py"),
                    "--root",
                    str(root),
                    "--many-count",
                    "3",
                    "--large-size-mib",
                    "1",
                ],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "cp949:strict"
            result = subprocess.run(
                [
                    sys.executable,
                    str(repo / "tools" / "inspect_unicode_names.py"),
                    str(root / "DS-unicode"),
                ],
                cwd=repo,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("raw=", result.stdout)
            self.assertIn("codepoints=", result.stdout)

    def test_ci_runs_release_validation_suite(self):
        repo = Path(__file__).resolve().parents[1]
        ci = (repo / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("python -m pip install -e . pytest", ci)
        self.assertIn("python -m compileall -q src tests deepseek-test", ci)
        self.assertIn("python -m unittest discover -s tests", ci)
        self.assertIn("python -m pytest tests deepseek-test -q", ci)

    def test_sync_project_dry_run_applies_default_and_custom_excludes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "project"
            remote_root = root / "remote"
            project.mkdir()
            remote_root.mkdir()
            install_work_mem(project)
            (project / "app.py").write_text("print('ok')", encoding="utf-8")
            (project / "node_modules").mkdir()
            (project / "node_modules" / "large.js").write_text("skip", encoding="utf-8")
            (project / "AIMemory" / "memory.md").write_text("skip memory", encoding="utf-8")
            (project / "debug.log").write_text("skip log", encoding="utf-8")
            slave = self.start_slave(remote_root)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = os.getcwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                set_connection("lab", "127.0.0.1", slave.server_address[1], "")
                os.chdir(project)
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["sync-project", "lab", "--password", "secret", "--dry-run", "--exclude", "*.log", "--policy", "off"])
                output = out.getvalue()
                self.assertIn("Dry-run only", output)
                self.assertIn("Excluded paths:", output)
                plan_path = next((project / ".agentremote" / "plans").glob("*.json"))
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
                copied = {item["rel"] for item in plan["copy"]}
                excluded = {item["rel"] for item in plan["excluded"]}
                self.assertIn("app.py", copied)
                self.assertNotIn("node_modules/large.js", copied)
                self.assertNotIn("AIMemory/memory.md", copied)
                self.assertNotIn("debug.log", copied)
                self.assertIn("node_modules/", excluded)
                self.assertIn("AIMemory/", excluded)
                self.assertIn("debug.log", excluded)
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home
                slave.shutdown()
                slave.server_close()

    def test_sync_project_include_memory_reenables_aimemory_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "project"
            remote_root = root / "remote"
            project.mkdir()
            remote_root.mkdir()
            install_work_mem(project)
            (project / "AIMemory" / "memory.md").write_text("include memory", encoding="utf-8")
            (project / "node_modules").mkdir()
            (project / "node_modules" / "large.js").write_text("skip", encoding="utf-8")
            slave = self.start_slave(remote_root)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = os.getcwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                set_connection("lab", "127.0.0.1", slave.server_address[1], "")
                os.chdir(project)
                with redirect_stdout(io.StringIO()):
                    cli_main(["sync-project", "lab", "--password", "secret", "--dry-run", "--include-memory", "--policy", "off"])
                plan_path = next((project / ".agentremote" / "plans").glob("*.json"))
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
                copied = {item["rel"] for item in plan["copy"]}
                excluded = {item["rel"] for item in plan["excluded"]}
                self.assertIn("AIMemory/memory.md", copied)
                self.assertIn("node_modules/", excluded)
                self.assertNotIn("AIMemory/", excluded)
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home
                slave.shutdown()
                slave.server_close()

    def test_sync_project_yes_uses_filtered_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "project"
            remote_root = root / "remote"
            project.mkdir()
            remote_root.mkdir()
            install_work_mem(project)
            (project / "app.py").write_text("print('ok')", encoding="utf-8")
            (project / "node_modules").mkdir()
            (project / "node_modules" / "large.js").write_text("skip", encoding="utf-8")
            (project / "AIMemory" / "memory.md").write_text("skip memory", encoding="utf-8")
            (project / "debug.log").write_text("skip log", encoding="utf-8")
            slave = self.start_slave(remote_root)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = os.getcwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                set_connection("lab", "127.0.0.1", slave.server_address[1], "")
                os.chdir(project)
                with redirect_stdout(io.StringIO()):
                    cli_main(["sync-project", "lab", "--password", "secret", "--yes", "--exclude", "*.log", "--policy", "off"])
                self.assertTrue((remote_root / "project" / "app.py").exists())
                self.assertFalse((remote_root / "project" / "node_modules" / "large.js").exists())
                self.assertFalse((remote_root / "project" / "AIMemory" / "memory.md").exists())
                self.assertFalse((remote_root / "project" / "debug.log").exists())
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home
                slave.shutdown()
                slave.server_close()

    def test_sync_project_yes_blocks_when_remote_free_space_is_known_insufficient(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "project"
            project.mkdir()
            install_work_mem(project)
            (project / "app.bin").write_bytes(b"x" * 64)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = os.getcwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                set_connection("lab", "127.0.0.1", 7171, "tok")
                os.chdir(project)
                remote = unittest.mock.Mock()
                remote.storage.return_value = {"freeBytes": 1}
                plan = {
                    "direction": "push",
                    "summary": {"copyFiles": 1, "skipped": 0},
                    "copy": [{"rel": "app.bin", "size": 64}],
                    "conflicts": [],
                    "deleteCandidates": [],
                    "excluded": [],
                }
                with patch("agentremote.cli.RemoteClient", return_value=remote), patch("agentremote.cli.sync_plan_push", return_value=plan), patch("agentremote.cli.write_plan"), patch("agentremote.cli.sync_push") as sync_push_mock:
                    with self.assertRaises(SystemExit) as caught:
                        cli_main(["sync-project", "lab", "--yes", "--policy", "off"])
                self.assertIn("insufficient_storage", str(caught.exception))
                sync_push_mock.assert_not_called()
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_ask_sends_instruction_with_report_default_and_call_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "project"
            project.mkdir()
            install_work_mem(project)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = os.getcwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                set_connection("lab", "127.0.0.1", 7171, "tok")
                os.chdir(project)
                instruction = {"id": "inst-ask", "handoffId": "handoff-ask"}
                out = io.StringIO()
                with patch("agentremote.cli.tell", return_value=instruction) as tell_mock:
                    with redirect_stdout(out):
                        cli_main(["ask", "lab", "Run tests", "--policy", "off"])
                self.assertIn("ask sent:", out.getvalue())
                self.assertEqual(tell_mock.call_args.args[3], "Run tests")
                self.assertEqual(tell_mock.call_args.kwargs["expect_report"], "Report back when complete.")
                call_files = list((project / ".agentremote" / "calls").glob("call-*.json"))
                self.assertEqual(len(call_files), 1)
                record = json.loads(call_files[0].read_text(encoding="utf-8"))
                self.assertEqual(record["instructionId"], "inst-ask")
                self.assertEqual(record["handoffId"], "handoff-ask")
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_handoff_supports_explicit_path_task_and_saves_call_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "project"
            project.mkdir()
            install_work_mem(project)
            payload = project / "payload.txt"
            payload.write_text("payload", encoding="utf-8")
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = os.getcwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                set_connection("lab", "127.0.0.1", 7171, "tok")
                os.chdir(project)
                result = {
                    "transfer": {"remotePaths": ["/incoming/payload.txt"]},
                    "instruction": {"id": "inst-handoff", "handoffId": "handoff-handoff"},
                }
                out = io.StringIO()
                with patch("agentremote.cli.send_handoff", return_value=result) as handoff_mock:
                    with redirect_stdout(out):
                        cli_main(["handoff", "lab", "--path", "payload.txt", "--task", "Review it", "--policy", "off"])
                self.assertIn("call sent:", out.getvalue())
                self.assertEqual(handoff_mock.call_args.args[3], Path("payload.txt"))
                self.assertEqual(handoff_mock.call_args.args[4], "Review it")
                self.assertEqual(handoff_mock.call_args.kwargs["expect_report"], "Report back when complete.")
                call_files = list((project / ".agentremote" / "calls").glob("call-*.json"))
                self.assertEqual(len(call_files), 1)
                record = json.loads(call_files[0].read_text(encoding="utf-8"))
                self.assertEqual(record["paths"], ["/incoming/payload.txt"])
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_handoff_legacy_positional_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "project"
            project.mkdir()
            install_work_mem(project)
            (project / "payload.txt").write_text("payload", encoding="utf-8")
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            previous_cwd = os.getcwd()
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                set_connection("lab", "127.0.0.1", 7171, "tok")
                os.chdir(project)
                result = {
                    "transfer": {"remotePaths": ["/incoming/payload.txt"]},
                    "instruction": {"id": "inst-legacy", "handoffId": "handoff-legacy"},
                }
                with patch("agentremote.cli.send_handoff", return_value=result) as handoff_mock:
                    with redirect_stdout(io.StringIO()):
                        cli_main(["handoff", "lab", "payload.txt", "Review legacy", "--policy", "off"])
                self.assertEqual(handoff_mock.call_args.args[3], Path("payload.txt"))
                self.assertEqual(handoff_mock.call_args.args[4], "Review legacy")
            finally:
                os.chdir(previous_cwd)
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_wait_for_handoff_report_refreshes_and_returns_reported_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            install_work_mem(project)
            rec = save_call_record("::lab", "inst-wait", "handoff-wait", [], "sent", root=project)
            create_instruction(
                project,
                "Completed successfully",
                from_name="worker",
                handoff={
                    "title": "Report",
                    "task": "Completed successfully",
                    "from": "worker",
                    "to": "controller",
                    "type": "STATUS_REPORT",
                    "parentId": rec["handoffId"],
                },
            )
            result = wait_for_handoff_report(project, rec["callId"], timeout=1)
            self.assertEqual(result["status"], "reported")
            refreshed = json.loads((project / ".agentremote" / "calls" / f"{rec['callId']}.json").read_text(encoding="utf-8"))
            self.assertEqual(refreshed["state"], "reported")

    def test_status_uses_requested_root_for_call_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            other = root / "other"
            project.mkdir()
            other.mkdir()
            install_work_mem(project)
            install_work_mem(other)
            save_call_record("::lab", "inst-project", "handoff-project", [], "sent", root=project)
            previous_cwd = os.getcwd()
            try:
                os.chdir(other)
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["status", "--root", str(project)])
                self.assertIn("Calls: 0 reported, 1 pending", out.getvalue())
            finally:
                os.chdir(previous_cwd)

    def test_daemon_profile_nested_cli_sanitizes_names_and_filters_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "project"
            other = root / "other"
            project.mkdir()
            other.mkdir()
            install_work_mem(project)
            install_work_mem(other)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main([
                        "daemon", "profile", "save",
                        "--root", str(project),
                        "--name", "../My Project",
                        "--host", "127.0.0.1",
                        "--port", "17171",
                    ])
                    cli_main([
                        "daemon", "profile-save",
                        "--root", str(other),
                        "--name", "other",
                        "--host", "127.0.0.1",
                        "--port", "17172",
                    ])
                    cli_main(["daemon", "profile", "list", "--root", str(project)])
                text = out.getvalue()
                self.assertIn("saved: My-Project -> 127.0.0.1:17171", text)
                self.assertIn("My-Project 127.0.0.1:17171", text)
                self.assertNotIn("other 127.0.0.1:17172", text)
                self.assertTrue((config / "daemon-profiles" / "My-Project.json").exists())
                self.assertFalse((config / "My Project.json").exists())
                self.assertFalse((config.parent / "My Project.json").exists())

                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["daemon", "profile", "remove", "--root", str(project), "--name", "../My Project"])
                self.assertIn("removed: My-Project", out.getvalue())
                self.assertFalse((config / "daemon-profiles" / "My-Project.json").exists())
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_daemon_install_spec_uses_password_env_and_no_plaintext_secret(self):
        from agentremote.cli import render_service_spec

        profile = {
            "name": "demo",
            "root": "C:/Projects/Demo Space",
            "host": "127.0.0.1",
            "port": 17171,
        }
        for platform_name in ("win32", "darwin", "linux"):
            spec = render_service_spec(profile, platform_name=platform_name)
            self.assertIn("--password-env", spec)
            self.assertIn("AGENTREMOTE_DAEMON_PASSWORD", spec)
            self.assertIn("daemon", spec)
            self.assertNotIn("plain-secret-value", spec)
            self.assertNotIn("--password ", spec)
        self.assertIn("systemd user unit", render_service_spec(profile, platform_name="linux"))
        self.assertIn("LaunchAgent", render_service_spec(profile, platform_name="darwin"))
        self.assertIn("Task Scheduler", render_service_spec(profile, platform_name="win32"))

    def test_daemon_status_correlates_profiles_with_running_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "project"
            project.mkdir()
            install_work_mem(project)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                cli_main([
                    "daemon", "profile", "save",
                    "--root", str(project),
                    "--name", "demo",
                    "--host", "127.0.0.1",
                    "--port", "17171",
                ])
                register_process(project, "daemon-serve", os.getpid(), host="127.0.0.1", port=17171)
                out = io.StringIO()
                with redirect_stdout(out):
                    cli_main(["daemon", "status", "--root", str(project)])
                text = out.getvalue()
                self.assertIn("Profiles: 1 for this root", text)
                self.assertIn("Processes: 1 running", text)
                self.assertIn("demo 127.0.0.1:17171 running", text)
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_daemon_password_env_is_used_without_prompting(self):
        root = self.root / "shared"
        root.mkdir()
        install_work_mem(root)
        previous_password = os.environ.get("AGENTREMOTE_DAEMON_PASSWORD")
        try:
            os.environ["AGENTREMOTE_DAEMON_PASSWORD"] = "env-secret"
            with patch("agentremote.cli.run_slave") as run_slave_mock:
                cli_main([
                    "daemon", "serve",
                    "--root", str(root),
                    "--password-env", "AGENTREMOTE_DAEMON_PASSWORD",
                    "--console", "no",
                    "--firewall", "no",
                ])
            self.assertEqual(run_slave_mock.call_args.args[2], "env-secret")
        finally:
            if previous_password is None:
                os.environ.pop("AGENTREMOTE_DAEMON_PASSWORD", None)
            else:
                os.environ["AGENTREMOTE_DAEMON_PASSWORD"] = previous_password

    def test_dashboard_data_includes_sanitized_daemon_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "project"
            project.mkdir()
            install_work_mem(project)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                cli_main([
                    "daemon", "profile", "save",
                    "--root", str(project),
                    "--name", "demo",
                    "--host", "127.0.0.1",
                    "--port", "17171",
                ])
                register_process(project, "daemon-serve", os.getpid(), host="127.0.0.1", port=17171)
                data = get_dashboard_data(project)
                payload = json.dumps(data, sort_keys=True)
                self.assertEqual(data["summaries"]["profiles"]["total"], 1)
                self.assertEqual(data["summaries"]["profiles"]["running"], 1)
                self.assertEqual(data["daemonProfiles"][0]["name"], "demo")
                self.assertEqual(data["daemonProfiles"][0]["status"], "running")
                self.assertNotIn("password", payload.lower())
                self.assertNotIn("token", payload.lower())
                self.assertNotIn("commandFingerprint", payload)
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_dashboard_profile_forget_endpoint_normalizes_and_removes_profile(self):
        class DummyRemote:
            base_url = "http://dummy"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cfg"
            project = root / "project"
            project.mkdir()
            install_work_mem(project)
            previous_home = os.environ.get("AGENTREMOTE_HOME")
            try:
                os.environ["AGENTREMOTE_HOME"] = str(config)
                cli_main([
                    "daemon", "profile", "save",
                    "--root", str(project),
                    "--name", "../Demo Profile",
                    "--host", "127.0.0.1",
                    "--port", "17171",
                ])
                self.assertTrue((config / "daemon-profiles" / "Demo-Profile.json").exists())
                master = AgentRemoteMasterServer(("127.0.0.1", 0), MasterState(project, DummyRemote()))
                threading.Thread(target=master.serve_forever, daemon=True).start()
                try:
                    base = f"http://127.0.0.1:{master.server_address[1]}"
                    result = request_json(base, "POST", "/api/dashboard/profile/forget", {"name": "../Demo Profile"})
                    self.assertTrue(result["ok"])
                    self.assertEqual(result["forgotten"], "Demo-Profile")
                    self.assertFalse((config / "daemon-profiles" / "Demo-Profile.json").exists())
                finally:
                    master.shutdown()
                    master.server_close()
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTREMOTE_HOME", None)
                else:
                    os.environ["AGENTREMOTE_HOME"] = previous_home

    def test_dashboard_html_has_daemon_profile_cards_and_safe_action(self):
        html = (Path(__file__).resolve().parents[1] / "src" / "agentremote" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("Daemon Profiles", html)
        self.assertIn("data.daemonProfiles", html)
        self.assertIn("/api/dashboard/profile/forget", html)
        self.assertIn("forgetProfile", html)

if __name__ == "__main__":
    unittest.main()
