#!/bin/bash
# One live boot per merge: lock screen at 90 s, short swipe at 95 s, Home at
# 105/140 s and the Settings tap (145 s) at 152 s. Keep-awake taps from H.
# Usage: research/tasks/merge-live.sh [output-dir]
cd "$(dirname "$0")/../.."
OUT=${1:-backend/evidence/merge-$(date +%Y%m%d-%H%M)-live}
VPHONE_DISPLAY=cocoa VPHONE_TCG_THREAD=multi \
VPHONE_TOUCH_SCRIPT="$(cat research/evidence/H-home-live/awake-touch-script.txt)" \
.venv/bin/python backend/display_probe.py \
  --plan build/virtual-activated-profile-v5/boot/boot_plan.json \
  --diagnostic-system build/virtual-activated-profile-v5/system.raw \
  --diagnostic-data build/virtual-activated-profile-v5/data.raw \
  --seconds 160 --stop-condition duration \
  --framebuffer-at 90 --framebuffer-at 105 --framebuffer-at 140 --framebuffer-at 152 \
  --output "$OUT"
echo "probe exit $?"
rm -rf "$OUT/images" && echo "images removed"
