# Task D: time dilation for the unlock → Home animation

Worktree `/Users/bagas/Documents/Projects/vphone/vphone-wt-dilation`, branch `exp/time-dilation`. You are the **only** worker allowed to boot QEMU on the MBP right now (one boot at a time; K and S work offline). Build your own QEMU here first: `backend/.venv/bin/python scripts/qemu/build_backend.py`.

## Why
At normal speed (k=1) the unlock looks wrong: a no-dump boot captured every 0.25 s (`build/agx-corpus/transition-live/`) shows the lock content vanishing at about 98 s, about 3 s of bare wallpaper, then a complete Home at 101 s with no icon fly-in. The GPU is never asked to draw the icons in between: SpringBoard (TCG-emulated, slow) commits Home late, and CoreAnimation, whose animation begin time has already passed, jumps to the end state. The fix being tested is to slow guest time relative to the wall clock so each guest second gets more emulated instructions.

## What exists (commit 00a8f80 on this branch)
`VPHONE_TIME_DILATION=k` (1..16, default 1, read once in `backend/s5l8960x.c` `vphone_time_dilation()`):
- AP and IOP generic timers count at 24 MHz / k (`cntfrq`), while the DeviceTree still announces 24 MHz;
- vblank period 16.67 ms × k (60 Hz in guest time);
- touch contact timestamps and the PMU RTC are divided by k.
Touch-script pacing (`at=` seconds, 16.67 ms ticks) and probe timing stay on the wall clock: a 0.7 s wall swipe is 0.35 s of guest time at k=2.

## Results so far (all with a visible Cocoa window, MTTCG, no frame dumps)
- k=2 boot, captures every 10 s (`build/dilation/k2-boot/`): lock screen by **70 s** (k=1: about 85–90 s), no panic, the guest clock advances about half as fast as the wall clock.
- k=2 unlock, swipe `at=80;down 40,1036;move 600,1036 dur=0.7 steps=42;up`, captures every 0.25 s from 79.5 s to 93.25 s (`build/dilation/k2-unlock/`): **did not unlock**, before and after scaling the touch timestamps. Touch frames drained (queued=1) but each took about 50 ms of wall time to be consumed. The "slide to unlock" label looks very faint in these captures.
- Tools: `/private/tmp/claude-501/-Users-bagas-Documents-Projects-vphone-vphone-legacy/5887db82-681a-46a6-a231-ec9e639c9486/scratchpad/dilate_run.py K OUT SECONDS "t1,t2,..." [TOUCH_SCRIPT]` (one visible boot plus captures; it deletes the image clones) and `strip_live.py OUT sheet.png` (filmstrip). Copy them into `research/tasks/` or your worktree; that scratch directory may disappear.

## Goals, in order
1. **Unlock at k=2.** Find why the swipe fails. Candidates: gesture velocity and duration in guest time (try `dur=1.4 steps=84` so the guest sees 0.7 s, and the 18-frame release settle, which is now only 150 ms of guest time); the touch-script tick versus guest time; the MultitouchHID timestamp unit (check what unit the 30-byte contact timestamp is in, µs or mach ticks); anything else in the touch path that reads the wall clock. Do not change k=1 behaviour: every change must be a no-op when k=1 (the 232 unit tests and the touch_input/spi_touch contracts must still pass).
2. **Measure the animation.** With a working unlock at k=2 (then k=3 and k=4 if k=2 is not enough), capture every 0.1–0.25 s across the transition and report: whether the lock content slides off instead of vanishing, whether Home icons and labels fly in (how many distinct intermediate frames), and the wall time from swipe to settled Home. Compare with k=1 (`build/agx-corpus/transition-live/`).
3. **Measure the cost.** Wall time to lock screen and to settled Home for each k you test, panics, and anything that breaks (display sleep, keep-awake, app launch: tap Settings at `245,815` after Home and check it opens).
4. Save filmstrips (PNG, small) and a JSON summary under `research/evidence/D-time-dilation/`, and write `research/reports/D-time-dilation.md` with a recommendation (which k, if any, as the default for `backend/live_run.py`). Commit to `exp/time-dilation`. Do not merge or push.

Delete each boot's `<output>/images` (the script does) and wait for `uptime` load < 5 before timing measurements. Never write into `build/` except `build/dilation/<run>/` outputs.
