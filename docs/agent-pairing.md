# Agent Pairing

agent-remote-sync is easiest to understand as an agent-launched transport layer, not only
as a command you type by hand.

## What Pairing Means

Pairing is the relationship between:

- the project folder,
- the local agent working in that folder,
- `AIMemory/` from agent-work-mem,
- the running `agentremote slave`, `master`, or `worker` process,
- the saved remote alias such as `::lab`.

When an agent starts agent-remote-sync from its current project root, file transfers and
handoffs are recorded in the same AIMemory context the agent is already using.
That makes remote reports, host history, and future handoffs easier to reason
about.

Running `agentremote slave` from a plain terminal still works for basic file
transfer. It is just less clear which agent/profile owns later handoff work.

## Recommended Agent-Led Flow

On the receiving host, ask the local agent:

```text
In this project folder, run agent-remote-sync slave mode.
Install agent-work-mem if it asks.
Ask me for the pairing password.
Use port 7171.
Do not open the firewall unless I approve it.
```

The agent should run:

```powershell
agentremote bootstrap
agentremote slave
```

On Windows, if the agent launches this from a non-interactive background
session, agent-remote-sync opens a visible console window by default. That console is the
real slave process. Keep it open while the master connects, and type `q` there
to stop it. Use `--console no` only when you intentionally want the process to
stay in the current non-interactive session.

On the sending host, ask the local agent:

```text
Connect to the remote agent-remote-sync slave as "lab",
then open the master browser UI.
Ask me for the password if needed.
```

The agent should run:

```powershell
agentremote connect lab <ip-or-url>
agentremote master lab
```

The same flow can be described with the newer swarm vocabulary:

```powershell
agentremote daemon serve
agentremote controller gui lab
agentremote nodes list
agentremote topology show
agentremote policy allow lab --note "trusted worker"
agentremote route set lab 100.64.1.20 7171 --priority 10
```

These commands currently wrap or summarize the existing slave/master and saved
connection behavior. Policy and route entries are local controller-side
metadata for now: useful for visibility and future automation, but not yet
enforced by the slave wire protocol. They are intended to become the stable
vocabulary for future swarm daemon, controller, topology, policy, and routing
features.

For headless handoff:

```text
Use agent-remote-sync to send ./project to lab and ask the remote agent to run tests.
Wait for a report if one comes back.
```

The agent can use:

```powershell
agentremote handoff lab ./project "Run tests and report failures." --expect-report "Test result"
```

## Expected First-Run Prompts

These prompts are normal. They are not a sign that pairing failed.

| Prompt | Why it appears | Recommended answer |
| --- | --- | --- |
| `agent-work-mem AIMemory... Install/setup it now?` | agent-remote-sync requires AIMemory to pair handoffs and reports with the local agent/project. | Say yes unless you intentionally do not want agent-remote-sync to run in this project. |
| `Pairing password` | The slave needs a temporary password so a master can authenticate and receive a session token. | Enter a one-time strong password and share it only with the connecting master/user. |
| `Open the local firewall...` | The slave may need inbound TCP access on port `7171`. | Say no for local testing; say yes only on trusted networks or when you understand the network path. |
| `Trust this certificate...` | A self-signed HTTPS slave needs fingerprint pinning on first connection. | Compare the printed fingerprint on the slave, then approve if it matches. |
| `Run these agentremote-run commands?` | `worker --execute ask` requires local approval before executing remote handoff commands. | Approve only after inspecting the plan. Use `--execute yes` only on trusted unattended workers. |

## Why `agentremote slave` Can Feel Like It Stops

`agentremote slave` is intentionally interactive on first use:

1. It verifies agent-work-mem.
2. It asks for a pairing password.
3. It may ask about the firewall.
4. It then stays open as a console server and waits for master connections.

That final waiting state is success. The slave console should remain open while
the master connects. It prints local, LAN, and Tailscale-style addresses when
available.

If stdin closes because the process was started by a non-interactive runner,
agent-remote-sync no longer treats that as a stop command. It keeps the slave/master
server alive. On Windows, the preferred path is the auto-opened visible console;
on hosts where no console can be opened, terminate the process through the host
process manager.

If the user wants fewer prompts, have the agent run bootstrap first:

```powershell
agentremote bootstrap --install ask
```

Then run the slave:

```powershell
agentremote slave --firewall no
```

Use `--firewall yes` only when opening the port is already approved.

Console behavior can be controlled explicitly:

```powershell
agentremote slave --console auto
agentremote slave --console yes
agentremote slave --console no
agentremote master lab --console auto
```

`auto` is the default. It opens a visible Windows console when agent-remote-sync detects
that it was launched without an interactive terminal. `yes` forces that attempt.
`no` disables console relaunch.

## Worker Approval Caveat

The master cannot answer prompts on the slave host. If a remote worker is using
`--execute ask`, or if the underlying agent runtime asks for permission before
shell/filesystem/network access, execution waits on that slave console.

For unattended remote workers, configure the slave-side policy deliberately:

- use a narrow project root,
- use scoped tokens,
- accept only explicit `agentremote-run:` commands,
- use HTTPS or a trusted private network,
- keep `--execute yes` limited to trusted senders and trusted tasks.
