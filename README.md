# SourceFerry

*A local web search and fetch gateway for AI clients*

SourceFerry brings web search and page content to AI clients through a local, authenticated API. It cleans and deduplicates results, and limits successfully enriched search snippets to help reduce the tokens sent to AI models. Powered by SearXNG and Crawl4AI, it combines metasearch with browser rendering and readable Markdown extraction.

```text
AI client -> gateway:8080 -> SearXNG (search)
                       -> Crawl4AI / Chromium (page content)
```

Only the gateway exposes a host port. SearXNG and Crawl4AI communicate on Docker's private network. No paid search or LLM API key is required.

## Requirements

| Requirement | Details |
| --- | --- |
| Docker | Docker Engine 28.0.0+ and Compose 2.24+ (`docker compose`), or current Docker Desktop. Docker must be running and accessible to your user. |
| System | Linux with Bash, or Windows with PowerShell and Docker Desktop's WSL 2 backend in Linux container mode. On Windows, extract/clone to a local NTFS directory so private file permissions can be applied. |
| CPU | Modern x86-64. VMs must expose SSE4.2 and POPCNT; host CPU passthrough is recommended. Other architectures need separate image compatibility checks. |
| Memory / disk | Allocate at least 4 GiB RAM and 2 CPU threads to Docker; 8 GiB RAM and 4 CPU threads are recommended for the whole stack. Allow at least 10 GiB free disk space, preferably 10–15 GiB. |
| Network | DNS and outbound HTTP/HTTPS to image registries, PyPI during the gateway build, search engines, and fetched websites. Installation verification also uses `httpbin.org`, `iana.org`, and `docs.docker.com`. |

**Host Node.js, npm, Python, and Chromium are not required.** Python, verification tools, and the browser run in containers. Git is optional if you download the release ZIP.

Install Docker first using its [Linux instructions](https://docs.docker.com/engine/install/) or [Windows instructions](https://docs.docker.com/desktop/setup/install/windows-install/).

### Docker installation on Ubuntu

Install Docker Engine and the Compose plugin using [Docker's official APT repository instructions](https://docs.docker.com/engine/install/ubuntu/#install-using-the-apt-repository).

**Docker installed through Snap is incompatible with SourceFerry's security settings.** SourceFerry enables `no-new-privileges:true`, which Snap's AppArmor confinement can prevent from working. [Canonical documents this limitation](https://github.com/canonical/docker-snap#usage).

Typical symptoms are containers repeatedly restarting with messages such as:

```text
exec /usr/local/searxng/entrypoint.sh: operation not permitted
exec /usr/bin/bash: operation not permitted
```

Check whether Docker is installed through Snap:

```bash
sudo snap list docker
sudo docker info --format 'DockerRootDir={{.DockerRootDir}}'
```

A Docker root directory under `/var/snap/docker/` indicates Snap-managed storage.

To switch to the official Docker packages:

1. Preserve the SourceFerry installation directory, especially `.env`, which contains your configuration and API keys.
2. If Docker hosts other applications, back up their data and plan their migration first. **Disabling Snap Docker stops all containers it manages.** Its images, containers, and volumes do not automatically transfer to the replacement installation.
3. From the SourceFerry directory, stop the stack and disable Snap Docker:

   ```bash
   sudo docker compose stop
   sudo snap disable docker
   ```

4. Follow [Docker's official Ubuntu APT installation instructions](https://docs.docker.com/engine/install/ubuntu/#install-using-the-apt-repository). Install Docker Engine, Buildx, and the Compose plugin. Leave Snap Docker disabled while the replacement daemon is running.
5. Confirm that the previously incompatible security setting now works:

   ```bash
   sudo /usr/bin/docker run --rm --network none \
     --security-opt no-new-privileges:true hello-world
   ```

   Expected output includes:

   ```text
   Hello from Docker!
   ```

6. From the SourceFerry directory, rebuild and verify the installation:

   ```bash
   sudo bash ./install.sh
   ```

The installer downloads/builds the required images and tests installation, search, and fetch. It preserves valid existing configuration and API keys in `.env`.

## Install with one command

Clone the repository or extract the release ZIP, open a terminal in its directory, and run:

Linux:

```bash
sudo bash ./install.sh
```

Use `sudo` when Docker requires root access or the installation files are root-owned. You can omit it if your user has Docker access and owns the installation files.

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

## Connect an AI client

Set these values in your client's environment or private client configuration, using the URL and key printed by the installer:

```dotenv
LOCAL_WEB_GATEWAY_URL=http://127.0.0.1:8080
LOCAL_WEB_API_TOKEN=<your gateway API key>
```

Requests to `POST /search` and `POST /fetch` send `Authorization: Bearer <your gateway API key>`. `GET /health` is public liveness; `/ready` checks both backends. Search accepts `queries` and `max_results`; fetch accepts `url`. See the [API contract](docs/API.md) for JSON examples, field limits, response fields, and errors. The client only needs the gateway key.

### Make the gateway accessible on your LAN

By default, the gateway listens on `127.0.0.1`, so only the server itself can connect.

1. Open a terminal on the Ubuntu server and enter the SourceFerry installation directory. Replace the example path with your actual location:

   ```bash
   cd /path/to/SourceFerry
   ```

2. Find the server's LAN IP address:

   ```bash
   ip -4 addr show
   ```

   Choose the address assigned to its Ethernet or Wi-Fi interface. Do not use a Docker bridge address.

3. Edit the hidden configuration file:

   ```bash
   sudo nano .env
   ```

   If the installer ran with `sudo`, you will normally also need `sudo` to read or edit this file.

   For a server whose LAN address is `192.168.1.50`, set:

   ```dotenv
   GATEWAY_BIND_ADDRESS=192.168.1.50
   GATEWAY_PORT=8080
   LOCAL_WEB_GATEWAY_URL=http://192.168.1.50:8080
   ```

   Replace `192.168.1.50` with your server's actual address. Preserve the existing API keys. In Nano, save with **Ctrl+O**, press **Enter**, then exit with **Ctrl+X**.

4. Apply the change with **this single command**:

   ```bash
   sudo docker compose up -d --no-deps --force-recreate gateway
   ```

   This recreates the gateway using the updated configuration without restarting its dependencies. A plain container restart does not apply a changed port binding.

5. From another computer on the same LAN, test:

   ```bash
   curl http://192.168.1.50:8080/health
   ```

   Expected response:

   ```json
   {"status":"ok"}
   ```

6. Configure your AI client with:

   ```dotenv
   LOCAL_WEB_GATEWAY_URL=http://192.168.1.50:8080
   LOCAL_WEB_API_TOKEN=<your existing gateway API key>
   ```

   Search and fetch requests still require:

   ```http
   Authorization: Bearer <your gateway API key>
   ```

Use a stable LAN address, preferably through a DHCP reservation. If you change `GATEWAY_PORT`, update the port in both server and client URLs. Restrict access to trusted clients; use HTTPS or a VPN across untrusted networks.

## Included tuning

- SearXNG's default engines and JSON output are enabled.
- URL repair and canonical deduplication remove duplicate sources. Up to 4 queries are used; results default to 8 and are bounded to 1–20.
- Search and fetch share 4 concurrent crawl slots per gateway process and 120-second upstream timeouts. Eight protected requests can be admitted, each with a 300-second total deadline. Failed enrichment keeps the original SearXNG snippet without applying the cleaned-snippet character cap.
- Search snippets remove Markdown link/image syntax, duplicate text, navigation clutter, and fused labels, with the specific GitHub/Reddit rules described in the API contract. Cleaned snippets are capped at 1,200 characters. Fetch preserves successful crawler payloads within the upstream byte limit; oversized responses fail explicitly.
- Base images are pinned by digest and gateway dependencies by version and wheel hash. The crawler includes pinned dependency and system-package updates. Services have memory/CPU/process limits and capped logs; Chromium receives 1 GiB shared memory.

Edit `.env` using the documented [configuration ranges](docs/CONFIGURATION.md). Runtime changes need `docker compose up -d`; code, dependency, Python-base, or crawler-base changes need the installer or `docker compose up -d --build`. Search engines and websites can block requests or return captchas; verification makes these failures visible.

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

See [operations and recovery](docs/OPERATIONS.md) for credential rotation, upgrades, rollback, and troubleshooting. [CONTRIBUTING.md](CONTRIBUTING.md) documents tests, lint/type checks, dependency locks, and packaging. [VALIDATION.md](VALIDATION.md) records tested revisions and limitations.

## Credits and licenses

SourceFerry's original gateway code, installer, tests, and documentation are licensed under the [MIT License](LICENSE). You may use, modify, and redistribute them, including commercially, while retaining the copyright and license notice. The third-party projects below retain their own licenses.

| Project | Credit | License for the pinned release |
| --- | --- | --- |
| [SearXNG](https://github.com/searxng/searxng) | SearXNG contributors; metasearch discovery. | [GNU AGPL-3.0-or-later](https://github.com/searxng/searxng/blob/6671d89bede8c9fc108b17bb98916170f5657650/LICENSE) |
| [Crawl4AI](https://github.com/unclecode/crawl4ai) | UncleCode and Crawl4AI contributors; browser rendering and Markdown extraction. | [Apache License 2.0 with the upstream attribution requirement](https://github.com/unclecode/crawl4ai/blob/v0.9.4/LICENSE) |

This product includes software developed by UncleCode (https://x.com/unclecode) as part of the Crawl4AI project (https://github.com/unclecode/crawl4ai).

Full upstream license copies and release references are included in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [third_party](third_party/).
