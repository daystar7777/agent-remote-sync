from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from agentremote.workmem import install_work_mem
from generate_fullscale_lab_data import generate


@dataclass
class Check:
    name: str
    status: str
    command: str = ""
    expected: str = ""
    actual: str = ""
    evidence: list[str] = field(default_factory=list)
    severity: str = "none"


class DockerFullscaleRunner:
    def __init__(
        self,
        *,
        nodes: list[str],
        password: str,
        work_root: Path,
        config: Path,
        results: Path,
        many_count: int,
        large_size_mib: int,
    ) -> None:
        self.nodes = nodes
        self.password = password
        self.work_root = work_root.resolve()
        self.config = config.resolve()
        self.results = results.resolve()
        self.many_count = many_count
        self.large_size_mib = large_size_mib
        self.checks: list[Check] = []
        self.env = os.environ.copy()
        self.env["AGENTREMOTE_HOME"] = str(self.config)
        self.env["PYTHONUNBUFFERED"] = "1"

    def prepare(self) -> None:
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.config.mkdir(parents=True, exist_ok=True)
        self.results.mkdir(parents=True, exist_ok=True)
        os.environ["AGENTREMOTE_HOME"] = str(self.config)
        install_work_mem(self.work_root)
        data_dir = self.work_root / "data"
        generate(
            data_dir,
            many_count=self.many_count,
            large_size_mib=self.large_size_mib,
        )
        for sub in ("DS-small", "DS-many"):
            target = data_dir / sub
            if target.is_dir():
                install_work_mem(target)

    def run_cli(self, args: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "agentremote", *args],
            cwd=self.work_root,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )

    def masked_command(self, args: list[str]) -> str:
        masked: list[str] = []
        skip_next = False
        for index, part in enumerate(args):
            if skip_next:
                skip_next = False
                continue
            masked.append(shlex.quote(part))
            if part == "--password" and index + 1 < len(args):
                masked.append("<redacted>")
                skip_next = True
        return "python -m agentremote " + " ".join(masked)

    def add_result(
        self,
        name: str,
        status: str,
        *,
        args: list[str] | None = None,
        expected: str = "",
        actual: str = "",
        evidence: list[str] | None = None,
        severity: str = "none",
    ) -> None:
        self.checks.append(
            Check(
                name=name,
                status=status,
                command=self.masked_command(args or []) if args else "",
                expected=expected,
                actual=self.sanitize(actual),
                evidence=evidence or [],
                severity=severity,
            )
        )

    def sanitize(self, text: str) -> str:
        value = text.replace(self.password, "<redacted>")
        return value[-4000:] if len(value) > 4000 else value

    def expect_ok(
        self,
        name: str,
        args: list[str],
        *,
        expected: str,
        timeout: int = 120,
        retries: int = 1,
    ) -> subprocess.CompletedProcess[str]:
        last: subprocess.CompletedProcess[str] | None = None
        for attempt in range(retries):
            last = self.run_cli(args, timeout=timeout)
            if last.returncode == 0:
                self.add_result(
                    name,
                    "PASS",
                    args=args,
                    expected=expected,
                    actual=(last.stdout + last.stderr).strip(),
                )
                return last
            if attempt + 1 < retries:
                time.sleep(1)
        assert last is not None
        self.add_result(
            name,
            "FAIL",
            args=args,
            expected=expected,
            actual=(last.stdout + last.stderr).strip(),
            severity="P1",
        )
        raise RuntimeError(f"{name} failed")

    def expect_fail(
        self,
        name: str,
        args: list[str],
        *,
        expected: str,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[str]:
        result = self.run_cli(args, timeout=timeout)
        if result.returncode != 0:
            self.add_result(
                name,
                "PASS",
                args=args,
                expected=expected,
                actual=(result.stdout + result.stderr).strip(),
            )
            return result
        self.add_result(
            name,
            "FAIL",
            args=args,
            expected=expected,
            actual=(result.stdout + result.stderr).strip(),
            severity="P1",
        )
        raise RuntimeError(f"{name} unexpectedly succeeded")

    def sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def verify_file_hash(self, name: str, left: Path, right: Path) -> None:
        if left.exists() and right.exists() and self.sha256(left) == self.sha256(right):
            self.add_result(
                name,
                "PASS",
                expected="hashes match",
                actual=f"{left} == {right}",
                evidence=[str(left), str(right)],
            )
            return
        self.add_result(
            name,
            "FAIL",
            expected="hashes match",
            actual=f"left exists={left.exists()} right exists={right.exists()}",
            evidence=[str(left), str(right)],
            severity="P1",
        )
        raise RuntimeError(f"{name} hash mismatch")

    def verify_unicode_manifest_hashes(self, source_dir: Path, received_dir: Path) -> None:
        manifest_path = source_dir / "unicode-manifest.json"
        received_manifest = received_dir / "unicode-manifest.json"
        errors: list[str] = []
        evidence = [str(manifest_path), str(received_manifest)]
        if not manifest_path.exists():
            errors.append(f"missing source manifest: {manifest_path}")
        if not received_manifest.exists():
            errors.append(f"missing received manifest: {received_manifest}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            manifest = []
            errors.append(f"could not read source manifest: {exc}")
        if not isinstance(manifest, list):
            manifest = []
            errors.append("source manifest is not a list")

        expected_names: set[str] = {"unicode-manifest.json"}
        for entry in manifest:
            if not isinstance(entry, dict):
                errors.append(f"bad manifest entry: {entry!r}")
                continue
            actual_name = str(entry.get("actual", ""))
            if not actual_name:
                errors.append(f"manifest entry missing actual name: {entry!r}")
                continue
            wire_name = unicodedata.normalize("NFC", actual_name)
            expected_names.add(wire_name)
            source = source_dir / actual_name
            received = received_dir / wire_name
            evidence.extend([str(source), str(received)])
            if not source.exists():
                errors.append(f"missing source file: {actual_name}")
                continue
            if not received.exists():
                errors.append(f"missing received file for {actual_name}: expected {wire_name}")
                continue
            if self.sha256(source) != self.sha256(received):
                errors.append(f"hash mismatch: {actual_name} -> {wire_name}")

        extra_names = sorted(path.name for path in received_dir.iterdir() if path.name not in expected_names) if received_dir.exists() else []
        if errors:
            self.add_result(
                "R7 unicode manifest hashes",
                "FAIL",
                expected="manifest files exist and all expected Unicode file hashes match",
                actual="\n".join(errors),
                evidence=evidence[:20],
                severity="P1",
            )
            raise RuntimeError("unicode manifest verification failed")
        self.add_result(
            "R7 unicode manifest hashes",
            "PASS",
            expected="manifest files exist and all expected Unicode file hashes match",
            actual=f"verified {len(expected_names) - 1} manifest file(s); extra remote leftovers ignored={len(extra_names)}",
            evidence=evidence[:20],
        )

    def run(self) -> None:
        self.prepare()
        data = self.work_root / "data"
        first = self.nodes[0]
        second = self.nodes[1] if len(self.nodes) > 1 else self.nodes[0]

        self.expect_ok(
            "R0/R1 setup status",
            ["status", "--root", str(self.work_root)],
            expected="controller AIMemory status is readable",
        )

        for node in self.nodes:
            self.expect_ok(
                f"R1 connect {node}",
                ["connect", node, node, "7171", "--password", self.password],
                expected=f"saved alias ::{node}",
                retries=20,
            )

        self.expect_fail(
            "R3 wrong password rejected",
            ["connect", "bad-node", first, "7171", "--password", "wrong-password"],
            expected="bad password does not create a valid session",
        )

        self.expect_ok(
            "R1 send DS-small",
            ["send", first, str(data / "DS-small"), "/incoming/small", "--overwrite"],
            expected="folder uploads to remote node",
            timeout=180,
        )
        self.expect_ok(
            "R1 pull DS-small",
            ["pull", first, "/incoming/small/DS-small/file-0.txt", "received-small", "--overwrite"],
            expected="uploaded file can be pulled back",
            timeout=180,
        )
        self.verify_file_hash(
            "R1 pulled file hash",
            data / "DS-small" / "file-0.txt",
            self.work_root / "received-small" / "file-0.txt",
        )

        self.expect_ok(
            "R4 sync-project plan",
            ["sync-project", first, "/sync-small", "--local", str(data / "DS-small")],
            expected="sync plan prints without transferring",
        )
        self.expect_ok(
            "R4 sync-project execute",
            ["sync-project", first, "/sync-small", "--local", str(data / "DS-small"), "--yes", "--overwrite"],
            expected="sync executes with explicit --yes",
            timeout=180,
        )

        self.expect_ok(
            "R4 tell instruction",
            ["tell", first, "Inspect /sync-small and report back.", "--path", "/sync-small"],
            expected="instruction-only handoff is accepted",
        )
        self.expect_ok(
            "R4 file handoff",
            [
                "handoff",
                first,
                str(data / "DS-small" / "file-1.txt"),
                "Review this file and report back.",
                "--remote-dir",
                "/handoff-files",
                "--overwrite",
            ],
            expected="file plus task handoff succeeds",
        )

        self.expect_ok(
            "R6 route set",
            ["route", "set", first, first, "7171", "--priority", "10"],
            expected="route preference is saved",
        )
        self.expect_ok(
            "R6 route probe",
            ["route", "probe", first, "--timeout", "5"],
            expected="route health is probed",
        )
        self.expect_ok(
            "R6 map",
            ["map", "--root", str(self.work_root)],
            expected="topology map is readable",
        )

        self.expect_ok(
            "R3 scoped readonly connect",
            ["connect", f"readonly-{second}", second, "7171", "--password", self.password, "--scopes", "read,handoff"],
            expected="read/handoff scoped token can be saved",
        )
        self.expect_fail(
            "R3 scoped readonly write denied",
            ["send", f"readonly-{second}", str(data / "DS-small" / "file-2.txt"), "/blocked"],
            expected="read/handoff token cannot upload",
        )

        self.expect_ok(
            "R7 unicode dataset send",
            ["send", first, str(data / "DS-unicode"), "/unicode", "--overwrite"],
            expected="unicode filenames transfer in Linux Docker lab",
            timeout=180,
        )
        self.expect_ok(
            "R7 unicode dataset pull",
            ["pull", first, "/unicode/DS-unicode", "received-unicode", "--overwrite"],
            expected="unicode filenames pull back",
            timeout=180,
        )
        self.verify_unicode_manifest_hashes(
            data / "DS-unicode",
            self.work_root / "received-unicode" / "DS-unicode",
        )

        large_file = data / "DS-large" / f"large-{self.large_size_mib}mib.bin"
        if self.large_size_mib > 0 and large_file.exists():
            self.expect_ok(
                "R8 large file upload rehearsal",
                ["send", first, str(large_file), "/large", "--overwrite"],
                expected=f"{self.large_size_mib} MiB file uploads",
                timeout=300,
            )
        else:
            self.add_result(
                "R8 large file upload rehearsal",
                "BLOCKED",
                expected="large-size-mib > 0",
                actual="large dataset skipped",
                severity="none",
            )

        self.expect_ok(
            "R8 many-file sync rehearsal",
            ["sync-project", first, "/many", "--local", str(data / "DS-many"), "--yes", "--overwrite"],
            expected=f"{self.many_count}+ file sync completes",
            timeout=600,
        )

        stale_file = self.work_root / "mirror-stale.txt"
        stale_file.write_text("stale remote mirror candidate\n", encoding="utf-8")
        self.expect_ok(
            "R8 mirror setup stale remote file",
            ["send", first, str(stale_file), "/mirror-delete", "--overwrite"],
            expected="stale remote file exists before mirror delete check",
            timeout=180,
        )
        self.expect_ok(
            "R8 mirror sync delete rehearsal",
            ["sync-project", first, "/mirror-delete", "--local", str(data / "DS-small"), "--yes", "--overwrite", "--delete"],
            expected="sync-project --delete removes remote files absent from the source",
            timeout=180,
        )
        self.expect_fail(
            "R8 mirror stale file removed",
            ["pull", first, "/mirror-delete/mirror-stale.txt", "mirror-stale-check", "--overwrite"],
            expected="deleted stale remote file can no longer be pulled",
            timeout=120,
        )

    def write_report(self) -> Path:
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        report = self.results / f"test-results_{timestamp}-docker-fullscale.md"
        failures = [check for check in self.checks if check.status == "FAIL"]
        blocked = [check for check in self.checks if check.status == "BLOCKED"]
        lines = [
            "# Docker Full-Scale Validation Report",
            "",
            f"- Date: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
            "- Executor: docker_fullscale_runner.py",
            f"- Nodes: {', '.join(self.nodes)}",
            f"- Work root: `{self.work_root}`",
            f"- Results root: `{self.results}`",
            f"- Many-file count: {self.many_count}",
            f"- Large-file size: {self.large_size_mib} MiB",
            "",
            "## Summary",
            "",
            f"- Total checks: {len(self.checks)}",
            f"- Failures: {len(failures)}",
            f"- Blocked: {len(blocked)}",
            "",
            "| Status | Count |",
            "|--------|-------|",
        ]
        for status in ("PASS", "FAIL", "BLOCKED", "NOT_RUN"):
            lines.append(f"| {status} | {sum(1 for check in self.checks if check.status == status)} |")
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
        report.write_text("\n".join(lines), encoding="utf-8")
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Docker Compose full-scale agentremote validation")
    parser.add_argument("--nodes", default="node-a,node-b")
    parser.add_argument("--password", required=True)
    parser.add_argument("--work-root", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--results", required=True)
    parser.add_argument("--many-count", type=int, default=500)
    parser.add_argument("--large-size-mib", type=int, default=16)
    args = parser.parse_args()

    runner = DockerFullscaleRunner(
        nodes=[node.strip() for node in args.nodes.split(",") if node.strip()],
        password=args.password,
        work_root=Path(args.work_root),
        config=Path(args.config),
        results=Path(args.results),
        many_count=max(0, args.many_count),
        large_size_mib=max(0, args.large_size_mib),
    )
    exit_code = 0
    try:
        runner.run()
    except Exception as exc:
        runner.add_result("runner fatal error", "FAIL", expected="all checks complete", actual=str(exc), severity="P1")
        exit_code = 1
    finally:
        report = runner.write_report()
        print(f"docker fullscale report: {report}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
