"""Dependency-free configuration and URL contracts shared by runtime and tools."""

import math
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

# default, minimum, maximum; these are deployment controls, not request fields.
INTEGER_SETTINGS = {
    "ENRICHMENT_CONCURRENCY": (4, 1, 32),
    "SNIPPET_MAX_CHARS": (1200, 128, 10000),
    "GATEWAY_MAX_REQUESTS": (8, 1, 64),
    "MAX_REQUEST_BYTES": (65536, 1024, 1048576),
    "MAX_UPSTREAM_BYTES": (8388608, 65536, 33554432),
    "SNIPPET_INPUT_MAX_CHARS": (65536, 10000, 262144),
}
FLOAT_SETTINGS = {
    "SEARXNG_TIMEOUT_SECONDS": (120.0, 3600.0),
    "CRAWL4AI_TIMEOUT_SECONDS": (120.0, 3600.0),
    "REQUEST_DEADLINE_SECONDS": (300.0, 3600.0),
    "API_CLIENT_TIMEOUT_SECONDS": (1200.0, 7200.0),
}
MAX_INPUT_QUERIES = 16
MAX_QUERY_CHARS = 2048
MAX_URL_CHARS = 8192


def numeric_settings(values: Mapping[str, str]) -> dict[str, int | float]:
    result: dict[str, int | float] = {}
    for name, (default, minimum, maximum) in INTEGER_SETTINGS.items():
        try:
            value = int(values.get(name, str(default)))
        except ValueError:
            raise ValueError(f"{name} must be an integer between {minimum} and {maximum}.") from None
        if not minimum <= value <= maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}.")
        result[name] = value
    for name, (float_default, float_maximum) in FLOAT_SETTINGS.items():
        try:
            number = float(values.get(name, str(float_default)))
        except ValueError:
            raise ValueError(f"{name} must be a positive finite number no greater than {float_maximum:g}.") from None
        if not math.isfinite(number) or not 0 < number <= float_maximum:
            raise ValueError(f"{name} must be a positive finite number no greater than {float_maximum:g}.")
        result[name] = number
    return result


def valid_http_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        port = parts.port
        return (
            parts.scheme.lower() in {"http", "https"}
            and bool(parts.hostname)
            and parts.username is None and parts.password is None
            and (port is None or 1 <= port <= 65535)
            and not any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in url)
            and "\\" not in url
        )
    except ValueError:
        return False


def canonical_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        port = parts.port
        hostname = (parts.hostname or "").lower().removeprefix("www.")
    except ValueError:
        return url
    scheme = parts.scheme.lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    if port is not None and (scheme, port) not in {("http", 80), ("https", 443)}:
        hostname += f":{port}"
    return urlunsplit((scheme, hostname, (parts.path or "/").rstrip("/") or "/", parts.query, ""))
