#!/bin/bash
cd /Users/bagas/Downloads/vphone-legacy
OUT=backend/evidence/merge-20261023-live
VPHONE_DISPLAY=cocoa VPHONE_TCG_THREAD=multi \
VPHONE_TOUCH_SCRIPT='at=95;down 40,1036;move 600,1036 dur=0.7 steps=42;up' \
.venv/bin/python backend/display_probe.py \
  --plan build/virtual-activated-profile-v5/boot/boot_plan.json \
  --diagnostic-system build/virtual-activated-profile-v5/system.raw \
  --diagnostic-data build/virtual-activated-profile-v5/data.raw \
  --seconds 150 --stop-condition duration \
  --framebuffer-at 90 --framebuffer-at 105 --framebuffer-at 140 \
  --output $OUT
echo "probe exit $?"
rm -rf $OUT/images && echo "images removed"
