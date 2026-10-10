"""Verify health, bearer authentication, browser fetch, and search response shape."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from common import ROOT, GatewayClient


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--base-url")
    parser.add_argument("--wait-seconds", type=float, default=120, help="Maximum time to wait for the gateway health endpoint.")
    parser.add_argument("--fetch-url", default="https://example.com")
    parser.add_argument("--query", default="example domain IANA")
    args = parser.parse_args()
    try:
        client = GatewayClient(args.env_file, args.base_url)
        deadline = time.monotonic() + max(0, args.wait_seconds)
        while True:
            try:
                status, health = client.request("/health", authenticated=False, timeout=5)
                if status == 200 and health.get("status") == "ok":
                    break
            except RuntimeError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("Gateway did not become healthy. Inspect docker compose ps and logs.")
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        print("PASS: /health")

        for path, payload in (("/search", {"queries": [args.query]}), ("/fetch", {"url": args.fetch_url})):
            status, _ = client.request(path, payload, authenticated=False, timeout=10)
            require(status == 401, f"{path} did not reject an unauthenticated request with HTTP 401.")
        print("PASS: bearer authentication required by /search and /fetch")

        status, fetched = client.request("/fetch", {"url": args.fetch_url})
        require(status == 200, f"/fetch returned HTTP {status}.")
        content = fetched.get("content")
        require(isinstance(content, dict), "/fetch content is not an object.")
        assert isinstance(content, dict)
        require(content.get("success") is True, "Crawl4AI reported an unsuccessful fetch.")
        markdown = content.get("markdown")
        require(isinstance(markdown, str) and bool(markdown.strip()), "Fetched Markdown is empty.")
        assert isinstance(markdown, str)
        require(fetched.get("url") == args.fetch_url and fetched.get("truncated") is False, "/fetch response shape differs from the documented API.")
        print(f"PASS: /fetch returned {len(markdown)} Markdown characters")

        status, searched = client.request("/search", {"queries": [args.query], "max_results": 3})
        require(status == 200, f"/search returned HTTP {status}.")
        sources = searched.get("sources")
        require(isinstance(sources, list) and 0 < len(sources) <= 3, "Search returned no usable results, or exceeded its requested limit.")
        assert isinstance(sources, list)
        require(searched.get("truncated") is False, "/search truncated flag differs from the documented API.")
        for source in sources:
            require(isinstance(source, dict) and isinstance(source.get("url"), str) and isinstance(source.get("snippet"), str) and "title" in source, "A search source does not have the documented url/title/snippet shape.")
        print(f"PASS: /search returned {len(sources)} source(s)")
        print("Smoke checks passed. Live search quality depends on upstream engines and target pages.")
    except (OSError, ValueError, RuntimeError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
