"""Standard-library helpers for setup and authenticated gateway clients."""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SECRET_KEYS = ("LOCAL_WEB_API_TOKEN", "CRAWL4AI_API_TOKEN", "SEARXNG_SECRET_KEY")
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def parse_env(text: str) -> dict[str, str]:
    """Read literal .env values without executing shell code or expanding variables."""
    result: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = ASSIGNMENT.fullmatch(line)
        if not match:
            raise ValueError(f"Invalid .env assignment on line {number}.")
        key, value = match.groups()
        if key in result:
            raise ValueError(f"Duplicate .env setting: {key}.")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key] = value
    return result


def read_env(path: Path) -> dict[str, str]:
    return parse_env(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}


def is_placeholder(value: str) -> bool:
    return not value or value.upper().startswith(("CHANGE_ME", "REPLACE_ME")) or value.startswith("<")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward a bearer token to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GatewayClient:
    def __init__(self, env_file: Path, base_url: str | None = None, timeout: float | None = None):
        self.settings = read_env(env_file)
        self.base_url = (
            base_url
            or os.environ.get("LOCAL_WEB_GATEWAY_URL")
            or self.settings.get("LOCAL_WEB_GATEWAY_URL")
            or "http://127.0.0.1:8080"
        ).rstrip("/")
        parts = urlsplit(self.base_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("Gateway base URL must be an HTTP(S) URL without credentials, query, or fragment.")
        self.token = os.environ.get("LOCAL_WEB_API_TOKEN") or self.settings.get("LOCAL_WEB_API_TOKEN", "")
        configured_timeout = os.environ.get("API_CLIENT_TIMEOUT_SECONDS") or self.settings.get("API_CLIENT_TIMEOUT_SECONDS", "1200")
        self.timeout = float(timeout if timeout is not None else configured_timeout)
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("Client timeout must be a positive finite number.")
        self.opener = urllib.request.build_opener(NoRedirect())

    def redact(self, text: str) -> str:
        values = [self.token] + [os.environ.get(key) or self.settings.get(key, "") for key in SECRET_KEYS]
        for value in values:
            if value and not is_placeholder(value):
                text = text.replace(value, "[REDACTED]")
        return text

    def request(self, path: str, payload: dict | None = None, *, authenticated: bool = True, timeout: float | None = None) -> tuple[int, dict]:
        headers = {"Accept": "application/json"}
        if authenticated:
            if is_placeholder(self.token):
                raise ValueError("Gateway token missing. Run scripts/setup.py or set LOCAL_WEB_API_TOKEN securely.")
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=headers)
        try:
            with self.opener.open(request, timeout=timeout or self.timeout) as response:
                status = response.status
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            body = error.read(8192).decode("utf-8", errors="replace")
            try:
                decoded = json.loads(body)
            except ValueError:
                decoded = {"detail": self.redact(body[:1000])}
            return error.code, decoded
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise RuntimeError(self.redact(f"Cannot reach gateway: {error}")) from None
        try:
            decoded = json.loads(body)
        except ValueError:
            raise RuntimeError(f"Gateway returned HTTP {status} with a non-JSON response.") from None
        if not isinstance(decoded, dict):
            raise RuntimeError("Gateway response must be a JSON object.")
        return status, decoded
