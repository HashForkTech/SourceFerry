"""Offline regression tests for the tuned search and fetch gateway.

All upstream HTTP requests use httpx.MockTransport. Docker, Chromium and
internet access are deliberately unnecessary for this suite.
"""

import asyncio
from contextlib import contextmanager
import importlib
import json
import os
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient


# Test credentials are deterministic and never read a deployment's .env file.
with patch.dict(
    os.environ,
    {
        "LOCAL_WEB_API_TOKEN": "offline-test-gateway-token",
        "CRAWL4AI_API_TOKEN": "offline-test-crawl-token",
        "SEARXNG_URL": "http://searxng:8080",
        "CRAWL4AI_URL": "http://crawl4ai:11235",
        "SEARXNG_TIMEOUT_SECONDS": "120",
        "CRAWL4AI_TIMEOUT_SECONDS": "120",
        "ENRICHMENT_CONCURRENCY": "4",
        "SNIPPET_MAX_CHARS": "1200",
    },
):
    gateway = importlib.import_module("gateway.app")


REAL_ASYNC_CLIENT = httpx.AsyncClient
AUTH = {"Authorization": f"Bearer {gateway.LOCAL_WEB_API_TOKEN}"}


@contextmanager
def offline_client(handler):
    """Replace gateway HTTP clients while preserving a real ASGI test client."""
    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return REAL_ASYNC_CLIENT(*args, **kwargs)

    with patch.object(gateway.httpx, "AsyncClient", side_effect=factory):
        with TestClient(gateway.app, raise_server_exceptions=False) as client:
            yield client


def no_upstream(request):
    raise AssertionError(f"Unexpected upstream request: {request.method} {request.url}")


def results(count):
    return [
        {
            "url": f"https://example.test/article/{index}",
            "title": f"Article {index}",
            "content": f"Original snippet {index}",
        }
        for index in range(count)
    ]


class UrlCleanupTests(unittest.TestCase):
    def test_malformed_markdown_result_url_is_unwrapped_and_stripped(self):
        self.assertEqual(
            gateway.clean_result_url("  [https://example.com](https://example.com)  "),
            "https://example.com",
        )
        self.assertEqual(
            gateway.clean_result_url("[http://label.test](https://target.test/path?q=1#anchor)"),
            "https://target.test/path?q=1#anchor",
        )

    def test_plain_url_and_unrelated_markdown_label_are_unchanged(self):
        self.assertEqual(gateway.clean_result_url(" https://example.test/a?q=1 "), "https://example.test/a?q=1")
        self.assertEqual(gateway.clean_result_url("[Read more](https://example.test)"), "[Read more](https://example.test)")

    def test_canonicalization_collapses_the_documented_duplicates(self):
        cases = [
            ("HTTPS://WWW.DeepSeek.COM:443/harness/#intro", "https://deepseek.com/harness"),
            ("https://deepseek.com/harness", "https://deepseek.com/harness"),
            ("http://WWW.EXAMPLE.TEST:80/", "http://example.test/"),
            ("https://example.test#intro", "https://example.test/"),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(gateway.canonical_url(source), expected)

    def test_queries_nondefault_ports_scheme_and_path_case_stay_distinct(self):
        base = gateway.canonical_url("https://example.test/path?q=one")
        for distinct in [
            "https://example.test/path?q=two",
            "https://example.test:8443/path?q=one",
            "http://example.test/path?q=one",
            "https://example.test/Path?q=one",
        ]:
            with self.subTest(distinct=distinct):
                self.assertNotEqual(base, gateway.canonical_url(distinct))
        self.assertEqual(
            gateway.canonical_url("https://www.example.test/path/?b=2&a=1#heading"),
            "https://example.test/path?b=2&a=1",
        )

    def test_ipv6_and_bad_port_do_not_crash_canonicalization(self):
        self.assertEqual(gateway.canonical_url("https://[2001:db8::1]:443/path/"), "https://[2001:db8::1]/path")
        self.assertEqual(gateway.canonical_url("https://example.test:bad/path"), "https://example.test:bad/path")


class MarkdownCleanupTests(unittest.TestCase):
    def test_duplicate_heading_example(self):
        source = "# DeepSeek Harness\nDeepSeek Harness is now an official open-source agent framework..."
        self.assertEqual(
            gateway.markdown_to_snippet(source),
            "DeepSeek Harness is now an official open-source agent framework...",
        )

    def test_duplicate_heading_ignores_formatting_case_and_blank_lines(self):
        self.assertEqual(
            gateway.markdown_to_snippet("## **DeepSeek Harness**\n\n**deepseek harness** works well."),
            "deepseek harness works well.",
        )

    def test_nonduplicate_heading_is_retained(self):
        self.assertEqual(gateway.markdown_to_snippet("# Overview\nA useful description."), "Overview A useful description.")

    def test_command_selector_artifacts_are_removed_but_the_command_is_kept(self):
        for selector in ["npxgit clone", "npmgit clone", "pnpngit clone", "pnpmgit clone", "yarngit clone"]:
            with self.subTest(selector=selector):
                self.assertEqual(
                    gateway.markdown_to_snippet(f"{selector}\n`npx @deepseek-ai/dsh web`"),
                    "npx @deepseek-ai/dsh web",
                )

    def test_all_documented_fused_ui_word_examples(self):
        cases = {
            "Quick startInstall from source": "Quick start Install from source",
            "## Harnesskeeps agents working in real-world environments": "Harness keeps agents working in real-world environments",
            "DeepSeek HarnessReady to use.": "DeepSeek Harness Ready to use.",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(gateway.markdown_to_snippet(raw), expected)

    def test_brand_names_are_preserved(self):
        self.assertEqual(gateway.markdown_to_snippet("DeepSeek and GitHub work together."), "DeepSeek and GitHub work together.")

    def test_fused_repairs_do_not_rewrite_urls_links_or_commands(self):
        for source in [
            "https://example.test/QuickStart",
            "[Quick startInstall](https://example.test)",
            "`Quick startInstall`",
            "x" * 121 + "Install from source",
        ]:
            with self.subTest(source=source):
                self.assertEqual(gateway.repair_fused_markdown_lines(source), source)

    def test_markdown_unescaping_links_images_and_autolinks(self):
        source = r"\*Read\* [the guide](https://example.test/guide) ![Logo](https://example.test/logo.png) <https://example.test/api>"
        self.assertEqual(gateway.markdown_to_snippet(source), "Read the guide https://example.test/api")

    def test_duplicate_lines_and_excess_dots_are_cleaned(self):
        self.assertEqual(
            gateway.markdown_to_snippet("Useful result.\n  useful   result.\nAnother result....."),
            "Useful result. Another result...",
        )

    def test_duplicate_labeled_sections_keep_richer_description_and_list_number(self):
        cases = [
            (
                "1. Storage: Saves files. Storage: Saves files and keeps their version history.",
                "1. Storage — Saves files and keeps their version history.",
            ),
            (
                "Storage — Saves files and keeps their version history. Storage — Saves files.",
                "Storage — Saves files and keeps their version history.",
            ),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(gateway.markdown_to_snippet(raw), expected)

    def test_apostrophe_punctuation_and_parentheses_spacing(self):
        self.assertEqual(
            gateway.markdown_to_snippet("Cordis 's useful result , isn ’t missing ( details ) !"),
            "Cordis's useful result, isn’t missing (details)!",
        )

    def test_all_documented_leading_page_chrome_is_removed(self):
        for chrome in ["Skip to main content", "Skip to content", "← Back to blog", "Back to blog"]:
            with self.subTest(chrome=chrome):
                self.assertEqual(gateway.markdown_to_snippet(f"{chrome}\nArticle body."), "Article body.")
        self.assertEqual(
            gateway.markdown_to_snippet("Skip to main content\nSkip to content\n← Back to blog\nBack to blog\nArticle body."),
            "Article body.",
        )

    def test_snippet_limit_includes_ellipsis(self):
        snippet = gateway.markdown_to_snippet("A" * 1500)
        self.assertEqual(len(snippet), 1200)
        self.assertTrue(snippet.endswith("..."))

    def test_snippet_truncation_prefers_a_late_sentence_boundary(self):
        sentence = "A" * 800 + "."
        snippet = gateway.markdown_to_snippet(sentence + " " + "B" * 800)
        self.assertEqual(snippet, sentence + "...")
        self.assertLessEqual(len(snippet), 1200)

    def test_short_snippets_are_not_truncated(self):
        self.assertEqual(gateway.markdown_to_snippet("A short sentence."), "A short sentence.")

    def test_github_trim_uses_the_exact_readme_heading(self):
        source = "Navigation\nIssues\n# DeepSeek Harness\nEnglish | 中文\n`npx @deepseek-ai/dsh web`\nhttp://127.0.0.1:3080\ngit clone https://github.com/deepseek-ai/deepseek-harness.git"
        for host in ["github.com", "www.github.com"]:
            with self.subTest(host=host):
                trimmed = gateway.trim_page_chrome(f"https://{host}/deepseek-ai/deepseek-harness", source)
                self.assertTrue(trimmed.startswith("# DeepSeek Harness"))
                snippet = gateway.markdown_to_snippet(trimmed)
                self.assertNotIn("Navigation", snippet)
                for useful in ["English | 中文", "npx @deepseek-ai/dsh web", "http://127.0.0.1:3080", "git clone https://github.com/deepseek-ai/deepseek-harness.git"]:
                    self.assertIn(useful, snippet)

    def test_github_trim_is_specific_to_the_documented_heading_and_host(self):
        source = "Navigation\n# Deepseek Harness\nRepository text."
        self.assertEqual(gateway.trim_page_chrome("https://github.com/example/repo", source), source)
        exact = "Navigation\n# DeepSeek Harness\nRepository text."
        self.assertEqual(gateway.trim_page_chrome("https://github.com.example.test/repo", exact), exact)

    def test_reddit_trim_keeps_first_post_and_stops_at_read_more(self):
        source = "Skip to main content\nGo to LocalLLaMA\nusername\n## Navigation\n# Post title\nactual post\nREAD MORE\npromoted content\nSort by:\nComments Section\ncomments..."
        for host in ["reddit.com", "www.reddit.com", "old.reddit.com"]:
            with self.subTest(host=host):
                self.assertEqual(
                    gateway.trim_page_chrome(f"https://{host}/r/LocalLLaMA/comments/example", source),
                    "# Post title\nactual post",
                )

    def test_reddit_without_post_heading_and_other_hosts_are_not_trimmed(self):
        source = "Navigation\n## Lower heading\nArticle body."
        self.assertEqual(gateway.trim_page_chrome("https://reddit.com/r/example", source), source)
        source = "Navigation\n# Article\nBody\nRead more\nRelated links"
        self.assertEqual(gateway.trim_page_chrome("https://example.test/article", source), source)


class EndpointTests(unittest.TestCase):
    def test_health_is_public_and_does_not_use_the_backends(self):
        with offline_client(no_upstream) as client:
            response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_search_and_fetch_require_exact_bearer_auth(self):
        with offline_client(no_upstream) as client:
            for path, body in [
                ("/search", {"queries": ["example"]}),
                ("/fetch", {"url": "https://example.test"}),
            ]:
                for header in [None, "Bearer incorrect", "bearer offline-test-gateway-token", "Basic credentials"]:
                    with self.subTest(path=path, header=header):
                        headers = {} if header is None else {"Authorization": header}
                        response = client.post(path, json=body, headers=headers)
                        self.assertEqual(response.status_code, 401)
                        self.assertEqual(response.json(), {"detail": "Unauthorized"})
                        self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")

    def test_invalid_request_shapes_are_rejected_without_upstream_calls(self):
        with offline_client(no_upstream) as client:
            for path, body in [
                ("/search", {}),
                ("/search", {"queries": "not a list"}),
                ("/search", {"queries": [None]}),
                ("/fetch", {}),
            ]:
                with self.subTest(path=path, body=body):
                    self.assertEqual(client.post(path, json=body, headers=AUTH).status_code, 422)

    def test_invalid_fetch_urls_are_rejected_without_upstream_calls(self):
        with offline_client(no_upstream) as client:
            for url in ["", "file:///etc/passwd", "ftp://example.test/file", "https://example.test:bad", "https://user:password@example.test", "https://example.test/a b"]:
                with self.subTest(url=url):
                    self.assertEqual(client.post("/fetch", json={"url": url}, headers=AUTH).status_code, 422)

    def test_empty_queries_return_an_empty_result(self):
        with offline_client(no_upstream) as client:
            for queries in [[], ["", " ", "\n"]]:
                with self.subTest(queries=queries):
                    response = client.post("/search", json={"queries": queries}, headers=AUTH)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), {"sources": [], "truncated": False})
                    self.assertEqual(response.headers.get("X-Crawl4AI-Enriched"), "0")
                    self.assertEqual(response.headers.get("X-Crawl4AI-Fallback"), "0")

    def test_search_trims_ignores_blank_queries_and_caps_at_four(self):
        seen_queries = []

        def handler(request):
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.url.host, "searxng")
            self.assertEqual(request.url.params.get("format"), "json")
            seen_queries.append(request.url.params.get("q"))
            return httpx.Response(200, json={"results": []})

        with offline_client(handler) as client:
            response = client.post("/search", headers=AUTH, json={"queries": [" ", " one ", "two", "", "three", "four", "five"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen_queries, ["one", "two", "three", "four"])

    def test_result_default_and_clamps_and_early_query_stop(self):
        for requested, expected in [(None, 8), (-100, 1), (0, 1), (3, 3), (100, 20)]:
            with self.subTest(requested=requested):
                counts = {"search": 0, "crawl": 0}

                def handler(request):
                    if request.url.path == "/search":
                        counts["search"] += 1
                        return httpx.Response(200, json={"results": results(25)})
                    counts["crawl"] += 1
                    return httpx.Response(200, json={"success": False})

                body = {"queries": ["one", "two"]}
                if requested is not None:
                    body["max_results"] = requested
                with offline_client(handler) as client:
                    response = client.post("/search", headers=AUTH, json=body)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.json()["sources"]), expected)
                self.assertEqual(counts, {"search": 1, "crawl": expected})

    def test_deduplication_across_queries_preserves_first_result_and_query_variants(self):
        crawled_urls = []
        discoveries = {
            "one": [
                {"url": " [https://example.test](https://WWW.example.test:443/path/#first) ", "title": "First", "content": "First snippet"},
                {"url": "https://example.test/path", "title": "Duplicate", "content": "Duplicate snippet"},
            ],
            "two": [
                {"url": "https://www.example.test/path/#second", "title": "Another duplicate"},
                {"url": "https://example.test/path?q=one", "title": "Query one", "content": "One"},
                {"url": "https://example.test/path?q=two", "title": "Query two", "content": "Two"},
            ],
        }

        def handler(request):
            if request.url.path == "/search":
                return httpx.Response(200, json={"results": discoveries[request.url.params["q"]]})
            crawled_urls.append(json.loads(request.content)["url"])
            return httpx.Response(200, json={"success": False})

        with offline_client(handler) as client:
            response = client.post("/search", headers=AUTH, json={"queries": ["one", "two"]})
        self.assertEqual(response.status_code, 200)
        sources = response.json()["sources"]
        self.assertEqual([source["title"] for source in sources], ["First", "Query one", "Query two"])
        self.assertEqual(sources[0]["snippet"], "First snippet")
        self.assertEqual(sources[0]["url"], "https://WWW.example.test:443/path/#first")
        self.assertCountEqual(crawled_urls, [source["url"] for source in sources])

    def test_malformed_result_entries_are_skipped_or_safely_normalized(self):
        malformed = [
            None,
            "string entry",
            42,
            {},
            {"url": None},
            {"url": 123},
            {"url": " "},
            {"url": "file:///etc/passwd"},
            {"url": "https://example.test:bad"},
            {"url": "https://user:password@example.test"},
            {"url": "https://example.test/valid", "title": ["bad title"], "content": {"bad": "snippet"}},
        ]

        def handler(request):
            if request.url.path == "/search":
                return httpx.Response(200, json={"results": malformed})
            return httpx.Response(200, json={"success": False})

        with offline_client(handler) as client:
            response = client.post("/search", headers=AUTH, json={"queries": ["example"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sources"], [{"url": "https://example.test/valid", "title": None, "snippet": ""}])

    def test_crawl_enrichment_is_concurrent_capped_at_four_and_keeps_result_order(self):
        active = 0
        peak = 0

        async def handler(request):
            nonlocal active, peak
            if request.url.path == "/search":
                return httpx.Response(200, json={"results": results(12)})
            active += 1
            peak = max(peak, active)
            try:
                index = int(json.loads(request.content)["url"].rsplit("/", 1)[1])
                await asyncio.sleep(0.005 * (4 - index % 4))
                return httpx.Response(200, json={"success": True, "markdown": f"Clean content {index}."})
            finally:
                active -= 1

        with offline_client(handler) as client:
            response = client.post("/search", headers=AUTH, json={"queries": ["example"], "max_results": 12})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(peak, 4)
        self.assertEqual(active, 0)
        self.assertEqual([source["snippet"] for source in response.json()["sources"]], [f"Clean content {index}." for index in range(12)])

    def test_each_crawl_failure_falls_back_without_affecting_good_results(self):
        def handler(request):
            if request.url.path == "/search":
                return httpx.Response(200, json={"results": results(9)})
            index = int(json.loads(request.content)["url"].rsplit("/", 1)[1])
            if index == 0:
                return httpx.Response(200, json={"success": True, "markdown": "# Useful heading\nUseful heading contains fresh content."})
            if index == 1:
                return httpx.Response(503, text="Unavailable")
            if index == 2:
                raise httpx.ReadTimeout("Crawl timeout", request=request)
            if index == 3:
                return httpx.Response(200, text="Not JSON")
            if index == 4:
                return httpx.Response(200, json={"success": False, "markdown": "Failed crawl text"})
            if index == 5:
                return httpx.Response(200, json={"success": True, "markdown": "  "})
            if index == 6:
                return httpx.Response(200, json={"success": True, "markdown": ["Wrong type"]})
            if index == 7:
                return httpx.Response(200, json=["Wrong payload shape"])
            return httpx.Response(200, json={"success": True, "markdown": "![Only image](https://example.test/image.png)"})

        with offline_client(handler) as client:
            response = client.post("/search", headers=AUTH, json={"queries": ["example"], "max_results": 9})
        self.assertEqual(response.status_code, 200)
        snippets = [source["snippet"] for source in response.json()["sources"]]
        self.assertEqual(snippets[0], "Useful heading contains fresh content.")
        self.assertEqual(snippets[1:], [f"Original snippet {index}" for index in range(1, 9)])
        self.assertEqual(response.headers.get("X-Crawl4AI-Enriched"), "1")
        self.assertEqual(response.headers.get("X-Crawl4AI-Fallback"), "8")

    def test_github_and_reddit_trimming_are_applied_by_search(self):
        crawl_text = {
            "https://github.com/deepseek-ai/deepseek-harness": "Navigation\nIssues\n# DeepSeek Harness\nDeepSeek Harness works with GitHub.",
            "https://old.reddit.com/r/LocalLLaMA/comments/example": "Skip to main content\nNavigation\n# Post title\nUseful post body.\nRead more\npromoted content\nSort by:\nComments Section",
        }

        def handler(request):
            if request.url.path == "/search":
                return httpx.Response(200, json={"results": [{"url": url} for url in crawl_text]})
            url = json.loads(request.content)["url"]
            return httpx.Response(200, json={"success": True, "markdown": crawl_text[url]})

        with offline_client(handler) as client:
            response = client.post("/search", headers=AUTH, json={"queries": ["DeepSeek Harness"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [source["snippet"] for source in response.json()["sources"]],
            ["DeepSeek Harness works with GitHub.", "Post title Useful post body."],
        )

    def test_fetch_preserves_the_raw_crawl_payload(self):
        payload = {
            "url": "https://example.test/article",
            "filter": "raw",
            "query": None,
            "cache": "bypass",
            "markdown": "  # Heading\n[Raw link](https://example.test)\n![Image](https://example.test/img.png)  ",
            "success": True,
            "extra": {"nested": [1, "two", None]},
        }
        calls = []

        def handler(request):
            calls.append(request)
            self.assertEqual(request.url.host, "crawl4ai")
            self.assertEqual(request.url.path, "/md")
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.headers["Authorization"], f"Bearer {gateway.CRAWL4AI_API_TOKEN}")
            self.assertEqual(json.loads(request.content), {"url": "https://example.test/article"})
            return httpx.Response(200, json=payload)

        with offline_client(handler) as client:
            response = client.post("/fetch", headers=AUTH, json={"url": " https://example.test/article "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"url": "https://example.test/article", "content": payload, "truncated": False})
        self.assertEqual(len(calls), 1)

    def test_backend_failures_have_json_502_or_504_responses(self):
        for path, body in [("/search", {"queries": ["example"]}), ("/fetch", {"url": "https://example.test"})]:
            for kind, expected in [("timeout", 504), ("connection", 502), ("http", 502), ("json", 502)]:
                with self.subTest(path=path, kind=kind):
                    def handler(request):
                        if kind == "timeout":
                            raise httpx.ReadTimeout("Upstream timed out", request=request)
                        if kind == "connection":
                            raise httpx.ConnectError("Upstream unavailable", request=request)
                        if kind == "http":
                            return httpx.Response(503, text="Sensitive upstream error body")
                        return httpx.Response(200, text="This is not valid JSON")

                    with offline_client(handler) as client:
                        response = client.post(path, headers=AUTH, json=body)
                    self.assertEqual(response.status_code, expected)
                    self.assertEqual(response.headers["content-type"], "application/json")
                    self.assertIsInstance(response.json().get("detail"), str)
                    self.assertNotIn("Sensitive upstream error body", response.text)

    def test_malformed_searxng_payloads_return_json_502(self):
        for payload in [None, [], {}, {"results": None}, {"results": {}}, {"results": "wrong shape"}]:
            with self.subTest(payload=payload):
                def handler(request):
                    return httpx.Response(200, content=json.dumps(payload), headers={"Content-Type": "application/json"})

                with offline_client(handler) as client:
                    response = client.post("/search", headers=AUTH, json={"queries": ["example"]})
                self.assertEqual(response.status_code, 502)
                self.assertIsInstance(response.json().get("detail"), str)

    def test_unsuccessful_or_malformed_fetch_payloads_return_json_502(self):
        for payload in [None, [], {}, {"success": False, "markdown": "text"}, {"success": True}, {"success": True, "markdown": None}, {"success": True, "markdown": " "}, {"success": True, "markdown": ["text"]}]:
            with self.subTest(payload=payload):
                def handler(request):
                    return httpx.Response(200, content=json.dumps(payload), headers={"Content-Type": "application/json"})

                with offline_client(handler) as client:
                    response = client.post("/fetch", headers=AUTH, json={"url": "https://example.test"})
                self.assertEqual(response.status_code, 502)
                self.assertIsInstance(response.json().get("detail"), str)

    def test_upstream_requests_use_120_second_default_timeouts(self):
        seen_timeouts = []

        def handler(request):
            seen_timeouts.append(request.extensions["timeout"])
            if request.url.path == "/search":
                return httpx.Response(200, json={"results": results(1)})
            return httpx.Response(200, json={"success": True, "markdown": "Useful content."})

        with offline_client(handler) as client:
            self.assertEqual(client.post("/search", headers=AUTH, json={"queries": ["example"]}).status_code, 200)
            self.assertEqual(client.post("/fetch", headers=AUTH, json={"url": "https://example.test"}).status_code, 200)
        self.assertEqual(len(seen_timeouts), 3)
        self.assertTrue(all(all(value == 120 for value in timeout.values()) for timeout in seen_timeouts))


if __name__ == "__main__":
    unittest.main()
