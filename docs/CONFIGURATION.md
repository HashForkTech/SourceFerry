# Configuration

The installer treats the private `.env` in the installation directory as authoritative and preserves existing values. Numeric constraints come from `gateway/contracts.py` and are checked by both the installer and runtime.

| Setting | Default | Valid values and units |
| --- | --- | --- |
| `GATEWAY_BIND_ADDRESS` | `127.0.0.1` | Host IPv4/IPv6 interface; `0.0.0.0`/`::` publishes on every interface. |
| `GATEWAY_PORT` | `8080` | Integer 1–65535. |
| `LOCAL_WEB_GATEWAY_URL` | `http://127.0.0.1:8080` | Client HTTP(S) URL without credentials, query, or fragment. |
| `SEARXNG_TIMEOUT_SECONDS` | `120` | Positive finite seconds, at most 3600. |
| `CRAWL4AI_TIMEOUT_SECONDS` | `120` | Positive finite seconds, at most 3600. |
| `REQUEST_DEADLINE_SECONDS` | `300` | Positive finite seconds, at most 3600; total request deadline. |
| `API_CLIENT_TIMEOUT_SECONDS` | `1200` | Positive finite seconds, at most 7200; bundled general-purpose client only. The acceptance verifier has its own `--timeout` (360 seconds). |
| `ENRICHMENT_CONCURRENCY` | `4` | Integer 1–32; shared search/fetch crawl slots per gateway process. |
| `GATEWAY_MAX_REQUESTS` | `8` | Integer 1–64; admitted protected requests per process. |
| `SNIPPET_MAX_CHARS` | `1200` | Integer 128–10000; cleaned crawler snippet characters including ellipsis. |
| `SNIPPET_INPUT_MAX_CHARS` | `65536` | Integer 10000–262144; input characters processed per snippet. |
| `MAX_REQUEST_BYTES` | `65536` | Integer 1024–1048576; protected request body bytes. |
| `MAX_UPSTREAM_BYTES` | `8388608` | Integer 65536–33554432; bytes per upstream JSON response. |

The three secrets must be distinct, non-placeholder tokens of at least 32 characters. The installers generate 64 hexadecimal characters each. Keep secrets out of command history, issue reports, source control, and shared logs. Literal quoted values and comments are supported; shell interpolation is not. Use the generated hexadecimal format for secrets so Docker Compose and both installers interpret them identically.

Runtime settings and secrets take effect after container recreation: `docker compose up -d`. Code, dependency locks, `PYTHON_IMAGE`, and `CRAWL4AI_IMAGE` require a rebuild: use the installer or `docker compose up -d --build`. `CRAWL4AI_IMAGE` is the **base** for a local derived image containing the pinned PyJWT and system-package updates. `SEARXNG_IMAGE` is used directly; pull the changed image before recreating it.

Existing `.env` image pins are preserved. For this release, set `CRAWL4AI_IMAGE` to the exact value in the new `.env.example` before reinstalling. Merely replacing `.env.example` does not upgrade an existing installation. Keep tag/digest pairs together and run the acceptance checks after each upgrade.

Compose caps gateway memory/CPU/processes at 1 GiB/2 CPUs/128, SearXNG at 1 GiB/2 CPUs/256, and crawler at 4 GiB/4 CPUs/512. These are ceilings, not reservations. The minimum 4 GiB Docker allocation is for light workloads; 8 GiB is recommended because the combined ceilings total 6 GiB plus host overhead. Raising request or upstream limits can exhaust a container; adjust measured resource needs and rerun tests. The gateway filesystem is read-only except a 64 MiB temporary filesystem. The gateway and crawler drop Linux capabilities and prohibit privilege escalation. Chromium has 1 GiB shared memory.

Do not add worker processes without accounting for multiplied request/crawl limits. The supplied Uvicorn command also limits connections/tasks to 80 as a second boundary.
