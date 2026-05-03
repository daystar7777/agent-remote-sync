# agentremote Agent Prompt

You can operate agentremote for cross-machine folder transfer.

## Install

When asked to install from GitHub:

1. Check Python 3.10+ is available.
2. Clone or install the repository with `pipx install git+<repo-url>`.
3. Run `agentremote bootstrap --install ask`.
4. If bootstrap asks to install Git, pipx, or agent-work-mem, explain the impact
   and proceed only if the user approves.
5. Run `agentremote doctor`.
6. Report the installed version and any missing runtime requirements.

## Saved Connections

When the user gives a short target name such as `XXX`, first check whether it is
already a saved connection:

```powershell
agentremote connections
```

If it is not saved, create it:

```powershell
agentremote connect XXX <host-or-ip>
```

For HTTPS self-signed slaves, use the printed `https://...` URL and
fingerprint:

```powershell
agentremote connect XXX https://<host>:7171 --tls-fingerprint <sha256>
```

This prompts for the slave password once and stores a session token. agentremote
normalizes the visible name to `::XXX`; use this prefix in summaries so humans
can recognize that it is a saved remote host. Do not ask for the password again
while the saved connection works. If the token is rejected because the slave
restarted, reconnect. Saved HTTPS entries also retain their certificate
fingerprint.

Host-specific history is stored under `AIMemory/agentremote_hosts/`. Check that
file when the user asks what was exchanged with a specific host.

## Slave Mode

When asked to run slave mode:

1. Confirm the current working folder is the folder the user wants to expose.
2. Run `agentremote slave`, or `agentremote slave --tls self-signed` when HTTPS is requested.
3. Let the user set the session password.
4. If HTTPS is enabled, report the displayed fingerprint.
5. Report the displayed connection addresses and port.

The slave root is the current folder unless the user explicitly provides another
root.

## Master Mode

When asked to run master mode:

1. Run `agentremote master <host> <port>`.
2. Ask the user for the slave password if not already provided.
3. Wait for the browser UI URL.
4. Tell the user the UI opened automatically, or provide the local URL if it did
   not.

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
- Use `.agentremote/sessions/<id>.json` and `.agentremote/logs/*.jsonl` for detailed
  transfer diagnostics. Keep AIMemory summaries short.

## Headless and Handoff

When asked to sync or hand off work without a browser:

1. Prefer headless `push`, `pull`, or `sync` commands when available.
2. For a simple file transfer, use `agentremote push <connection-name> <local> <remote-dir>`.
3. For instruction-only handoff, use `agentremote tell <connection-name> "<task>"`.
4. For file plus instruction, prefer
   `agentremote handoff <connection-name> <local> "<task>"`.
5. For full handoff, package task intent, notes, files, and expected report into
   a handoff manifest.
6. Do not enable automatic execution on the receiver unless the user explicitly
   asks for auto mode.
7. After receiving a handoff, inspect the manifest and report the intended
   action before running commands unless auto mode is already active.
8. For receiver-side processing, use `agentremote worker --once` first. It dry-runs
   by default and only executes explicit `agentremote-run:` lines when
   `--execute ask` or `--execute yes` is supplied.
9. For automatic report return, include `--callback-alias <alias>` only when that
   alias is already saved on the receiving host. Never place passwords or bearer
   tokens in handoff text.
