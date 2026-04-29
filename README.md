# agentFTP

[English](README.md) | [한국어](README.ko.md)

[![CI](https://github.com/daystar7777/agentFTP/actions/workflows/ci.yml/badge.svg)](https://github.com/daystar7777/agentFTP/actions/workflows/ci.yml)

Easy cross-host file/folder transfer and remote-agent handoff for building agent swarm workflows.

agentFTP lets one machine expose a project folder as a **slave**, while another
machine connects as a **master** through a browser UI or headless CLI. It is
designed for agent workflows: move project folders, send task intent, receive
status reports, and keep local/remote handoff history through
[`agent-work-mem`](https://github.com/daystar7777/agent-work-mem).

In that sense, agentFTP is a network extension for agent-work-mem: it carries
agent memory, handoff intent, and status reports beyond one local machine and
into trusted remote hosts.

agentFTP is not the FTP protocol. It uses a small HTTP/HTTPS API built for
root-confined browsing, resumable large-file transfer, sync planning, and
agent-to-agent handoff.

## Why agentFTP?

- **Easy setup**: install from GitHub, bootstrap prerequisites, then run slave or master mode.
- **Powerful file transfer**: browser UI, headless push/pull, folder sync, resumable large files, cancel/resume, conflict checks, and disk-space preflight.
- **Remote agent handoff**: send instructions with files, receive reports, and let a remote worker process explicit `agentftp-run:` tasks.
- **Swarm-ready foundation**: saved host aliases, host history, scoped tokens, worker daemon mode, and local/remote AIMemory records powered by agent-work-mem.
- **Cross-platform**: Windows, macOS, and Linux with Unicode filename normalization for Korean/accented filenames.

## Two Operating Modes

agentFTP is useful both when a human wants to move files directly and when an
agent should handle transfer or handoff without a GUI.

### GUI Mode: User-Driven Transfer

- Start the receiver as a console slave with `agentftp slave`.
- Open the browser-based master UI with `agentftp master lab`.
- The master browser opens automatically and shows remote files on the left,
  local files on the right.
- Use `agentftp master lab --no-browser` when you only want the local UI URL.

GUI mode is best when a user wants to inspect folders, select files manually,
upload/download in either direction, and confirm conflicts visually.

### Headless Mode: Agent-Driven Transfer And Handoff

- Transfer files with `agentftp push`, `agentftp pull`, and `agentftp sync`.
- Send remote instructions with `agentftp tell` or files plus instructions with
  `agentftp handoff`.
- Let a receiving agent process eligible work with `agentftp worker`.
- Send structured results back with `agentftp report`.

Headless mode is the automation path: an agent can move a project folder, hand
off task intent, wait for remote work, and receive a report without opening the
browser UI.

## Status

agentFTP is an early `v0.1` prototype. The core transfer and handoff flows are
implemented and covered by scenario tests, but the project is still evolving.
Use a trusted network or HTTPS, and review the security notes before using it
with sensitive project data.

The v1 direction is tracked in [docs/development-plan.md](docs/development-plan.md).

## Required: agent-work-mem

agentFTP requires [`agent-work-mem`](https://github.com/daystar7777/agent-work-mem)
in each project root before runtime commands can operate.

agent-work-mem gives agents a local working memory through `AIMemory/`. agentFTP
extends that memory model across hosts: outgoing handoffs are recorded locally,
incoming handoffs are recorded remotely, and reports can travel back as
structured memory instead of disappearing into a chat transcript.

`agentftp bootstrap` checks for agent-work-mem. If it is missing, agentFTP asks
whether to install/setup it first. If you decline, agentFTP intentionally stops
instead of running without memory and handoff records.

## Install

```powershell
pipx install git+https://github.com/daystar7777/agentFTP.git
agentftp bootstrap
```

`bootstrap` checks Python, pip, Git, pipx, GitHub reachability, and
agent-work-mem AIMemory. If agent-work-mem is missing, agentFTP asks before
installing it. If you decline, runtime setup fails intentionally.

For local development:

```powershell
git clone https://github.com/daystar7777/agentFTP.git
cd agentFTP
python -m pip install -e .
agentftp doctor
```

## Quick Start

On the receiving machine, run slave mode from the folder you want to share:

```powershell
cd my-project
agentftp bootstrap
agentftp slave
```

The slave prints its local/LAN/Tailscale addresses. The default port is `7171`,
and the current folder becomes the root. A master cannot browse outside it.

On the sending machine, save the connection and open the browser master UI:

```powershell
cd my-project
agentftp bootstrap
agentftp connect lab 100.64.1.20
agentftp master lab
```

The browser opens automatically. The left panel shows the remote slave folder;
the right panel shows your local folder. Select files or folders and transfer
them in either direction.

## Usage Examples

### 1. Browser-Based File/Folder Transfer

Use this when a human wants to browse both sides and move files visually.

```mermaid
flowchart LR
  Local["Master host\nBrowser UI\nLocal folder"] -->|"upload files/folders"| Remote["Slave host\nagentftp slave\nShared root"]
  Remote -->|"download files/folders"| Local
  Local -. "saved alias ::lab" .- Remote
```

```powershell
# Slave host
cd project-to-share
agentftp bootstrap
agentftp slave

# Master host
cd my-project
agentftp bootstrap
agentftp connect lab 100.64.1.20
agentftp master lab
```

### 2. Headless Project Push Or Pull

Use this when an agent or script should transfer a folder without opening the UI.

```mermaid
sequenceDiagram
  participant M as Master Agent
  participant S as Slave Host
  M->>S: agentftp push lab ./project /incoming
  S-->>M: resumable upload status
  M->>S: chunks + final hash
  S-->>M: session summary
```

```powershell
agentftp push lab ./project /incoming
agentftp pull lab /result ./received
```

### 3. Remote Agent Handoff With Report

Use this when the remote side should receive files, understand the task, and
send back a structured report.

```mermaid
flowchart LR
  A["Master agent\nAIMemory"] -->|"handoff + files"| B["Remote slave\nAIMemory"]
  B -->|"worker executes explicit task"| C["Remote result"]
  C -->|"STATUS_REPORT"| A
```

```powershell
# Send project plus intent
agentftp handoff lab ./project "Review this project and report the test result." --expect-report "Summary and next steps"

# On the remote host
agentftp worker --execute ask

# Or send a manual report
agentftp report master <handoff-id> "Tests passed. Suggested next step: release."
```

### 4. Agent Swarm Foundation

Use this pattern when one coordinator wants to hand off different folders or
tasks to multiple trusted hosts.

```mermaid
flowchart TB
  Coordinator["Coordinator agent\nagent-work-mem"] -->|"handoff frontend"| WorkerA["::frontend\nworker host"]
  Coordinator -->|"handoff tests"| WorkerB["::tests\nworker host"]
  Coordinator -->|"handoff docs"| WorkerC["::docs\nworker host"]
  WorkerA -->|"report"| Coordinator
  WorkerB -->|"report"| Coordinator
  WorkerC -->|"report"| Coordinator
```

```powershell
agentftp connect frontend 100.64.1.21
agentftp connect tests 100.64.1.22
agentftp connect docs 100.64.1.23

agentftp handoff frontend ./web "Review UI changes and report risks." --expect-report "Findings"
agentftp handoff tests ./project "Run the test suite and report failures." --expect-report "Test result"
agentftp tell docs "Review README and suggest clearer examples." --expect-report "Doc suggestions"
```

## HTTPS

For safer cross-host use, start the slave with a self-signed certificate:

```powershell
agentftp slave --tls self-signed
```

The slave prints an HTTPS URL and SHA-256 fingerprint. Pin that fingerprint when
connecting:

```powershell
agentftp connect lab https://100.64.1.20:7171 --tls-fingerprint <sha256-fingerprint>
```

Saved aliases remember the fingerprint for later `master`, `push`, `pull`,
`sync`, `tell`, `handoff`, and `report` commands.

## Headless File Transfer

Upload a file or folder:

```powershell
agentftp push lab ./project /incoming
```

Download from the remote host:

```powershell
agentftp pull lab /result ./received
```

Plan or apply conservative folder sync:

```powershell
agentftp sync plan lab ./project /project
agentftp sync push lab ./project /project --compare-hash
agentftp sync pull lab /project ./project
```

Sync copies missing files and treats changed target files as conflicts unless
you confirm or pass `--overwrite`. Extra target files are reported as delete
candidates and are removed only when `--delete` is explicitly supplied.

## Remote Agent Handoff

Send files and a task together:

```powershell
agentftp handoff lab ./project "Review this project and report the test result." --expect-report "Summary and next steps"
```

Send only an instruction:

```powershell
agentftp tell lab "Review /incoming/project and report back." --path /incoming/project
```

Receive and inspect remote work:

```powershell
agentftp inbox
agentftp inbox --read <instruction-id>
agentftp report lab <handoff-id> "Tests passed."
```

Run a receiving worker:

```powershell
agentftp worker --once
agentftp worker --execute ask
```

Worker mode only executes commands that are explicitly written as
`agentftp-run: <command>` lines, and only when `--execute ask` or
`--execute yes` is supplied. Without `--once`, the worker polls continuously for
eligible `autoRun` handoffs.

## Saved Host Aliases

```powershell
agentftp connect lab 100.64.1.20
agentftp connections
agentftp disconnect lab
```

agentFTP stores aliases with a `::` prefix internally, such as `::lab`, to avoid
confusing saved hosts with ordinary words. You can still type `lab` in commands.

Host activity is recorded in `AIMemory/agentftp_hosts/<name>.md`, while detailed
transfer logs stay under `.agentftp/`.

## Security Model

agentFTP is built around conservative defaults:

- all file operations are confined to the selected root folder,
- delete is immediate and requires explicit user action,
- bearer tokens can be scoped with `read`, `write`, `delete`, and `handoff`,
- login attempts and requests are rate-limited,
- JSON bodies and transfer chunks have size limits,
- HTTPS supports self-signed and manually provided certificates,
- firewall opening is opt-in through `--firewall ask|yes|no`.

Example scoped connection:

```powershell
agentftp connect reviewer 100.64.1.20 --scopes read,handoff
```

Read the full security notes in [docs/security.md](docs/security.md).

## Transfer State

High-volume transfer details are kept out of AIMemory:

```text
.agentftp/
  logs/
  sessions/
  plans/
.agentftp_partial/
```

Transfers are resumable, logs rotate by size, and cancelled transfers leave
partial files in place for a later resume. Clean stale partials with:

```powershell
agentftp cleanup --older-than-hours 24
```

See [docs/transfer-state.md](docs/transfer-state.md).

## Documentation

- [Usage scenarios](docs/usage-scenarios.md)
- [Headless handoff](docs/headless-handoff.md)
- [Security](docs/security.md)
- [Protocol](docs/protocol.md)
- [Transfer state](docs/transfer-state.md)
- [Filename normalization](docs/filename-normalization.md)
- [Bootstrap](docs/bootstrap.md)

## Development

Run the test suite:

```powershell
$env:PYTHONPATH='src'
python -m unittest discover -s tests
```

The current scenario suite covers install/bootstrap, slave/master transfer,
headless push/pull, handoff/report round trips, TLS, scoped tokens, sync,
storage preflight, cancellation, cleanup, and worker daemon behavior.

## License

MIT
