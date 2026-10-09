# SourceFerry

*A local web search and fetch gateway for AI clients*

A local, authenticated search and page-fetch API for Harness or other clients. SearXNG discovers results; Crawl4AI renders pages in Chromium and converts them to Markdown. The gateway deduplicates URLs, cleans enriched search snippets, and retains the original search snippet when a crawl fails. Direct fetches return the full Crawl4AI payload, including `content.markdown`.

```text
Harness -> gateway:8080 -> SearXNG (search)
                       -> Crawl4AI / Chromium (page content)
```

Only the gateway exposes a host port. SearXNG and Crawl4AI communicate on Docker's private network. No paid search or LLM API key is required.

## Requirements

| Requirement | Details |
| --- | --- |
| Docker | Docker Engine 20.10+ and Compose 2.24+ (`docker compose`), or current Docker Desktop. Docker must be running and accessible to your user. |
| System | Linux with Bash, or Windows with PowerShell and Docker Desktop's WSL 2 backend in Linux container mode. On Windows, extract/clone to a local NTFS directory so private file permissions can be applied. |
| CPU | Modern x86-64. VMs must expose SSE4.2 and POPCNT; host CPU passthrough is recommended. Other architectures need separate image compatibility checks. |
| Memory / disk | Allocate at least 4 GiB RAM and 2 CPU threads to Docker; 8 GiB RAM and 4 CPU threads are recommended for the whole stack. Allow at least 10 GiB free disk space, preferably 10–15 GiB. |
| Network | DNS and outbound HTTP/HTTPS to image registries, PyPI during the gateway build, search engines, and fetched websites. Installation verification also uses `httpbin.org`, `iana.org`, and `docs.docker.com`. |

**Host Node.js, npm, Python, and Chromium are not required.** Python, verification tools, and the browser run in containers. Git is optional if you download the release ZIP.

Install Docker first using its [Linux instructions](https://docs.docker.com/engine/install/) or [Windows instructions](https://docs.docker.com/desktop/setup/install/windows-install/).

## Install with one command

Clone the repository or extract the release ZIP, open a terminal in its directory, and run:

Linux:

```bash
bash ./install.sh
```

Windows PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

The installer checks prerequisites, creates a private `.env` with independent secrets, pulls the pinned images, builds the gateway, waits for readiness, and runs installation and search/fetch verification. Repeating installation preserves your existing secrets and configuration. First startup can take several minutes.

The Windows command allows the local installer script for this process; it does not change your saved PowerShell execution policy. [Microsoft documentation](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_powershell_exe?view=powershell-5.1#-executionpolicy-executionpolicy)

Verification checks authentication, browser extraction of known text and JavaScript-generated content, tuned search processing, live search relevance, successful crawl enrichment, and the content fetched from a matching search result. Live checks compare expected official domains and topic text; they do not require fixed rankings. The report is saved as `artifacts/installation-report.json` without credentials. An external-site failure produces a failed verification result and a nonzero exit code while leaving the installed services available for troubleshooting.

**After successful verification, the installer displays the gateway URL and API key (`LOCAL_WEB_API_TOKEN`) for your client.** The key remains in `.env` in the installation directory. To display it again:

```bash
bash ./install.sh --show-token
```

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -ShowToken
```

Keep `.env` private and back it up.

## Connect Harness

Set these values in Harness's environment or private client configuration, using the URL and key printed by the installer:

```dotenv
LOCAL_WEB_GATEWAY_URL=http://127.0.0.1:8080
LOCAL_WEB_API_TOKEN=<your gateway API key>
```

Requests to `POST /search` and `POST /fetch` send `Authorization: Bearer <your gateway API key>`. `GET /health` is public. Search accepts `queries` and `max_results`; fetch accepts `url`. Harness only needs the gateway key.

For Harness on another machine, edit `.env` to set `GATEWAY_BIND_ADDRESS` to the gateway's LAN IP address (or `0.0.0.0`), set `LOCAL_WEB_GATEWAY_URL` to `http://<gateway-lan-ip>:8080`, and apply:

```bash
docker compose up -d gateway
```

Use that LAN URL in Harness. If you change `GATEWAY_PORT`, include the new port in both URLs. Restrict access to trusted clients; use HTTPS or a VPN across untrusted networks.

## Included tuning

- SearXNG's default engines and JSON output are enabled.
- URL repair and canonical deduplication remove duplicate sources. Up to 4 queries are used; results default to 8 and are bounded to 1–20.
- Crawl4AI enrichment runs with 4 concurrent requests and 120-second upstream timeouts. Failed enrichment keeps the original SearXNG snippet.
- Search snippets remove Markdown link/image syntax, duplicate text, navigation clutter, and fused labels, with GitHub/Reddit trimming. Cleaned snippets are capped at 1,200 characters. Full fetch content remains intact.
- Images are pinned by digest. Chromium receives 1 GB of shared memory; services restart with Docker and use capped logs.

Edit `.env` to change the settings described in [.env.example](.env.example), then run `docker compose up -d`. Search engines and websites can block requests or return captchas; verification makes these failures visible.

## Verify and operate

Rerun verification without reinstalling:

```bash
bash ./install.sh --verify
```

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Verify
```

```bash
docker compose ps                     # service status
docker compose logs --tail=100         # recent logs
docker compose stop                   # stop services
docker compose start                  # resume services
```

To update gateway code from a new release, preserve `.env`, replace the package files, and rerun the installer. Existing image references in `.env` are preserved; upstream image upgrades require updating their compatible tag/digest pairs before reinstalling. On Linux, enable Docker at boot; on Windows, enable Docker Desktop startup if desired.

## Credits and licenses

SourceFerry's original gateway code, installer, tests, and documentation are licensed under the [MIT License](LICENSE). You may use, modify, and redistribute them, including commercially, while retaining the copyright and license notice. The third-party projects below retain their own licenses.

| Project | Credit | License for the pinned release |
| --- | --- | --- |
| [SearXNG](https://github.com/searxng/searxng) | SearXNG contributors; metasearch discovery. | [GNU AGPL-3.0-or-later](https://github.com/searxng/searxng/blob/6671d89bede8c9fc108b17bb98916170f5657650/LICENSE) |
| [Crawl4AI](https://github.com/unclecode/crawl4ai) | UncleCode and Crawl4AI contributors; browser rendering and Markdown extraction. | [Apache License 2.0 with the upstream attribution requirement](https://github.com/unclecode/crawl4ai/blob/v0.9.2/LICENSE) |

This product includes software developed by UncleCode (https://x.com/unclecode) as part of the Crawl4AI project (https://github.com/unclecode/crawl4ai).

Full upstream license copies and release references are included in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [third_party](third_party/).
