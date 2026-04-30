# agent-remote-sync Usage Scenarios

agent-remote-sync is the CLI/product name. Its role is agent-work-mem multi-host handoff
transport: it moves files, task intent, and reports between hosts while writing
AICP-compatible records on both sides.

## Scenario Matrix

| ID | Scenario | User phrasing | Expected result | Test coverage |
|----|----------|---------------|-----------------|---------------|
| S01 | Install memory | "Install agent-remote-sync here." | AIMemory exists before agent-remote-sync operations continue. | `test_s01_install_work_mem_is_idempotent` |
| S02 | Start slave | "Run agent-remote-sync slave mode here." | Current folder is strict remote root; reserved folders are hidden. | `test_s02_slave_lists_root_and_hides_reserved_state` |
| S03 | Connect alias | "Connect to this host as lab." | Saved alias is `::lab`; password is replaced by session token. | `test_s03_connect_alias_token_reuse_and_disconnect` |
| S04 | Master browser transfer | "Open master mode to lab." | Browser API can upload and download through the master job queue. | `test_s04_master_browser_api_upload_download` |
| S05 | Simple headless push | "Send KKK folder to XXX." | Folder is uploaded resumably; host history records the push. | `test_s05_headless_push_folder_records_host_history` |
| S06 | Simple headless pull | "Fetch result folder from XXX." | Folder is downloaded resumably; host history records the pull. | `test_s06_headless_pull_folder_records_host_history` |
| S07 | Conflict handling | "Send it again." | Existing target conflicts abort unless overwrite is explicit. | `test_s07_conflict_aborts_without_overwrite_and_succeeds_with_overwrite` |
| S08 | Instruction only | "Tell XXX to do ZZZ." | Local outgoing AICP handoff and remote external AICP handoff are created. | `test_s08_instruction_only_handoff_records_both_sides` |
| S09 | File plus instruction | "Send LLL and tell XXX to do ZZZ." | File is pushed, then instruction references the remote path. | `test_s09_file_plus_instruction_links_remote_path` |
| S10 | Full round trip | "Tell XXX to do it and report back." | Slave sends STATUS_REPORT back; master receives external report. | `test_s10_full_handoff_report_round_trip` |
| S11 | Remote file operations | "Create, rename, move, delete on remote." | API performs full-permission operations inside root. | `test_s11_remote_file_operations_stay_inside_root` |
| S12 | Safety failures | "Try to access outside root." | Traversal and reserved paths are rejected. | `test_s12_security_rejects_traversal_and_reserved_paths` |
| S13 | Missing memory | "Run without agent-work-mem." | Non-interactive operations fail instead of silently proceeding. | `test_s13_missing_work_mem_blocks_runtime_operations` |
| S14 | Slave model execution | "Run this on the remote agent." | Handoff metadata records the slave-starting model as executor. | `test_s14_slave_model_is_recorded_for_remote_execution` |
| S15 | Security throttles | "What if someone floods it?" | Oversized requests and repeated bad logins are rejected. | `test_s15_security_limits_reject_oversized_json_upload_and_login_flood` |
| S16 | Firewall UX | "Open the firewall for agent-remote-sync." | Firewall changes require explicit ask/yes and use OS-specific commands. | `test_s16_firewall_skip_and_bad_port_are_safe` |
| S17 | Bootstrap prerequisites | "Install agent-remote-sync here." | Bootstrap reports Python/pip/Git/pipx/AIMemory and can set up AIMemory. | `test_s17_bootstrap_installs_work_mem_and_reports_checks` |
| S18 | Cross-OS filenames | "Send these Korean filenames between Windows and Mac." | NFC/NFD variants resolve correctly and new files are written in NFC. | `test_s18_unicode_filename_normalization_across_os_styles` |
| S19 | Handoff command | "Send LLL and tell XXX to do ZZZ." | `agent-remote-sync handoff` pushes the file and sends an AICP handoff referencing the remote path. | `test_s19_handoff_command_pushes_file_and_sends_instruction` |
| S20 | HTTPS self-signed | "Run this securely over HTTPS." | A self-signed slave works when the master pins the certificate fingerprint. | `test_s20_https_self_signed_fingerprint_allows_transfer` |
| S21 | Inbox claim | "Take this handoff." | `agent-remote-sync inbox --claim` marks a received instruction as claimed and records AIMemory. | `test_s21_inbox_claim_marks_instruction_and_records_memory` |
| S22 | Worker dry-run | "Auto-run this, but inspect first." | `agent-remote-sync worker --once` claims autoRun work and records a plan without executing. | `test_s22_worker_dry_run_claims_autorun_without_executing` |
| S23 | Worker execute | "Run this explicit command." | Worker executes `agent-remote-sync-run:` commands and writes a local STATUS_REPORT. | `test_s23_worker_executes_explicit_command_and_writes_local_report` |
| S24 | Worker callback report | "Run it and report back." | Worker sends STATUS_REPORT back through a receiver-side saved callback alias. | `test_s24_worker_sends_report_to_callback_alias` |
| S25 | Transfer log rotation | "Syncs will create a lot of logs." | File-level JSONL logs rotate and old logs are pruned. | `test_s25_transfer_logger_rotates_and_prunes` |
| S26 | Transfer session summary | "Show what happened without bloating AIMemory." | Push writes `.agent_remote_sync` session/log files and AIMemory stores only summary pointers. | `test_s26_headless_push_writes_session_log_and_memory_summary` |
| S27 | Storage failure report | "What if disk or permissions fail?" | Remote storage failures become structured errors and failed transfer sessions. | `test_s27_remote_storage_errors_are_structured_and_logged` |
| S28 | Sync plan | "Show me what would sync." | Plan reports copied files, conflicts, skipped files, and delete candidates. | `test_s28_sync_plan_detects_copy_conflict_and_delete_candidates` |
| S29 | Sync push | "Sync this project to XXX." | Missing files upload resumably; session, plan, log, and host history are recorded. | `test_s29_sync_push_uploads_missing_files_and_records_session` |
| S30 | Sync conflict policy | "Sync over an existing changed file." | Changed targets abort unless overwrite is explicit. | `test_s30_sync_push_conflict_requires_overwrite` |
| S31 | Sync pull | "Sync result folder back from XXX." | Missing remote files download into the local folder and record host history. | `test_s31_sync_pull_downloads_missing_files_and_records_session` |
| S32 | Missing remote sync source | "Pull a folder that does not exist." | Pull reports `not_found` instead of treating it as an empty sync. | `test_s32_sync_pull_missing_remote_reports_not_found` |
| S33 | Sync CLI plan | "agent-remote-sync sync plan ..." | CLI writes a plan file under `.agent_remote_sync/plans` and prints the JSON plan. | `test_s33_sync_plan_cli_writes_plan_file` |
| S34 | GUI disk space | "Show whether both disks have room." | Master UI APIs report local and remote total/free disk space for display. | `test_s34_gui_storage_api_reports_local_and_remote_free_space` |
| S35 | Headless upload space preflight | "Upload only if the receiver has room." | Headless push fails with `insufficient_storage` before writing remote files. | `test_s35_headless_push_preflight_blocks_insufficient_remote_space` |
| S36 | GUI upload space preflight | "Upload from browser only if remote has room." | Master upload job reports an error before transferring. | `test_s36_master_upload_job_preflight_reports_remote_space_error` |
| S37 | Headless download space preflight | "Download only if my disk has room." | Headless pull fails with `insufficient_storage` before writing local files. | `test_s37_headless_pull_preflight_blocks_insufficient_local_space` |
| S38 | GUI download space preflight | "Download from browser only if local has room." | Master download job reports an error before transferring. | `test_s38_master_download_job_preflight_reports_local_space_error` |
| S39 | GUI upload transfer plan | "Show me the upload plan first." | Master creates a reusable upload plan and executes by `planId`. | `test_s39_master_upload_plan_previews_and_reuses_plan_id` |
| S40 | GUI download transfer plan | "Show conflicts and space before download." | Master creates a download plan with conflicts and destination storage. | `test_s40_master_download_plan_reports_conflicts_and_space` |
| S41 | CLI pull memory root | "Pull into a new folder from this project." | CLI pull records session and host history in the current AIMemory project, not the destination folder. | `test_s41_cli_pull_records_memory_in_current_project_not_destination` |
| S42 | Sync hash compare | "Ignore timestamp-only changes." | `--compare-hash` removes same-content mtime conflicts. | `test_s42_sync_compare_hash_avoids_same_content_mtime_conflict` |
| S43 | Sync delete apply | "Mirror this folder and remove stale remote files." | `sync push --delete` applies remote delete candidates after explicit opt-in. | `test_s43_sync_push_delete_applies_remote_delete_candidates` |
| S44 | Sync empty dirs | "Keep empty folders too." | Sync creates missing empty directories. | `test_s44_sync_push_preserves_empty_directories` |

## Natural Language Flows

### Simple Transfer

```powershell
agent-remote-sync connect lab 100.64.1.20
agent-remote-sync push lab ./KKK /incoming
```

The alias is shown and stored as `::lab`. Users may say `lab`; summaries should
say `::lab`.

### HTTPS Connection

```powershell
agent-remote-sync slave --tls self-signed
agent-remote-sync connect lab https://100.64.1.20:7171 --tls-fingerprint <sha256-fingerprint>
agent-remote-sync handoff lab ./LLL "Use the uploaded file and report back."
```

The saved `::lab` entry stores the session token and TLS fingerprint.

### Instruction Only

```powershell
agent-remote-sync tell lab "Run the parser tests and report failures."
```

This writes:

- local `AIMemory/handoff_*.md` with direction `local`,
- remote `AIMemory/handoff_*.md` with direction `external`,
- remote `.agent_remote_sync_inbox/<id>/manifest.json`.

### File Plus Instruction

```powershell
agent-remote-sync handoff lab ./LLL "Use the uploaded file to do ZZZ and report back."
```

### Full Round Trip

1. Master pushes files if needed.
2. Master sends an AICP handoff via `handoff` or `tell`.
3. Slave agent reads `agent-remote-sync inbox` or runs `agent-remote-sync worker --once`.
4. Slave performs the task manually or executes explicit `agent-remote-sync-run:` lines.
5. Slave sends `agent-remote-sync report master <handoff-id> "<result>"`, or worker sends it through `--callback-alias`.
6. Master sees the returned report through `agent-remote-sync inbox` and AIMemory.

### Transfer State

```text
.agent_remote_sync/logs/transfer-YYYYMMDD.jsonl
.agent_remote_sync/sessions/<session-id>.json
.agent_remote_sync/plans/<sync-plan-id>.json
```

AIMemory records only session summaries and pointers to these files. Detailed
file-level events are kept out of work logs and rotate automatically.

### Storage Preflight

Before push/upload, agent-remote-sync compares the remaining upload bytes against the
remote free space. Before pull/download, it compares the remaining download
bytes against local free space. Existing partial files reduce the remaining byte
estimate. If the destination is already too small, the operation fails with
`insufficient_storage` before starting file writes.

Filesystem allocation overhead, compression, sparse files, and atomic replace
behavior can still make the real disk use differ from the byte estimate. Those
cases are handled by the write-time storage error path and reported with the
same structured failure policy.

### Browser Transfer Plan

The master browser asks for `/api/plan/upload` or `/api/plan/download` before
starting a job. The plan shows:

- selected file and directory counts,
- total and remaining transfer bytes,
- destination free space,
- conflicts requiring overwrite,
- warnings such as `insufficient_storage`.

The UI then starts `/api/jobs/upload` or `/api/jobs/download` with the plan id.
Multiple local or remote rows can be selected with Ctrl/Cmd-click.
If a transfer is taking too long or the wrong files were selected, the footer
cancel button marks the running job as `cancelled` and keeps partial files for a
future resume.

### Scoped Connections

```powershell
agent-remote-sync connect reviewer 100.64.1.20 --scopes read,handoff
agent-remote-sync tell reviewer "Please inspect /project and report back." --path /project
```

Scoped tokens limit the receiver API surface even after successful password
authentication. A read/handoff token can browse and send instructions but cannot
upload, rename, move, or delete.

### Worker Daemon

```powershell
agent-remote-sync worker --execute ask
agent-remote-sync worker --execute yes --max-iterations 10
```

Without `--once`, the worker polls for received `autoRun` handoffs. It still
executes only explicit `agent-remote-sync-run:` command lines, and `--execute ask`
requires an interactive terminal before running them.

### Conservative Sync

```powershell
agent-remote-sync sync plan lab ./project /project
agent-remote-sync sync push lab ./project /project --compare-hash
agent-remote-sync sync pull lab /project ./project
agent-remote-sync sync push lab ./project /project --delete
```

Sync copies missing files and treats changed target files as conflicts unless
the caller confirms overwrite or passes `--overwrite`. Extra target files are
reported as delete candidates. Destructive mirror deletion is applied only when
`--delete` is explicitly supplied; interactive terminals ask for final delete
confirmation. `--compare-hash` hashes same-size changed files to avoid false
mtime conflicts across Windows, macOS, and Linux.

### Cleanup

```powershell
agent-remote-sync cleanup --older-than-hours 24
```

Cleanup removes stale `.agent_remote_sync_partial` files after interrupted or cancelled
transfers. It does not delete completed sessions, transfer logs, AIMemory, or
ordinary project files.

## Current Limits

- Worker auto-execution is limited to explicit `agent-remote-sync-run:` command lines.
- Broader natural-language task execution still requires a local agent to inspect
  the plan and act.
- HTTPS is implemented for self-signed or manual certificates, but public CA
  trust and richer certificate management still need more UX work.
- Sync delete currently applies file delete candidates only; empty target
  directory deletion still needs a richer mirror policy.
