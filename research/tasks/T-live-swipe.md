# Task T (agy): a mouse swipe in the Cocoa window unlocks the lock screen
Worktree: /Users/bagas/Downloads/vphone-wt-live-swipe (branch wt/live-swipe). Report id: T-live-swipe.

## Problem
Mouse-to-touch forwarding exists (backend/README.md "Mouse input in the Cocoa window"; `a7-touch-display` absolute pointer handler in backend/s5l8960x.c; VPHONE_MOUSE_TOUCH). But an ordinary mouse drag across "slide to unlock" in the live window does NOT unlock. Only the slow scripted swipe works (hold 0.3 s, 3 s drag, hold 1 s, y=1036). Handover section 20 explains why: SBLockScreenScrollView (320 pt pages) needs >50% travel or a flick; SpringBoard's main thread handles HID late and UIKit coalesces events, so a fast pan sees Began -> one Changed -> Ended with velocity 0 and bounces back. MultitouchHID shifts the fingertip ~16 px up.

## Goal
An ordinary left-to-right drag of about 0.5-1 s (mouse in the window, and the equivalent short VPHONE_TOUCH_SCRIPT) unlocks reliably: 3 of 3 attempts.

## Approach
1. Measure first: log the HBPP contact frames produced by the mouse path (timestamps, positions, states 1/3/4/7) and what SpringBoard sees (hooks `backend/evidence/keybag-contract-20261015/sb-scroll-hooks.json` with display_probe `exe_hooks`: page offset, pan states, velocity).
2. Candidate board/QEMU-side fixes (no guest patching): keep emitting move frames at ~60 Hz while the button is held even if the pointer is still; make sure the last move before up7 carries the release position and there is enough time between the last moves and up7 for UIKit to compute a non-zero velocity / >50% offset (e.g. delay up7 by a few frames, emit a stationary move); compensate the ~16 px fingertip offset if it matters; check timestamps in the reports are monotonic and realistic so velocity is right.
3. Keep the scripted path (VPHONE_TOUCH_SCRIPT) consistent with the mouse path.

## Done when
- A short scripted swipe (dur <= 1 s, e.g. `at=95;down 40,1036;move 600,1036 dur=0.7 steps=42;up`) unlocks 3/3 boots (screenshot after unlock shows the lock screen gone / Home or passcode-less unlock).
- A manual mouse drag in the window also unlocks (describe what you did and attach the screenshot).
- `tests/test_touch_script.py`, `backend/touch_input_contract_test.py`, `backend/spi_touch_contract_test.py` pass. Report + commit on wt/live-swipe.
