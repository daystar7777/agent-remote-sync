from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


def check_docker_compose(*, cwd: Path, env: dict[str, str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["docker", "compose", "version"],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
        )
    except FileNotFoundError:
        return False, "docker command not found"
    except subprocess.TimeoutExpired:
        return False, "docker compose version timed out"

    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        return False, output or f"docker compose version exited {result.returncode}"
    return True, output or "docker compose is available"


def write_blocked_report(results_dir: Path, *, reason: str, compose_file: Path) -> Path:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    report = results_dir / f"test-results_{timestamp}-docker-fullscale-blocked.md"
    report.write_text(
        "\n".join(
            [
                "# Docker Full-Scale Validation Report",
                "",
                f"- Date: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
                "- Executor: tools/run_docker_fullscale.py",
                "- Status: BLOCKED",
                "",
                "## Summary",
                "",
                "Docker full-scale validation did not start because Docker Compose is unavailable.",
                "",
                "## Reason",
                "",
                "```text",
                reason,
                "```",
                "",
                "## Requested Compose File",
                "",
                f"`{compose_file}`",
                "",
                "## Next Action",
                "",
                "Run this command on a host where Docker Compose is installed and the Docker daemon is reachable:",
                "",
                "```powershell",
                "python tools\\run_docker_fullscale.py --many-count 50 --large-size-mib 1 --down",
                "```",
                "",
                "Or trigger `.github/workflows/docker-fullscale.yml` with `workflow_dispatch`.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return report


def run(cmd: list[str], *, cwd: Path, env: dict[str, str]) -> int:
    print("+ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=cwd, env=env).returncode


def latest_report_passed(results_dir: Path) -> bool:
    candidates = sorted((results_dir / "results").glob("test-results_*-docker-fullscale.md"))
    if not candidates:
        candidates = sorted(results_dir.glob("test-results_*-docker-fullscale.md"))
    if not candidates:
        return False
    text = candidates[-1].read_text(encoding="utf-8", errors="replace")
    return "| PASS | 24 |" in text and "| FAIL | 0 |" in text and "| BLOCKED | 0 |" in text


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Run the agentremote Docker full-scale lab")
    parser.add_argument("--compose-file", default=str(repo / "docker" / "compose.fullscale.yml"))
    parser.add_argument("--results-dir", default=str(repo / "build" / "docker-fullscale-results"))
    parser.add_argument("--many-count", type=int, default=500)
    parser.add_argument("--large-size-mib", type=int, default=16)
    parser.add_argument("--password", default="lab-secret")
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="remove existing compose containers and named volumes before starting",
    )
    parser.add_argument(
        "--down",
        action="store_true",
        help="stop and remove compose containers after copying results; named volumes are kept",
    )
    parser.add_argument(
        "--down-volumes",
        action="store_true",
        help="stop containers and remove named volumes after copying results",
    )
    args = parser.parse_args()

    compose_file = Path(args.compose_file)
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["AGENTREMOTE_DOCKER_MANY_COUNT"] = str(max(0, args.many_count))
    env["AGENTREMOTE_DOCKER_LARGE_SIZE_MIB"] = str(max(0, args.large_size_mib))
    env["AGENTREMOTE_DOCKER_PASSWORD"] = args.password

    docker_ok, docker_status = check_docker_compose(cwd=repo, env=env)
    if not docker_ok:
        report = write_blocked_report(results_dir, reason=docker_status, compose_file=compose_file)
        print(f"docker fullscale blocked: {docker_status}", file=sys.stderr, flush=True)
        print(f"blocked report: {report}", flush=True)
        return 2

    print(f"docker compose: {docker_status}", flush=True)

    if args.fresh:
        fresh_rc = run(
            ["docker", "compose", "-f", str(compose_file), "down", "--volumes"],
            cwd=repo,
            env=env,
        )
        if fresh_rc != 0:
            return fresh_rc

    up_cmd = [
        "docker",
        "compose",
        "-f",
        str(compose_file),
        "up",
        "--abort-on-container-exit",
        "--exit-code-from",
        "controller",
    ]
    if not args.no_build:
        up_cmd.insert(5, "--build")

    rc = run(up_cmd, cwd=repo, env=env)

    copy_rc = run(
        [
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "cp",
            "controller:/lab/controller/results",
            str(results_dir),
        ],
        cwd=repo,
        env=env,
    )
    if copy_rc != 0:
        print("warning: could not copy Docker fullscale results", file=sys.stderr)
    elif rc != 0 and latest_report_passed(results_dir):
        print(
            "docker compose returned a non-zero code after aborting helper nodes, "
            "but the controller fullscale report is PASS; treating this run as PASS.",
            flush=True,
        )
        rc = 0

    if args.down or args.down_volumes:
        down_cmd = ["docker", "compose", "-f", str(compose_file), "down"]
        if args.down_volumes:
            down_cmd.append("--volumes")
        down_rc = run(down_cmd, cwd=repo, env=env)
        if rc == 0 and down_rc != 0:
            rc = down_rc

    print(f"results copied under: {results_dir}", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
