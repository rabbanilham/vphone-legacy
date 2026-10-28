#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."

NAME="${1:-before}"
OUT="research/evidence/anim-20261027/$NAME"
LOCK=/tmp/vphone-perf-live.lock

until mkdir "$LOCK" 2>/dev/null; do
  echo "waiting for another live run ($(cat "$LOCK/owner" 2>/dev/null))"; sleep 5
done
echo "$OUT" > "$LOCK/owner"
trap 'rm -rf "$LOCK"' EXIT

mkdir -p "$(dirname "$OUT")"
rm -rf "$OUT"

echo "=== Starting fly-out animation capture ($NAME) ==="
VPHONE_TCG_THREAD=multi \
VPHONE_TOUCH_SCRIPT="$(cat research/evidence/H-home-live/awake-touch-script.txt)" \
VPHONE_AGX_FRAME_DUMP="$OUT/agx_dumps" \
VPHONE_AGX_FRAME_DUMP_AFTER_NS=155600000000 \
VPHONE_AGX_FRAME_DUMP_COUNT=30 \
.venv/bin/python backend/display_probe.py \
  --plan build/virtual-activated-profile-v5/boot/boot_plan.json \
  --diagnostic-system build/virtual-activated-profile-v5/system.raw \
  --diagnostic-data build/virtual-activated-profile-v5/data.raw \
  --seconds 159 --stop-condition duration \
  --framebuffer-at 90 --framebuffer-at 105 --framebuffer-at 152 --framebuffer-at 157 \
  --home-press-at 154.0 \
  --framebuffer-range 155.50,156.30,0.033 \
  --output "$OUT" > "research/evidence/anim-20261027/${NAME}.stdout" 2>&1

echo "Capture finished. Generating strip..."
rm -rf "$OUT/images"
.venv/bin/python scripts/anim_strip.py "$OUT" "research/evidence/anim-20261027/strip_${NAME}.png"
echo "Done. Strip saved to research/evidence/anim-20261027/strip_${NAME}.png"
