# Task G2 (agy): continue Codex's lock-screen graphics work
Worktree: ${WORKTREES_ROOT}/vphone-wt-lock-gfx (branch wt/lock-gfx). Report id: G2-lock-gfx (also update research/reports/G-lock-gfx.md).

Codex was stopped mid-task (usage limit). FIRST read `research/reports/G-lock-gfx.md` in this worktree: it lists what Codex changed (all UNCOMMITTED in the working tree: backend/s5l8960x.c, agx_replay_offline.py, agx_frame.py, agx_contract_test.py, display_probe.py, tests), what works, and the evidence in backend/evidence/G-lock-gfx/. Keep Codex's changes; build on them. The full Codex log is ${REPO_ROOT}/research/tasks/G-lock-gfx.log (read the narration, skip the huge outputs).

## Current state
- Live lock screen now shows the star-field wallpaper + "slide to unlock" (final/lock-75.png). Clock and date are still missing.
- Live Home after unlock: still wallpaper + small white rectangle (final/home-115.png), although the isolated C replay of the same dump draws Home (c-home.png).
- Latest finding (cursor-audit-r2): the clock IS drawn into all three display buffers, then ERASED by a later one-draw wallpaper pass. Suspected: missing clipping / scissor / dirty-region handling on that draw, so the full-screen wallpaper redraw covers regions it should not touch.
- backend/bin/qemu-system-aarch64 is OLDER than the last s5l8960x.c edit: rebuild first.

## Steps
1. Rebuild QEMU (`backend/.venv/bin/python scripts/qemu/build_backend.py`), run `.venv/bin/python -m unittest discover -s tests` and `backend/agx_contract_test.py` (Cocoa window: VPHONE_DISPLAY=cocoa). Then commit Codex's WIP as-is on wt/lock-gfx ("WIP from Codex: ...", Co-Authored-By: Codex <noreply@openai.com>) so it is safe.
2. Decode the state of the one-draw wallpaper pass that overwrites the clock (scissor/clip rect, ISP/PPP state words, dirty region, load/store flags, which TA it pairs with) and compare with the pass that draws the clock. Implement the missing clip in the live replay AND the offline mirror. Add a contract: a clipped redraw must not touch pixels outside its region.
3. Live probe (display_probe.py, VPHONE_DISPLAY=cocoa, `--framebuffer-at 75 --framebuffer-at 90 --framebuffer-at 115 --framebuffer-at 140`, unlock swipe `VPHONE_TOUCH_SCRIPT='at=95;down 40,1036;hold 0.3;move 600,1036 dur=3 steps=180;hold 1.0;up'`): check clock+date on the lock screen and Home icons/dock after unlock. If Home is still missing, apply the same analysis to the Home passes.
4. Check the wallpaper colour channel order (BGRA vs RGBA) against the source texture.
5. Remove or env-gate the temporary `agx_audit_clock` tracing.
6. Tests + contracts pass, commit, update the report with PASS/FAIL per goal and evidence paths.
