# Docker Full-Scale Lab

The Docker lab is the repeatable Linux multi-node validation path for
agent-remote-sync / `agentremote`. It does not replace real Windows/macOS
filesystem validation, but it gives us a stable way to stress protocol, sync,
handoff, routing, scoped tokens, many-file transfers, and large-file rehearsals.

## What It Covers

- Multi-node controller/daemon topology on a private Docker bridge network.
- `connect`, `send`, `pull`, `sync-project`, `tell`, `handoff`, `map`, and
  `route` commands.
- Wrong-password rejection and scoped read/handoff token write denial.
- Unicode filename transfer inside Linux containers.
- Many-file sync rehearsal.
- Config/log/AIMemory behavior in isolated volumes.
- Optional large-file upload rehearsal.

## What It Cannot Prove

- Native Windows NTFS behavior.
- Native macOS APFS/NFD behavior.
- Real Windows/macOS console encoding and path length behavior.
- Native firewall prompts and service managers such as Task Scheduler,
  LaunchAgent, or `systemd --user`.
- Real Tailscale routing unless Tailscale is explicitly added to the lab.

Keep using `docs/v0.1-fullscale-lab-runbook.md` for cross-OS and large-soak
validation outside Docker.

## Files

```text
docker/Dockerfile
docker/compose.fullscale.yml
tools/docker_node_entry.py
tools/docker_fullscale_runner.py
tools/run_docker_fullscale.py
```

## Quick Start

From the repository root:

```powershell
python tools\run_docker_fullscale.py
```

This builds the image, starts `node-a`, `node-b`, and `controller`, runs the
controller validation, and copies result files under:

```text
build/docker-fullscale-results/
```

For a faster rehearsal:

```powershell
python tools\run_docker_fullscale.py --many-count 50 --large-size-mib 1 --down
```

For a stronger local soak:

```powershell
python tools\run_docker_fullscale.py --fresh --many-count 5000 --large-size-mib 1024 --down-volumes
```

Use `--down` to stop and remove the compose containers after the run. Named
volumes are kept so Docker can still inspect state unless you explicitly remove
them yourself. Use `--fresh` before a run to remove old named volumes first, and
use `--down-volumes` after a run when you want to leave no Docker lab state
behind. For release validation, run at least one fresh-volume pass and one
persistent-volume rerun so stale partial/retry behavior is covered.

If Docker Compose is not installed or the Docker daemon is unreachable, the
wrapper exits with code `2` and writes a structured `BLOCKED` report under the
results directory instead of failing silently:

```text
build/docker-fullscale-results/test-results_YYYYMMDD-HHMMSS-docker-fullscale-blocked.md
```

That report is not a validation pass. It is only evidence that the Docker gate
must be rerun on a host with Docker Compose or through GitHub Actions.

## GitHub Actions

The repository includes a manual workflow:

```text
.github/workflows/docker-fullscale.yml
```

Run it from the GitHub Actions tab with conservative inputs first:

```text
many_count=500
large_size_mib=16
```

For a stronger pre-release run, increase the inputs:

```text
many_count=5000
large_size_mib=1024
```

The workflow uploads `docker-fullscale-results` as an artifact. Docker success
is still Linux-container evidence only; it does not replace the native
Windows/macOS cross-OS lab.

## Direct Compose

```powershell
$env:AGENTREMOTE_DOCKER_MANY_COUNT = "500"
$env:AGENTREMOTE_DOCKER_LARGE_SIZE_MIB = "16"
docker compose -f docker/compose.fullscale.yml up --build --abort-on-container-exit --exit-code-from controller
docker compose -f docker/compose.fullscale.yml cp controller:/lab/controller/results build/docker-fullscale-results
```

On bash/zsh:

```bash
AGENTREMOTE_DOCKER_MANY_COUNT=500 \
AGENTREMOTE_DOCKER_LARGE_SIZE_MIB=16 \
docker compose -f docker/compose.fullscale.yml up --build --abort-on-container-exit --exit-code-from controller
docker compose -f docker/compose.fullscale.yml cp controller:/lab/controller/results build/docker-fullscale-results
```

## Result Format

The controller writes:

```text
test-results_YYYYMMDD-HHMMSS-docker-fullscale.md
```

The report includes:

- node list
- dataset sizes
- command-level PASS/FAIL/BLOCKED rows
- masked commands where passwords are redacted
- actual command output snippets
- evidence paths inside the controller container

## Tunables

Environment variables used by `docker/compose.fullscale.yml`:

```text
AGENTREMOTE_DOCKER_PASSWORD
AGENTREMOTE_DOCKER_MANY_COUNT
AGENTREMOTE_DOCKER_LARGE_SIZE_MIB
```

Defaults:

```text
AGENTREMOTE_DOCKER_PASSWORD=lab-secret
AGENTREMOTE_DOCKER_MANY_COUNT=500
AGENTREMOTE_DOCKER_LARGE_SIZE_MIB=16
```

The default large-file value is intentionally modest. Increase it for real soak
runs on machines with enough disposable disk space.

## Interpreting Failures

- Any FAIL in connect/send/pull/sync/handoff is at least P1.
- Wrong-password rejection and scoped-token write denial should be PASS only when
  the operation fails safely.
- BLOCKED large-file checks are acceptable only when
  `AGENTREMOTE_DOCKER_LARGE_SIZE_MIB=0` or disk is unavailable.
- Docker PASS does not close the cross-OS validation requirement; it only closes
  the repeatable Linux-container validation requirement.
