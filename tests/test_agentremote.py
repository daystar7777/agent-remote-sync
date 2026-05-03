from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from agentremote.common import TransferJob
from agentremote.connections import normalize_alias
from agentremote.headless import pull, push
from agentremote.headless import tell as headless_tell
from agentremote.inbox import list_instructions
from agentremote.master import AgentRemoteMasterServer, MasterState, RemoteClient
from agentremote.slave import AgentRemoteSlaveServer, SlaveState
from agentremote.workmem import install_work_mem


class AgentRemoteTests(unittest.TestCase):
    def start_slave(self, root: Path, password: str = "secret") -> AgentRemoteSlaveServer:
        state = SlaveState(root, password)
        server = AgentRemoteSlaveServer(("127.0.0.1", 0), state)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def test_slave_resumable_upload_and_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            remote_root = Path(tmp) / "remote"
            remote_root.mkdir()
            slave = self.start_slave(remote_root)
            try:
                client = RemoteClient("127.0.0.1", slave.server_address[1], "secret")
                client.mkdir("/incoming")
                payload = (b"agentremote-resume-test-" * 1024) + b"end"
                digest = hashlib.sha256(payload).hexdigest()
                client.upload_chunk(
                    "/incoming/data.bin", 0, len(payload), payload[:1234], overwrite=False
                )
                status = client.upload_status("/incoming/data.bin", len(payload))
                self.assertEqual(status["partialSize"], 1234)
                client.upload_chunk(
                    "/incoming/data.bin",
                    1234,
                    len(payload),
                    payload[1234:],
                    overwrite=False,
                )
                client.upload_finish(
                    "/incoming/data.bin", len(payload), time.time(), digest, overwrite=False
                )
                self.assertEqual((remote_root / "incoming" / "data.bin").read_bytes(), payload)
                self.assertEqual(client.download_chunk("/incoming/data.bin", 10, 20), payload[10:30])
            finally:
                slave.shutdown()
                slave.server_close()

    def test_master_jobs_upload_and_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote_root = root / "remote"
            local_root = root / "local"
            remote_root.mkdir()
            local_root.mkdir()
            (local_root / "hello.txt").write_text("hello from master", encoding="utf-8")
            slave = self.start_slave(remote_root)
            master = None
            try:
                client = RemoteClient("127.0.0.1", slave.server_address[1], "secret")
                master = AgentRemoteMasterServer(
                    ("127.0.0.1", 0), MasterState(local_root, client)
                )
                threading.Thread(target=master.serve_forever, daemon=True).start()
                base = f"http://127.0.0.1:{master.server_address[1]}"

                conflicts = request_json(
                    base,
                    "POST",
                    "/api/conflicts/upload",
                    {"paths": ["/hello.txt"], "remoteDir": "/"},
                )
                self.assertEqual(conflicts["conflicts"], [])
                job = request_json(
                    base,
                    "POST",
                    "/api/jobs/upload",
                    {"paths": ["/hello.txt"], "remoteDir": "/", "overwrite": False},
                )
                self.assertEqual(wait_job(base, job["id"])["state"], "done")
                self.assertEqual(
                    (remote_root / "hello.txt").read_text(encoding="utf-8"),
                    "hello from master",
                )

                job = request_json(
                    base,
                    "POST",
                    "/api/jobs/download",
                    {"paths": ["/hello.txt"], "localDir": "/downloads", "overwrite": False},
                )
                self.assertEqual(wait_job(base, job["id"])["state"], "done")
                self.assertEqual(
                    (local_root / "downloads" / "hello.txt").read_text(encoding="utf-8"),
                    "hello from master",
                )
            finally:
                if master:
                    master.shutdown()
                    master.server_close()
                slave.shutdown()
                slave.server_close()

    def test_headless_push_pull(self) -> None:
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote_root = root / "remote"
            local_a = root / "local-a"
            local_b = root / "local-b"
            remote_root.mkdir()
            local_a.mkdir()
            local_b.mkdir()
            (local_a / "send.txt").write_text("headless transfer", encoding="utf-8")
            slave = self.start_slave(remote_root)
            try:
                os.chdir(local_a)
                push("127.0.0.1", slave.server_address[1], "secret", Path("send.txt"), "/")
                self.assertEqual(
                    (remote_root / "send.txt").read_text(encoding="utf-8"),
                    "headless transfer",
                )
                os.chdir(original)
                pull("127.0.0.1", slave.server_address[1], "secret", "/send.txt", local_b)
                self.assertEqual(
                    (local_b / "send.txt").read_text(encoding="utf-8"),
                    "headless transfer",
                )
            finally:
                os.chdir(original)
                slave.shutdown()
                slave.server_close()

    def test_token_reuse_and_instruction_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            remote_root = Path(tmp) / "remote"
            remote_root.mkdir()
            install_work_mem(remote_root)
            (remote_root / "project").mkdir()
            slave = self.start_slave(remote_root)
            try:
                first = RemoteClient("127.0.0.1", slave.server_address[1], "secret")
                second = RemoteClient("127.0.0.1", slave.server_address[1], token=first.token)
                response = second.send_instruction(
                    "Run tests and report the result.",
                    from_name="test-agent",
                    paths=["/project"],
                    expect_report="Tell me pass/fail.",
                )
                self.assertTrue(response["ok"])
                instructions = list_instructions(remote_root)
                self.assertEqual(len(instructions), 1)
                self.assertEqual(instructions[0]["task"], "Run tests and report the result.")
                self.assertEqual(instructions[0]["paths"], ["/project"])
            finally:
                slave.shutdown()
                slave.server_close()

    def test_headless_tell_records_local_and_remote_handoffs(self) -> None:
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_root = root / "local"
            remote_root = root / "remote"
            local_root.mkdir()
            remote_root.mkdir()
            install_work_mem(local_root)
            install_work_mem(remote_root)
            slave = self.start_slave(remote_root)
            try:
                os.chdir(local_root)
                headless_tell(
                    "127.0.0.1",
                    slave.server_address[1],
                    "secret",
                    "Run tests and report back.",
                    local_root=local_root,
                    from_name="master-agent",
                    paths=["/project"],
                    expect_report="Pass/fail summary.",
                )
                local_handoffs = list((local_root / "AIMemory").glob("handoff_*.md"))
                remote_handoffs = list((remote_root / "AIMemory").glob("handoff_*.md"))
                self.assertEqual(len(local_handoffs), 1)
                self.assertEqual(len(remote_handoffs), 1)
                self.assertIn("direction: `local`", local_handoffs[0].read_text(encoding="utf-8"))
                self.assertIn("direction: `external`", remote_handoffs[0].read_text(encoding="utf-8"))
                self.assertIn("HANDOFF_RECEIVED", (remote_root / "AIMemory" / "work.log").read_text(encoding="utf-8"))
            finally:
                os.chdir(original)
                slave.shutdown()
                slave.server_close()

    def test_alias_normalization(self) -> None:
        self.assertEqual(normalize_alias("lab"), "::lab")
        self.assertEqual(normalize_alias("::lab"), "::lab")

    def test_transfer_job_reports_speed_and_eta(self) -> None:
        job = TransferJob(id="job-1", kind="upload")
        job.started_at = time.time() - 4
        job.total_bytes = 1000
        job.done_bytes = 250

        data = job.as_dict()

        self.assertGreater(data["speedBytesPerSecond"], 0)
        self.assertGreater(data["etaSeconds"], 0)

        job.done_bytes = 1000
        data = job.as_dict()

        self.assertGreater(data["speedBytesPerSecond"], 0)
        self.assertIsNone(data["etaSeconds"])


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


def wait_job(base: str, job_id: str) -> dict:
    for _ in range(100):
        job = request_json(base, "GET", f"/api/jobs/{job_id}")
        if job["state"] in ("done", "error"):
            return job
        time.sleep(0.1)
    raise AssertionError("job timed out")


if __name__ == "__main__":
    unittest.main()
