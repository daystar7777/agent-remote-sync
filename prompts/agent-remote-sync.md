# agent-remote-sync Agent Prompt

You can operate agent-remote-sync for cross-machine folder transfer.

## Install

When asked to install from GitHub:

1. Check Python 3.10+ is available.
2. Clone or install the repository with `pipx install git+<repo-url>`.
3. Run `agent-remote-sync bootstrap --install ask`.
4. If bootstrap asks to install Git, pipx, or agent-work-mem, explain the impact
   and proceed only if the user approves.
5. Run `agent-remote-sync doctor`.
6. Report the installed version and any missing runtime requirements.

## Saved Connections

When the user gives a short target name such as `XXX`, first check whether it is
already a saved connection:

```powershell
agent-remote-sync connections
```

If it is not saved, create it:

```powershell
agent-remote-sync connect XXX <host-or-ip>
```

For HTTPS self-signed slaves, use the printed `https://...` URL and
fingerprint:

```powershell
agent-remote-sync connect XXX https://<host>:7171 --tls-fingerprint <sha256>
```

This prompts for the slave password once and stores a session token. agent-remote-sync
normalizes the visible name to `::XXX`; use this prefix in summaries so humans
can recognize that it is a saved remote host. Do not ask for the password again
while the saved connection works. If the token is rejected because the slave
restarted, reconnect. Saved HTTPS entries also retain their certificate
fingerprint.

Host-specific history is stored under `AIMemory/agent_remote_sync_hosts/`. Check that
file when the user asks what was exchanged with a specific host.

## Process Dashboard

Before starting or diagnosing long-running master/slave work, inspect local
agent-remote-sync process state:

```powershell
agent-remote-sync ps
agent-remote-sync status
```

Use the local dashboard when the user wants a visual overview of project-level
connections, transfer history, or handoff history:

```powershell
agent-remote-sync dashboard
```

The dashboard is localhost-only. It shows running master/slave/dashboard
processes, saved channels, project roots, recent transfers, recent handoffs,
and received instructions. If the dashboard is offline, start it rather than
guessing from memory. For machine-readable agent work, use:

```powershell
agent-remote-sync ps --json
agent-remote-sync status --json
agent-remote-sync history --root <project> --json
```

The dashboard can stop other local agent-remote-sync processes after confirmation, but
this can interrupt active transfers or handoffs. Confirm with the user before
stopping a process. In CLI, use `agent-remote-sync stop <instance-id>` interactively, or
`--yes` only when the user has explicitly confirmed the exact process.

## Slave Mode

When asked to run slave mode:

1. Confirm the current working folder is the folder the user wants to expose.
2. Run `agent-remote-sync slave`, or `agent-remote-sync slave --tls self-signed` when HTTPS is requested.
3. Let the user set the session password.
4. If HTTPS is enabled, report the displayed fingerprint.
5. Report the displayed connection addresses and port.

The slave root is the current folder unless the user explicitly provides another
root.

## Master Mode

When asked to run master mode:

1. Run `agent-remote-sync master <host> <port>`.
2. Ask the user for the slave password if not already provided.
3. Wait for the browser UI URL.
4. Tell the user the UI opened automatically, or provide the local URL if it did
   not.
5. If multiple projects are active, use `agent-remote-sync ps` or the dashboard to avoid
   confusing one project's master UI with another.

## Safety Rules

- Never expose a sensitive parent folder by accident.
- Prefer Tailscale or trusted private networks.
- Prefer HTTPS with fingerprint pinning when project files, handoffs, or bearer
  tokens cross machines.
- Confirm before delete, overwrite, rename, or move operations.
- If transfer fails, retry; resumable transfer should continue from partial
  data.
- For `insufficient_storage`, `permission_denied`, `read_only_filesystem`,
  `bad_token`, conflicts, or TLS fingerprint changes, stop and ask the user or
  send a blocked STATUS_REPORT instead of guessing.
- Use `.agent_remote_sync/sessions/<id>.json` and `.agent_remote_sync/logs/*.jsonl` for detailed
  transfer diagnostics. Keep AIMemory summaries short.
- Prefer `agent-remote-sync status --json` for a current view of active channels,
  process health, transfer sessions, handoffs, and received instructions.

## Headless and Handoff

When asked to sync or hand off work without a browser:

1. Prefer headless `push`, `pull`, or `sync` commands when available.
2. For a simple file transfer, use `agent-remote-sync push <connection-name> <local> <remote-dir>`.
3. For instruction-only handoff, use `agent-remote-sync tell <connection-name> "<task>"`.
4. For file plus instruction, prefer
   `agent-remote-sync handoff <connection-name> <local> "<task>"`.
5. For full handoff, package task intent, notes, files, and expected report into
   a handoff manifest.
6. Do not enable automatic execution on the receiver unless the user explicitly
   asks for auto mode.
7. After receiving a handoff, inspect the manifest and report the intended
   action before running commands unless auto mode is already active.
8. For receiver-side processing, use `agent-remote-sync worker --once` first. It dry-runs
   by default and only executes explicit `agent-remote-sync-run:` lines when
   `--execute ask` or `--execute yes` is supplied.
9. For automatic report return, include `--callback-alias <alias>` only when that
   alias is already saved on the receiving host. Never place passwords or bearer
   tokens in handoff text.
