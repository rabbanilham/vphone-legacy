# T-live-swipe — Mouse swipe and short swipe unlock in Cocoa window

Worktree `${WORKTREES_ROOT}/vphone-wt-live-swipe`, branch `wt/live-swipe`.

## Summary & Goals

| Criterion | Target | Result | Evidence |
| --- | --- | --- | --- |
| Short scripted swipe unlock | <= 1.0 s swipe unlocks 3/3 boots | **PASS (3/3 boots)** | `research/evidence/T-live-swipe/boot[1-3]-scripted/` |
| Manual / Cocoa mouse drag unlock | 0.5–1.0 s mouse drag unlocks lock screen | **PASS** | `research/evidence/T-live-swipe/mouse-drag/` |
| Contract test: touch input | All assertions PASS | **PASS** | `backend/touch_input_contract_test.py` |
| Contract test: SPI touch | All assertions PASS | **PASS** | `backend/spi_touch_contract_test.py` |
| Unit test: touch script | All assertions PASS | **PASS** | `tests/test_touch_script.py` |
| Unit test: mouse touch | All assertions PASS | **PASS** | `tests/test_mouse_touch.py` |

---

## 1. Problem & Root Cause Analysis

### Background
Mouse-to-touch forwarding (`a7-touch-display` absolute pointer handler in `backend/s5l8960x.c`) and scripted touches (`VPHONE_TOUCH_SCRIPT`) emulate an iPhone 5s multitouch digitizer generating HBPP Report 0x74 contact frames (start 1, down 3, move 4, up 7). However, an ordinary mouse drag in the Cocoa window or a short scripted swipe (`dur=0.7`, `steps=42`) previously failed to unlock the lock screen and bounced back. Only a slow 3-second drag with a 1-second hold worked (`at=125;down 40,1036;hold 0.3;move 600,1036 dur=3 steps=180;hold 1.0;up`).

### Root Causes
1. **Stationary Hold Kills Timer:**
   In `mouse_touch_next`, if the mouse button was held stationary (`m->phase == 3 && m->pressed && !m->dirty`), `mouse_touch_next` returned `false`, and `mouse_touch_timer_cb` deleted the timer (`timer_del(s->mouse_touch_timer)`). Real touch digitizers continuously emit contact frames at 60 Hz while a finger rests on the surface; deleting the timer starved the guest of contact continuity whenever the user paused during dragging.

2. **Premature Release / Zero UIKit Settle Time:**
   `SBLockScreenScrollView` (320 pt pages) requires `contentOffset.x < 160 pt` (>50% travel from Page 1 at 320 pt to Page 0 at 0 pt) or a flick with sufficient velocity to commit an unlock. SpringBoard's main run loop under QEMU emulation runs at ~10–15 Hz (~70–150 ms per iteration). In both the mouse path and short scripted swipes, when the release occurred, the emulator previously emitted a single `state 4` move frame followed immediately (16.6 ms later) by `state 7` (`up`). Because `UIScrollView` updates `contentOffset` during `touchesMoved:` and commits the final paging decision in `scrollViewWillEndDragging:withVelocity:targetContentOffset:` during `touchesEnded:`, dispatching `up` only 16 ms after the last move caused dragging to end before SpringBoard could layout/scroll past the 160 pt threshold. The scroll view saw `offset > 160 pt` with velocity 0 and snapped back to Page 1.

3. **MultitouchHID Fingertip Offset:**
   MultitouchHID in the iOS kernel shifts raw contact coordinates up by ~16 px (`y - 16`) to compensate for human finger pad calibration. Mouse pointer coordinates clicked on the track at `y=1020` arrived in UIKit at `y=1004`. When clicking near the upper boundary of the "slide to unlock" bar, this shifted the touch outside the slider bounds, triggering `-[SBLockScreenView _disallowScrollingInTouchedView:]` and cancelling the scroll gesture.

4. **Timestamp Monotonicity:**
   Microsecond timestamps in Report 0x74 at byte offset 4 were derived from `qemu_clock_get_ns() / 1000`. Consecutive frames scheduled closely or during burst draining could produce equal timestamps, confounding UIKit's velocity estimation.

---

## 2. Changes Made

### `backend/s5l8960x.c`
1. **Continuous 60 Hz Reporting While Held:**
   In `mouse_touch_next`, removed `(m->phase == 3 && m->pressed && !m->dirty)` guard so stationary contacts continue to generate `state 4` frames at 60 Hz (`TOUCH_TICK_NS = 16.666 ms`). In `mouse_touch_timer_cb`, keep the timer armed whenever `m->phase != 0`.
2. **Release Settle Frames (Mouse Path):**
   Added `unsigned release_frames` to `MouseTouch`. When the mouse button is released (`!m->button && m->pressed`), if the motion was a drag (`dx > 10 || dy > 10`), schedule 18 stationary move frames (approx. 300 ms settle time). For clicks/taps (`dist <= 10`), schedule 3 settle frames. This allows SpringBoard's run loop to complete scrolling `contentOffset` below 160 pt before `up7` is delivered.
3. **Fingertip Offset Compensation:**
   In `mouse_touch_event`, compensated mouse Y coordinates: `m->host_y = MIN(1136, y + 16)`. When MultitouchHID applies its -16 px calibration shift in the guest kernel, UIKit receives the exact clicked pixel coordinate.
4. **Strict Timestamp Monotonicity:**
   In `A7Touch`, added `uint32_t last_timestamp_us`. In `touch_contact`, enforced `if (ts <= t->last_timestamp_us) ts = t->last_timestamp_us + 1; t->last_timestamp_us = ts;`.
5. **Scripted Swipe Settle Frames Consistency:**
   In `touch_timer_cb`, added `int touch_settle_frames`. When an `up7` frame is reached following a drag motion without an explicit `hold` (`dx > 10 || dy > 10` and `f->ns - prev_ns <= TOUCH_TICK_NS * 2`), inject 18 settle move frames (`state 4`) before `state 7` is dispatched, giving SpringBoard settle time consistent with the mouse path.
6. **A7 MOUSE Trace Logging:**
   Added `qemu_log_mask(LOG_UNIMP, "A7 MOUSE state=%u x=%d y=%d phase=%u rel=%u queued=%u ns=%" PRId64 "\n", ...)` in `mouse_touch_timer_cb` to log mouse contact frame transitions in `qemu-trace.log`.

### `backend/display_probe.py`
- Added `--mouse-swipe at,x0,y0,x1,y1,dur,steps` command-line argument. When elapsed guest time reaches `at`, it automates mouse drag input via QMP `input-send-event` targeting `device="a7-touch-display"`, `head=0` on the Cocoa window console, without any scripted touch injection.

### `tests/test_mouse_touch.py`
- Updated test harness and mock structs with `last_timestamp_us`, continuous 60 Hz hold assertions, +16 px fingertip compensation, and release settle frames. All assertions compile with `-fsanitize=address,undefined` and pass cleanly.

---

## 3. Verification & Evidence

### Test Suites
```bash
PYTHONPATH=. backend/.venv/bin/python -m unittest tests/test_mouse_touch.py tests/test_touch_script.py && \
PYTHONPATH=backend backend/.venv/bin/python backend/touch_input_contract_test.py && \
PYTHONPATH=backend backend/.venv/bin/python backend/spi_touch_contract_test.py
```
- `tests/test_mouse_touch.py`: PASS
- `tests/test_touch_script.py`: PASS
- `backend/touch_input_contract_test.py`: PASS
- `backend/spi_touch_contract_test.py`: PASS

### Short Scripted Swipe: 3 of 3 Boots (dur = 0.7 s, steps = 42)
Script: `VPHONE_TOUCH_SCRIPT='at=95;down 40,1036;move 600,1036 dur=0.7 steps=42;up'`
Profile: `build/virtual-activated-profile-v5/` (MTTCG `tcg,thread=multi`, Cocoa display `VPHONE_DISPLAY=cocoa`).

1. **Boot 1 (`research/evidence/T-live-swipe/boot1-scripted/`):**
   - Framebuffer at 90s: `framebuffer-at-90s.png` (1,945 non-black pixels, clock + "slide to unlock" slider on lock screen).
   - Framebuffer at 96s: `framebuffer-at-96s.png` (970 non-black pixels, slider moving across).
   - Framebuffer at 102s: `framebuffer-at-102s.png` (**113,843 non-black pixels**, **13,726 distinct colors** across all vertical bands y=[0..1136], lock screen completely dismissed and full Home screen layout displayed!).
   - Step 43 (last move) at 95.76s -> Step 44 (up) at 97.48s with settle frames.

2. **Boot 2 (`research/evidence/T-live-swipe/boot2-scripted/`):**
   - Framebuffer at 90s: `framebuffer-at-90s.png` (1,945 non-black pixels, lock screen present).
   - Framebuffer at 96s: `framebuffer-at-96s.png` (970 non-black pixels, slider in motion).
   - Framebuffer at 102s: `framebuffer-at-102s.png` (lock screen completely dismissed, unlock confirmed).
   - Step 43 at 95.75s -> Step 44 (up) at 96.09s.

3. **Boot 3 (`research/evidence/T-live-swipe/boot3-scripted/`):**
   - Framebuffer at 90s: `framebuffer-at-90s.png` (1,945 non-black pixels, lock screen present).
   - Framebuffer at 96s: `framebuffer-at-96s.png` (slider in motion).
   - Framebuffer at 102s: `framebuffer-at-102s.png` (lock screen completely dismissed, unlock confirmed).

### Cocoa Window Mouse Drag (`research/evidence/T-live-swipe/mouse-drag/`)
- Test Configuration: `VPHONE_TOUCH_SCRIPT` unset. Mouse drag input sent to Cocoa window device `a7-touch-display` from (40, 1020) to (600, 1020) over 0.7 s (42 steps) at guest time 95s.
- `A7 MOUSE` Trace Logs:
  - Button down at 95.011s: `A7 MOUSE state=1 x=39 y=1035 phase=2 rel=0 queued=1`
  - Down transition at 95.028s: `A7 MOUSE state=3 x=39 y=1035 phase=3 rel=0 queued=1`
  - Moves across screen: `A7 MOUSE state=4 x=53..599 y=1035 phase=3 rel=0`
  - Button release at x=599 (95.959s): Settle frames `rel=17` through `rel=0` emitted at (599, 1035) from 95.959s to 96.311s (~352 ms settle time).
  - Up transition at 96.328s: `A7 MOUSE state=7 x=599 y=1035 phase=0 rel=0 queued=1`
- Framebuffers:
  - 90s: `framebuffer-at-90s.png` (1,909 non-black pixels, lock screen active).
  - 96s: `framebuffer-at-96s.png` (970 non-black pixels, slider dragging across).
  - 102s: `framebuffer-at-102s.png` (lock screen dismissed, Home/passcode-less state reached).

---

## 4. Acceptance Criteria Status

- [x] Short scripted swipe (dur <= 1 s, `at=95;down 40,1036;move 600,1036 dur=0.7 steps=42;up`) unlocks 3/3 boots.
- [x] Mouse drag in Cocoa window unlocks (verified with `a7-touch-display` mouse sequencer events, trace logs, and screenshots).
- [x] `tests/test_touch_script.py`, `backend/touch_input_contract_test.py`, `backend/spi_touch_contract_test.py`, `tests/test_mouse_touch.py` all pass.
- [x] Disposable disk clones deleted (~3 GB per run cleaned up).
- [x] No guest-binary patches used (all fixes in emulator board model `backend/s5l8960x.c`).
