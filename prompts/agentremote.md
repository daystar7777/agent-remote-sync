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

If the command appears to run old code, `agentremote doctor --root <project>`
must be the first diagnostic. It shows the executable path, imported package
path, detected checkout, AIMemory status, and registered local processes. If
the imported checkout differs from the intended repository, stop old
agentremote processes and reinstall from the intended checkout with
`python -m pip install -e .`.

You can print the packaged onboarding prompt with:

```powershell
agentremote onboarding --ko
```

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
3. By default, slave/daemon starts an embedded auto-worker. It processes pending
   `autoRun` messages on startup. Use `--no-auto-worker` only when the user
   wants manual inbox review.
4. For natural-language handoffs, include a trusted
   `--worker-agent-command <command>` so the receiving host can actually ask its
   local agent to do the task and write a report.
5. Let the user set the session password.
6. If HTTPS is enabled, report the displayed fingerprint.
7. Report the displayed connection addresses and port.

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
6. `ask`, `tell`, `handoff`, and `call` send `autoRun` instructions by default.
   Use `--no-auto-run` only when the user asks for manual inbox review.
7. After receiving a handoff, the default embedded worker processes eligible
   `autoRun` items first. It only executes explicit `agentremote-run:` lines
   unless the receiver configured `--worker-agent-command`.
8. For manual receiver-side processing, use `agentremote worker --once`. It
   dry-runs by default and only executes explicit `agentremote-run:` lines when
   `--execute ask` or `--execute yes` is supplied.
9. For automatic report return, include `--callback-alias <alias>` only when that
   alias is already saved on the receiving host. Never place passwords or bearer
   tokens in handoff text.

Do not use `--wait-report` as if it were remote execution. `--wait-report` only
waits for a STATUS_REPORT that arrives back in the current project. In current
builds, slave/daemon starts an embedded worker by default and processes pending
`autoRun` inbox messages on startup, but report return still requires one of
these:

1. the receiver has a saved callback alias back to this host, or
2. a human/agent on the receiver will manually inspect the inbox, do the work,
   and send a report back, or
3. the receiver writes the expected result file and the requester pulls it.

If a handoff appears stuck:

```powershell
agentremote calls list --root <project>
agentremote calls show <call-id> --root <project>
agentremote calls wait <call-id> --root <project> --timeout 300
agentremote status --root .
agentremote processes --root .
```

Then ask the receiver-side agent, from the project root that started
slave/daemon, to inspect or manually process the item:

```powershell
agentremote inbox
agentremote inbox --read <instruction-id>
agentremote worker --once --execute ask
```

For unattended receiver-side processing, slave/daemon starts an embedded worker
by default. On startup it processes pending `autoRun` inbox messages before
idling. Use `--no-auto-worker` only when the user wants manual inbox review:

```powershell
agentremote daemon serve --root <project>
agentremote daemon serve --root <project> --no-auto-worker
```

This only runs explicit `agentremote-run:` lines. For natural-language handoffs
with no explicit command, the receiver must also configure a local agent bridge:

```powershell
agentremote daemon serve --root <project> --worker-agent-command "<trusted local agent bridge command>"
```

The bridge command receives `AGENTREMOTE_BRIDGE_INPUT` and must write a markdown
report to `AGENTREMOTE_BRIDGE_OUTPUT` or print a report to stdout. Do not imply
that `--auto-run` alone wakes a remote LLM; it only marks the instruction as
eligible for a receiver-side worker. `ask`, `tell`, `handoff`, and `call` now
send auto-run instructions by default; use `--no-auto-run` for manual inbox-only
delivery.

Avoid remote destinations under `.agentremote_*`; those are protocol-reserved.
For human-readable handoff attachments, use a project path such as
`/Project/AIMemory/incoming_handoffs`.

For safe project sync, prefer:

```powershell
agentremote sync-project <host> <remote-dir> --local <project> --dry-run --include-memory --profile unity-python-llm
```

Add `--yes` only after reviewing the plan. Use `--all-files` only when the user
explicitly wants default generated/secret/volatile excludes disabled.
