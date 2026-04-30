# agent-remote-sync Bootstrap

agent-remote-sync should be installed by an agent only after the host is ready enough to
run it safely.

agent-remote-sync requires agent-work-mem AIMemory before runtime commands are useful.
The package is intentionally built as a multi-host extension of
agent-work-mem: file transfer moves project state, while AIMemory records the
handoff intent and reports on both sides.

## Command

```powershell
agent-remote-sync bootstrap
agent-remote-sync bootstrap --install ask
agent-remote-sync bootstrap --install yes
agent-remote-sync bootstrap --install no
```

`ask` is the default. Missing installable prerequisites are installed only after
explicit user approval. `yes` is for pre-approved automation. `no` is report-only
and never changes the machine.

## Checks

Required:

- Python 3.10+
- pip
- Git
- agent-work-mem AIMemory in the project root

Recommended:

- pipx
- GitHub network reachability
- agent runtime marker

## Install Behavior

- `agent-work-mem`: creates `AIMemory/` in the project root.
- `pipx`: installs with `python -m pip install --user pipx` and runs
  `python -m pipx ensurepath`.
- `git`: uses the host package manager when one is detectable.

Python itself cannot be installed by a Python program when Python is missing.
The agent should instruct the user to install Python 3.10+ first, then rerun
bootstrap.

If agent-work-mem is missing and the user declines installation, agent-remote-sync should
stop setup instead of running without local/remote memory records.

## Agent Flow

When a user says "install agent-remote-sync":

1. Ensure Python 3.10+ is available.
2. Clone or install the repo.
3. Run `agent-remote-sync bootstrap --install ask`.
4. If the user approves missing prerequisites, install/setup them.
5. Run `agent-remote-sync doctor`.
6. Report what is ready and what still needs manual action.
