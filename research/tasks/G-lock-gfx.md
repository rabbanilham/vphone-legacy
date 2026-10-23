# Task G (Codex): full lock screen and live Home rendering
Worktree: /Users/bagas/Downloads/vphone-wt-lock-gfx (branch wt/lock-gfx). Report id: G-lock-gfx.

## Problem
In the live Cocoa window the virtual-activated profile reaches the lock screen, but ONLY the "slide to unlock" text is visible: no wallpaper, no clock, no date. After unlock, Home is black with a small white rectangle (handover section 23), although offline replay of `backend/evidence/virtual-activated-20261023/render-dumps/frame-1.bin` (`backend/agx_replay_offline.py`) shows the Home icons and dock.

## Goal
1. Live lock screen shows wallpaper, clock and date (plus the slide text).
2. After a scripted unlock, Home is visible live (icons, labels, dock), matching the offline replay.

## Approach
- Capture live lock-screen frames: `VPHONE_AGX_FRAME_DUMP=<dir>` (+ `VPHONE_AGX_FRAME_DUMP_COUNT`, SKIP/EVERY, `VPHONE_AGX_FRAME_DUMP_OFFSCREEN=1` for offscreen passes) and timed QMP captures (`--framebuffer-at 90` etc.).
- Replay them offline (`agx_replay_offline.py <frame.bin> out.png`, `--audit`). Decide per missing layer: is the draw absent from the submission, skipped/miscomputed by the replay, or rendered into an offscreen target that never gets composited / scanned out (e.g. target 0x10214000, display MMU 0x803d0c000, wrong plane or buffer)?
- Fix in the live replay (`backend/s5l8960x.c`) and the offline mirror together; add a regression to `backend/agx_contract_test.py` or `tests/`.
- Context only (do not import wholesale): the stopped graphics-quality work in /Users/bagas/.codex/worktrees/graphics-quality/vphone-legacy (CLAUDE_HANDOVER.md section 24, evidence/graphics-quality-20261023). The agy triple-buffer audit there is unproven.

## Done when
- Live screenshots (QMP screendump, saved PNGs) of the lock screen at ~90 s showing wallpaper+clock, and of Home after the unlock swipe.
- `backend/agx_contract_test.py` and `.venv/bin/python -m unittest discover -s tests` pass.
- Report + commit on wt/lock-gfx.
