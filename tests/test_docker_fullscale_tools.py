from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = REPO_ROOT / "tools" / "run_docker_fullscale.py"
GENERATOR_PATH = REPO_ROOT / "tools" / "generate_fullscale_lab_data.py"


def load_tool_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(module)
    return module


def load_runner_module():
    return load_tool_module("run_docker_fullscale_for_test", RUNNER_PATH)


def load_generator_module():
    return load_tool_module("generate_fullscale_lab_data_for_test", GENERATOR_PATH)


class DockerFullscaleToolTests(unittest.TestCase):
    def test_check_docker_compose_reports_missing_docker(self) -> None:
        runner = load_runner_module()
        with patch.object(runner.subprocess, "run", side_effect=FileNotFoundError):
            ok, reason = runner.check_docker_compose(cwd=REPO_ROOT, env={})
        self.assertFalse(ok)
        self.assertIn("docker command not found", reason)

    def test_blocked_report_contains_reason_and_next_action(self) -> None:
        runner = load_runner_module()
        with tempfile.TemporaryDirectory() as tmp:
            report = runner.write_blocked_report(
                Path(tmp),
                reason="docker command not found",
                compose_file=REPO_ROOT / "docker" / "compose.fullscale.yml",
            )
            text = report.read_text(encoding="utf-8")
        self.assertIn("Status: BLOCKED", text)
        self.assertIn("docker command not found", text)
        self.assertIn("run_docker_fullscale.py", text)
        self.assertIn("docker-fullscale.yml", text)

    def test_main_writes_blocked_report_when_docker_is_missing(self) -> None:
        runner = load_runner_module()
        with tempfile.TemporaryDirectory() as tmp:
            args = [
                "run_docker_fullscale.py",
                "--results-dir",
                tmp,
                "--many-count",
                "1",
                "--large-size-mib",
                "0",
            ]
            with patch.object(runner.subprocess, "run", side_effect=FileNotFoundError):
                with patch.object(sys, "argv", args):
                    rc = runner.main()
            reports = list(Path(tmp).glob("*docker-fullscale-blocked.md"))
        self.assertEqual(rc, 2)
        self.assertEqual(len(reports), 1)

    def test_main_supports_fresh_and_down_volumes(self) -> None:
        runner = load_runner_module()
        with tempfile.TemporaryDirectory() as tmp:
            args = [
                "run_docker_fullscale.py",
                "--results-dir",
                tmp,
                "--many-count",
                "1",
                "--large-size-mib",
                "0",
                "--no-build",
                "--fresh",
                "--down-volumes",
            ]
            with patch.object(runner, "check_docker_compose", return_value=(True, "docker compose ok")):
                with patch.object(runner, "run", return_value=0) as run_mock:
                    with patch.object(sys, "argv", args):
                        rc = runner.main()
        self.assertEqual(rc, 0)
        commands = [call.args[0] for call in run_mock.call_args_list]
        self.assertEqual(commands[0][-2:], ["down", "--volumes"])
        self.assertIn("up", commands[1])
        self.assertEqual(commands[-1][-2:], ["down", "--volumes"])

    def test_lab_data_generator_avoids_normalized_unicode_target_collisions(self) -> None:
        generator = load_generator_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generator.generate(root, many_count=3, large_size_mib=0)
            first_names = sorted(path.name for path in (root / "DS-unicode").iterdir())
            generator.generate(root, many_count=3, large_size_mib=0)
            second_names = sorted(path.name for path in (root / "DS-unicode").iterdir())
        self.assertEqual(first_names, second_names)
        payload_names = [name for name in second_names if name != "unicode-manifest.json"]
        normalized = [unicodedata.normalize("NFC", name) for name in payload_names]
        self.assertEqual(len(normalized), len(set(normalized)))
        self.assertTrue(any(name.startswith("collision-") for name in payload_names))

    def test_docker_runner_verifies_unicode_manifest_hashes_after_wire_normalization(self) -> None:
        runner_module = load_tool_module("docker_fullscale_runner_for_test", REPO_ROOT / "tools" / "docker_fullscale_runner.py")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            received = root / "received"
            source.mkdir()
            received.mkdir()
            actual = unicodedata.normalize("NFD", "cafe-é.txt")
            wire = unicodedata.normalize("NFC", actual)
            (source / actual).write_text("payload\n", encoding="utf-8")
            (received / wire).write_text("payload\n", encoding="utf-8")
            manifest = [{"actual": actual, "requested": actual, "collision": False}]
            (source / "unicode-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (received / "unicode-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            runner = runner_module.DockerFullscaleRunner(
                nodes=["node-a"],
                password="secret",
                work_root=root / "work",
                config=root / "config",
                results=root / "results",
                many_count=1,
                large_size_mib=0,
            )
            runner.verify_unicode_manifest_hashes(source, received)
        self.assertEqual(runner.checks[-1].status, "PASS")
        self.assertIn("verified 1", runner.checks[-1].actual)


if __name__ == "__main__":
    unittest.main()
