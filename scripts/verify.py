"""Acceptance checks for an installed gateway, using real browser and web requests.

Run inside the built gateway image; host Python and Node.js are not required.
Reports contain only check statuses and metrics, never tokens or page contents.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Any

import argparse
import base64
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from common import GatewayClient, ROOT, is_placeholder, canonical_url, valid_http_url as valid_url

STATIC_MARKER = "Static canary: violet otter"
BROWSER_MARKER = "Browser rendered: cobalt badger 7319"
CANARY_ORIGIN = "https://httpbin.org/base64/"
BLOCKED_PAGE_PHRASES = (
    "verify you are human", "verify that you are human", "checking your browser",
    "enable javascript and cookies to continue", "attention required! | cloudflare",
    "just a moment...", "unusual traffic from your computer network",
)


class VerificationError(RuntimeError):
    """A failure message authored locally, safe for reports and terminal output."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def default_canary_url() -> str:
    """Serve our exact HTML over public HTTPS without weakening egress policy.

    HTTPBin's Base64 endpoint echoes raw HTML. Only this public, bundled fixture
    is encoded in the URL; gateway secrets and installation settings are omitted.
    """
    try:
        html = (ROOT / "verification" / "canary.html").read_bytes()
    except OSError:
        raise VerificationError("Cannot read the bundled browser verification page.") from None
    encoded = base64.urlsafe_b64encode(html).decode("ascii")
    return CANARY_ORIGIN + encoded


def matches_groups(text: str, groups: list[list[str]]) -> bool:
    normalized = " ".join(text.casefold().split())
    return all(any(phrase.casefold() in normalized for phrase in group) for group in groups)


def is_blocked_page(text: str) -> bool:
    # Look at the lead so a legitimate document discussing captchas still passes.
    lead = " ".join(text[:1000].casefold().split())
    return any(phrase in lead for phrase in BLOCKED_PAGE_PHRASES)


class AcceptanceClient(GatewayClient):
    def request_details(self, path: str, payload: dict | None = None, *,
                        auth: str = "valid", timeout: float | None = None) -> tuple[int, dict, dict[str, str]]:
        headers = {"Accept": "application/json"}
        if auth == "valid":
            require(not is_placeholder(self.token), "Gateway token is missing or a placeholder.")
            headers["Authorization"] = f"Bearer {self.token}"
        elif auth == "wrong":
            headers["Authorization"] = f"Bearer invalid-{secrets.token_hex(16)}"
        elif auth != "none":
            raise ValueError("Invalid authentication test mode.")
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=headers)
        try:
            response = self.opener.open(request, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as error:
            response = error
        except (urllib.error.URLError, TimeoutError, OSError):
            raise VerificationError("Cannot reach the gateway before the request timeout.") from None
        try:
            with response:
                status = response.code
                response_headers = {key.lower(): value for key, value in response.headers.items()}
                # Limit responses to 8 MiB. Do not retain upstream text in failures.
                body = response.read(8 * 1024 * 1024 + 1)
            require(len(body) <= 8 * 1024 * 1024, "Gateway response exceeded the verification size limit.")
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise VerificationError(f"Gateway returned HTTP {status} with invalid JSON.") from None
        except (TimeoutError, OSError):
            raise VerificationError("Gateway response could not be read before the request timeout.") from None
        require(isinstance(decoded, dict), "Gateway response must be a JSON object.")
        return status, decoded, response_headers


def check_fetch(client: AcceptanceClient, url: str, groups: list[list[str]], *,
                min_chars: int = 100, browser: bool = False) -> dict:
    status, fetched, _ = client.request_details("/fetch", {"url": url})
    require(status == 200, f"Fetch returned HTTP {status}.")
    require(fetched.get("url") == url and fetched.get("truncated") is False,
            "Fetch response does not preserve the requested URL and full-content contract.")
    content = fetched.get("content")
    require(isinstance(content, dict) and content.get("success") is True,
            "Crawl4AI did not report a successful fetch.")
    assert isinstance(content, dict)
    markdown = content.get("markdown")
    require(isinstance(markdown, str) and len(markdown.strip()) >= min_chars,
            "Fetched Markdown is empty or too short to verify.")
    assert isinstance(markdown, str)
    require(not is_blocked_page(markdown), "Fetch returned an anti-bot challenge instead of the requested page.")
    require(matches_groups(markdown, groups), "Fetched Markdown is missing the expected page topic.")
    if browser:
        require(STATIC_MARKER in markdown, "Fetch is missing the known static canary content.")
        require(BROWSER_MARKER in markdown, "Fetch is missing the JavaScript-generated browser marker.")
    metrics: dict[str, Any] = {"markdown_characters": len(markdown), "browser_marker": browser}
    if browser:
        metrics["fixture_domain"] = (urlsplit(url).hostname or "").lower()
    return metrics


def check_search(client: AcceptanceClient, case: dict, *, max_results: int, snippet_limit: int) -> dict:
    status, searched, headers = client.request_details("/search", {
        "queries": [case["query"]], "max_results": max_results,
    })
    require(status == 200, f"Search returned HTTP {status}.")
    sources = searched.get("sources")
    require(isinstance(sources, list) and 0 < len(sources) <= max_results,
            "Search returned no usable sources or exceeded the requested result limit.")
    assert isinstance(sources, list)
    require(searched.get("truncated") is False, "Search response has an unexpected truncated flag.")
    try:
        enriched = int(headers["x-crawl4ai-enriched"])
        fallback = int(headers["x-crawl4ai-fallback"])
    except (KeyError, ValueError):
        raise VerificationError("Search is missing valid Crawl4AI enrichment diagnostics.") from None
    require(enriched >= 0 and fallback >= 0 and enriched + fallback == len(sources),
            "Search enrichment diagnostics do not account for every source.")
    seen: set[str] = set()
    matching_sources = []
    for source in sources:
        require(isinstance(source, dict), "A search source is not an object.")
        url, title, snippet = source.get("url"), source.get("title"), source.get("snippet")
        require(isinstance(url, str) and valid_url(url), "A search source has an invalid or unclean HTTP(S) URL.")
        require(isinstance(title, str) and bool(title.strip()), "A search source is missing a nonempty title.")
        require(isinstance(snippet, str) and bool(snippet.strip()), "A search source is missing a nonempty snippet.")
        # Original SearXNG snippets are preserved on crawl failure. Counts identify
        # fully enriched responses; mixed responses cannot identify each fallback.
        if fallback == 0:
            require(len(snippet) <= snippet_limit, "A search snippet exceeds the configured character limit.")
        canonical = canonical_url(url)
        require(canonical not in seen, "Search returned duplicate canonical source URLs.")
        seen.add(canonical)
        hostname = (urlsplit(url).hostname or "").lower().removeprefix("www.")
        expected_domains = {domain.lower().removeprefix("www.") for domain in case["domains"]}
        if hostname in expected_domains and matches_groups(f"{title} {snippet}", case["topic_groups"]):
            require(not is_blocked_page(snippet), "Search returned an anti-bot challenge as a relevant snippet.")
            matching_sources.append(source)
    require(bool(matching_sources), "Search returned no relevant source from the expected official domain.")
    # Fetch a returned source, never replace it with a separately chosen URL.
    fetch_error = None
    fetch_metrics = None
    for source in matching_sources:
        try:
            fetch_metrics = check_fetch(client, source["url"], case["fetch_groups"])
            break
        except VerificationError as error:
            fetch_error = error
    if fetch_metrics is None:
        raise fetch_error or VerificationError("No matching search source could be fetched.")
    return {"sources": len(sources), "relevant_official_sources": len(matching_sources),
            "enriched": enriched, "fallback": fallback,
            "fetched_domain": (urlsplit(source["url"]).hostname or "").lower(),
            **fetch_metrics}


def load_cases(path: Path) -> list[dict]:
    try:
        cases = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise VerificationError("Cannot read the verification test cases JSON file.") from None
    require(isinstance(cases, list) and 1 <= len(cases) <= 8, "Test cases must contain between one and eight cases.")
    for case in cases:
        require(isinstance(case, dict), "Each test case must be an object.")
        for key in ("name", "query"):
            require(isinstance(case.get(key), str) and bool(case[key].strip()), f"Test case requires {key}.")
        require(isinstance(case.get("domains"), list) and bool(case["domains"])
                and all(isinstance(domain, str) and re.fullmatch(r"[A-Za-z0-9.-]+", domain)
                        for domain in case["domains"]), "Test case requires valid expected domains.")
        for key in ("topic_groups", "fetch_groups"):
            groups = case.get(key)
            require(isinstance(groups, list) and bool(groups)
                    and all(isinstance(group, list) and bool(group)
                            and all(isinstance(phrase, str) and bool(phrase.strip()) for phrase in group)
                            for group in groups), f"Test case requires nonempty {key}.")
    return cases


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def preserve_installer_report(path: Path, report: dict) -> dict:
    """Merge only fixed installer fields; never retain arbitrary page contents."""
    require(not path.is_symlink(), "The installation report must not be a symlink.")
    if not path.is_file():
        return report
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise VerificationError("Existing installation report could not be read.") from None
    require(isinstance(previous, dict), "Existing installation report must be an object.")
    stages = previous.get("installer_stages", [])
    require(isinstance(stages, list), "Existing installer stages must be a list.")
    allowed_statuses = {"running", "passed", "failed", "verified", "verification_failed"}
    preserved_stages = []
    for stage in stages:
        require(isinstance(stage, dict) and isinstance(stage.get("stage"), str)
                and bool(re.fullmatch(r"[a-z][a-z0-9-]{0,63}", stage["stage"]))
                and stage.get("status") in allowed_statuses,
                "Existing installer stage contains invalid fields.")
        preserved_stages.append({"stage": stage["stage"], "status": stage["status"]})
    report["installer_stages"] = preserved_stages
    if previous.get("installer_status") in allowed_statuses:
        report["installer_status"] = previous["installer_status"]
    if isinstance(previous.get("updated_at"), str) and re.fullmatch(r"[0-9T:.+Z-]{10,40}", previous["updated_at"]):
        report["updated_at"] = previous["updated_at"]
    return report


class Verifier:
    def __init__(self, client: AcceptanceClient, *, canary_url: str, cases: list[dict],
                 wait_seconds: float, attempts: int, retry_delay: float,
                 max_results: int, snippet_limit: int):
        self.client = client
        self.canary_url = canary_url
        require(valid_url(self.canary_url), "Canary URL must be an HTTP(S) URL without credentials.")
        self.cases, self.wait_seconds = cases, wait_seconds
        self.attempts, self.retry_delay = attempts, retry_delay
        self.max_results, self.snippet_limit = max_results, snippet_limit
        self.report: dict[str, Any] = {"version": 1, "started_at": utc_now(), "checks": []}

    def run_check(self, name: str, operation: Callable[[], dict], *, retry: bool = False) -> dict:
        started = time.monotonic()
        result: dict[str, Any] = {"name": name, "status": "failed", "attempts": 0}
        for attempt in range(1, (self.attempts if retry else 1) + 1):
            result["attempts"] = attempt
            try:
                result["metrics"] = operation()
                result["status"] = "passed"
                result.pop("message", None)
                break
            except VerificationError as error:
                result["message"] = self.client.redact(str(error))
                if retry and attempt < self.attempts:
                    time.sleep(self.retry_delay)
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        self.report["checks"].append(result)
        label = "PASS" if result["status"] == "passed" else "FAIL"
        message = f" ({result['message']})" if "message" in result else ""
        print(self.client.redact(f"{label}  {name}{message}"), flush=True)
        return result

    def health(self) -> dict:
        deadline = time.monotonic() + self.wait_seconds
        while True:
            try:
                status, health, _ = self.client.request_details("/health", auth="none", timeout=5)
                if status == 200 and health.get("status") == "ok":
                    return {"http_status": status}
            except VerificationError:
                pass
            if time.monotonic() >= deadline:
                raise VerificationError("Gateway did not become healthy before the liveness timeout.")
            time.sleep(min(1, max(0, deadline - time.monotonic())))

    def authentication(self) -> dict:
        checks = 0
        for path, payload in (("/search", {"queries": ["installation authentication check"]}),
                              ("/fetch", {"url": self.canary_url})):
            for auth in ("none", "wrong"):
                status, _, _ = self.client.request_details(path, payload, auth=auth, timeout=10)
                require(status == 401, f"{path} did not reject {auth} bearer authentication with HTTP 401.")
                checks += 1
        return {"rejected_requests": checks}

    def run(self) -> dict:
        health = self.run_check("Gateway liveness", self.health)
        if health["status"] == "passed":
            self.run_check("Authentication rejects missing and incorrect tokens", self.authentication)
            self.run_check("Browser fetch returns known static and JavaScript content",
                           lambda: check_fetch(self.client, self.canary_url,
                                               [["gateway installation canary"]], browser=True), retry=True)
            live_checks = []
            for index, case in enumerate(self.cases, start=1):
                # Never echo user-defined query/name or upstream text into reports.
                live_checks.append(self.run_check(f"Live search and returned-source fetch {index}",
                    partial(check_search, self.client, case, max_results=self.max_results,
                            snippet_limit=self.snippet_limit), retry=True))
            enriched = sum(check.get("metrics", {}).get("enriched", 0) for check in live_checks)
            fallback = sum(check.get("metrics", {}).get("fallback", 0) for check in live_checks)

            def enrichment() -> dict:
                require(enriched > 0, "No successful live search case demonstrated Crawl4AI snippet enrichment.")
                return {"enriched": enriched, "fallback": fallback,
                        "degraded": fallback > 0}

            self.run_check("Live search uses Crawl4AI enrichment", enrichment)
        self.report["finished_at"] = utc_now()
        passed = all(check["status"] == "passed" for check in self.report["checks"])
        self.report["status"] = "passed" if passed else "failed"
        self.report["degraded"] = any(check.get("metrics", {}).get("fallback", 0) > 0
                                      for check in self.report["checks"])
        return self.report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        "This product includes software developed by UncleCode (https://x.com/unclecode) "
        "as part of the Crawl4AI project (https://github.com/unclecode/crawl4ai)."))
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--base-url", default="http://gateway:8080")
    parser.add_argument("--canary-url", help="Full public HTTP(S) fixture URL; defaults to the bundled HTML echoed by httpbin.org.")
    parser.add_argument("--report", type=Path, default=ROOT / "artifacts" / "installation-report.json")
    parser.add_argument("--test-cases", type=Path, default=ROOT / "verification" / "cases.json")
    parser.add_argument("--wait-seconds", type=float, default=30)
    parser.add_argument("--timeout", type=float, default=360)
    parser.add_argument("--attempts", type=int, choices=(1, 2, 3), default=2)
    parser.add_argument("--retry-delay", type=float, default=3)
    parser.add_argument("--max-results", type=int, choices=range(1, 6), default=3)
    args = parser.parse_args(argv)
    client = None
    try:
        for value, name, allow_zero in ((args.wait_seconds, "Readiness timeout", True),
                                        (args.timeout, "Request timeout", False),
                                        (args.retry_delay, "Retry delay", True)):
            require(math.isfinite(value) and (value >= 0 if allow_zero else value > 0),
                    f"{name} must be a finite {'nonnegative' if allow_zero else 'positive'} number.")
        client = AcceptanceClient(args.env_file, args.base_url, timeout=args.timeout)
        require(not is_placeholder(client.token), "Gateway token is missing or a placeholder.")
        snippet_limit = int(client.settings.get("SNIPPET_MAX_CHARS", "1200"))
        require(128 <= snippet_limit <= 10000, "SNIPPET_MAX_CHARS must be between 128 and 10000.")
        report = Verifier(client, canary_url=args.canary_url or default_canary_url(), cases=load_cases(args.test_cases),
                          wait_seconds=args.wait_seconds, attempts=args.attempts,
                          retry_delay=args.retry_delay, max_results=args.max_results,
                          snippet_limit=snippet_limit).run()
    except (OSError, ValueError, VerificationError) as error:
        message = str(error) if isinstance(error, VerificationError) else "Verification configuration could not be loaded."
        report = {"version": 1, "started_at": utc_now(), "finished_at": utc_now(),
                  "status": "failed", "degraded": False,
                  "checks": [{"name": "Verification configuration", "status": "failed", "message": message}]}
    try:
        report = preserve_installer_report(args.report, report)
    except VerificationError as error:
        print(f"FAIL  {error}", file=sys.stderr)
        return 1
    serialized = json.dumps(report, indent=2) + "\n"
    if client is not None:
        serialized = client.redact(serialized)
    try:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(serialized, encoding="utf-8")
    except OSError:
        print("FAIL  Cannot save the installation verification report.", file=sys.stderr)
        return 1
    if report["status"] != "passed":
        print("Installation completed; verification failed. See artifacts/installation-report.json.", file=sys.stderr)
        return 1
    print("Installation verified. Report: artifacts/installation-report.json.", flush=True)
    if report["degraded"]:
        print("Some search snippets used the SearXNG fallback; see enrichment metrics in the report.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
