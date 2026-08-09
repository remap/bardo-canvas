#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

: "${GEMINI_API_KEY:?GEMINI_API_KEY must be set}"
export LAYOUT_DRIVER_URL="${LAYOUT_DRIVER_URL:-https://localhost:8443}"
export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"

cd "$REPO_ROOT"
uv run --extra flux-gallery python -m flux_gallery.worker
