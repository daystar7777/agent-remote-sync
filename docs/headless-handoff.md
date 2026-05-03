# Headless Sync and Handoff

agent-remote-sync extends agent-work-mem across hosts. It supports both human-driven
browser transfers and agent-driven headless transfers, with AICP handoff records
written on both machines.

## Headless Transfer

Headless mode is for commands that can run without opening the browser UI:

```powershell
agentremote connect lab 100.64.1.20
agentremote push lab ./project /incoming/project
agentremote pull lab /result ./received
agentremote handoff lab ./LLL "Use the uploaded file to do ZZZ and report back."
```

The same resumable chunk protocol is used. If a target file already exists, the
command must ask for confirmation in interactive terminals or fail with a clear
conflict list in non-interactive terminals unless `--overwrite` is supplied.

## Sync

Sync is a higher-level operation over push/pull:

```powershell
agentremote sync plan lab ./project /project
agentremote sync push lab ./project /project --compare-hash
agentremote sync pull lab /project ./project
agentremote sync push lab ./project /project --delete
```

The implemented sync is conservative:

- compare path, size, and modified time,
- optionally hash same-size changed files with `--compare-hash`,
- copy missing files,
- create missing empty directories,
- treat changed target files as conflicts unless overwrite is confirmed,
- never delete missing files by default,
- delete file candidates only when `--delete` is explicit,
- write a JSON plan before applying changes,
- record sessions/logs under `.agentremote`.

## Handoff

The natural-language target experience is:

- "Use agent-remote-sync to send folder `KKK` to `XXX`."
- "Use agent-remote-sync to send file `LLL` to `XXX`, tell it to do `ZZZ`, then report
  back."
- "Use agent-remote-sync to tell `XXX` to do `ZZZ`."

`XXX` should usually be a saved connection alias:

```powershell
agentremote connect XXX 100.64.1.20
```

Aliases are normalized with a `::` prefix. `agentremote connect lab ...` creates
`::lab`; later commands may use either `lab` or `::lab`.

The first authentication stores a session token for that slave runtime. Later
commands can use `XXX` without asking for the password again. If the slave is
restarted, the token is invalid and the agent should reconnect.

## Relationship To agent-work-mem

agent-remote-sync is best understood as the multi-host extension of agent-work-mem:

- local side records outgoing `HANDOFF` or `STATUS_REPORT` AICP files,
- remote side records incoming `HANDOFF_RECEIVED` files marked as external,
- remote work can produce a report handoff,
- the report is sent back and displayed/recorded on the original master.

agent-remote-sync must not rewrite or delete existing AIMemory entries. It appends
namespaced AICP files and work.log events only.

## Host History

Each saved host gets a project-local history file:

```text
AIMemory/agentremote_hosts/lab.md
```

This file records:

- connection creation and reconnects,
- push/pull summaries,
- outgoing handoff files,
- incoming report files,
- bytes transferred when known.

This makes it possible to ask "what did we exchange with ::lab?" without reading
the entire work log.

A handoff is a portable task package:

```text
.agentremote_handoff/
  manifest.json
  notes.md
  files/
```

`manifest.json` should include:

```json
{
  "version": 1,
  "id": "handoff-id",
  "createdAt": "2026-04-29T00:00:00Z",
  "from": "agent-a",
  "task": "Implement the parser and run tests.",
  "workspaceRoot": ".",
  "entrypoint": "",
  "expectedReport": "Summarize changed files, tests, and blockers.",
  "autoRun": false
}
```

With `autoRun: true`, a receiving agent may execute the handoff instructions
after validating the package and root path. After completion, it can create a
report handoff and send it back.

## Instruction Inbox

The minimal v1 primitive is an instruction inbox on the slave:

```powershell
agentremote tell XXX "Run tests for /incoming/project and report failures." --path /incoming/project
agentremote inbox
agentremote inbox --read <instruction-id>
```

For file plus instruction, the agent can perform:

```powershell
agentremote handoff XXX ./LLL "Use the uploaded file to do ZZZ and report back."
```

`agentremote handoff` is the preferred high-level command for user requests such as
"send this file and tell the other agent what to do." Internally it performs a
resumable `push`, records the remote path, then sends the AICP handoff with that
path attached.

The receiving side stores the manifest under `.agentremote_inbox`. A local agent on
that machine can read it, perform the task, and later send a report instruction
or report package back.

## Worker Mode

The receiver can claim and inspect work without opening the browser:

```powershell
agentremote inbox --claim <instruction-id>
agentremote worker --once
```

`worker --once` processes one `autoRun: true` instruction. By default it is a
dry run: it claims the instruction, records a worker plan in the inbox manifest,
and prints related paths plus executable command candidates.

Automatic command execution is intentionally narrow. The worker only executes
lines explicitly prefixed with `agentremote-run:`:

```text
Please run the tests.
agentremote-run: python -m unittest discover -s tests
```

Run with approval:

```powershell
agentremote worker --once --execute ask
```

The approval prompt appears on the receiver/slave console. The master cannot
answer it remotely. This is useful when a human is supervising the receiver, but
it can make a headless handoff appear stuck if nobody is watching that host.

Run non-interactively only when the receiver already trusts the sender and the
handoff:

```powershell
agentremote worker --once --execute yes
```

For unattended workers, combine non-interactive execution with a narrow project
root, scoped tokens, explicit `agentremote-run:` commands, and a trusted network or
TLS. If the underlying agent runtime has its own permission prompts, configure
those prompts on the slave host before expecting automatic remote execution.

Reports are generated as local `STATUS_REPORT` AICP handoffs. To have the
receiver send a report back automatically, the sender includes a callback alias
that already exists on the receiver:

```powershell
agentremote handoff worker ./LLL "Do ZZZ" --auto-run --callback-alias master
```

The callback alias stores only receiver-local connection data; agent-remote-sync does not
send passwords or bearer tokens inside handoff manifests.

## Remote Execution Model

Headless remote execution should default to the model/profile that started the
slave process. The slave advertises this as `executorModel`, and received
handoffs record it in metadata. This keeps ownership clear: the remote side is
executing under its own already-running agent identity, not the master's model.

## Round Trip

1. Master runs `agentremote handoff XXX ./LLL "Do ZZZ"` when files are needed.
2. agent-remote-sync pushes the file and sends a handoff referencing the remote path.
3. Master AIMemory gets an outgoing AICP handoff.
4. Slave AIMemory gets an external incoming AICP handoff and inbox manifest.
5. Slave agent runs `agentremote worker --once --execute ask` or performs the task manually.
6. Slave runs `agentremote report master <handoff-id> "<result>"`, or worker sends it through `--callback-alias`.
7. Master receives an external STATUS_REPORT handoff and displays it through
   `agentremote inbox` or future notification UI.

## Safety

Auto handoff execution is powerful. It needs explicit opt-in:

- receiver starts with `agentremote handoff receive --auto`,
- receiver claims/inspects the manifest with `agentremote worker --once`,
- commands run inside the receiver-selected project root,
- only explicit `agentremote-run:` command lines are executable,
- report includes commands run and files changed.
