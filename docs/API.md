# API contract

SourceFerry serves JSON over HTTP. Only the gateway has a published port. AI clients, including a custom harness, can use this contract without any client-specific integration.

## Authentication and health

Send `Authorization: Bearer <LOCAL_WEB_API_TOKEN>` on every `/search` and `/fetch` request. Authentication runs before body parsing. Duplicate authorization headers are rejected. The backend token and SearXNG secret are never needed by clients.

`GET /health` returns `200 {"status":"ok"}` when the gateway process responds. It is public liveness, not a backend check. `GET /ready` is public readiness: `200 {"status":"ready"}` if both backends respond, otherwise `503 {"status":"unavailable"}`. Probes have a three-second total deadline and a five-second cache. Readiness does not guarantee that a search engine or website will allow a later request.

## Search

```http
POST /search
Authorization: Bearer <gateway key>
Content-Type: application/json

{"queries":["Docker Compose documentation"],"max_results":2}
```

Example response (illustrative content):

```json
{
  "sources": [
    {"url":"https://docs.docker.com/compose/","title":"Docker Compose","snippet":"Docker Compose manages applications with multiple containers."}
  ],
  "truncated": false
}
```

`queries` is required: at most 16 strings, each at most 2,048 characters. The gateway strips whitespace, drops blank entries, and uses the first four remaining queries. An empty list produces an empty `sources` list. `max_results` defaults to 8 and is clamped to 1–20. Discovery stops as soon as enough distinct usable URLs have been found, so later queries may not run.

Results preserve discovery order. Canonical deduplication lowercases scheme/host, removes leading `www.`, ignores fragments/default ports/trailing slashes, and preserves path case and query strings. HTTP and HTTPS remain distinct. Malformed entries and URLs are skipped. A missing or non-string title becomes `null`; a missing or non-string fallback snippet becomes `""`.

Each source is enriched by crawling its page. `X-Crawl4AI-Enriched` and `X-Crawl4AI-Fallback` count the successful replacements and retained snippets; their sum equals the source count. A failed, empty, or oversized crawl retains the original SearXNG snippet. **Fallback snippets are not subject to `SNIPPET_MAX_CHARS`**; the upstream response byte limit still applies.

Successfully cleaned crawler snippets are capped at `SNIPPET_MAX_CHARS` characters, including any ellipsis. Cleanup processes only the first `SNIPPET_INPUT_MAX_CHARS` characters. GitHub trimming recognizes only the exact `# DeepSeek Harness` heading on github.com; it is not a general README detector. Reddit trimming uses the first top-level heading and the `Read more` marker. Other cleanup removes selected navigation text, repeated headings, Markdown syntax, and known fused labels. It does not sanitize prompt injection.

`truncated: false` is retained for client compatibility: the gateway has not cut off an already assembled response. It does **not** mean unlimited results, unshortened snippets, or that the crawler retrieved every part of a website.

## Fetch

```http
POST /fetch
Authorization: Bearer <gateway key>
Content-Type: application/json

{"url":"https://example.com/"}
```

```json
{
  "url":"https://example.com/",
  "content":{"success":true,"markdown":"# Example Domain\nExample content."},
  "truncated":false
}
```

`url` is required, is limited to 8,192 characters, and must be HTTP(S) without credentials, whitespace inside the URL, backslashes, or an invalid port. Surrounding whitespace is stripped. `content` preserves the complete successful JSON payload returned by Crawl4AI `/md`, including fields beyond this example. Fetch applies no snippet cleanup or character truncation. An upstream response exceeding `MAX_UPSTREAM_BYTES` is rejected rather than partially returned.

The crawler validates destinations and uses a connection-pinning proxy to reject private/non-global addresses, including redirects and subresources. These protections must remain enabled. Syntactic gateway URL validation alone is not the SSRF boundary.

## Errors and capacity

Errors are JSON with a `detail` field, except Uvicorn's outer connection-capacity rejection, which may be plain text. Clients should use the HTTP status first and tolerate either format on 503.

| Status | Meaning |
| --- | --- |
| 400 | Invalid Content-Length. |
| 401 | Missing, incorrect, or duplicate bearer authentication; includes `WWW-Authenticate: Bearer`. |
| 413 | Body exceeds the configured limit, including chunked uploads. |
| 415 | Compressed request body; only identity encoding is supported. |
| 422 | Invalid JSON, field shape, field bound, or fetch URL. Validation responses omit supplied values. |
| 502 | Failed, malformed, compressed, or oversized upstream response; failed fetch. Individual search crawl failures use fallback instead. |
| 503 | Gateway capacity exhausted, or `/ready` reports a backend unavailable. Admission rejection includes `Retry-After: 5`. |
| 504 | Upstream timeout or total gateway deadline exceeded. A search crawl timeout normally falls back unless the total deadline expires. |

By default, eight protected requests are admitted, four crawls run concurrently **across all searches and fetches**, and every admitted request has a 300-second total deadline including upload and capacity waits. There is no unbounded admission queue. Limits apply to one process; the supplied image explicitly uses one worker. Multiple replicas need a shared quota or divided budgets.

Treat returned content as untrusted source material. Clients must keep page text separate from system instructions, tool authorization, and secrets.
