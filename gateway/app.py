import asyncio
import hmac
import json
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
import os
import re
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from gateway.contracts import (
    MAX_INPUT_QUERIES, MAX_QUERY_CHARS, MAX_URL_CHARS,
    canonical_url, numeric_settings, valid_http_url,
)
from gateway.middleware import RequestGuard


def required_secret(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value or value.upper().startswith(("REPLACE-", "REPLACE_", "CHANGE_ME")) or value.startswith("<"):
        raise RuntimeError(f"{name} must be set to a non-placeholder secret")
    return value


LOCAL_WEB_API_TOKEN = required_secret("LOCAL_WEB_API_TOKEN")
SEARXNG_URL = os.environ.get(
    "SEARXNG_URL",
    "http://searxng:8080",
).rstrip("/")
CRAWL4AI_URL = os.environ.get(
    "CRAWL4AI_URL",
    "http://crawl4ai:11235",
).rstrip("/")
CRAWL4AI_API_TOKEN = required_secret("CRAWL4AI_API_TOKEN")
SETTINGS = numeric_settings(os.environ)
SEARXNG_TIMEOUT_SECONDS = float(SETTINGS["SEARXNG_TIMEOUT_SECONDS"])
CRAWL4AI_TIMEOUT_SECONDS = float(SETTINGS["CRAWL4AI_TIMEOUT_SECONDS"])
ENRICHMENT_CONCURRENCY = int(SETTINGS["ENRICHMENT_CONCURRENCY"])
SNIPPET_MAX_CHARS = int(SETTINGS["SNIPPET_MAX_CHARS"])
MAX_UPSTREAM_BYTES = int(SETTINGS["MAX_UPSTREAM_BYTES"])
SNIPPET_INPUT_MAX_CHARS = int(SETTINGS["SNIPPET_INPUT_MAX_CHARS"])


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    async with httpx.AsyncClient(
        timeout=120, follow_redirects=False, trust_env=False,
        headers={"Accept-Encoding": "identity"},
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
    ) as client:
        application.state.client = client
        application.state.crawls = asyncio.Semaphore(ENRICHMENT_CONCURRENCY)
        application.state.readiness_lock = asyncio.Lock()
        application.state.readiness_until = 0.0
        application.state.ready = False
        yield


app = FastAPI(title="SourceFerry", description="web search and fetch for AI clients", lifespan=lifespan)


class SearchRequest(BaseModel):
    queries: list[Annotated[str, Field(max_length=MAX_QUERY_CHARS)]] = Field(max_length=MAX_INPUT_QUERIES)
    max_results: int = 8


class FetchRequest(BaseModel):
    url: str = Field(max_length=MAX_URL_CHARS)


def check_auth(authorization: str | None) -> None:
    expected = f"Bearer {LOCAL_WEB_API_TOKEN}"

    if authorization is None or not hmac.compare_digest(
        authorization.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )


app.add_middleware(
    RequestGuard, authenticate=lambda authorization: check_auth(authorization),
    max_bytes=int(SETTINGS["MAX_REQUEST_BYTES"]),
    max_requests=int(SETTINGS["GATEWAY_MAX_REQUESTS"]),
    deadline=float(SETTINGS["REQUEST_DEADLINE_SECONDS"]),
)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
    # Never reflect request input or arbitrary parser context in error responses.
    return JSONResponse({"detail": [
        {"loc": item["loc"], "type": item["type"], "msg": "Invalid request field"}
        for item in error.errors()
    ]}, status_code=422)


@app.get("/health")
async def health():
    return {"status": "ok"}


def clean_result_url(url: str) -> str:
    url = url.strip()

    match = re.fullmatch(
        r"\[https?://[^\]]+\]\((https?://[^)]+)\)",
        url,
    )

    if match:
        return match.group(1)

    return url


async def upstream_json(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> Any:
    async with client.stream(method, url, **kwargs) as response:
        response.raise_for_status()
        if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
            raise ValueError("Compressed upstream response refused")
        size = response.headers.get("content-length")
        if size and (len(size) > 10 or not size.isdecimal() or int(size) > MAX_UPSTREAM_BYTES):
            raise ValueError("Upstream response too large")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > MAX_UPSTREAM_BYTES:
                raise ValueError("Upstream response too large")
            body.extend(chunk)
    return json.loads(body)


@app.get("/ready")
async def ready(request: Request) -> JSONResponse:
    state = request.app.state
    # Cache both outcomes so public polling cannot cause unbounded backend probes.
    async with state.readiness_lock:
        now = asyncio.get_running_loop().time()
        if now >= state.readiness_until:
            try:
                async with asyncio.timeout(3):
                    async def probe(url: str) -> None:
                        async with state.client.stream("GET", url, timeout=2) as response:
                            response.raise_for_status()
                    await asyncio.gather(probe(f"{SEARXNG_URL}/"), probe(f"{CRAWL4AI_URL}/health"))
                state.ready = True
            except (httpx.HTTPError, TimeoutError):
                state.ready = False
            state.readiness_until = asyncio.get_running_loop().time() + 5
    return JSONResponse({"status": "ready" if state.ready else "unavailable"}, 200 if state.ready else 503)


async def crawl_payload(client: httpx.AsyncClient, url: str, semaphore: asyncio.Semaphore) -> dict[str, Any]:
    try:
        async with semaphore:
            async with asyncio.timeout(CRAWL4AI_TIMEOUT_SECONDS):
                data = await upstream_json(
                    client, "POST", f"{CRAWL4AI_URL}/md",
                    headers={"Authorization": f"Bearer {CRAWL4AI_API_TOKEN}"},
                    json={"url": url}, timeout=CRAWL4AI_TIMEOUT_SECONDS,
                )
    except (httpx.TimeoutException, TimeoutError) as error:
        raise HTTPException(status_code=504, detail="Crawl4AI timed out") from error
    except (httpx.HTTPError, ValueError, RecursionError) as error:
        raise HTTPException(status_code=502, detail="Crawl4AI request failed") from error

    if not isinstance(data, dict) or data.get("success") is not True:
        raise HTTPException(status_code=502, detail="Crawl4AI did not return a successful crawl")
    markdown = data.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        raise HTTPException(status_code=502, detail="Crawl4AI returned no Markdown")
    return data


async def crawl_markdown(
    client: httpx.AsyncClient,
    url: str,
    semaphore: asyncio.Semaphore,
) -> str | None:
    try:
        data = await crawl_payload(client, url, semaphore)
        return data["markdown"].strip()
    except HTTPException:
        return None


def collapse_adjacent_labeled_duplicates(text: str) -> str:
    pattern = re.compile(
        r"(?P<prefix>(?:\d+\.\s*)?)"
        r"(?P<label>[A-Z][A-Za-z0-9 &/+_-]{1,48})"
        r"\s*[—:]\s*"
        r"(?P<first>[^.!?]{8,220}[.!?])"
        r"\s*"
        r"(?P=label)"
        r"\s*[—:]\s*"
        r"(?P<second>[^.!?]{8,220}[.!?])"
    )

    while True:
        def replace(match: re.Match) -> str:
            first = match.group("first").strip()
            second = match.group("second").strip()
            richer = first if len(first) >= len(second) else second

            return (
                f'{match.group("prefix")}'
                f'{match.group("label").strip()} — {richer}'
            )

        cleaned, count = pattern.subn(replace, text)

        if count == 0 or cleaned == text:
            return text

        text = cleaned


def trim_page_chrome(url: str, markdown: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except Exception:
        return markdown

    lines = markdown.splitlines()

    if host in {"github.com", "www.github.com"}:
        for index, line in enumerate(lines):
            if line.strip() == "# DeepSeek Harness":
                return "\n".join(lines[index:])

        return markdown

    if host in {
        "reddit.com",
        "www.reddit.com",
        "old.reddit.com",
    }:
        start_index = None

        for index, line in enumerate(lines):
            stripped = line.strip()

            if stripped.startswith("# ") and not stripped.startswith("## "):
                start_index = index
                break

        if start_index is None:
            return markdown

        kept: list[str] = []

        for line in lines[start_index:]:
            stripped = line.strip()

            if stripped.casefold() == "read more":
                break

            kept.append(line)

        if kept:
            return "\n".join(kept)

    return markdown


def clean_markdown_structure(markdown: str) -> str:
    lines = markdown.splitlines()
    output: list[str] = []
    next_nonempty = [""] * len(lines)
    following = ""
    for index in range(len(lines) - 1, -1, -1):
        next_nonempty[index] = following
        if lines[index].strip():
            following = lines[index].strip()

    for index, raw_line in enumerate(lines):
        stripped = raw_line.strip()

        if not stripped:
            output.append(raw_line)
            continue

        heading = re.match(r"^#{1,6}\s+(.+?)\s*$", stripped)

        if heading:
            title = re.sub(
                r"[*_`~]+",
                "",
                heading.group(1),
            ).strip()

            next_line = next_nonempty[index]

            next_plain = re.sub(
                r"[*_`~]+",
                "",
                next_line,
            ).strip()

            if (
                title
                and next_plain.casefold().startswith(title.casefold())
            ):
                continue

        compact = re.sub(
            r"[^a-z]+",
            "",
            stripped.casefold(),
        )

        if compact in {
            "npxgitclone",
            "npmgitclone",
            "pnpngitclone",
            "pnpmgitclone",
            "yarngitclone",
        }:
            continue

        output.append(raw_line)

    return "\n".join(output)


def repair_fused_markdown_lines(markdown: str) -> str:
    output: list[str] = []

    heading_verbs = (
        "keeps",
        "helps",
        "lets",
        "makes",
        "works",
        "runs",
        "provides",
        "supports",
        "enables",
        "manages",
        "uses",
    )

    for raw_line in markdown.splitlines():
        stripped = raw_line.strip()

        if not stripped:
            output.append(raw_line)
            continue

        if (
            len(stripped) <= 120
            and "http://" not in stripped
            and "https://" not in stripped
            and "[" not in stripped
            and "]" not in stripped
            and "`" not in stripped
        ):
            repaired = re.sub(
                r"(?<=[a-z])(?=(?:Install|Download|Open|View|Run|Start|Learn|Read|Ready)\b)",
                " ",
                stripped,
            )

            if repaired.startswith("#"):
                verbs = "|".join(heading_verbs)

                repaired = re.sub(
                    rf"\b([A-Z][A-Za-z0-9_-]{{2,}})({verbs})\b",
                    r"\1 \2",
                    repaired,
                )

            output.append(repaired)
            continue

        output.append(raw_line)

    return "\n".join(output)


def strip_leading_page_chrome(text: str) -> str:
    patterns = (
        r"^Skip to main content\s+",
        r"^Skip to content\s+",
        r"^←\s*Back to blog\s+",
        r"^Back to blog\s+",
    )

    changed = True

    while changed:
        changed = False

        for pattern in patterns:
            cleaned = re.sub(
                pattern,
                "",
                text,
                count=1,
                flags=re.IGNORECASE,
            )

            if cleaned != text:
                text = cleaned.lstrip()
                changed = True

    return text


def markdown_to_snippet(markdown: str, max_chars: int = SNIPPET_MAX_CHARS) -> str:
    text = repair_fused_markdown_lines(
        clean_markdown_structure(markdown[:SNIPPET_INPUT_MAX_CHARS])
    )

    text = re.sub(
        r"\\(?=[^\w\s])",
        "",
        text,
    )

    text = re.sub(
        r"<(https?://[^>\s]+)>",
        r"\1",
        text,
    )

    text = re.sub(
        r"!\[[^\]]*\]\([^)]+\)",
        " ",
        text,
    )

    text = re.sub(
        r"\[([^\]]+)\]\([^)]+\)",
        r"\1 ",
        text,
    )

    text = re.sub(
        r"[#*_>`~]+",
        " ",
        text,
    )

    cleaned_lines: list[str] = []
    seen_lines: set[str] = set()

    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()

        if not line:
            continue

        key = line.casefold()

        if key in seen_lines:
            continue

        seen_lines.add(key)
        cleaned_lines.append(line)

    text = " ".join(cleaned_lines)

    text = re.sub(
        r"\.{4,}",
        "...",
        text,
    )

    text = collapse_adjacent_labeled_duplicates(text)
    text = strip_leading_page_chrome(text)

    text = re.sub(
        r"\s+",
        " ",
        text,
    ).strip()

    text = re.sub(
        r"\s+([,.;:!?])",
        r"\1",
        text,
    )

    text = re.sub(
        r"\s+(['’])(?=(?:s|t|re|ve|d|ll|m)\b)",
        r"\1",
        text,
    )

    text = re.sub(
        r"\(\s+([^()]{1,80}?)\s+\)",
        r"(\1)",
        text,
    )

    if len(text) <= max_chars:
        return text

    # Reserve the ellipsis so the final snippet also respects the limit.
    candidate = text[:max(0, max_chars - 3)]
    minimum_boundary = int(max_chars * 0.60)

    boundaries = [
        candidate.rfind(". "),
        candidate.rfind("! "),
        candidate.rfind("? "),
    ]

    boundary = max(boundaries)

    if boundary >= minimum_boundary:
        return candidate[:boundary + 1].rstrip() + "..."

    return candidate.rstrip() + "..."


def page_snippet(url: str, markdown: str) -> str:
    return markdown_to_snippet(trim_page_chrome(url, markdown[:SNIPPET_INPUT_MAX_CHARS]))


@app.post("/search")
async def search(
    request: SearchRequest,
    http_response: Response,
    connection: Request,
) -> dict[str, Any]:
    queries = [
        query.strip()
        for query in request.queries
        if isinstance(query, str) and query.strip()
    ][:4]

    max_results = max(
        1,
        min(int(request.max_results), 20),
    )

    results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    enriched_count = 0

    client = connection.app.state.client
    semaphore = connection.app.state.crawls
    for query in queries:
        try:
            async with asyncio.timeout(SEARXNG_TIMEOUT_SECONDS):
                data = await upstream_json(
                    client, "GET", f"{SEARXNG_URL}/search",
                    params={"q": query, "format": "json"}, timeout=SEARXNG_TIMEOUT_SECONDS,
                )
        except (httpx.TimeoutException, TimeoutError) as error:
            raise HTTPException(status_code=504, detail="SearXNG timed out") from error
        except (httpx.HTTPError, ValueError, RecursionError) as error:
            raise HTTPException(status_code=502, detail="SearXNG request failed") from error

        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise HTTPException(status_code=502, detail="SearXNG returned invalid results")

        for item in data.get("results", []):
            if not isinstance(item, dict):
                continue
            url = item.get("url")

            if isinstance(url, str):
                url = clean_result_url(url)

            if not isinstance(url, str) or not valid_http_url(url):
                continue

            canonical = canonical_url(url)

            if canonical in seen_urls:
                continue

            seen_urls.add(canonical)

            results.append(
                {
                    "url": url,
                    "title": item.get("title") if isinstance(item.get("title"), str) else None,
                    "snippet": item.get("content") if isinstance(item.get("content"), str) else "",
                }
            )

            if len(results) >= max_results:
                break

        if len(results) >= max_results:
            break

    async def enrich(
        result: dict[str, Any],
    ) -> dict[str, Any]:
        nonlocal enriched_count
        markdown = await crawl_markdown(client, result["url"], semaphore)

        if markdown:
            snippet = await asyncio.to_thread(page_snippet, result["url"], markdown)
            if snippet:
                enriched_count += 1
                return {**result, "snippet": snippet}

        return result

    enriched = await asyncio.gather(
        *(enrich(result) for result in results)
    )

    # Diagnostics let installation checks distinguish crawling from fallback
    # while preserving the existing response body consumed by Harness.
    http_response.headers["X-Crawl4AI-Enriched"] = str(enriched_count)
    http_response.headers["X-Crawl4AI-Fallback"] = str(len(enriched) - enriched_count)
    return {
        "sources": enriched,
        "truncated": False,
    }


@app.post("/fetch")
async def fetch(
    request: FetchRequest,
    connection: Request,
) -> dict[str, Any]:
    url = request.url.strip()
    if not valid_http_url(url):
        raise HTTPException(status_code=422, detail="url must be an HTTP or HTTPS URL without credentials")

    data = await crawl_payload(connection.app.state.client, url, connection.app.state.crawls)

    return {
        "url": url,
        "content": data,
        "truncated": False,
    }
