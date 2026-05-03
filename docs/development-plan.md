# agent-remote-sync v1 Development Plan

## Product Goal

Build the multi-host transport extension for agent-work-mem: an OS-neutral
transfer and handoff tool that agents can install from GitHub and run with
natural commands:

- "Install agent-remote-sync from GitHub."
- "Run agent-remote-sync slave mode."
- "Run agent-remote-sync master mode with this IP, port, and password."

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
- agent-remote-sync is the product/CLI name; the project category is
  agent-work-mem multi-host handoff transport.

## Implementation Phases

### Phase 0: Repository Skeleton

- Python package scaffold for fast iteration and easy `pipx` install from
  GitHub.
- CLI entrypoint: `agentremote`.
- Docs: README, protocol, security, prompt guide.
- Swarm vocabulary scaffold: `daemon serve/status`, `controller gui`,
  `nodes list`, `topology show`, `policy list`, and `route list` as
  backward-compatible wrappers/views over the current slave/master/connection
  model.
- Local swarm metadata: editable controller-side whitelist and route
  preferences in `swarm.json`, with explicit route priority but no slave-side
  enforcement yet.

### Phase 1: Functional Prototype

- Slave HTTP server with password challenge and bearer session token.
- Strict root confinement for all file operations.
- Master local browser UI served from `127.0.0.1`.
- Remote/local two-pane file browser.
- Upload/download/delete/rename/mkdir/move.
- Sequential chunk upload/download with `.agentremote_partial` resume state.

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

### Phase 2.5: Swarm Security and Route Health

- Slave/daemon-side whitelist policy so each node protects itself before
  issuing a session token.
- Named client identity in login payloads, while remaining compatible with
  older clients that do not send a name.
- Route probing with compact latency/health memory and deterministic route
  selection: priority first, health/latency only as a tie breaker.
- AIMemory swarm journal under `AIMemory/swarm/` for compact node, route,
  policy, and probe events that agents can inspect.
- Safe read-modify-write handling for hotter swarm metadata such as
  `swarm.json`, preserving unrelated route and whitelist entries.
- Carry forward Claude hardening notes as regression boundaries:
  transactional handoff cleanup, explicit-only `agentremote-run:` execution,
  atomic config saves, log rotation discipline, zero-byte EOF downloads, and
  panic-on-flood behavior must remain intact.

### Phase 2.6: Daemon Lifecycle and Repeat Operation

- Daemon profiles for repeated project shares: root, host, port, and safe
  display name only.
- Nested and compatibility CLI forms:
  `agentremote daemon profile save/list/remove` and
  `agentremote daemon profile-save/profile-list/profile-remove`.
- `agentremote daemon status` correlates saved profiles with local process
  registry entries so users can see whether a project daemon is running.
- Service install/uninstall remain dry-run planners in v0.1. They render
  Windows Task Scheduler, macOS LaunchAgent, or Linux `systemd --user` specs
  without changing the OS.
- Service commands read the pairing password through
  `--password-env AGENTREMOTE_DAEMON_PASSWORD`; profiles never store plaintext
  passwords or bearer tokens.

### Phase 2.7: Release Candidate Readiness

- Shared daemon profile persistence lives in `agentremote.daemon_profiles` so the
  CLI, dashboard API, and swarm dashboard data use the same sanitize/load/save
  behavior.
- Release notes and README limitations call out v0.1 boundaries clearly:
  daemon service commands are dry-run planners, relay/mobile remain future
  features, worker policy metadata is not full sandboxing, and internet-facing
  deployments still need network-layer DDoS protection.
- Fresh wheel/install smoke verification is part of the release checklist before
  tagging or publishing to GitHub.

### Phase 3: Distribution

- GitHub Actions builds.
- Signed release artifacts if a compiled implementation is added later.
- `pipx install git+...` and release download install instructions.

## Future Go/Rust Track

The architecture should remain portable enough to rewrite the runtime in Go or
Rust later for single-binary distribution. The protocol and UI can remain
compatible.

## Headless and Handoff Direction

Headless mode turns agent-remote-sync into a coordination layer, not only a file browser.

### Headless Commands

- `agentremote push <host> <local-path> <remote-dir>`
- `agentremote pull <host> <remote-path> <local-dir>`
- `agentremote handoff <host> <local-path> "<task>"`
- `agentremote sync plan <host> <local-path> <remote-dir>`
- `agentremote sync push <host> <local-path> <remote-dir>`
- `agentremote sync pull <host> <remote-dir> <local-path>`

Default conflict behavior remains safe: abort and report conflicts unless the
caller explicitly confirms overwrite.

### Handoff Commands

- `agentremote tell <host> "<task>"` sends instruction-only handoff.
- `agentremote handoff <host> <local-path> "<task>"` pushes files and sends the
  handoff in one command.
- `agentremote inbox --claim <instruction-id>` claims received work.
- `agentremote worker --once` dry-runs one received autoRun handoff.
- `agentremote worker --once --execute ask` executes explicit `agentremote-run:`
  command lines after approval.
- `agentremote report <host> <handoff-id> "<result>"` sends a STATUS_REPORT back.

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
- explicit command execution through `agentremote-run:` lines only
- local STATUS_REPORT generation
- optional callback report delivery through a receiver-side saved alias
- worker daemon loop (`agentremote worker --interval N`): poll, claim, execute, repeat
- worker command allowlist policy framework in `.agentremote/worker-policy.json`
- explicit whitelist rules with command name, args pattern (fnmatch), timeout,
  stdout cap, shell/no-shell toggle, CWD pattern, and env allowlist
- built-in policy templates: python-tests, python-compile, git-readonly,
  echo-safe, node-tests
- CLI management: `agentremote worker-policy init|list|allow|remove|templates|apply-template`
- master API endpoints for remote policy inspection and template application
- blocked commands still enforced via substring safety fragments in addition
  to the allowlist
- secret redaction in policy display (password, token, credential, API key patterns)

Still planned:

- full OS-level sandboxing (network isolation, filesystem confinement beyond
  CWD pattern metadata),
- model/agent adapter that can ask the local LLM agent to perform natural
  language tasks after claim,
- report notification UI in master mode.

## TLS Direction

Implemented v0.1 TLS primitives:

- `agentremote slave --tls self-signed`
- `agentremote slave --tls manual --cert-file ... --key-file ...`
- HTTPS clients with system CA verification, CA file override, fingerprint
  pinning, or explicit insecure test mode
- saved connection fingerprint reuse

Still planned:

- friendlier first-use trust UI,
- certificate rotation commands,
- optional local CA generation for a user's agent fleet.
