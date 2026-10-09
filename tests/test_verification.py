"""Exercise acceptance gates against a local HTTP fixture, without the internet."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify

TOKEN = "a" * 64
CRAWL_TOKEN = "b" * 64
SEARX_SECRET = "c" * 64
OFFICIAL_URL = "https://docs.fixture.test/compose"
TOPIC = "Docker Compose runs multi-container applications and services."
CANARY_MARKDOWN = ("# Gateway installation canary\n" + verify.STATIC_MARKER + " documents installation.\n"
                   + verify.BROWSER_MARKER + ". The browser executed the page JavaScript correctly.\n")


@contextmanager
def gateway_fixture(**settings):
    state = {"settings": settings, "observed": [], "search_requests": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, data, headers=None):
            body = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, str(value))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.respond(200, {"status": "broken" if settings.get("unhealthy") else "ok"})

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["observed"].append((self.path, payload, self.headers.get("Authorization")))
            authenticated = self.headers.get("Authorization") == f"Bearer {TOKEN}"
            if not authenticated and not settings.get("auth_bypass"):
                self.respond(401, {"detail": "Unauthorized"})
                return
            if self.path == "/search":
                state["search_requests"] += 1
                if state["search_requests"] <= settings.get("initial_search_failures", 0):
                    self.respond(503, {"detail": f"Do not print this upstream body: {TOKEN}"})
                    return
                source = {"url": settings.get("source_url", OFFICIAL_URL),
                          "title": settings.get("title", "Docker Compose overview"),
                          "snippet": settings.get("snippet", TOPIC)}
                sources = [] if settings.get("empty_sources") else [source]
                if settings.get("duplicates"):
                    sources.append({**source, "url": "https://www.docs.fixture.test/compose/#fragment"})
                if settings.get("partial_fallback"):
                    sources.append({"url": "https://other.fixture.test/compose", "title": "Compose reference",
                                    "snippet": TOPIC})
                diagnostics = {"X-Crawl4AI-Enriched": 0 if settings.get("all_fallback") else 1,
                               "X-Crawl4AI-Fallback": len(sources) if settings.get("all_fallback") else len(sources) - 1}
                if settings.get("missing_diagnostics"):
                    diagnostics = {}
                if settings.get("bad_diagnostics"):
                    diagnostics = {"X-Crawl4AI-Enriched": "no", "X-Crawl4AI-Fallback": 0}
                if settings.get("count_mismatch"):
                    diagnostics = {"X-Crawl4AI-Enriched": 50, "X-Crawl4AI-Fallback": 0}
                self.respond(200, {"sources": sources, "truncated": settings.get("search_truncated", False)}, diagnostics)
            elif self.path == "/fetch":
                if payload["url"].endswith("/canary") or payload["url"].startswith(verify.CANARY_ORIGIN):
                    markdown = settings.get("canary_markdown", CANARY_MARKDOWN)
                else:
                    markdown = settings.get("fetched_markdown", "# Docker Compose overview\n" + TOPIC * 4)
                self.respond(200, {
                    "url": settings.get("fetch_returned_url", payload["url"]),
                    "content": {"success": settings.get("fetch_success", True), "markdown": markdown},
                    "truncated": settings.get("fetch_truncated", False),
                })
            else:
                self.respond(404, {})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class VerificationTests(unittest.TestCase):
    def invoke(self, address, *, existing_report=None, attempts=1, cases=None, default_canary=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / ".env"
            env.write_text(f"LOCAL_WEB_API_TOKEN={TOKEN}\nCRAWL4AI_API_TOKEN={CRAWL_TOKEN}\nSEARXNG_SECRET_KEY={SEARX_SECRET}\n")
            case_path = root / "cases.json"
            case_path.write_text(json.dumps(cases or [{
                "name": f"Do not echo {TOKEN}", "query": f"Docker Compose {CRAWL_TOKEN}",
                "domains": ["docs.fixture.test"],
                "topic_groups": [["compose"], ["container", "application"]],
                "fetch_groups": [["compose"], ["container", "application"]],
            }]))
            report = root / "installation-report.json"
            if existing_report is not None:
                report.write_text(json.dumps(existing_report))
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, {}, clear=True), redirect_stdout(stdout), redirect_stderr(stderr):
                arguments = [
                    "--env-file", str(env), "--base-url", address,
                    "--report", str(report),
                    "--test-cases", str(case_path), "--wait-seconds", "0", "--timeout", "2",
                    "--attempts", str(attempts), "--retry-delay", "0",
                ]
                if not default_canary:
                    arguments.extend(["--canary-url", "http://canary.fixture.test/canary"])
                code = verify.main(arguments)
            output = stdout.getvalue() + stderr.getvalue()
            report_text = report.read_text() if report.exists() else "{}"
            for secret in (TOKEN, CRAWL_TOKEN, SEARX_SECRET):
                self.assertNotIn(secret, output)
                self.assertNotIn(secret, report_text)
            self.assertNotIn(CANARY_MARKDOWN, report_text)
            return code, json.loads(report_text), output

    def assert_failure(self, settings, expected_message):
        with gateway_fixture(**settings) as (address, _):
            code, report, output = self.invoke(address)
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "failed")
        messages = " ".join(check.get("message", "") for check in report["checks"])
        self.assertIn(expected_message, messages)
        self.assertIn("Installation completed; verification failed", output)

    def test_all_acceptance_checks_pass_and_fetch_a_returned_source(self):
        with gateway_fixture() as (address, state):
            code, report, output = self.invoke(address)
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(len(report["checks"]), 5)
        self.assertIn("Installation verified", output)
        self.assertFalse(report["degraded"])
        fetched_urls = [payload["url"] for path, payload, auth in state["observed"]
                        if path == "/fetch" and auth == f"Bearer {TOKEN}"]
        self.assertIn(OFFICIAL_URL, fetched_urls)
        auth_check = report["checks"][1]
        self.assertEqual(auth_check["metrics"]["rejected_requests"], 4)

    def test_default_browser_fixture_uses_public_https_and_only_encodes_bundled_html(self):
        with gateway_fixture() as (address, state):
            code, report, _ = self.invoke(address, default_canary=True)
        self.assertEqual(code, 0)
        requested = [payload["url"] for path, payload, auth in state["observed"]
                     if path == "/fetch" and auth == f"Bearer {TOKEN}"]
        fixture_url = next(url for url in requested if url.startswith(verify.CANARY_ORIGIN))
        parts = urlsplit(fixture_url)
        self.assertEqual(parts.scheme, "https")
        self.assertEqual(parts.hostname, "httpbin.org")
        self.assertFalse(parts.query or parts.fragment)
        encoded = parts.path.removeprefix("/base64/")
        self.assertNotIn("/", encoded)
        html = base64.urlsafe_b64decode(encoded).decode()
        self.assertIn(verify.STATIC_MARKER, html)
        self.assertNotIn(verify.BROWSER_MARKER, html)
        self.assertIn("<script>", html)
        for secret in (TOKEN, CRAWL_TOKEN, SEARX_SECRET):
            self.assertNotIn(secret, html)
            self.assertNotIn(secret, fixture_url)
        self.assertEqual(report["checks"][2]["metrics"]["fixture_domain"], "httpbin.org")

    def test_installer_stage_report_is_preserved_without_arbitrary_fields(self):
        original = {"installer_stages": [{"stage": "preflight", "status": "passed"},
                                         {"stage": "host-health", "status": "passed"}],
                    "installer_status": "running", "updated_at": "2026-10-09T10:00:00+00:00",
                    "unwanted_page_content": CANARY_MARKDOWN}
        with gateway_fixture() as (address, _):
            code, report, _ = self.invoke(address, existing_report=original)
        self.assertEqual(code, 0)
        self.assertEqual(report["installer_stages"], original["installer_stages"])
        self.assertEqual(report["installer_status"], "running")
        self.assertEqual(report["updated_at"], original["updated_at"])
        self.assertNotIn("unwanted_page_content", report)

    def test_known_static_content_without_javascript_marker_fails(self):
        self.assert_failure({"canary_markdown": CANARY_MARKDOWN.replace(verify.BROWSER_MARKER, "Missing dynamic content")},
                            "JavaScript-generated browser marker")

    def test_dynamic_marker_without_static_content_fails(self):
        self.assert_failure({"canary_markdown": CANARY_MARKDOWN.replace(verify.STATIC_MARKER, "Wrong static page")},
                            "known static canary content")

    def test_wrong_live_content_fails_even_if_fetch_reports_success(self):
        self.assert_failure({"fetched_markdown": "# Unrelated page\n" + "An unrelated document discusses gardening and plants. " * 5},
                            "missing the expected page topic")

    def test_antibot_challenge_is_rejected_even_with_matching_topic(self):
        self.assert_failure({"fetched_markdown": "# Verify you are human\n" + TOPIC * 4},
                            "anti-bot challenge")

    def test_canonical_duplicates_are_rejected(self):
        self.assert_failure({"duplicates": True}, "duplicate canonical source URLs")

    def test_wrong_official_domain_is_rejected(self):
        self.assert_failure({"source_url": "https://docs.fixture.test.attacker.test/compose"},
                            "expected official domain")

    def test_unclean_markdown_url_is_rejected(self):
        self.assert_failure({"source_url": "[https://docs.fixture.test](https://docs.fixture.test/compose)"},
                            "invalid or unclean")

    def test_missing_empty_and_oversized_source_fields_are_rejected(self):
        for settings, expected in (({"title": ""}, "nonempty title"),
                                   ({"snippet": ""}, "nonempty snippet"),
                                   ({"snippet": "a" * 1201}, "configured character limit"),
                                   ({"empty_sources": True}, "no usable sources")):
            with self.subTest(settings=settings):
                self.assert_failure(settings, expected)

    def test_invalid_or_missing_enrichment_diagnostics_fail(self):
        for setting in ("missing_diagnostics", "bad_diagnostics", "count_mismatch"):
            with self.subTest(setting=setting):
                self.assert_failure({setting: True}, "enrichment diagnostics")

    def test_all_search_results_using_fallback_fail_enrichment_gate(self):
        self.assert_failure({"all_fallback": True}, "No successful live search case demonstrated")

    def test_partial_fallback_is_reported_as_degraded_but_passes(self):
        with gateway_fixture(partial_fallback=True) as (address, _):
            code, report, output = self.invoke(address)
        self.assertEqual(code, 0)
        self.assertTrue(report["degraded"])
        self.assertEqual(report["checks"][-1]["metrics"]["fallback"], 1)
        self.assertIn("SearXNG fallback", output)

    def test_original_fallback_snippets_can_exceed_enriched_character_limit(self):
        with gateway_fixture(partial_fallback=True, snippet=TOPIC * 30) as (address, _):
            code, report, _ = self.invoke(address)
        self.assertEqual(code, 0)
        self.assertTrue(report["degraded"])

    def test_transient_search_failure_is_retried_with_bounded_attempts(self):
        with gateway_fixture(initial_search_failures=1) as (address, state):
            code, report, _ = self.invoke(address, attempts=2)
        self.assertEqual(code, 0)
        self.assertEqual(state["search_requests"], 2)
        self.assertEqual(report["checks"][3]["attempts"], 2)

    def test_persistent_search_failure_is_not_replaced_with_fixture_success(self):
        with gateway_fixture(initial_search_failures=10) as (address, state):
            code, report, _ = self.invoke(address, attempts=2)
        self.assertEqual(code, 1)
        self.assertEqual(state["search_requests"], 2)
        self.assertEqual(report["checks"][3]["message"], "Search returned HTTP 503.")

    def test_failed_crawler_and_truncated_or_wrong_url_fetch_fail(self):
        for settings, expected in (({"fetch_success": False}, "successful fetch"),
                                   ({"fetch_truncated": True}, "full-content contract"),
                                   ({"fetch_returned_url": "https://unrelated.test"}, "full-content contract")):
            with self.subTest(settings=settings):
                self.assert_failure(settings, expected)

    def test_authentication_bypass_is_rejected(self):
        self.assert_failure({"auth_bypass": True}, "did not reject")

    def test_unhealthy_gateway_stops_dependent_tests(self):
        with gateway_fixture(unhealthy=True) as (address, state):
            code, report, _ = self.invoke(address)
        self.assertEqual(code, 1)
        self.assertEqual(len(report["checks"]), 1)
        self.assertEqual(state["observed"], [])

    def test_case_file_validation_rejects_empty_expected_phrases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            path.write_text('[{"name":"case","query":"query","domains":["docs.fixture.test"],'
                            '"topic_groups":[[]],"fetch_groups":[["compose"]]}]')
            with self.assertRaisesRegex(verify.VerificationError, "nonempty topic_groups"):
                verify.load_cases(path)


if __name__ == "__main__":
    unittest.main()
