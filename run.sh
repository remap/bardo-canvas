#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# A positional argument selects the app, as an alternative to the APP_DIR env var:
#   ./run.sh /path/to/app/static
export APP_DIR="${1:-${APP_DIR:-$REPO_ROOT/apps/test-pattern/static}}"
export LAYOUT_DRIVER_HOST="${LAYOUT_DRIVER_HOST:-0.0.0.0}"
export LAYOUT_DRIVER_PORT="${LAYOUT_DRIVER_PORT:-8443}"

# One instance = one config directory + one port. LAYOUT_DRIVER_CONFIG_DIR
# defaults to config/ (today's exact layout), so a second instance only needs
# to set this plus LAYOUT_DRIVER_PORT to a distinct value. Any of the three
# YAML paths or the runtime dir can still be overridden individually -- those
# always win over the config-dir-derived default.
export LAYOUT_DRIVER_CONFIG_DIR="${LAYOUT_DRIVER_CONFIG_DIR:-$REPO_ROOT/config}"
export SCREENS_YAML="${SCREENS_YAML:-$LAYOUT_DRIVER_CONFIG_DIR/screens.yaml}"
export AUDIO_YAML="${AUDIO_YAML:-$LAYOUT_DRIVER_CONFIG_DIR/audio.yaml}"
export BROADCASTER_YAML="${BROADCASTER_YAML:-$LAYOUT_DRIVER_CONFIG_DIR/broadcaster.yaml}"
if [[ "$LAYOUT_DRIVER_CONFIG_DIR" == "$REPO_ROOT/config" ]]; then
  DEFAULT_RUNTIME_DIR="$REPO_ROOT/runtime"
else
  DEFAULT_RUNTIME_DIR="$REPO_ROOT/runtime-$(basename "$LAYOUT_DRIVER_CONFIG_DIR")"
fi
export LAYOUT_DRIVER_RUNTIME_DIR="${LAYOUT_DRIVER_RUNTIME_DIR:-$DEFAULT_RUNTIME_DIR}"

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
