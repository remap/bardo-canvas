#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

export APP_DIR="${APP_DIR:-$REPO_ROOT/apps/test-pattern/static}"
export LAYOUT_DRIVER_HOST="${LAYOUT_DRIVER_HOST:-0.0.0.0}"
export LAYOUT_DRIVER_PORT="${LAYOUT_DRIVER_PORT:-8443}"

uv run python -m layout_server.main &
SERVER_PID=$!

BROADCASTER_PID=""
cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
  if [[ -n "$BROADCASTER_PID" ]]; then
    kill "$BROADCASTER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

HEALTHZ_HOST="$LAYOUT_DRIVER_HOST"
if [[ "$HEALTHZ_HOST" == "0.0.0.0" ]]; then
  HEALTHZ_HOST="localhost"
fi

# Point the broadcaster at the host/port the server actually started on, so a
# LAYOUT_DRIVER_PORT override does not leave it targeting config/broadcaster.yaml's
# hardcoded URL.
export LAYOUT_DRIVER_TARGET_URL="https://${HEALTHZ_HOST}:${LAYOUT_DRIVER_PORT}/"

uv run python -c "
from ndi_broadcaster.launcher import wait_for_healthy
wait_for_healthy('https://${HEALTHZ_HOST}:${LAYOUT_DRIVER_PORT}/healthz', timeout_seconds=30.0)
"

uv run python -m ndi_broadcaster.launcher &
BROADCASTER_PID=$!

wait "$SERVER_PID" "$BROADCASTER_PID"
