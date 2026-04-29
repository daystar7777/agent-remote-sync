# agentFTP v1 Development Plan

## Product Goal

Build the multi-host transport extension for agent-work-mem: an OS-neutral
transfer and handoff tool that agents can install from GitHub and run with
natural commands:

- "Install agentFTP from GitHub."
- "Run agentFTP slave mode."
- "Run agentFTP master mode with this IP, port, and password."

## Confirmed v1 Scope

- Default port: `7171`.
- Slave mode: terminal UI, current folder as the shared root by default.
- Master mode: browser UI, opens automatically.
- Protocol: custom HTTP API, not real FTP.
- Tailscale: supported and detected when available, but not required.
- Master permissions: list, upload, download, delete, rename, move, create
  folder.
- Delete behavior: immediate delete with UI confirmation.
- Conflicts: always ask before overwrite.
- Large file resume: required.
- Cross-platform: Windows, macOS, Linux.
- Headless operation: required for agent-to-agent project sync and handoff.
- Saved connection aliases: authenticate once, then use short names such as
  `lab` or `build-server`.
- Instruction inbox: send task-only or file-plus-task handoffs.
- agentFTP is the product/CLI name; the project category is
  agent-work-mem multi-host handoff transport.

## Implementation Phases

### Phase 0: Repository Skeleton

- Python package scaffold for fast iteration and easy `pipx` install from
  GitHub.
- CLI entrypoint: `agentftp`.
- Docs: README, protocol, security, prompt guide.

### Phase 1: Functional Prototype

- Slave HTTP server with password challenge and bearer session token.
- Strict root confinement for all file operations.
- Master local browser UI served from `127.0.0.1`.
- Remote/local two-pane file browser.
- Upload/download/delete/rename/mkdir/move.
- Sequential chunk upload/download with `.agentftp_partial` resume state.

### Phase 2: v1 Hardening

- Better progress UI with resumable job polling.
- Full-folder conflict preflight.
- Headless `push`, `pull`, and `sync` commands.
- Handoff package format for agent-to-agent task transfer.
- Report package flow so a receiving agent can send completion results back.
- Hash verification for completed transfers.
- Optional TLS mode or documented reverse proxy/Tailscale guidance.
- Stronger terminal UI for slave mode.
- Cross-platform test matrix.

### Phase 3: Distribution

- GitHub Actions builds.
- Signed release artifacts if a compiled implementation is added later.
- `pipx install git+...` and release download install instructions.

## Future Go/Rust Track

The architecture should remain portable enough to rewrite the runtime in Go or
Rust later for single-binary distribution. The protocol and UI can remain
compatible.

## Headless and Handoff Direction

Headless mode turns agentFTP into a coordination layer, not only a file browser.

### Headless Commands

- `agentftp push <host> <local-path> <remote-dir>`
- `agentftp pull <host> <remote-path> <local-dir>`
- `agentftp handoff <host> <local-path> "<task>"`
- `agentftp sync plan <host> <local-path> <remote-dir>`
- `agentftp sync push <host> <local-path> <remote-dir>`
- `agentftp sync pull <host> <remote-dir> <local-path>`

Default conflict behavior remains safe: abort and report conflicts unless the
caller explicitly confirms overwrite.

### Handoff Commands

- `agentftp tell <host> "<task>"` sends instruction-only handoff.
- `agentftp handoff <host> <local-path> "<task>"` pushes files and sends the
  handoff in one command.
- `agentftp inbox --claim <instruction-id>` claims received work.
- `agentftp worker --once` dry-runs one received autoRun handoff.
- `agentftp worker --once --execute ask` executes explicit `agentftp-run:`
  command lines after approval.
- `agentftp report <host> <handoff-id> "<result>"` sends a STATUS_REPORT back.

A handoff is a folder plus a manifest:

- task objective,
- source agent notes,
- included files,
- expected command or test,
- callback/reporting instruction,
- optional auto-run permission.

The receiving agent can inspect the manifest, apply the workspace files, run the
requested work when `--auto` is enabled, then send back a report handoff.

This is the base for an agent swarm: file state, task intent, execution result,
and follow-up instructions move through the same transport.

## Worker Direction

Implemented worker primitives:

- received instruction claim states: `received -> claimed -> completed/failed/blocked`
- worker dry-run plan stored in the inbox manifest
- explicit command execution through `agentftp-run:` lines only
- local STATUS_REPORT generation
- optional callback report delivery through a receiver-side saved alias

Still planned:

- richer safe command policy and sandbox profiles,
- long-running worker daemon mode,
- model/agent adapter that can ask the local LLM agent to perform natural
  language tasks after claim,
- report notification UI in master mode.

## TLS Direction

Implemented v0.1 TLS primitives:

- `agentftp slave --tls self-signed`
- `agentftp slave --tls manual --cert-file ... --key-file ...`
- HTTPS clients with system CA verification, CA file override, fingerprint
  pinning, or explicit insecure test mode
- saved connection fingerprint reuse

Still planned:

- friendlier first-use trust UI,
- certificate rotation commands,
- optional local CA generation for a user's agent fleet.
