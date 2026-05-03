from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentremote.common import derive_key, make_proof, unb64
from agentremote.tls import fetch_remote_fingerprint, format_fingerprint
from agentremote.workmem import install_work_mem


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUILD = ROOT / "build" / "agentremote-rfs"
DEFAULT_RESULTS = ROOT / "AIMemory"
PASSWORD = "rfs-test-password"


def agentremote_cmd(*args: str) -> list[str]:
    return [sys.executable, "-m", "agentremote", *args]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class Check:
    name: str
    status: str
    expected: str = ""
    actual: str = ""
    command: str = ""
    evidence: list[str] = field(default_factory=list)
    severity: str = "none"


class LocalRfsHarness:
    def __init__(
        self,
        *,
        build_root: Path,
        results_root: Path,
        port: int,
        ui_port: int,
        tls_port: int,
        strict_port: int,
    ) -> None:
        self.build_root = build_root.resolve()
        self.results_root = results_root.resolve()
        self.port = port
        self.ui_port = ui_port
        self.tls_port = tls_port
        self.strict_port = strict_port
        self.config = self.build_root / "config"
        self.remote = self.build_root / "remote"
        self.local = self.build_root / "local"
        self.tls_remote = self.build_root / "tls-remote"
        self.strict_remote = self.build_root / "strict-remote"
        self.checks: list[Check] = []
        self.processes: list[subprocess.Popen[str]] = []
        self.env = os.environ.copy()
        self.env["AGENTREMOTE_HOME"] = str(self.config)
        self.env["PYTHONUNBUFFERED"] = "1"

    def prepare(self, *, fresh: bool) -> None:
        if fresh and self.build_root.exists():
            self._safe_rmtree(self.build_root)
        for path in (self.build_root, self.results_root, self.config, self.remote, self.local, self.tls_remote, self.strict_remote):
            path.mkdir(parents=True, exist_ok=True)
        for root in (self.remote, self.local, self.tls_remote, self.strict_remote):
            install_work_mem(root)

    def _safe_rmtree(self, path: Path) -> None:
        resolved = path.resolve()
        repo = ROOT.resolve()
        if not str(resolved).lower().startswith(str(repo).lower()):
            raise RuntimeError(f"refusing to remove outside repository: {resolved}")
        shutil.rmtree(resolved)

    def record(
        self,
        name: str,
        status: str,
        *,
        expected: str = "",
        actual: str = "",
        command: list[str] | str = "",
        evidence: list[str] | None = None,
        severity: str = "none",
    ) -> None:
        if isinstance(command, list):
            rendered = self.mask(command)
        else:
            rendered = command
        self.checks.append(
            Check(
                name=name,
                status=status,
                expected=expected,
                actual=self.sanitize(actual),
                command=rendered,
                evidence=evidence or [],
                severity=severity,
            )
        )
        print(f"  [{status}] {name}")

    def sanitize(self, text: str) -> str:
        value = str(text).replace(PASSWORD, "<redacted>")
        return value[-4000:] if len(value) > 4000 else value

    def mask(self, cmd: list[str]) -> str:
        result: list[str] = []
        skip = False
        for index, part in enumerate(cmd):
            if skip:
                skip = False
                continue
            result.append(part)
            if part in {"--password", "--password-env"} and index + 1 < len(cmd):
                result.append("<redacted>")
                skip = True
        return " ".join(result)

    def run(self, args: list[str], *, cwd: Path | None = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=cwd or self.local,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )

    def expect_cmd(
        self,
        name: str,
        args: list[str],
        *,
        ok: bool = True,
        expected: str = "",
        cwd: Path | None = None,
        timeout: int = 120,
        evidence: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = self.run(args, cwd=cwd, timeout=timeout)
        success = result.returncode == 0
        passed = success if ok else not success
        self.record(
            name,
            "PASS" if passed else "FAIL",
            expected=expected,
            actual=(result.stdout + result.stderr).strip(),
            command=args,
            evidence=evidence,
            severity="none" if passed else "P1",
        )
        return result

    def start_slave(self, root: Path, port: int, *, tls: bool = False, policy: str = "off") -> subprocess.Popen[str]:
        args = [
            sys.executable,
            "-m",
            "agentremote",
            "slave",
            "--root",
            str(root),
            "--port",
            str(port),
            "--host",
            "127.0.0.1",
            "--password",
            PASSWORD,
            "--firewall",
            "no",
            "--console",
            "no",
            "--policy",
            policy,
        ]
        if tls:
            args.extend(["--tls", "self-signed"])
        proc = subprocess.Popen(
            args,
            cwd=root,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.processes.append(proc)
        scheme = "https" if tls else "http"
        self.wait_for_json(f"{scheme}://127.0.0.1:{port}/api/challenge", insecure_tls=tls)
        return proc

    def wait_for_json(self, url: str, *, insecure_tls: bool = False, timeout: float = 30.0) -> dict[str, Any]:
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            try:
                return self.http_json(url, insecure_tls=insecure_tls)
            except Exception as exc:
                last = str(exc)
                time.sleep(0.25)
        raise RuntimeError(f"timed out waiting for {url}: {last}")

    def http_json(
        self,
        url: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        insecure_tls: bool = False,
    ) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        context = None
        if insecure_tls:
            import ssl

            context = ssl._create_unverified_context()
        try:
            with urllib.request.urlopen(request, timeout=10, context=context) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            if raw:
                try:
                    return json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    pass
            return {"status": exc.code, "error": str(exc)}

    def login_raw(self, port: int, *, proof_override: str = "", scopes: list[str] | None = None) -> dict[str, Any]:
        challenge_url = f"http://127.0.0.1:{port}/api/challenge"
        challenge = self.http_json(challenge_url)
        nonce = str(challenge["nonce"])
        proof = proof_override or make_proof(
            derive_key(PASSWORD, unb64(str(challenge["salt"])), int(challenge["iterations"])),
            nonce,
        )
        return self.http_json(
            f"http://127.0.0.1:{port}/api/login",
            method="POST",
            body={"nonce": nonce, "proof": proof, "scopes": scopes or ["read", "write", "delete", "handoff"], "clientAlias": "::rfs"},
        )

    def sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def make_file(self, path: Path, size_mib: int) -> str:
        seed = hashlib.sha256(path.name.encode("utf-8")).digest() * 1024
        with path.open("wb") as handle:
            remaining = size_mib * 1024 * 1024
            while remaining:
                chunk = seed[: min(len(seed), remaining)]
                handle.write(chunk)
                remaining -= len(chunk)
        return self.sha256(path)

    def run_all(self) -> None:
        print("== Gate A: rename-safe baseline ==")
        self.expect_cmd("A1 python module version", [sys.executable, "-m", "agentremote", "--version"])
        self.expect_cmd("A2 console script version", ["agentremote", "--version"])

        print("\n== Gate B/G: local transfer, GUI backend, handoff ==")
        self.start_slave(self.remote, self.port)
        self.expect_cmd(
            "B1 connect",
            agentremote_cmd("connect", "rfs", "127.0.0.1", str(self.port), "--password", PASSWORD),
            expected="saved isolated connection",
        )
        if self.checks[-1].status != "PASS":
            raise RuntimeError("initial connection failed; aborting dependent local RFS checks")
        (self.local / "file-000.txt").write_text("hello from local\n", encoding="utf-8")
        (self.local / "folder").mkdir(exist_ok=True)
        (self.local / "folder" / "nested.txt").write_text("nested\n", encoding="utf-8")
        source_hash = self.sha256(self.local / "file-000.txt")
        self.expect_cmd("B2 file upload", agentremote_cmd("send", "rfs", str(self.local / "file-000.txt"), "/browser", "--overwrite"))
        self.expect_cmd("B3 folder upload", agentremote_cmd("send", "rfs", str(self.local / "folder"), "/browser-folder", "--overwrite"))
        self.expect_cmd("B4 file download", agentremote_cmd("pull", "rfs", "/browser/file-000.txt", str(self.local / "received"), "--overwrite"))
        received = self.local / "received" / "file-000.txt"
        self.record(
            "B5 upload/download hash",
            "PASS" if received.exists() and self.sha256(received) == source_hash else "FAIL",
            expected="round-trip hash match",
            actual=received.name if received.exists() else "missing",
            evidence=[str(received)],
            severity="none" if received.exists() and self.sha256(received) == source_hash else "P1",
        )
        for index in range(80):
            (self.local / f"many-{index:03d}.txt").write_text(f"{index}\n", encoding="utf-8")
        self.expect_cmd("B6 long-list folder upload", agentremote_cmd("send", "rfs", str(self.local), "/long-list", "--overwrite"), timeout=180)

        master = subprocess.Popen(
            [
                *agentremote_cmd("master"),
                "rfs",
                "--local",
                str(self.local),
                "--ui-port",
                str(self.ui_port),
                "--no-browser",
                "--console",
                "no",
            ],
            cwd=self.local,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.processes.append(master)
        dashboard = self.wait_for_json(f"http://127.0.0.1:{self.ui_port}/api/dashboard")
        text = json.dumps(dashboard)
        self.record(
            "B7 dashboard API sanitized",
            "PASS" if "nodes" in dashboard and PASSWORD not in text else "FAIL",
            expected="dashboard reachable without password/token leakage",
            actual="reachable" if "nodes" in dashboard else str(dashboard)[:200],
            severity="none" if "nodes" in dashboard and PASSWORD not in text else "P1",
        )
        self.http_json(f"http://127.0.0.1:{self.ui_port}/api/remote/mkdir", method="POST", body={"path": "/gui-created"})
        self.record("B8 GUI proxy mkdir", "PASS" if (self.remote / "gui-created").is_dir() else "FAIL", expected="remote folder created")
        self.expect_cmd("G1 instruction-only handoff", agentremote_cmd("tell", "rfs", "Inspect /browser and report.", "--path", "/browser"))
        self.expect_cmd("G2 file handoff", agentremote_cmd("handoff", "rfs", str(self.local / "file-000.txt"), "Review this file.", "--remote-dir", "/handoff", "--overwrite"))

        print("\n== Gate E: security ==")
        wrong = self.login_raw(self.port, proof_override="0" * 64)
        self.record("E1 wrong password rejected", "PASS" if "token" not in wrong else "FAIL", expected="no token returned", actual=str(wrong))
        self.expect_cmd(
            "E2 readonly scoped write denied",
            agentremote_cmd("connect", "readonly", "127.0.0.1", str(self.port), "--password", PASSWORD, "--scopes", "read,handoff"),
        )
        denied = self.expect_cmd(
            "E3 readonly upload denied",
            agentremote_cmd("send", "readonly", str(self.local / "file-000.txt"), "/blocked"),
            ok=False,
        )
        self.record(
            "E4 readonly denial mentions scope",
            "PASS" if "scope" in (denied.stdout + denied.stderr).lower() else "FAIL",
            expected="scope denial is visible",
            actual=(denied.stdout + denied.stderr)[-300:],
        )
        self.expect_cmd("E5 allow Tailscale CIDRs", agentremote_cmd("policy", "allow-tailscale"), cwd=self.local)

        print("\n== Gate E/TLS: fingerprint trust and mismatch ==")
        self.start_slave(self.tls_remote, self.tls_port, tls=True)
        fingerprint = fetch_remote_fingerprint(f"https://127.0.0.1:{self.tls_port}", self.tls_port)
        self.expect_cmd(
            "E6 TLS fingerprint trust",
            [
                *agentremote_cmd("connect"),
                "tls-rfs",
                f"https://127.0.0.1:{self.tls_port}",
                "--password",
                PASSWORD,
                "--tls-fingerprint",
                fingerprint,
            ],
            evidence=[format_fingerprint(fingerprint)],
        )
        mismatch = "00" * 32
        self.expect_cmd(
            "E7 TLS fingerprint mismatch",
            [
                *agentremote_cmd("connect"),
                "tls-bad",
                f"https://127.0.0.1:{self.tls_port}",
                "--password",
                PASSWORD,
                "--tls-fingerprint",
                mismatch,
            ],
            ok=False,
        )

        print("\n== Gate E/strict: whitelist ==")
        self.start_slave(self.strict_remote, self.strict_port, policy="strict")
        blocked = self.expect_cmd(
            "E8 strict whitelist blocks unlisted",
            agentremote_cmd("connect", "strict", "127.0.0.1", str(self.strict_port), "--password", PASSWORD),
            ok=False,
        )
        self.record(
            "E9 strict denial code visible",
            "PASS" if "policy" in (blocked.stdout + blocked.stderr).lower() else "FAIL",
            expected="policy denial is reported",
            actual=(blocked.stdout + blocked.stderr)[-300:],
        )
        self.expect_cmd("E10 allow strict alias", agentremote_cmd("policy", "allow", "strict"), cwd=self.local)
        self.expect_cmd("E11 strict whitelist allows listed", agentremote_cmd("connect", "strict", "127.0.0.1", str(self.strict_port), "--password", PASSWORD))

    def stop(self) -> None:
        for proc in reversed(self.processes):
            if proc.poll() is not None:
                continue
            try:
                if os.name == "nt":
                    proc.terminate()
                else:
                    proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

    def write_report(self) -> Path:
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        path = self.results_root / f"test-results_{timestamp}-agentremote-local-rfs.md"
        counts = {status: sum(1 for check in self.checks if check.status == status) for status in ("PASS", "FAIL", "BLOCKED", "NOT_RUN")}
        lines = [
            "# AgentRemote Local RFS Gate Report",
            "",
            f"- Date: {time.strftime('%Y-%m-%d %H:%M:%S')} KST",
            "- Executor: tools/agentremote_rfs_gates.py",
            f"- Build root: `{self.build_root}`",
            "",
            "## Summary",
            "",
            "| Status | Count |",
            "|--------|-------|",
        ]
        for status, count in counts.items():
            lines.append(f"| {status} | {count} |")
        lines.extend(["", "## Checks", ""])
        for check in self.checks:
            lines.extend(
                [
                    f"### {check.name}",
                    "",
                    f"- Status: {check.status}",
                    f"- Severity: {check.severity}",
                ]
            )
            if check.command:
                lines.append(f"- Command: `{check.command}`")
            if check.expected:
                lines.append(f"- Expected: {check.expected}")
            if check.actual:
                lines.extend(["- Actual:", "", "```text", check.actual, "```"])
            if check.evidence:
                lines.append("- Evidence:")
                for item in check.evidence:
                    lines.append(f"  - `{item}`")
            lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")
        return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local agentremote RFS gates with isolated state")
    parser.add_argument("--build-root", default=str(DEFAULT_BUILD))
    parser.add_argument("--results-root", default=str(DEFAULT_RESULTS))
    parser.add_argument("--port", type=int, default=0, help="plain slave port; 0 picks a free port")
    parser.add_argument("--ui-port", type=int, default=0, help="master GUI port; 0 picks a free port")
    parser.add_argument("--tls-port", type=int, default=0, help="TLS slave port; 0 picks a free port")
    parser.add_argument("--strict-port", type=int, default=0, help="strict-policy slave port; 0 picks a free port")
    parser.add_argument("--keep-state", action="store_true")
    args = parser.parse_args()

    harness = LocalRfsHarness(
        build_root=Path(args.build_root),
        results_root=Path(args.results_root),
        port=args.port or free_port(),
        ui_port=args.ui_port or free_port(),
        tls_port=args.tls_port or free_port(),
        strict_port=args.strict_port or free_port(),
    )
    exit_code = 0
    try:
        harness.prepare(fresh=not args.keep_state)
        harness.run_all()
    except Exception as exc:
        harness.record("runner fatal error", "FAIL", expected="all local RFS gates complete", actual=str(exc), severity="P1")
        exit_code = 1
    finally:
        harness.stop()
        report = harness.write_report()
        print(f"\nlocal RFS report: {report}")
    if any(check.status == "FAIL" for check in harness.checks):
        return 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
