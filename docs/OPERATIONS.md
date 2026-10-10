# Operations and recovery

Use Docker Engine 28.0.0 or later and Compose 2.24.0 or later. Both installers enforce these versions. Earlier Engines have a documented localhost-publication isolation issue; see [Docker port publishing](https://docs.docker.com/engine/network/port-publishing/).

## Updates

1. Save a private backup of `.env` and the current release. Record image IDs with `docker compose images`. Treat the backup as a credential file and restrict its permissions.
2. Replace release files while preserving `.env`. Review the new `.env.example`; update saved image tag/digest pairs explicitly. This release requires the Crawl4AI 0.9.4 pin from that file.
3. Run `bash ./install.sh` on Linux or `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1` on Windows. Both rebuild the gateway and patched crawler and run verification.
4. Inspect `artifacts/installation-report.json`. A failed external-site check leaves services running so you can diagnose it. Do not label a failed run as verified.

Runtime-only tuning can use `docker compose up -d`, followed by installer `--verify` / `-Verify`. Image/base/dependency changes require rebuilding.

## Credential rotation

Rotate all three independent secrets and recreate the services:

```bash
bash ./install.sh --rotate-secrets
```

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -RotateSecrets
```

Update every client with the newly displayed gateway key. Expect in-flight requests and clients using the old key to fail during rotation. The script reparses and verifies every replacement before saving. It preserves other settings and inline comments. If installation fails after writing `.env`, rerun the ordinary installer to finish applying those new credentials; do not rotate again merely to retry.

`--show-token` / `-ShowToken` displays the current gateway credential intentionally. Do not paste that output into shared reports. Backend credentials remain private.

## Troubleshooting

| Symptom | Checks and recovery |
| --- | --- |
| Docker/Engine prerequisite fails | Start Docker; select Linux containers; update Engine/Compose; use a local daemon. The installer report records `failed`. |
| Port conflict | Check `docker compose ps`; choose a free `GATEWAY_PORT` and update the client URL. Use a real local interface for the bind address. |
| `/health` passes but `/ready` fails | Inspect `docker compose ps` and `docker compose logs --tail=100 searxng crawl4ai`. Readiness depends on both backends and can be cached for five seconds. |
| 401 after update/rotation | Reconfigure the client with the saved gateway token. Confirm that all services were recreated from the same `.env`. |
| 413 or 422 | Reduce request body/field sizes; see [API limits](API.md). Increasing byte limits does not change field bounds. |
| 502, fallback snippets, or failed live checks | Inspect backend status, DNS, outbound connectivity, and site blocking/captchas. Repeat verification after a transient problem. Private destinations are intentionally blocked. |
| 503 or 504 | Reduce parallel client requests, honor Retry-After, and inspect memory/CPU usage with `docker stats`. The total deadline includes upload and crawl-slot waits. |
| Container killed / exit 137 | Check Docker memory and configured container ceilings. Avoid increasing concurrency before measuring memory. |
| Code/package CI fails | Read the failing step's logs. Reproduce the regression tests or lint/type checks using CONTRIBUTING.md. Rebuild and extract the release ZIP before testing changes to its allowlist. |

Review logs locally before sharing them; upstream logs can include requested URLs and search terms. The installation JSON report omits credentials and arbitrary page text.

## Rollback

Stop the affected stack with `docker compose stop`, restore the previous release files and compatible image pins, rebuild if needed, then run that release's installer and verification. Keep current secrets when rolling back code; do not restore a known-compromised or intentionally retired credential. If you must recover a private configuration backup after a failed rotation, rotate again immediately after recovery. Do not use `down --volumes` as routine troubleshooting.

The Compose project name defaults to `sourceferry`. If you used `COMPOSE_PROJECT_NAME`, use that same value for every operation so you do not accidentally create a second stack.
