# agent-remote-sync Security Model

## Root Confinement

Slave mode exposes only the folder where it was started, unless `--root` is
provided. The API rejects:

- absolute paths from the remote client,
- `..` traversal,
- Windows drive path escapes,
- symlink escapes outside the configured root.

## Authentication

The slave password is set at startup and kept in memory only. The prototype uses
a challenge-response login and bearer token sessions.

Login attempts are rate-limited per client IP. Repeated failed logins
temporarily block that IP.

Bearer tokens can be scoped at login time. The default token keeps the normal
master behavior and grants all scopes. Automation can request a narrower token:

```powershell
agentremote connect reviewer 100.64.1.20 --scopes read,handoff
```

Current scopes:

- `read`: browse, stat, tree, storage, and download,
- `write`: create folders and upload/rename/move files,
- `delete`: delete files or folders,
- `handoff`: send inbox instructions and STATUS_REPORT handoffs.

## Stored Connection Tokens

Saved connections store bearer tokens in the local agent-remote-sync config directory so
agents can reuse aliases without asking for the pairing password every time. On
POSIX systems, agent-remote-sync makes a best-effort attempt to keep the config directory
owner-only and `connections.json` readable/writable only by the owner. On
Windows, protect the file with the normal user profile permissions.

Treat saved tokens like local secrets. Use `agentremote disconnect <name>` when a
host is no longer trusted, and prefer HTTPS or a private VPN such as Tailscale
when tokens cross the network.

## Transport Encryption

Slave mode supports native HTTPS:

```powershell
agentremote slave --tls self-signed
agentremote slave --tls manual --cert-file ./cert.pem --key-file ./key.pem
```

`self-signed` generates a local certificate and stores the private key under the
agent-remote-sync config directory, not inside the exposed slave root. The slave prints a
SHA-256 fingerprint. Pin that fingerprint from the master side:

```powershell
agentremote connect lab https://100.64.1.20:7171 --tls-fingerprint <sha256-fingerprint>
```

Saved connections retain the fingerprint, so later `master`, `push`, `pull`,
`tell`, `handoff`, and `report` commands can verify the same certificate.

`--tls-insecure` exists only for trusted test networks. It skips certificate
verification and should not be used for real cross-host handoffs.

Plain HTTP is still useful on `127.0.0.1`, trusted LANs, or VPNs such as
Tailscale, but HTTPS is preferred when handoffs include sensitive project state
or bearer tokens.

## Firewall

Slave mode can help open the local firewall:

```powershell
agentremote slave --firewall ask
agentremote slave --firewall yes
agentremote slave --firewall no
```

`ask` is the default. It prompts before changing firewall rules. `yes` attempts
to open the configured TCP port immediately and may require administrator/root
privileges. `no` never changes firewall rules.

Supported automatic rules:

- Windows: `netsh advfirewall`
- Linux: `ufw` or `firewall-cmd` when available
- macOS: manual configuration required for now

Only open the port on trusted networks. Prefer VPNs such as Tailscale for
cross-network use.

## Flooding And DoS

agent-remote-sync has application-level protections:

- per-IP rate limits for unauthenticated and authenticated requests,
- temporary IP block after repeated failed login attempts,
- maximum concurrent request limit,
- socket timeouts for slow clients,
- JSON request body size limit,
- upload/download chunk size limits,
- optional `--panic-on-flood` shutdown after sustained overload.

Authenticated transfer endpoints use a separate, higher default bucket
(`30000` requests per minute per client IP) so project syncs with thousands of
small files do not look like abuse. This bucket still requires a valid bearer
token and the right token scope; memory, CPU, and bandwidth pressure are bounded
by the concurrent request limit, chunk size caps, storage checks, and OS/network
controls. Operators can lower or raise it when starting a node:

```powershell
agentremote slave --authenticated-transfer-per-minute 12000
agentremote daemon serve --authenticated-transfer-per-minute 12000
agentremote share --authenticated-transfer-per-minute 12000
```

These protections reduce accidental overload and small flooding attacks. They
do not replace OS/network-level DDoS protection. A volumetric DDoS must be
handled by firewall rules, VPN/private networking, reverse proxies, or upstream
network controls.

`--panic-on-flood` is intentionally optional: it can protect local resources, but
an attacker can also trigger it to make the service unavailable.

## Dangerous Operations

The master can delete, rename, move, upload, and download. UI confirmation is
required before destructive operations. Delete is immediate and does not move to
trash. A token without `delete` cannot call delete endpoints even when the
password was valid at login time.

## Swarm Whitelist And Route Metadata

The swarm controller can maintain local whitelist and route metadata:

```powershell
agentremote policy allow lab --note "trusted worker"
agentremote policy allow-tailscale
agentremote policy deny old-build-box
agentremote route set lab 100.64.1.20 7171 --priority 10
agentremote route list
agentremote topology show
```

This data is stored under the local agent-remote-sync config directory as non-secret
metadata. It is meant to make controller decisions visible and repeatable, and
to prepare for multi-hop or latency-aware routing. It is not a substitute for
authentication, scoped bearer tokens, HTTPS/VPN transport, or OS firewall rules.

The slave HTTP server can enforce the same whitelist with `--policy warn` or
`--policy strict`. `policy allow-tailscale` registers Tailscale's default
`100.64.0.0/10` IPv4 and `fd7a:115c:a1e0::/48` IPv6 tailnet ranges as CIDR
entries. Use it only when the server is intended to trust authenticated devices
arriving over the tailnet; password/token authentication and scoped bearer
tokens are still required.

## Daemon Profiles And Service Plans

Daemon profiles are stored under the local agent-remote-sync config directory as
metadata: profile name, project root, bind host, port, and timestamps. They must
not contain pairing passwords, bearer tokens, approval tokens, or command
fingerprints.

The v0.1 `agentremote daemon install` and `agentremote daemon uninstall` commands are
dry-run planners. They render the Windows Task Scheduler, macOS LaunchAgent, or
Linux `systemd --user` shape without mutating the OS. Generated daemon commands
use `--password-env AGENTREMOTE_DAEMON_PASSWORD`; configure that environment value
through the OS service manager or a local secret wrapper instead of writing the
password into the profile, README, shell history, or process command line.

Treat any file that provides `AGENTREMOTE_DAEMON_PASSWORD` as a local secret. On
Unix-like systems, keep it mode `0600` and inside a private directory.

## Approval Mode

Project roots can opt into approval gates for sensitive local, remote, and
worker operations:

```powershell
agentremote approvals policy --root .
agentremote approvals policy --root . --mode ask
agentremote approvals policy --root . --mode strict
agentremote approvals policy --root . --mode deny
agentremote approvals policy --root . --mode auto
```

The modes are:

- `auto`: default compatibility mode; no approval gate is created.
- `ask`: prompts only for high-risk or sensitive actions.
- `strict`: prompts for every write/delete-style action.
- `deny`: rejects sensitive actions immediately.

Gated actions currently include worker command execution, process stop/forget,
local delete, remote delete, and approval/whitelist policy changes. When a gate
is hit, an approval request is written to `.agentremote/approvals` and can be
approved or denied from the CLI or master dashboard:

```powershell
agentremote approvals list --root .
agentremote approvals approve <id> --root .
agentremote approvals deny <id> --root .
```

Approval records are mirrored into AIMemory as swarm events, but approval tokens
are not persisted and raw details are sanitized before being journaled. The
dashboard displays origin type/node, target node, risk level, and a summary so a
local controller can distinguish local-agent requests from remote-agent
requests. In `ask` or `strict` mode, headless and worker flows can pause until a
separate controller or CLI approves the request.

## Approval Prompts On The Slave

The slave is a console process, and worker execution happens on the receiver
host. If `agentremote worker --execute ask` is used, or if the underlying agent
runtime is configured to ask for filesystem/shell/network permission, execution
waits on that slave console. The master cannot approve that prompt remotely.

This is intentional for supervised receivers, but it is a common source of
"stuck" headless handoffs. For unattended operation, configure the slave-side
agent policy deliberately, keep the project root narrow, use scoped tokens, and
run only explicit `agentremote-run:` commands from trusted senders.

## Worker Command Policy

`agentremote-run:` is only an execution marker; it is not enough by itself to make a
command eligible for execution. Receiver-side workers use a project-local
allowlist at `.agentremote/worker-policy.json`:

```powershell
agentremote worker-policy init --root .
agentremote worker-policy allow python-tests python --args-pattern "*pytest*" --timeout 300 --max-stdout 20000 --root .
agentremote worker-policy templates --root .
agentremote worker-policy apply-template python-compile --root .
agentremote worker-policy list --root .
```

The worker first rejects hard-blocked command fragments, then checks the
allowlist. Commands without a matching rule are recorded as policy-blocked and
are not executed. A matching rule can cap timeout and stdout size for the
command. Rules execute with `shell=False` by default; `--shell` is explicit and
should be reserved for commands that require shell builtins or shell expansion.
The worker policy and approval mode are complementary: policy decides whether
the command is eligible at all, while approval decides whether a sensitive
eligible action may proceed now.

The `network`, `cwdPattern`, and `envAllowlist` rule fields are metadata-only in
v0.1. They document intent for humans and future policy engines, but they do not
currently sandbox commands or enforce network/filesystem/environment isolation.

## Partial Files

Incomplete uploads are stored in `.agentremote_partial`. This folder is reserved by
agent-remote-sync and hidden from normal listings. Cancelled or interrupted transfers
leave partial files in place for resume; stale partials can be removed with
`agentremote cleanup --older-than-hours 24`.

## Storage And Permission Failures

Disk-full, permission, read-only filesystem, and path-shape failures are returned
as structured errors rather than raw tracebacks:

- `insufficient_storage`
- `permission_denied`
- `read_only_filesystem`
- `not_directory`
- `storage_error`

Transfer sessions record these as `failed` in `.agentremote/sessions`. The slave is
quiet by default and only returns structured errors to the caller; use
`agentremote slave --verbose` for console request logs while debugging.

## Current Residual Risks

- Challenge-response authentication still allows offline guessing if traffic is
  captured; use strong session passwords and encrypted/private transport.
- Bearer tokens are not safe on unencrypted networks.
- Certificate pinning protects saved self-signed connections, but first-use
  fingerprint trust still depends on the user verifying the printed value.
- DDoS resistance is limited to application-level throttling.
- Token scopes reduce accidental authority but do not make bearer tokens safe on
  unencrypted networks.
