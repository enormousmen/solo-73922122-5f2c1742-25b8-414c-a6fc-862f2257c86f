#!/usr/bin/env bash
# Manual API demo: conflict rules + interrupting a running self-test.
#
# Start the stack first:
#   docker compose up --build
# then run:
#   ./scripts/demo-interrupt-requests.sh
#
# Environment:
#   API_URL=http://127.0.0.1:8000 ./scripts/demo-interrupt-requests.sh
#
# The script deliberately issues requests by hand so an operator can see the
# exact 409 / 200 / aborted payloads.  A fuller asserting suite lives in
# scripts/demo_interrupt.py.

set -u
API_URL="${API_URL:-http://127.0.0.1:8000}"
TMP_DIR="$(mktemp -d)"
ST_BODY="$TMP_DIR/selftest.body"
ST_CODE="$TMP_DIR/selftest.code"
trap 'rm -rf "$TMP_DIR"' EXIT

line() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }

# Truncate long lists/strings (e.g. release closed_paths) while keeping JSON
# structure and all scalar fields visible.
pretty() {
  python3 -c '
import sys, json
raw = sys.stdin.read()
try:
    data = json.loads(raw)
except Exception:
    sys.stdout.write(raw)
    raise SystemExit
def cap(x):
    if isinstance(x, dict):
        return {k: cap(v) for k, v in x.items()}
    if isinstance(x, list) and len(x) > 4:
        return [cap(v) for v in x[:4]] + [f"... +{len(x) - 4} more"]
    if isinstance(x, str) and len(x) > 160:
        return x[:160] + f"...<{len(x)} chars>"
    return x
print(json.dumps(cap(data), indent=2, ensure_ascii=False))'
}

# req METHOD PATH [JSON_BODY]  -- prints "-> HTTP <code>" and pretty body.
req() {
  local method="$1" path="$2" body="${3:-}"
  local args=(-s -X "$method" -w $'\n%{http_code}' "${API_URL}${path}")
  if [[ -n "$body" ]]; then
    args+=(-H 'Content-Type: application/json' -d "$body")
  fi
  local raw code
  raw="$(curl "${args[@]}")"
  code="${raw##*$'\n'}"
  printf -- '-> %s %s : HTTP %s\n' "$method" "$path" "$code"
  printf '%s' "${raw%$'\n'*}" | pretty
}

wait_active() {
  local i active
  for ((i = 0; i < 50; i++)); do
    active="$(curl -s "${API_URL}/api/v1/drill/self-test" \
      | python3 -c 'import sys,json
try:
    print(json.load(sys.stdin).get("active"))
except Exception:
    print(False)' 2>/dev/null)"
    [[ "$active" == "True" ]] && return 0
    sleep 0.05
  done
  echo "self-test did not become active in time" >&2
  return 1
}

line "0. Health and clean slate"
req GET /healthz
req POST /api/v1/drill/release

line "1. Manual sample open blocks self-test with 409 (and is not closed by it)"
req POST /api/v1/sample-files/open '{"count": 2}'
req POST /api/v1/drill/self-test
echo "open_sample_files after rejected self-test (expect 2):"
curl -s "${API_URL}/api/v1/status" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print("open_sample_files =", d["open_sample_files"], d.get("sample_owners"))'
req POST /api/v1/drill/release

line "2. Start a self-test in the BACKGROUND; it lasts ~0.5-1s"
(
  curl -s -X POST "${API_URL}/api/v1/drill/self-test" \
    -o "$ST_BODY" -w '%{http_code}' > "$ST_CODE"
) &
ST_PID=$!
wait_active
echo "background self-test is active (shell pid $ST_PID)"

line "3. While it runs: second self-test / open / exhaust all -> 409"
req POST /api/v1/drill/self-test
req POST /api/v1/sample-files/open '{"count": 1}'
req POST /api/v1/drill/exhaust

line "4. Status stays queryable (200) and reports active self-test"
curl -s "${API_URL}/api/v1/status" | python3 -c 'import sys,json; d=json.load(sys.stdin); st=d.get("self_test",{}); print("status ok; open_sample_files =", d["open_sample_files"], "; self_test.active =", st.get("active"), "; run_id =", st.get("run_id"))'

line "5. Operator RELEASE interrupts the run immediately (no need to wait)"
req POST /api/v1/drill/release

line "6. New manual samples can be opened the moment release returns"
req POST /api/v1/sample-files/open '{"count": 3}'

line "7. The background self-test now reports aborted"
wait "$ST_PID" 2>/dev/null || true
echo "background POST /self-test -> HTTP $(cat "$ST_CODE")"
python3 -c 'import json
d=json.load(open("'"$ST_BODY"'"))
r=d.get("detail") if isinstance(d.get("detail"), dict) else d
print("state       :", r.get("state"))
print("aborted     :", r.get("aborted"))
print("abort_reason:", r.get("abort_reason"))
print("run_id      :", r.get("run_id"))'

sleep 0.8
line "8. The 3 new manual descriptors survived the stale self-test"
curl -s "${API_URL}/api/v1/status" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print("open_sample_files =", d["open_sample_files"], d.get("sample_owners")); assert d["open_sample_files"] == 3, "late self-test closed new samples!"; print("SURVIVAL OK")'

line "9. Cleanup"
req POST /api/v1/drill/release

line "10. RESTART variant: start another background self-test"
ST_BODY2="$TMP_DIR/st2.body"; ST_CODE2="$TMP_DIR/st2.code"
(
  curl -s -X POST "${API_URL}/api/v1/drill/self-test" \
    -o "$ST_BODY2" -w '%{http_code}' > "$ST_CODE2"
) &
ST_PID=$!
wait_active
req POST /api/v1/drill/restart
req POST /api/v1/sample-files/open '{"count": 2}'
wait "$ST_PID" 2>/dev/null || true
echo "background POST /self-test -> HTTP $(cat "$ST_CODE2")"
python3 -c 'import json
d=json.load(open("'"$ST_BODY2"'"))
r=d.get("detail") if isinstance(d.get("detail"), dict) else d
print("state:", r.get("state"), "| aborted:", r.get("aborted"), "| reason:", r.get("abort_reason"))'
sleep 0.6
curl -s "${API_URL}/api/v1/status" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d["open_sample_files"] == 2; print("post-restart samples survived: open_sample_files =", d["open_sample_files"])'
req POST /api/v1/drill/release

printf '\nDone. For asserting pass/fail coverage run: python3 scripts/demo_interrupt.py\n'
