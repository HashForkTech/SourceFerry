import asyncio
import hmac
import os
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel


def required_secret(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value or value.upper().startswith(("REPLACE-", "REPLACE_", "CHANGE_ME")) or value.startswith("<"):
        raise RuntimeError(f"{name} must be set to a non-placeholder secret")
    return value


def positive_setting(name: str, default: float) -> float:
    value = float(os.environ.get(name, str(default)))
    if not 0 < value < float("inf"):
        raise RuntimeError(f"{name} must be a positive finite number")
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
SEARXNG_TIMEOUT_SECONDS = positive_setting("SEARXNG_TIMEOUT_SECONDS", 120)
CRAWL4AI_TIMEOUT_SECONDS = positive_setting("CRAWL4AI_TIMEOUT_SECONDS", 120)
ENRICHMENT_CONCURRENCY = int(os.environ.get("ENRICHMENT_CONCURRENCY", "4"))
SNIPPET_MAX_CHARS = int(os.environ.get("SNIPPET_MAX_CHARS", "1200"))
if not 1 <= ENRICHMENT_CONCURRENCY <= 32:
    raise RuntimeError("ENRICHMENT_CONCURRENCY must be between 1 and 32")
if not 128 <= SNIPPET_MAX_CHARS <= 10000:
    raise RuntimeError("SNIPPET_MAX_CHARS must be between 128 and 10000")

app = FastAPI(title="SourceFerry", description="web search and fetch for AI clients")


class SearchRequest(BaseModel):
    queries: list[str]
    max_results: int = 8


class FetchRequest(BaseModel):
    url: str


def check_auth(authorization: str | None):
    expected = f"Bearer {LOCAL_WEB_API_TOKEN}"

    if authorization is None or not hmac.compare_digest(
        authorization.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )


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


def canonical_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        port = parts.port
        hostname = (parts.hostname or "").lower()
    except ValueError:
        return url

    scheme = parts.scheme.lower()

    if hostname.startswith("www."):
        hostname = hostname[4:]

    if ":" in hostname:
        hostname = f"[{hostname}]"

    if (
        port is not None
        and not (scheme == "http" and port == 80)
        and not (scheme == "https" and port == 443)
    ):
        netloc = f"{hostname}:{port}"
    else:
        netloc = hostname

    path = parts.path or "/"

    if path != "/":
        path = path.rstrip("/")

    return urlunsplit(
        (
            scheme,
            netloc,
            path,
            parts.query,
            "",
        )
    )


def valid_http_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        # Reading port validates malformed port numbers as well.
        _ = parts.port
        return (
            parts.scheme.lower() in {"http", "https"}
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and not any(character.isspace() for character in url)
            and not any(ord(character) < 32 for character in url)
        )
    except ValueError:
        return False


async def crawl_payload(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    try:
        response = await client.post(
            f"{CRAWL4AI_URL}/md",
            headers={
                "Authorization": f"Bearer {CRAWL4AI_API_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"url": url},
            timeout=CRAWL4AI_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.TimeoutException as error:
        raise HTTPException(status_code=504, detail="Crawl4AI timed out") from error
    except (httpx.HTTPError, ValueError) as error:
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
) -> str | None:
    try:
        data = await crawl_payload(client, url)
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

            next_line = ""

            for candidate in lines[index + 1:]:
                candidate = candidate.strip()

                if candidate:
                    next_line = candidate
                    break

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
        clean_markdown_structure(markdown)
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


@app.post("/search")
async def search(
    request: SearchRequest,
    http_response: Response,
    authorization: str | None = Header(default=None),
):
    check_auth(authorization)

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

    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        for query in queries:
            try:
                response = await client.get(
                    f"{SEARXNG_URL}/search",
                    params={"q": query, "format": "json"},
                    timeout=SEARXNG_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                data = response.json()
            except httpx.TimeoutException as error:
                raise HTTPException(status_code=504, detail="SearXNG timed out") from error
            except (httpx.HTTPError, ValueError) as error:
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

        semaphore = asyncio.Semaphore(ENRICHMENT_CONCURRENCY)

        async def enrich(
            result: dict[str, Any],
        ) -> dict[str, Any]:
            nonlocal enriched_count
            async with semaphore:
                markdown = await crawl_markdown(
                    client,
                    result["url"],
                )

            if markdown:
                markdown = trim_page_chrome(
                    result["url"],
                    markdown,
                )

                snippet = markdown_to_snippet(markdown)
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
    authorization: str | None = Header(default=None),
):
    check_auth(authorization)

    url = request.url.strip()
    if not valid_http_url(url):
        raise HTTPException(status_code=422, detail="url must be an HTTP or HTTPS URL without credentials")

    async with httpx.AsyncClient(timeout=CRAWL4AI_TIMEOUT_SECONDS, follow_redirects=True) as client:
        data = await crawl_payload(client, url)

    return {
        "url": url,
        "content": data,
        "truncated": False,
    }
