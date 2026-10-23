# G-lock-gfx: full lock screen and live Home (interim report)

Worker: Codex (gpt-6.1-sol, medium), session `01a166f7-6893-7c50-857d-436ed88b70d4`.
Stopped by user request on 2026-10-23 because Codex was close to its usage limit. Report written by Claude from the Codex log (`/Users/bagas/Downloads/vphone-legacy/research/tasks/G-lock-gfx.log`), the worktree diff and the evidence.

Branch `wt/lock-gfx` (worktree `/Users/bagas/Downloads/vphone-wt-lock-gfx`), base `e63a591`. **All changes are uncommitted.**

## Status per goal

| Goal | Result |
| --- | --- |
| Lock screen: wallpaper | **PASS (live)**: star-field wallpaper plus slide text (`final/lock-75.png`). Colours may still have a channel mismatch (Codex's note); this is not verified against a real device. |
| Lock screen: clock and date | **FAIL**: still missing live |
| Home after unlock, live | **FAIL**: wallpaper only, plus a small white rectangle (`final/home-115.png`) |
| Home in offline C replay | Icons, labels and dock render (`c-home.png`), with wallpaper artefacts |
| Unit tests | 227 tests OK (7 skipped), rerun by Claude after the stop |
| AGX pixel contracts | Passed earlier in threaded and inline modes. **Not rerun** after the last edits. |

## Findings (in order)

1. **Baseline reproduced.** Lock at 90 s shows only "slide to unlock"; at 115 s after unlock there is a small white rectangle (`baseline/`).
2. **TA/3D pairing history too small.** Each context had 8 entries, but the guest queues more TA commands per batch. A 3D pass then found no matching TA, so the dump fell back to an unrelated status-bar TA and the live replay skipped the pass. The fix keeps 256 TA per context, and fallback dumps are no longer marked as a valid pair. Regression: 17 TA.
3. **Wallpaper.** The pass uses stride-16 vertices with clip-space X/Y and texel U/V, into a 744×1392 target. The replay treated the positions as pixels and capped the target at 640×1136. The source texture is Morton-tiled, decoded with Y-first interleaving (`wallpaper-morton-True.png` vs `-False.png`), and the output pitch comes from the PBE. **This fix is what makes the wallpaper appear live.**
4. **Type-3 stamp waits.** The replay ignored the native fence waits that guard reuse of commands and surfaces. They are now honoured, with a contract for an unsatisfied fence.
5. **Home is present in GPU memory, but not on screen.** Plane-register and MMU snapshots (`planes/`, `scanout/`) show the display tables matching the GPU strip layout row for row, and the extra planes are inactive. **The display model is not the cause.**
6. **The isolated C replay of the same dump draws the clock, date and Home correctly** (`c-home.png`, `lock-offline.png`). So the issue is buffer lifetime or ordering in the live replay, not rasterisation.
   - Tried and reverted: delaying TA completion until rasterisation finished. No effect.
   - Kept: a geometry snapshot taken at TA time (`agx_capture_ta` / `AGXReplaySnapshot`). Its regression changes vertex colour after TA and holds 3D behind a fence. It passes, but the live clock is still missing.
   - Kept: a fix for an empty-queue poll that published the private read cursor to the guest while rendering was in flight (`tests/test_agx_queue_cursor.py`).
7. **Latest finding (`cursor-audit-r2/`, the audit build).** The clock **is drawn into all three display buffers, then erased by a later one-draw wallpaper pass.** The suspected cause is missing clipping or dirty-region (scissor) handling on that wallpaper draw: the replay redraws the full-screen wallpaper over the region that should have been limited. Codex was decoding that draw's changed state when it was stopped.

## Changed files (uncommitted)
- `backend/s5l8960x.c` (+295/−30): TA history 256, snapshot/refcount, stamp waits, stride-16 wallpaper path plus Morton decode, PBE pitch, queue cursor, `agx_audit_clock` (temporary audit tracing).
- `backend/agx_replay_offline.py`, `backend/agx_frame.py`: mirror of the above; mismatched pairs are rejected.
- `backend/agx_contract_test.py`: fence, wallpaper at 744 px, and geometry-lifetime cases. Includes a 1 s window delay, because QEMU crashed at shutdown in Cocoa's keyboard handler during short fixtures.
- `backend/display_probe.py`: read-only plane/MMU snapshots.
- `tests/test_agx_frame.py`, `tests/test_agx_pass_load.py`, new `tests/test_agx_queue_cursor.py`, `tests/test_agx_wallpaper.py`.

**Caution:** `backend/bin/qemu-system-aarch64` (12:09) is older than the last edit to `s5l8960x.c` (12:12). Rebuild before using it.

## Evidence
`backend/evidence/G-lock-gfx/` (ignored by git, about 3 GB of frame dumps). Probe runs `baseline/`, `candidate/`, `inline/`, `snapshot/`, `lifetime/`, `final/`, `scanout/`, `planes/`, `cursor-audit*/`, each with `lock-*.png`/`home-*.png`, plus offline renders in the root. The disposable clone `cursor-audit-r2/images` (3 GB) was deleted after the stop.

## Next steps
1. Rebuild. Decode the state of the one-draw wallpaper pass that overwrites the clock (scissor/clip rect, dirty region, load/store flags), and apply clipping to that draw in the live replay and offline. Add a contract where a clipped redraw must not erase content outside its region.
2. Remove the `agx_audit_clock` audit tracing, or put it behind an env flag.
3. Recheck Home after unlock. It probably has the same cause (the full wallpaper redrawn over the icons).
4. Check the wallpaper's channel order (BGRA vs RGBA).
5. Rerun the AGX contracts plus unit tests, then commit on `wt/lock-gfx`.
