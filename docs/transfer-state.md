# Transfer State, Logging, and Failure Policy

agent-remote-sync keeps high-volume transfer detail out of AIMemory.

## State Layout

Runtime transfer state is project-local:

```text
.agent_remote_sync/
  logs/
    transfer-YYYYMMDD.jsonl
  sessions/
    push-YYYYMMDD-HHMMSS-xxxxxxxx.json
  plans/
```

`.agent_remote_sync` is reserved, hidden from normal remote listings, and ignored by Git.

## AIMemory Boundary

AIMemory records only human-useful summaries:

- operation type,
- remote alias or endpoint,
- files/bytes totals,
- session id,
- detail log path.

File-level events stay in `.agent_remote_sync/logs`. Chunk-level events are not logged by
default because they can be very high volume and slow large transfers.

## Log Rotation

Transfer logs are JSONL and rotate by size:

- default active log: `.agent_remote_sync/logs/transfer-YYYYMMDD.jsonl`,
- default max file size: `10 MB`,
- default retained files: `5`.

The logger writes buffered file-level events such as:

```json
{"event":"file_completed","session":"push-...","source":"/a.txt","target":"/incoming/a.txt","size":1234}
```

## Sessions

Each push/pull/sync operation gets a session file. Session files are small and
contain aggregate state:

- `running`, `completed`, or `failed`,
- total files/bytes,
- completed files/bytes,
- current detail log path,
- structured error code when failed.

Sessions are the resume and report boundary. A later transfer may resume from
remote/local partial files when path, size, and hash expectations still match;
the previous session id is not required.

Browser jobs can be cancelled. Cancellation stops the running job and marks it
`cancelled`; partial files are intentionally preserved so the next matching
transfer can continue from the saved offset.

## Sync Plans

`agent-remote-sync sync plan`, `sync push`, and `sync pull` write JSON plans under
`.agent_remote_sync/plans/`. A plan records:

- source and target roots,
- files to copy,
- conflicts that require overwrite approval,
- delete candidates that would be removed by a future mirror policy,
- skipped files that already match.

Plans are intentionally separate from high-volume logs. AIMemory host history
keeps only the plan path, session id, and aggregate counts.

## Failure Policy

agent-remote-sync distinguishes failures that the receiver can decide locally from
failures that require the master/user to decide.

Before writes start, transfer plans are checked against destination free space:

- push/sync push/upload checks remote free space,
- pull/sync pull/download checks local free space,
- partial files reduce the remaining byte estimate.

This preflight catches the obvious "not enough room" case. If filesystem
allocation overhead or atomic replace behavior still exhausts the destination
while writing, the write-time storage error is logged and reported too.

The master browser uses reusable transfer plans before starting jobs. Plans are
kept in the local master process and include conflicts, remaining bytes,
destination storage, warnings, and a `planId`. Jobs can then execute the same
plan instead of recomputing the user-facing preview.

Receiver can retry locally:

- transient network disconnect,
- partial upload/download offset mismatch that confirms a chunk already landed,
- upload finish calls whose success response was lost,
- temporary server busy.

Master/user must decide:

- overwrite conflicts,
- delete/mirror actions,
- TLS fingerprint changes,
- token expiry requiring reconnect,
- insufficient disk space,
- permission denied,
- read-only filesystem,
- ambiguous filename normalization.

These become structured errors such as:

- `insufficient_storage`,
- `permission_denied`,
- `read_only_filesystem`,
- `not_directory`,
- `conflicts`,
- `bad_token`,
- `tls_untrusted`.

In worker flows, blocked failures are reported as `STATUS_REPORT` handoffs
rather than guessed around. The slave remains quiet by default: it returns
structured errors to the caller and stores logs, while `agent-remote-sync slave --verbose`
enables console request logs for debugging.

## Permission Model

agent-remote-sync enforces permissions at both token and command levels:

- token scopes split `read`, `write`, `delete`, and `handoff`,
- push/sync push can write but not delete unless `--delete` exists and the token
  has `delete`,
- pull/sync pull can read but not overwrite without confirmation or
  `--overwrite`,
- `sync --delete` applies file delete candidates only after explicit opt-in,
- worker executes only explicit `agent-remote-sync-run:` lines,
- destructive commands require a future explicit policy layer.

There is intentionally no `execute` token scope yet. Worker execution remains a
local receiver-side decision controlled by `agent-remote-sync worker --execute ...`.

## Cleanup

Stale partial files can be removed without touching AIMemory or completed
sessions:

```powershell
agent-remote-sync cleanup --older-than-hours 24
```

The command only scans `.agent_remote_sync_partial/` under the selected root and reports
removed file count and freed bytes.
