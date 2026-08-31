#!/usr/bin/env bash
# End-to-end pipelines against the static pages in .test_app/ (port 8931).
# Every pipeline is headless and asserts on page state, so a VIDEO_OK exit
# code means the assertions passed, not just that a video was produced.
set -uo pipefail
cd "$(dirname "$0")/.."

PORT=8931
PY=./.venv/bin/python
DEMO=./.venv/bin/demotape

"$PY" -m http.server "$PORT" --directory .test_app >/dev/null 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null' EXIT
sleep 1

fail=0
for p in tests/pipelines/0*.json; do
  case "$p" in *failure_contract*) continue ;; esac
  echo "== $p"
  out=$("$DEMO" "$p" 2>/dev/null)
  if [ $? -ne 0 ] || ! grep -q "^VIDEO_OK " <<<"$out"; then
    echo "   FAILED"
    echo "$out" | tail -3
    fail=1
  fi
done

# The failure contract: this pipeline targets a missing selector, so success
# means the recorder failed to fail. The full contract lines must be present:
# ERROR names the step, LAST_SCREENSHOT points at the frame, VIDEO_PARTIAL
# proves footage exists up to the break.
echo "== tests/pipelines/05_failure_contract.json (must fail with the contract)"
out=$("$DEMO" tests/pipelines/05_failure_contract.json 2>/dev/null)
code=$?
if [ $code -eq 0 ]; then
  echo "   FAILED (unexpectedly succeeded)"
  fail=1
elif ! grep -q "^ERROR step .* click:" <<<"$out" \
  || ! grep -q "^LAST_SCREENSHOT .*error.png" <<<"$out" \
  || ! grep -q "^VIDEO_PARTIAL .*video.mp4" <<<"$out"; then
  echo "   FAILED (contract lines missing)"
  echo "$out" | tail -3
  fail=1
fi

# Narration coverage: the pipeline is excluded from the default run because
# its first use downloads a ~330MB model, but when the tts extra and the
# cached model are present, verify the produced file actually carries the
# narration (audio stream, matching A/V durations, real speech energy).
if "$PY" -c "import kokoro_onnx, numpy, soundfile" >/dev/null 2>&1; then
  echo "== tests/pipelines/narration_tts.json"
  out=$("$DEMO" tests/pipelines/narration_tts.json 2>/dev/null)
  if [ $? -ne 0 ] || ! grep -q "^VIDEO_OK " <<<"$out"; then
    echo "   FAILED"
    echo "$out" | tail -3
    fail=1
  else
    if "$PY" tests/check_narration_audio.py demo_out_narration/video.mp4; then
      echo "   audio verified"
    else
      echo "   FAILED (narration audio check)"
      fail=1
    fi
  fi
else
  echo "== tests/pipelines/narration_tts.json SKIPPED (tts extra not installed)"
fi

if [ "$fail" -eq 0 ]; then echo "ALL PIPELINES PASSED"; else echo "PIPELINES FAILED"; fi
exit "$fail"
