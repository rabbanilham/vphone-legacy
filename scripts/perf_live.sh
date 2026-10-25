#!/bin/bash
# Live performance run: one merge-live style boot (lock 90 s, Home 105 s,
# Settings tap 145 s) with host `sample` of QEMU at 30/100/145 s and
# per-thread CPU time. Only one live run at a time on this host (lock dir),
# because guest time tracks host time and parallel boots distort timing.
#
# Usage: scripts/perf_live.sh <out-dir> [qemu-binary]
# Env:   VPHONE_TCG_THREAD (default multi), PERF_SECONDS (default 160)
cd "$(dirname "$0")/.." || exit 1
OUT=$(cd "$(dirname "$1")" && pwd)/$(basename "$1"); BIN=${2:-}
LOCK=/tmp/vphone-perf-live.lock
until mkdir "$LOCK" 2>/dev/null; do
  echo "waiting for another live run ($(cat "$LOCK/owner" 2>/dev/null))"; sleep 10
done
echo "$OUT" > "$LOCK/owner"
trap 'rm -rf "$LOCK"' EXIT
mkdir -p "$OUT"
EXTRA=(); [ -n "$BIN" ] && EXTRA=(--qemu-binary "$BIN")
SECONDS_TOTAL=${PERF_SECONDS:-160}
VPHONE_TCG_THREAD=${VPHONE_TCG_THREAD:-multi} \
VPHONE_TOUCH_SCRIPT="$(cat research/evidence/H-home-live/awake-touch-script.txt)" \
.venv/bin/python backend/display_probe.py \
  --plan build/virtual-activated-profile-v5/boot/boot_plan.json \
  --diagnostic-system build/virtual-activated-profile-v5/system.raw \
  --diagnostic-data build/virtual-activated-profile-v5/data.raw \
  --seconds "$SECONDS_TOTAL" --stop-condition duration \
  --framebuffer-at 40 --framebuffer-at 50 --framebuffer-at 60 --framebuffer-at 70 \
  --framebuffer-at 80 --framebuffer-at 90 --framebuffer-at 105 --framebuffer-at 140 --framebuffer-at 152 \
  "${EXTRA[@]}" --output "$OUT/probe" > "$OUT/probe.stdout" 2>&1 &
PROBE=$!
START=$(date +%s)
for at in 30 100 145; do
  while [ $(( $(date +%s) - START )) -lt $at ]; do sleep 1; done
  PID=$(pgrep -n -f -- "-D $OUT/probe/qemu-trace.log")
  [ -n "$PID" ] || continue
  sample "$PID" 8 -file "$OUT/sample-$at.txt" >/dev/null 2>&1 &
  ps -M -p "$PID" > "$OUT/threads-$at.txt"
done
wait $PROBE
echo "probe exit $?" >> "$OUT/probe.stdout"
rm -rf "$OUT/probe/images"
{
  echo "AGX passes:   $(grep -c 'AGX replay target' "$OUT/probe/qemu-trace.log")"
  echo "swaps:        $(grep -c 'vblank swap completed' "$OUT/probe/qemu-trace.log" "$OUT/probe/stderr.log" | awk -F: '{s+=$2} END {print s}')"
  for at in 30 100 145; do
    [ -f "$OUT/sample-$at.txt" ] || continue
    for t in "CPU 0/TCG" "CPU 1/TCG" "agx-render" "qemu_main"; do
      line=$(grep -m1 "Thread_.*$t" "$OUT/sample-$at.txt" | awk '{print $1}')
      idle=$(sed -n "/Thread_.*$t/,/^    [0-9]* Thread_/p" "$OUT/sample-$at.txt" |
             grep -E '__psynch_cvwait|__select|qemu_cond_wait_impl' | head -1 | awk '{for(i=1;i<=NF;i++) if ($i ~ /^[0-9]+$/) {print $i; exit}}')
      echo "t=${at}s $t: samples $line, waiting ${idle:-0}"
    done
  done
  echo "framebuffers: $OUT/probe/framebuffer-at-*.ppm"
} | tee "$OUT/summary.txt"
