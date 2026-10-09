# SourceFerry package validation

Validated on 9 October 2026 against the user's `search_gateway.md` reference.

The SourceFerry branding and MIT license update was checked on the same date. The renamed Compose configuration resolves to `sourceferry`, all 11 installer tests pass, and both installer help commands identify SourceFerry. The release archive includes the MIT license with `Copyright (c) 2026 HashForkTech` alongside the original upstream license copies. The full live-stack results below were collected before this metadata update; search and fetch processing did not change.

## Completed checks

| Check | Result |
| --- | --- |
| One-command Windows installation | The documented `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1` command completed with exit code 0 against an isolated Docker Compose project. |
| Environment | Windows PowerShell 5.1.26100.9444, Docker Engine 29.7.2, Compose 5.4.0, Linux x86-64 backend with approximately 7.6 GiB RAM and 16 CPUs. |
| Gateway image | Builds with the pinned Python 3.12.15 base and four pinned direct dependencies; application runs as the unprivileged `gateway` user. |
| Automated tests | All 78 tests passed inside the Linux gateway image: 41 gateway regressions, 5 installation/client tests, 11 installer tests, and 21 acceptance-verifier tests. |
| Bash installer | Syntax and four mocked Docker orchestration tests passed in Linux, including failed verification and reruns that preserve the application services. A full native Linux-host installation was not performed. |
| Readiness | SearXNG, Crawl4AI, and the gateway became healthy; the published Windows host health endpoint returned the expected response. |
| Authentication | Missing and incorrect bearer tokens were rejected with HTTP 401 on both search and fetch. |
| Browser extraction | The installed gateway fetched the bundled known HTML through public HTTPS and returned 1,073 Markdown characters, including both the static marker and text created by browser JavaScript. |
| Live search and fetch | IANA example-domain and Docker Compose queries returned relevant official sources; a matching source returned by each search was fetched and its topic text verified. |
| Crawl enrichment | All four sources from the two live search cases were enriched by Crawl4AI; none used the SearXNG fallback in this run. |
| Credential display/recovery | Successful installation and `-ShowToken` displayed the same saved gateway token. Internal backend secrets were absent from both outputs; all three secrets were absent from the JSON report. Reinstallation preserved the private configuration byte for byte. |
| Release archive | The ZIP uses an explicit source-file allowlist, includes both upstream license copies, and excludes real `.env` files, logs, local runtimes, caches, and validation artifacts. A SHA-256 checksum accompanies it. |

The automated tests cover every cleanup example in the reference, canonical URL deduplication, query/result limits, concurrent enrichment, per-result failure fallback, raw fetch preservation, upstream timeouts and malformed responses. Installer and verifier tests exercise failure reporting, credential handling, preserved configuration, incorrect content, blocked pages, and absent browser rendering or enrichment.

## Preserved tuning and deployment adjustments

The original limits remain: four queries, eight results by default, one to twenty results, four concurrent enrichments per search request, 120-second upstream timeouts, and 1200-character search snippets. Fetches preserve Crawl4AI's full successful Markdown payload.

The snippet limit now includes the ellipsis; the reference could emit up to 1203 characters. Invalid upstream data receives JSON errors (502, or 504 for timeouts). Empty cleaned crawl output retains the original search snippet. Deployment adds independent secret generation, image digest pins, a non-root gateway image, bounded logs and a loopback default; remote clients need the documented LAN bind setting.

GitHub trimming retains the exact `# DeepSeek Harness` heading rule from the reference. Reddit trimming uses the first top-level heading and the `Read more` stop marker. These rules are intentionally specific to the reference's observed page output.

## Deployment verification

Each installation runs its own checks and saves `artifacts/installation-report.json`. Live checks use expected domains and topic text instead of fixed search rankings. The browser fixture is bundled HTML echoed by [HTTPBin's public base64 endpoint](https://httpbin.org/); it contains no credentials and preserves Crawl4AI's protection against private-network destinations. All verification uses the installed gateway and crawler.

Successful validation on this machine does not guarantee access from another network. Search engines and websites can change or block requests. Persistent failures return a nonzero installer exit code and retain the services for troubleshooting. The README provides commands to repeat verification and recover the gateway key.
