#!/usr/bin/env bash
# Builds the image, runs it and sends one synthetic session through the API.
# Needs docker, curl and uv. The last line is "ok" on success.
set -u
cd "$(dirname "$0")/.."

IMAGE="hifisync-acoustics:container-test"
NAME="hifisync-acoustics-test-$$"
PORT="${HIFISYNC_TEST_PORT:-18765}"
BASE="http://127.0.0.1:${PORT}/api/v1"
WORK="$(mktemp -d)"
TOKEN=""
STEP="start"

cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1
  rm -rf "$WORK"
}
fail() {
  echo "FAIL $STEP"
  docker logs "$NAME" 2>&1 | tail -n 50
  exit 1
}
trap cleanup EXIT

# Authenticated call; the token goes in a header read from stdin, never in argv, URL or output.
api() { # method path [curl args...]
  local method="$1" path="$2"; shift 2
  printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" |
    curl -sS --fail-with-body -X "$method" -K - "$BASE$path" "$@"
}
jget() { python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }

STEP="build"
docker build -q -t "$IMAGE" . >/dev/null || fail

STEP="run"
mkdir -p "$WORK/data" && chmod 777 "$WORK/data"
docker run -d --name "$NAME" -p "127.0.0.1:${PORT}:8765" -v "$WORK/data:/data" \
  "$IMAGE" hifisync-acoustics serve --host 0.0.0.0 --port 8765 --no-zeroconf >/dev/null || fail

STEP="health"
for _ in $(seq 60); do
  curl -sf "$BASE/health" >/dev/null && break
  sleep 1
done
curl -sf "$BASE/health" >/dev/null || fail

STEP="token"
TOKEN="$(docker exec "$NAME" hifisync-acoustics token create ci 2>/dev/null)" || fail
[ -n "$TOKEN" ] || fail

STEP="recording"
uv run python - "$WORK/rec.wav" <<'PY' || fail
import sys
import numpy as np
import scipy.signal
from scipy.io import wavfile
from hifisync_acoustics.analysis.sweep import SweepSpec, generate_sweep

sweep = generate_sweep(SweepSpec(duration_s=2.0, repeats=1))
ir = np.zeros(2400)
ir[480] = 0.5
ir[1200] = 0.1
wet = scipy.signal.fftconvolve(sweep.signal, ir)
lead = int(0.6 * 48000)
x = 1e-4 * np.random.default_rng(0).standard_normal(lead + len(wet) + int(1.5 * 48000))
x[lead : lead + len(wet)] += wet
pcm = (np.clip(x, -1.0, 1.0) * 32767).astype(np.int16)
wavfile.write(sys.argv[1], 48000, pcm)
PY

STEP="room"
api PUT /rooms/ci-room -H 'Content-Type: application/json' \
  -d '{"name":"Test room","width_m":4,"length_m":5,"height_m":2.5}' >/dev/null || fail
STEP="point"
POINT="$(api POST /rooms/ci-room/points -H 'Content-Type: application/json' \
  -d '{"label":"LP","x":1,"y":1,"z":1.2}' | jget id)" || fail
STEP="node"
api PUT /nodes/ci-node -H 'Content-Type: application/json' \
  -d '{"esphome_name":"ci-mic"}' >/dev/null || fail
STEP="session"
SESSION="$(api POST /sessions -H 'Content-Type: application/json' \
  -d "{\"room_id\":\"ci-room\",\"assignments\":[{\"point_id\":\"$POINT\",\"node_id\":\"ci-node\"}],\"sweep\":{\"duration_s\":2.0,\"repeats\":1}}" |
  jget id)" || fail

STEP="upload"
api PUT "/sessions/$SESSION/recordings/ci-node" -H 'Content-Type: audio/wav' \
  --data-binary "@$WORK/rec.wav" >/dev/null || fail

STEP="results"
DONE=""
for _ in $(seq 60); do
  RES="$(api GET "/sessions/$SESSION/results")" || fail
  DONE="$(printf '%s' "$RES" | python3 -c '
import json, sys
pts = json.load(sys.stdin)["points"]
print("yes" if pts and all(p["state"] == "done" for p in pts) else "no")
')"
  [ "$DONE" = "yes" ] && break
  sleep 1
done
[ "$DONE" = "yes" ] || fail
printf '%s' "$RES" | python3 -c '
import json, sys, os
pts = json.load(sys.stdin)["points"]
sys.exit(0 if any(p["point_id"] == sys.argv[1] and p["result"] for p in pts) else 1)
' "$POINT" || fail

STEP="stop"
docker stop "$NAME" >/dev/null || fail
docker rm "$NAME" >/dev/null || fail
docker rmi "$IMAGE" >/dev/null 2>&1 || true
echo ok
