# agent-remote-sync / agentremote v0.1 Release Notes

This release candidate packages the first usable cross-host file/folder
transfer and remote-agent handoff flow on top of agent-work-mem.

## Highlights

- Easy cross-host file/folder transfer through `agentremote share`, `agentremote open`,
  and the browser GUI.
- Headless project sync and handoff commands for agent-to-agent work:
  `push`, `pull`, `sync-project`, `handoff`, `tell`, `call`, and `report`.
- Swarm-oriented controller views for topology, route health, process state,
  approvals, worker policy, and recent handoff activity.
- Dashboard daemon profiles for repeated project shares. Profiles store root,
  host, port, and display name only; passwords and bearer tokens are never saved
  in profile JSON.
- Defensive security defaults: whitelist checks, scoped tokens, approval mode,
  worker command allowlists, rate limits, panic-on-flood behavior, TLS guidance,
  and Tailscale whitelist import helpers.
- AIMemory integration for local and remote handoff records, swarm journals,
  and project-level state.

## Install

agent-work-mem is required. Install it first, then install this package from the
GitHub repository:

```powershell
python -m pip install git+https://github.com/daystar7777/agent-work-mem.git
python -m pip install git+https://github.com/daystar7777/agent-remote-sync.git
agentremote setup --root . --install
```

## Compatibility

- Python 3.10 or newer.
- Windows, macOS, and Linux are supported by the Python runtime.
- Direct LAN/VPN/Tailscale routes are recommended for v0.1.
- HTTPS is supported when certificates, reverse proxy, or explicit fingerprints
  are configured.

## Known Limitations

- `agentremote daemon install` and `agentremote daemon uninstall` are dry-run planners
  in v0.1. They print Windows Task Scheduler, macOS LaunchAgent, or Linux
  `systemd --user` specs but do not create or remove OS services yet.
- Relay pairing, QR onboarding, and the paid mobile controller are future
  features. They are intentionally not enabled in this release candidate.
- Flood and DDoS handling is local to the daemon. Rate limits, panic shutdown,
  and whitelist policy reduce risk, but internet-facing deployments still need
  firewall, VPN, reverse proxy, or cloud edge protection.
- Worker policy `network`, `cwd`, and `env` fields are recorded as metadata in
  v0.1. Command allow/deny and timeout enforcement are active.
- Topology and route health are controller-side coordination records. Offline
  machines appear stale/offline after registry heartbeats and probes age out.

## Verification

The release candidate should pass:

```powershell
python -m compileall -q src tests deepseek-test smoke.py
python -m pytest tests deepseek-test -q
python smoke.py
python -m unittest discover -s tests
python -m pip wheel . -w dist-wheel --no-deps
```
