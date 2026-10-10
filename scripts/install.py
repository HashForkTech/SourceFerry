"""Containerized installer helpers. No Docker socket or host Python is needed."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import platform
import re
import shutil
import socket
import sys
from urllib.parse import urlsplit
import urllib.request

from common import ROOT, SECRET_KEYS, is_placeholder, read_env, numeric_settings, valid_http_url

MIN_COMPOSE_VERSION = (2, 24, 0)
MIN_ENGINE_VERSION = (28, 0, 0)
MIN_MEMORY = 4 * 1024**3
MIN_DISK = 10 * 1024**3
MIN_CPUS = 2
PUBLIC_DNS = ("github.com", "ghcr.io", "registry-1.docker.io")
REPORT_STAGES = {"helper-image", "prerequisites", "configuration", "images", "offline-tests", "services", "published-health", "verification"}
ATTRIBUTION = "This product includes software developed by UncleCode (https://x.com/unclecode) as part of the Crawl4AI project (https://github.com/unclecode/crawl4ai)."


def validate_compose_version(version: str) -> None:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+][A-Za-z0-9_.-]+)?", version.strip())
    if not match or tuple(int(part) for part in match.groups()) < MIN_COMPOSE_VERSION:
        raise ValueError("Docker Compose v2.24.0 or later is required. Update Docker Desktop or the Docker Compose plugin.")


def validate_engine_version(version: str) -> None:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+][A-Za-z0-9_.-]+)?", version.strip())
    if not match or tuple(int(part) for part in match.groups()) < MIN_ENGINE_VERSION:
        raise ValueError("Docker Engine 28.0.0 or later is required for localhost port isolation.")


def validate_resources(memory_bytes: int, cpus: int, disk_bytes: int) -> None:
    if memory_bytes < MIN_MEMORY:
        raise ValueError("Docker needs at least 4 GiB RAM; 8 GiB is recommended. Increase Docker's memory allocation.")
    if cpus < MIN_CPUS:
        raise ValueError("Docker needs at least 2 CPUs.")
    if disk_bytes < MIN_DISK:
        raise ValueError("At least 10 GiB of free disk space is required in the installation directory.")


def validate_cpu(machine: str, cpuinfo: str) -> None:
    if machine.lower() not in {"x86_64", "amd64"}:
        raise ValueError("This installer supports verified x86-64 Docker hosts; other architectures require separate validation.")
    flags = [set(line.partition(":")[2].split()) for line in cpuinfo.splitlines() if line.lower().startswith("flags")]
    if not flags or any(not {"sse4_2", "popcnt"} <= cpu_flags for cpu_flags in flags):
        raise ValueError("The Docker host CPU must support SSE4.2 and POPCNT for the pinned Crawl4AI image.")


def validate_environment(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("A regular .env file is required; run the installer first.")
    values = read_env(path)
    secrets = [values.get(key, "") for key in SECRET_KEYS]
    if any(is_placeholder(value) or len(value) < 32 or re.search(r"[^A-Za-z0-9._~+/=-]", value) for value in secrets):
        raise ValueError("Each secret must contain at least 32 characters using letters, digits, or common base64/token punctuation.")
    if len(set(secrets)) != len(secrets):
        raise ValueError("Gateway, Crawl4AI and SearXNG secrets must be different.")
    bind = values.get("GATEWAY_BIND_ADDRESS", "127.0.0.1")
    try:
        address = ipaddress.ip_address(bind)
        port = int(values.get("GATEWAY_PORT", "8080"))
    except ValueError:
        raise ValueError("GATEWAY_BIND_ADDRESS must be an IP address and GATEWAY_PORT must be an integer.") from None
    if not 1 <= port <= 65535:
        raise ValueError("GATEWAY_PORT must be between 1 and 65535.")
    numeric_settings(values)
    local_address = ipaddress.ip_address("::1" if address.version == 6 else "127.0.0.1") if address.is_unspecified else address
    hostname = f"[{local_address}]" if local_address.version == 6 else str(local_address)
    host_url = f"http://{hostname}:{port}"
    client_url = values.get("LOCAL_WEB_GATEWAY_URL", host_url).rstrip("/")
    if client_url == "http://127.0.0.1:8080":
        client_url = host_url
    parts = urlsplit(client_url)
    if not valid_http_url(client_url) or parts.query or parts.fragment:
        raise ValueError("LOCAL_WEB_GATEWAY_URL must be an HTTP(S) URL without credentials, query or fragment.")
    return {"bind": bind, "port": str(port), "host_url": host_url, "client_url": client_url, **{key: values[key] for key in SECRET_KEYS}}


def check_port(bind: str, port: int, existing_gateway: bool = False) -> None:
    family = socket.AF_INET6 if ":" in bind else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as listener:
            listener.bind((bind, port))
    except OSError:
        if not existing_gateway:
            raise RuntimeError(f"Cannot bind the gateway address {bind}:{port}. Choose a free port and an address assigned to this host in .env.") from None


def check_host_health(base_url: str) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"{base_url.rstrip('/')}/health", timeout=15) as response:
        payload = json.loads(response.read(8192))
        if response.status != 200 or payload != {"status": "ok"}:
            raise RuntimeError("The published gateway health endpoint returned an unexpected response.")


def safe_report(path: Path, *, stage: str | None = None, status: str | None = None, initialize: bool = False) -> None:
    if path.is_symlink():
        raise ValueError("The installation report must not be a symlink.")
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {} if initialize or not path.exists() else json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError("Existing installation report is invalid.")
    report.setdefault("installer_stages", [])
    report["updated_at"] = datetime.now(timezone.utc).isoformat()
    if initialize:
        report["installer_status"] = "running"
    if stage:
        # These fields are fixed labels, never a captured command or its output.
        if stage not in REPORT_STAGES or status not in {"passed", "failed", "verified", "verification_failed"}:
            raise ValueError("Invalid report stage or status.")
        report["installer_stages"].append({"stage": stage, "status": status})
        if status in {"verified", "verification_failed", "failed"}:
            report["installer_status"] = status
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def show_connection(path: Path) -> None:
    config = validate_environment(path)
    print(f"LOCAL_WEB_GATEWAY_URL={config['client_url']}")
    print(f"LOCAL_WEB_API_TOKEN={config['LOCAL_WEB_API_TOKEN']}")
    print("Use this gateway token as Authorization: Bearer <token> in your client.")
    print("The token is saved as LOCAL_WEB_API_TOKEN in .env in this installation directory.")
    print("Display it again (Linux): bash ./install.sh --show-token")
    print("Display it again (Windows): powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\\install.ps1 -ShowToken")
    if ipaddress.ip_address(config["bind"]).is_unspecified:
        print("For a client on another machine, replace the loopback address with this machine's LAN address.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, epilog=ATTRIBUTION)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    sub = parser.add_subparsers(dest="command", required=True)
    preflight = sub.add_parser("preflight")
    preflight.add_argument("--compose-version", required=True)
    preflight.add_argument("--engine-version", required=True)
    preflight.add_argument("--memory-bytes", type=int, required=True)
    preflight.add_argument("--cpus", type=int, required=True)
    preflight.add_argument("--offline", action="store_true", help="Skip public DNS checks, for token recovery only.")
    sub.add_parser("config")
    sub.add_parser("token")
    port = sub.add_parser("port-check")
    port.add_argument("--existing-gateway", action="store_true")
    sub.add_parser("host-health")
    report = sub.add_parser("report")
    report.add_argument("--report", type=Path, default=ROOT / "artifacts" / "installation-report.json")
    report.add_argument("--initialize", action="store_true")
    report.add_argument("--stage")
    report.add_argument("--status")
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            validate_compose_version(args.compose_version)
            validate_engine_version(args.engine_version)
            validate_resources(args.memory_bytes, args.cpus, shutil.disk_usage(ROOT).free)
            validate_cpu(platform.machine(), Path("/proc/cpuinfo").read_text(encoding="utf-8"))
            if not args.offline:
                for hostname in PUBLIC_DNS:
                    socket.getaddrinfo(hostname, 443)
            print("PASS Docker resources, CPU compatibility, disk space and DNS")
        elif args.command == "config":
            values = validate_environment(args.env_file)
            print("\n".join(values[key] for key in ("bind", "port", "host_url", "client_url")))
        elif args.command == "token":
            show_connection(args.env_file)
        elif args.command == "port-check":
            values = validate_environment(args.env_file)
            check_port(values["bind"], int(values["port"]), args.existing_gateway)
            print("PASS Published gateway address and port")
        elif args.command == "host-health":
            check_host_health(validate_environment(args.env_file)["host_url"])
            print("PASS Published gateway health endpoint")
        else:
            safe_report(args.report, stage=args.stage, status=args.status, initialize=args.initialize)
    except (OSError, ValueError, RuntimeError) as error:
        # Do not include exception text from environment values, HTTP bodies or JSON.
        message = str(error)
        try:
            redacted_values = read_env(args.env_file).values() if args.env_file.is_file() else ()
        except (OSError, ValueError):
            redacted_values = ()
        for value in redacted_values:
            if value and len(value) >= 32:
                message = message.replace(value, "[REDACTED]")
        print(f"Installer check failed: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
