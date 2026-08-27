#!/bin/bash
# Reference solution entrypoint.
#
# Installs the reference publisher into the location the task requires
# (/app/publisher/release-publisher.mjs -- deliberately absent from the shipped
# environment so the empty-run proof is meaningful), starts the distribution
# gateway, and runs `npm run report` so the verifier has a populated
# releases.duckdb and a live gateway ledger to inspect.
#
# Idempotent: safe to run twice. The second `npm run report` must reproduce the
# first byte-for-byte and must not create duplicate publications.

set -euo pipefail

APP_ROOT="${APP_ROOT:-/app}"
GATEWAY_DIR="${APP_ROOT}/distribution-gateway"
SOLUTION_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATEWAY_URL="${GATEWAY_URL:-http://127.0.0.1:7070}"

# Readiness probe. node:20-slim ships no curl/wget, so poll with python3.
wait_for_gateway() {
  python3 - "$1" <<'PYPROBE'
import sys, time, urllib.request
url = sys.argv[1] + "/healthz"
for _ in range(50):
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            if response.status == 200:
                sys.exit(0)
    except Exception:
        time.sleep(0.2)
sys.exit(1)
PYPROBE
}

# --- 1. Install the reference publisher -------------------------------------
mkdir -p "${APP_ROOT}/publisher"
cp "${SOLUTION_DIR}/publisher/release-publisher.mjs" \
   "${APP_ROOT}/publisher/release-publisher.mjs"

# --- 2. Start the gateway ----------------------------------------------------
# Reuse an already-running gateway if the harness started one; otherwise launch
# it here and shut it down on exit.
GATEWAY_PID=""
cleanup() {
  if [ -n "${GATEWAY_PID}" ]; then
    kill "${GATEWAY_PID}" 2>/dev/null || true
    wait "${GATEWAY_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

if ! wait_for_gateway "${GATEWAY_URL}"; then
  ( cd "${GATEWAY_DIR}" && node server.js >/tmp/gateway.log 2>&1 ) &
  GATEWAY_PID=$!

  if ! wait_for_gateway "${GATEWAY_URL}"; then
    echo "solution: gateway did not become ready" >&2
    cat /tmp/gateway.log >&2 || true
    exit 1
  fi
fi

# --- 3. Run the publisher ----------------------------------------------------
cd "${APP_ROOT}"
npm run --silent report
