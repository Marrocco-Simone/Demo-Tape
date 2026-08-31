#!/usr/bin/env bash
# End-to-end pipelines against the static pages in .test_app/ (port 8931).
# Every pipeline is headless and asserts on page state, so a VIDEO_OK exit
# code means the assertions passed, not just that a video was produced.
set -uo pipefail
cd "$(dirname "$0")/.."

PORT=8931
./.venv/bin/python -m http.server "$PORT" --directory .test_app >/dev/null 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null' EXIT
sleep 1

fail=0
for p in tests/pipelines/0*.json; do
  case "$p" in *failure_contract*) continue ;; esac
  echo "== $p"
  if ! ./.venv/bin/demotape "$p" >/dev/null 2>&1; then
    echo "   FAILED"
    fail=1
  fi
done

# The failure contract: this pipeline targets a missing selector, so success
# means the recorder failed to fail.
echo "== tests/pipelines/05_failure_contract.json (must exit 1)"
if ./.venv/bin/demotape tests/pipelines/05_failure_contract.json >/dev/null 2>&1; then
  echo "   FAILED (unexpectedly succeeded)"
  fail=1
fi

if [ "$fail" -eq 0 ]; then echo "ALL PIPELINES PASSED"; else echo "PIPELINES FAILED"; fi
exit "$fail"
