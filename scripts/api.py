"""Call the gateway without placing a bearer token in shell history or arguments."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from common import ROOT, GatewayClient


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--base-url", help="Override LOCAL_WEB_GATEWAY_URL.")
    parser.add_argument("--timeout", type=float, help="Overall client socket timeout in seconds (default 1200).")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("health")
    fetch = commands.add_parser("fetch")
    fetch.add_argument("url")
    search = commands.add_parser("search")
    search.add_argument("queries", nargs="+", help="Quote each search query as a single argument.")
    search.add_argument("--max-results", type=int, default=8)
    args = parser.parse_args()
    try:
        client = GatewayClient(args.env_file, args.base_url, args.timeout)
        if args.command == "health":
            status, response = client.request("/health", authenticated=False, timeout=10)
        elif args.command == "fetch":
            status, response = client.request("/fetch", {"url": args.url})
        else:
            status, response = client.request("/search", {"queries": args.queries, "max_results": args.max_results})
        print(client.redact(json.dumps(response, indent=2, ensure_ascii=False)))
        if status != 200:
            print(f"Gateway returned HTTP {status}.", file=sys.stderr)
            return 1
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Request failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
