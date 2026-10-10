# SourceFerry validation record

The gateway, installers, and release packaging are checked with offline regressions and code/package CI. The behavior specification is [docs/API.md](docs/API.md); deployment settings are in [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## Functional checks

The regression suite covers request authentication, body and field limits, shared concurrency, deadlines, cancellation, readiness, Markdown cleanup, installer configuration, credential rotation, failure reports, and release archive readability. Native Windows fixtures exercise the real PowerShell parser and credential rotation functions.

Ruff checks gateway, scripts, tests, and integration checks; mypy checks gateway and scripts. ShellCheck and `bash -n` validate the Bash installer. Linux tests exercise its orchestration with a fake Docker executable.

Code/package CI builds the pinned gateway, runs the regressions, checks lint and types, builds the release ZIP, and repeats the regressions against the extracted release. The ZIP and checksum use readable file permissions when built by a container for a separate host user.

## Reproducing verification

Use [CONTRIBUTING.md](CONTRIBUTING.md) for offline tests, native Windows fixtures, browser egress checks, packaging, and quality-check commands.

The one-command installer runs offline regressions, waits for the stack, checks the published endpoint, and verifies live search/fetch. It saves `artifacts/installation-report.json`. Repeat with the installer's `--verify` option on Linux or `-Verify` on Windows.

The browser canary is bundled HTML echoed by [HTTPBin's public base64 endpoint](https://httpbin.org/). It exercises browser JavaScript. Live cases check expected official domains and topic text rather than fixed search rankings. External blocking or changed content can fail a later run; the installer returns failure and retains services for troubleshooting.
