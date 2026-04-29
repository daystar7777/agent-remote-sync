# Agent Pairing

agentFTP is easiest to understand as an agent-launched transport layer, not only
as a command you type by hand.

## What Pairing Means

Pairing is the relationship between:

- the project folder,
- the local agent working in that folder,
- `AIMemory/` from agent-work-mem,
- the running `agentftp slave`, `master`, or `worker` process,
- the saved remote alias such as `::lab`.

When an agent starts agentFTP from its current project root, file transfers and
handoffs are recorded in the same AIMemory context the agent is already using.
That makes remote reports, host history, and future handoffs easier to reason
about.

Running `agentftp slave` from a plain terminal still works for basic file
transfer. It is just less clear which agent/profile owns later handoff work.

## Recommended Agent-Led Flow

On the receiving host, ask the local agent:

```text
In this project folder, run agentFTP slave mode.
Install agent-work-mem if it asks.
Ask me for the pairing password.
Use port 7171.
Do not open the firewall unless I approve it.
```

The agent should run:

```powershell
agentftp bootstrap
agentftp slave
```

On the sending host, ask the local agent:

```text
Connect to the remote agentFTP slave as "lab",
then open the master browser UI.
Ask me for the password if needed.
```

The agent should run:

```powershell
agentftp connect lab <ip-or-url>
agentftp master lab
```

For headless handoff:

```text
Use agentFTP to send ./project to lab and ask the remote agent to run tests.
Wait for a report if one comes back.
```

The agent can use:

```powershell
agentftp handoff lab ./project "Run tests and report failures." --expect-report "Test result"
```

## Expected First-Run Prompts

These prompts are normal. They are not a sign that pairing failed.

| Prompt | Why it appears | Recommended answer |
| --- | --- | --- |
| `agent-work-mem AIMemory... Install/setup it now?` | agentFTP requires AIMemory to pair handoffs and reports with the local agent/project. | Say yes unless you intentionally do not want agentFTP to run in this project. |
| `Pairing password` | The slave needs a temporary password so a master can authenticate and receive a session token. | Enter a one-time strong password and share it only with the connecting master/user. |
| `Open the local firewall...` | The slave may need inbound TCP access on port `7171`. | Say no for local testing; say yes only on trusted networks or when you understand the network path. |
| `Trust this certificate...` | A self-signed HTTPS slave needs fingerprint pinning on first connection. | Compare the printed fingerprint on the slave, then approve if it matches. |
| `Run these agentftp-run commands?` | `worker --execute ask` requires local approval before executing remote handoff commands. | Approve only after inspecting the plan. Use `--execute yes` only on trusted unattended workers. |

## Why `agentftp slave` Can Feel Like It Stops

`agentftp slave` is intentionally interactive on first use:

1. It verifies agent-work-mem.
2. It asks for a pairing password.
3. It may ask about the firewall.
4. It then stays open as a console server and waits for master connections.

That final waiting state is success. The slave console should remain open while
the master connects. It prints local, LAN, and Tailscale-style addresses when
available.

If the user wants fewer prompts, have the agent run bootstrap first:

```powershell
agentftp bootstrap --install ask
```

Then run the slave:

```powershell
agentftp slave --firewall no
```

Use `--firewall yes` only when opening the port is already approved.

## Worker Approval Caveat

The master cannot answer prompts on the slave host. If a remote worker is using
`--execute ask`, or if the underlying agent runtime asks for permission before
shell/filesystem/network access, execution waits on that slave console.

For unattended remote workers, configure the slave-side policy deliberately:

- use a narrow project root,
- use scoped tokens,
- accept only explicit `agentftp-run:` commands,
- use HTTPS or a trusted private network,
- keep `--execute yes` limited to trusted senders and trusted tasks.
