#!/usr/bin/env bash
# SourceFerry one-command Linux installation; all Python runs in Docker.
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd -- "$ROOT"
PYTHON_HELPER='python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f'
PROJECT="${COMPOSE_PROJECT_NAME:-sourceferry}"
MODE=install
ROTATE=false
WAIT_TIMEOUT=300
STAGE=prerequisites
HELPER_READY=false
REPORT_READY=false
REPORT="$ROOT/artifacts/installation-report.json"

usage() {
    printf '%s\n' 'SourceFerry - web search and fetch for AI clients' \
        'Usage: bash ./install.sh [--verify | --show-token] [--rotate-secrets] [--wait-timeout SECONDS]' \
        'Docker Engine 28+ with Linux containers and Compose v2.24+ must already be installed.' \
        '--verify reruns all acceptance checks without rebuilding or restarting the gateway.' \
        '--show-token displays the saved gateway token and client configuration.' \
        'This product includes software developed by UncleCode (https://x.com/unclecode) as part of the Crawl4AI project (https://github.com/unclecode/crawl4ai).'
}

while (($#)); do
    case "$1" in
        --verify) [[ "$MODE" == install ]] || { usage; exit 2; }; MODE=verify; shift ;;
        --show-token) [[ "$MODE" == install ]] || { usage; exit 2; }; MODE=token; shift ;;
        --rotate-secrets) ROTATE=true; shift ;;
        --wait-timeout) (($# >= 2)) || { usage; exit 2; }; WAIT_TIMEOUT="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done
if [[ ! "$WAIT_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || ((WAIT_TIMEOUT > 3600)); then
    printf '%s\n' 'Wait timeout must be 1..3600 seconds.' >&2
    exit 2
fi
[[ "$PROJECT" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || { printf '%s\n' 'COMPOSE_PROJECT_NAME must contain lowercase letters, digits, underscores or hyphens.' >&2; exit 2; }
[[ "$ROTATE" == false || "$MODE" == install ]] || { usage; exit 2; }
[[ "$(uname -s)" == Linux ]] || { printf '%s\n' 'Use install.ps1 on Windows. install.sh requires a Linux host.' >&2; exit 1; }

# Compose otherwise gives exported shell variables precedence over the saved .env.
# The installation directory is the single source of gateway settings and secrets.
unset LOCAL_WEB_API_TOKEN CRAWL4AI_API_TOKEN SEARXNG_SECRET_KEY GATEWAY_BIND_ADDRESS GATEWAY_PORT LOCAL_WEB_GATEWAY_URL \
    PYTHON_IMAGE SEARXNG_IMAGE CRAWL4AI_IMAGE SEARXNG_TIMEOUT_SECONDS CRAWL4AI_TIMEOUT_SECONDS \
    ENRICHMENT_CONCURRENCY SNIPPET_MAX_CHARS API_CLIENT_TIMEOUT_SECONDS GATEWAY_MAX_REQUESTS MAX_REQUEST_BYTES MAX_UPSTREAM_BYTES SNIPPET_INPUT_MAX_CHARS REQUEST_DEADLINE_SECONDS

helper() {
    docker run --rm --user "$(id -u):$(id -g)" --env PYTHONDONTWRITEBYTECODE=1 \
        --mount "type=bind,source=$ROOT,target=/workspace" --workdir /workspace \
        "$PYTHON_HELPER" python /workspace/scripts/install.py "$@"
}
host_helper() {
    docker run --rm --network host --user "$(id -u):$(id -g)" --env PYTHONDONTWRITEBYTECODE=1 \
        --mount "type=bind,source=$ROOT,target=/workspace" --workdir /workspace \
        "$PYTHON_HELPER" python /workspace/scripts/install.py "$@"
}
compose() {
    docker compose --project-name "$PROJECT" --env-file "$ROOT/.env" \
        --file "$ROOT/docker-compose.yml" "$@"
}
mark_pass() {
    helper report --stage "$STAGE" --status passed
    printf 'PASS %s\n' "$STAGE"
}
fail() {
    local code=$?
    trap - ERR
    if [[ "$HELPER_READY" == true && "$MODE" != token ]]; then
        local status=failed
        [[ "$STAGE" == verification ]] && status=verification_failed
        helper report --stage "$STAGE" --status "$status" >/dev/null 2>&1 || true
    elif [[ "$REPORT_READY" == true && "$MODE" != token ]]; then
        printf '{"installer_status":"failed","installer_stages":[{"stage":"%s","status":"failed"}]}\n' "$STAGE" > "$REPORT"
    fi
    if [[ "$STAGE" == verification ]]; then
        printf '%s\n' 'Installation completed; acceptance verification failed. The gateway stack is retained.' >&2
    else
        printf 'FAIL %s. The gateway stack is retained for troubleshooting.\n' "$STAGE" >&2
    fi
    printf '%s\n' 'Report: artifacts/installation-report.json' 'After fixing the problem, rerun bash ./install.sh (or --verify to repeat checks).' >&2
    exit "$code"
}
trap fail ERR

if [[ "$MODE" != token ]]; then
    [[ ! -L "$ROOT/artifacts" && ! -L "$REPORT" ]] || { printf '%s\n' 'Refusing a symlink for the report directory or file.' >&2; exit 1; }
    mkdir -p -- "$ROOT/artifacts"
    printf '%s\n' '{"installer_status":"running","installer_stages":[]}' > "$REPORT"
    REPORT_READY=true
fi
command -v docker >/dev/null || { printf '%s\n' 'Install Docker Engine and the Compose plugin (Linux), or Docker Desktop (Windows) first. See README.md.' >&2; false; }
DOCKER_INFO="$(docker info --format '{{.OSType}}|{{.NCPU}}|{{.MemTotal}}|{{.ServerVersion}}')"
IFS='|' read -r OS_TYPE CPUS MEMORY_BYTES ENGINE_VERSION <<< "$DOCKER_INFO"
[[ "$OS_TYPE" == linux ]] || { printf '%s\n' 'Switch Docker to Linux containers.' >&2; false; }
CONTEXT_HOST="$(docker context inspect --format '{{.Endpoints.docker.Host}}')"
[[ "${DOCKER_HOST:-$CONTEXT_HOST}" == unix://* ]] || { printf '%s\n' 'The installer requires a local Linux Docker daemon, because it verifies the published host port.' >&2; false; }

if [[ "$MODE" == token ]]; then
    helper token
    exit 0
fi

COMPOSE_VERSION="$(docker compose version --short)"
STAGE=helper-image
docker pull "$PYTHON_HELPER"
HELPER_READY=true
helper report --initialize
mark_pass

STAGE=prerequisites
helper preflight --engine-version "$ENGINE_VERSION" --compose-version "$COMPOSE_VERSION" --memory-bytes "$MEMORY_BYTES" --cpus "$CPUS"
mark_pass

STAGE=configuration
if [[ "$MODE" == install ]]; then
    SETUP_ARGS=()
    [[ "$ROTATE" == true ]] && SETUP_ARGS+=(--rotate-secrets)
    docker run --rm --user "$(id -u):$(id -g)" --env PYTHONDONTWRITEBYTECODE=1 \
        --mount "type=bind,source=$ROOT,target=/workspace" --workdir /workspace \
        "$PYTHON_HELPER" python /workspace/scripts/setup.py "${SETUP_ARGS[@]}"
fi
CONFIGURATION="$(helper config)"
mapfile -t CONFIG <<< "$CONFIGURATION"
compose config --quiet
if [[ "$MODE" == install ]]; then
    EXISTING_PORT="$(compose port gateway 8080 2>/dev/null || true)"
    PORT_ARGS=()
    [[ "$EXISTING_PORT" == *":${CONFIG[1]}" ]] && PORT_ARGS+=(--existing-gateway)
    host_helper port-check "${PORT_ARGS[@]}"
fi
mark_pass

STAGE=images
if [[ "$MODE" == install ]]; then
    compose pull searxng
    compose build --pull gateway crawl4ai
fi
if [[ "$MODE" == install ]]; then
    # Inspect the just-built tag, including when an older gateway is still running.
    GATEWAY_IMAGE="$(docker image inspect --format '{{.Id}}' "${PROJECT}-gateway")"
else
    GATEWAY_IMAGE="$(compose images --quiet gateway | head -n 1)"
fi
[[ -n "$GATEWAY_IMAGE" ]] || { printf '%s\n' 'No installed gateway image was found. Run bash ./install.sh first.' >&2; false; }
mark_pass

STAGE=offline-tests
docker run --rm --user "$(id -u):$(id -g)" --env PYTHONDONTWRITEBYTECODE=1 \
    --mount "type=bind,source=$ROOT,target=/workspace" --workdir /workspace \
    "$GATEWAY_IMAGE" python -m unittest discover -s tests -v
mark_pass

STAGE=services
if [[ "$MODE" == install ]]; then
    compose up --detach --wait --wait-timeout "$WAIT_TIMEOUT" gateway searxng crawl4ai
fi
mark_pass

STAGE=published-health
host_helper host-health
mark_pass

STAGE=verification
docker run --rm --network "${PROJECT}_default" --user "$(id -u):$(id -g)" --env PYTHONDONTWRITEBYTECODE=1 \
    --mount "type=bind,source=$ROOT,target=/workspace" --workdir /workspace \
    "$GATEWAY_IMAGE" python /workspace/scripts/verify.py --base-url http://gateway:8080 \
    --env-file /workspace/.env --report /workspace/artifacts/installation-report.json
helper report --stage verification --status verified
printf '\n%s\n' 'Installation verified. Report: artifacts/installation-report.json' 'Save these client settings securely:'
# This is the only stage that intentionally prints a real credential.
helper token
