#!/usr/bin/env bash
# Convenience demo requests for the FD/EMFILE drill.
# Usage:
#   API_URL=http://127.0.0.1:8000 scripts/demo-requests.sh
# Extra arguments are passed to scripts/demo.py, e.g.:
#   scripts/demo-requests.sh --echo-host localhost --echo-port 9000

set -euo pipefail

API_URL="${API_URL:-http://127.0.0.1:8000}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== Health =="
curl -fsS "${API_URL}/healthz" | python3 -m json.tool

echo
echo "== One-request server-side self-test =="
curl -fsS -X POST "${API_URL}/api/v1/drill/self-test" | python3 -m json.tool

echo
echo "== Host TCP/timing demo =="
python3 "${SCRIPT_DIR}/demo.py" --api-base "${API_URL}" "$@"

echo
echo "Other copy-paste requests are in requests/demo-requests.http and README.md."
