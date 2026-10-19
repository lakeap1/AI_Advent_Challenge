"""CLI entry point for the standalone support MCP service and smoke."""

import argparse
import json

from .mcp_server import main as serve_main
from .smoke import run_smoke


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m support")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("serve")
    smoke = subcommands.add_parser("smoke")
    smoke.add_argument("--db", required=True)
    args = parser.parse_args()
    if args.command == "serve":
        return serve_main()
    print(json.dumps(run_smoke(args.db), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
