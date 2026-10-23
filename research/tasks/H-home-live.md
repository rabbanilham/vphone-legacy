# Task H (Codex): Home screen renders live after unlock, every boot
Worktree: /Users/bagas/Downloads/vphone-wt-home-live (branch wt/home-live, based on wt/live-swipe d096333 which has the working short swipe). Report id: H-home-live.

## Problem: intermittent, with a strong lead
After unlocking, the live display usually shows black with a small white rectangle (handover section 23), but NOT always. On the same build and same profile, four runs in /Users/bagas/Downloads/vphone-wt-live-swipe/research/evidence/T-live-swipe/:
- boot1-scripted: framebuffer-at-102s.png shows the full Home screen live (icons, labels, dock, status bar).
- boot2-scripted, boot3-scripted, mouse-drag: framebuffer-at-102s.png black + small white rectangle.
Each has qemu-trace.log, serial.log, framebuffer captures. `AGX replay target` vs `AGX RT queue` counts are similar in all four (~19-22k vs ~19-22k), so it is not simply a stall.
Earlier Codex findings (research/reports/G-lock-gfx.md in /Users/bagas/Downloads/vphone-wt-lock-gfx, read it): the isolated C replay of a Home frame dump draws Home correctly; display MMU tables match the GPU strip layout; Home content is submitted into an offscreen target that never reaches scanout.

## Goal
Home renders live after unlock in 3/3 boots (screenshots at ~102 s and ~140 s), with taps still working.

## Approach
1. Diff boot1 vs boot2/3 traces around the unlock (95-102 s): which contexts/targets/passes are replayed, display swaps (`[DISP0]` layer base, swap tokens), which surface is scanned out, ordering/timing of TA vs 3D vs swap, replay-thread vs inline. Find what boot1 did differently.
2. Reproduce with frame dumps (VPHONE_AGX_FRAME_DUMP*) and offline replay to separate missing draws from composition/scanout ordering. Consider races between the replay thread and the guest's buffer reuse (stamps/fences) and the display swap.
3. Fix in the emulator (s5l8960x.c + offline mirror) with a contract test.

## Coordination (important)
- agy G2 is concurrently changing the AGX replay in /Users/bagas/Downloads/vphone-wt-lock-gfx (clipping of a full-screen wallpaper redraw that erases the lock-screen clock; uncommitted + commit a77b241 contains your earlier Codex WIP: TA history 256, geometry snapshot, queue cursor, stride-16 wallpaper). Do NOT edit that worktree. You may read it, and you may cherry-pick a77b241 into your branch if you need it, noting that in the report. Keep your changes focused so they merge cleanly.
- Use the scripted swipe from this branch: `VPHONE_TOUCH_SCRIPT='at=95;down 40,1036;move 600,1036 dur=0.7 steps=42;up'` (settle frames make it reliable).
