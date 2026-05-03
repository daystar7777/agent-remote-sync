from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from agentremote.cli import main as cli_main
from agentremote.workmem import install_work_mem


def main() -> int:
    parser = argparse.ArgumentParser(description="Start an agentremote Docker lab node")
    parser.add_argument("--root", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7171)
    parser.add_argument("--password", required=True)
    parser.add_argument("--node-name", required=True)
    args = parser.parse_args()

    root = Path(args.root).resolve()
    config = Path(args.config).resolve()
    root.mkdir(parents=True, exist_ok=True)
    config.mkdir(parents=True, exist_ok=True)
    os.environ["AGENTREMOTE_HOME"] = str(config)
    install_work_mem(root)

    print(
        f"agentremote docker node {args.node_name} serving {root} "
        f"on {args.host}:{args.port}",
        flush=True,
    )
    cli_main(
        [
            "share",
            "--root",
            str(root),
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--password",
            args.password,
            "--node-name",
            args.node_name,
            "--firewall",
            "no",
            "--console",
            "no",
        ]
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"docker node failed: {exc}", file=sys.stderr, flush=True)
        raise
