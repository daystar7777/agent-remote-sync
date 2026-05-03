#!/usr/bin/env python3
"""agent-remote-sync v0.1 local smoke test

Runs a local daemon + controller transfer + handoff + worker-policy execution smoke.
No external network required.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SRC_ROOT = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))
os.environ["PYTHONPATH"] = str(SRC_ROOT)

from agentremote.cli import main as cli_main
from agentremote.inbox import create_instruction
from agentremote.slave import AgentRemoteSlaveServer, SlaveState
from agentremote.workmem import install_work_mem
import threading


def main():
    print("agent-remote-sync v0.1 smoke test")
    print("=================================")
    passed = 0
    failed = 0

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = root / "cfg"
        project = root / "local"
        remote = root / "remote"
        project.mkdir()
        remote.mkdir()
        os.environ["AGENTREMOTE_HOME"] = str(config)

        # Setup
        install_work_mem(project)
        install_work_mem(remote)
        (project / "test.txt").write_text("smoke test data", encoding="utf-8")
        original_cwd = os.getcwd()

        try:
            # 1. Bootstrap
            out = io.StringIO()
            with redirect_stdout(out):
                cli_main(["bootstrap", "--root", str(project), "--install", "no", "--no-network-check"])
            assert "agent-remote-sync bootstrap" in out.getvalue() or "agent-work-mem" in out.getvalue()
            passed += 1
            print("  [PASS] bootstrap")

            # 2. Start daemon (slave)
            state = SlaveState(remote, "smoke")
            slave = AgentRemoteSlaveServer(("127.0.0.1", 0), state)
            t = threading.Thread(target=slave.serve_forever, daemon=True)
            t.start()
            time.sleep(0.3)
            port = slave.server_address[1]
            passed += 1
            print(f"  [PASS] daemon serve on port {port}")

            # 3. Connect
            os.chdir(str(project))
            out = io.StringIO()
            with redirect_stdout(out):
                cli_main(["connect", "smoke-node", "127.0.0.1", str(port), "--password", "smoke"])
            assert "connected" in out.getvalue()
            passed += 1
            print("  [PASS] connect")

            # 4. Push
            out = io.StringIO()
            with redirect_stdout(out):
                cli_main(["push", "smoke-node", "test.txt", "/incoming", "--overwrite"])
            assert (remote / "incoming" / "test.txt").exists()
            passed += 1
            print("  [PASS] push")

            # 5. Handoff
            out = io.StringIO()
            with redirect_stdout(out):
                cli_main(["handoff", "smoke-node", "test.txt", "Review smoke test", "--remote-dir", "/incoming-hf"])
            assert "handoff complete" in out.getvalue()
            passed += 1
            print("  [PASS] handoff")

            # 6. Worker policy
            out = io.StringIO()
            with redirect_stdout(out):
                cli_main(["worker-policy", "init", "--root", str(project)])
                cli_main(["worker-policy", "allow", "echo-safe", "echo", "--root", str(project)])
                cli_main(["worker-policy", "allow", "python-smoke", "python", "--args-pattern", "*smoke-worker.txt*", "--root", str(project)])
                cli_main(["worker-policy", "list", "--root", str(project)])
            assert "echo-safe" in out.getvalue()
            assert "python-smoke" in out.getvalue()
            passed += 1
            print("  [PASS] worker-policy")

            # 7. Worker execution
            create_instruction(
                project,
                "Run smoke worker.\nagentremote-run: python -c \"from pathlib import Path; Path('smoke-worker.txt').write_text('ok', encoding='utf-8')\"",
                auto_run=True,
            )
            out = io.StringIO()
            with redirect_stdout(out):
                cli_main(["worker", "--root", str(project), "--once", "--execute", "yes"])
            assert (project / "smoke-worker.txt").read_text(encoding="utf-8") == "ok"
            passed += 1
            print("  [PASS] worker execution")

            # 8. Approval policy
            out = io.StringIO()
            with redirect_stdout(out):
                cli_main(["approvals", "policy", "--root", str(project), "--mode", "auto"])
            passed += 1
            print("  [PASS] approval policy")

            slave.shutdown()
            slave.server_close()

        finally:
            os.chdir(original_cwd)

    print(f"\nResult: {passed} passed, {failed} failed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
