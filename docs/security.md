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
agent-remote-sync connect reviewer 100.64.1.20 --scopes read,handoff
```

Current scopes:

- `read`: browse, stat, tree, storage, and download,
- `write`: create folders and upload/rename/move files,
- `delete`: delete files or folders,
- `handoff`: send inbox instructions and STATUS_REPORT handoffs.

## Transport Encryption

Slave mode supports native HTTPS:

```powershell
agent-remote-sync slave --tls self-signed
agent-remote-sync slave --tls manual --cert-file ./cert.pem --key-file ./key.pem
```

`self-signed` generates a local certificate and stores the private key under the
agent-remote-sync config directory, not inside the exposed slave root. The slave prints a
SHA-256 fingerprint. Pin that fingerprint from the master side:

```powershell
agent-remote-sync connect lab https://100.64.1.20:7171 --tls-fingerprint <sha256-fingerprint>
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
agent-remote-sync slave --firewall ask
agent-remote-sync slave --firewall yes
agent-remote-sync slave --firewall no
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

## Approval Prompts On The Slave

The slave is a console process, and worker execution happens on the receiver
host. If `agent-remote-sync worker --execute ask` is used, or if the underlying agent
runtime is configured to ask for filesystem/shell/network permission, execution
waits on that slave console. The master cannot approve that prompt remotely.

This is intentional for supervised receivers, but it is a common source of
"stuck" headless handoffs. For unattended operation, configure the slave-side
agent policy deliberately, keep the project root narrow, use scoped tokens, and
run only explicit `agent-remote-sync-run:` commands from trusted senders.

## Partial Files

Incomplete uploads are stored in `.agent_remote_sync_partial`. This folder is reserved by
agent-remote-sync and hidden from normal listings. Cancelled or interrupted transfers
leave partial files in place for resume; stale partials can be removed with
`agent-remote-sync cleanup --older-than-hours 24`.

## Storage And Permission Failures

Disk-full, permission, read-only filesystem, and path-shape failures are returned
as structured errors rather than raw tracebacks:

- `insufficient_storage`
- `permission_denied`
- `read_only_filesystem`
- `not_directory`
- `storage_error`

Transfer sessions record these as `failed` in `.agent_remote_sync/sessions`. The slave is
quiet by default and only returns structured errors to the caller; use
`agent-remote-sync slave --verbose` for console request logs while debugging.

## Current Residual Risks

- Challenge-response authentication still allows offline guessing if traffic is
  captured; use strong session passwords and encrypted/private transport.
- Bearer tokens are not safe on unencrypted networks.
- Certificate pinning protects saved self-signed connections, but first-use
  fingerprint trust still depends on the user verifying the printed value.
- DDoS resistance is limited to application-level throttling.
- Token scopes reduce accidental authority but do not make bearer tokens safe on
  unencrypted networks.
