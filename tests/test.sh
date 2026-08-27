#!/bin/bash
# Verifier entrypoint.
#
# Resets prior state, launches the provided distribution gateway on a fixed port,
# waits for readiness, runs the pytest suite, and writes a binary 0/1 reward.
#
# Identical logic runs for the oracle and the candidate: this script never looks
# at solution/, and the tests drive only the candidate-visible surface
# (/app/publisher/release-publisher.mjs, releases.duckdb, and the gateway's HTTP
# endpoints).

if [ "$PWD" = "/" ]; then
    echo "Error: No working directory set. Please set a WORKDIR in your Dockerfile before running this script."
    exit 1
fi

mkdir -p /logs/verifier

APP_ROOT="${APP_ROOT:-/app}"
GATEWAY_DIR="${APP_ROOT}/distribution-gateway"
GATEWAY_URL="${GATEWAY_URL:-http://127.0.0.1:7070}"

# Readiness probe. node:20-slim ships no curl/wget, so poll with python3
# (requests/urllib are available in the verifier image).
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

# --- Reset state so grading never depends on a previous run -------------------
# The candidate's database and the gateway's ledger both start empty; the tests
# themselves exercise re-run/idempotency explicitly.
rm -f "${APP_ROOT}/releases.duckdb" "${APP_ROOT}/releases.duckdb.wal"
rm -f "${GATEWAY_DIR}/data/gateway.json"

# --- Launch the gateway -------------------------------------------------------
GATEWAY_PID=""
cleanup() {
  if [ -n "${GATEWAY_PID}" ]; then
    kill "${GATEWAY_PID}" 2>/dev/null || true
    wait "${GATEWAY_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

( cd "${GATEWAY_DIR}" && node server.js >/tmp/gateway.log 2>&1 ) &
GATEWAY_PID=$!

ready=0
if wait_for_gateway "${GATEWAY_URL}"; then
  ready=1
fi

if [ "$ready" -ne 1 ]; then
  echo "Error: distribution-gateway did not become ready on ${GATEWAY_URL}"
  cat /tmp/gateway.log || true
  echo 0 > /logs/verifier/reward.txt
  exit 1
fi

# --- Run the suite ------------------------------------------------------------
# pytest + pytest-json-ctrf are pre-installed in the verifier image (shared mode).
# allow_internet=false, so no wheels are resolved at run time — invoke pytest directly.
python3 -m pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA
code=$?

# Surface pytest's raw exit code so the negative-control check can tell "tests ran
# and failed" (code 1, expected with no solution) from "tests could not run" (>=2).
echo "pytest exit code: ${code}"

if [ "$code" -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
