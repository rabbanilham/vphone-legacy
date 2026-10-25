# Task GE (agy): lock-screen grabber/camera draws (stride 36, two UV sets)

Worktree `/Users/bagas/Documents/Projects/vphone/vphone-wt-gfx-e`, branch `wt/gfx-e`, based on `gfx/fidelity-3`. That base is `gfx/fidelity-2` (PR #6) and already contains tasks GA–GD. Read `research/reports/GD-textured-sampling.md` first.

Report: `research/reports/GE-stride36-dual-texture.md`. Lead/orchestrator: Claude.

A parallel worker (task GF, `vphone-wt-gfx-f`) is changing the canvas-backdrop blur and the `ROUNDED_CAPS` tint at the same time. Keep your changes inside a new stride-36 textured path. Do not refactor shared helpers, rename things or reformat code you don't need.

## You own
- `backend/s5l8960x.c`, ONLY between `AGX_REPLAY_CORE_BEGIN` and `AGX_REPLAY_CORE_END`
- `backend/agx_replay_offline.py`
- `tests/test_agx_*` (new test files preferred). Tests must not import PIL/numpy; CI has only the standard library.
- your report

Touch nothing else. Never touch other worktrees or `build/agx-corpus/`, which is read-only and shared.

## Work offline. No QEMU boots. Never start qemu-system-aarch64 or display_probe.py.
Your private corpus is in `corpus/`. To replay everything (~6 s):

    .venv/bin/python scripts/agx_corpus.py --corpus corpus

Before changing anything, save a baseline with `cp -R corpus/report corpus/report-baseline`.

To replay a single dump with the C harness: `corpus/.harness/agx_replay_host-* frame.bin out.rgba`. Set `AGX_HOST_VERBOSE=1` for the log.

To run the Python audit/replay of a single dump: `cd backend && PYTHONPATH=. ../.venv/bin/python agx_replay_offline.py ../corpus/unlock/dumps/frame-14.bin out.png`.

## The defect
In the iOS 7 lock screen, the bottom Control-Center grabber is a short translucent white bar, and the bottom-right corner has a camera glyph. Both are drawn with "vibrancy" over the wallpaper. This replay draws them wrong:

- The draws are **stride 36, textured**: 12 in `unlock` (lock-screen frames, e.g. frame-10, frame-14 draw 0, frame-28, frame-42). In `unlock/dumps/frame-14.bin`, draw 0 covers rect (283,1114)–(357,1131). Its TSW points at `0x10618000`, an offscreen 640×96 surface.
- Vertex layout, decoded by the lead:
  - `+0` float4 position (x, y, 0, 1)
  - `+16` u32 colour (`0xffffffff`)
  - `+20` float2 UV set 0, **in pixels** (0..74, 0..17)
  - `+28` float2 UV set 1 (identical values in this draw)
- The current code (after a lead fix) does not read the UVs for stride 36 at all. It samples the whole texture scaled into the rect, which gives small dark dash marks where the grabber should be.
- GD briefly read UV set 0 like stride 28. That sampled the top-left 74×17 of `0x10618000` directly and painted **solid black bars** live (evidence: `backend/evidence/gfx-fidelity-2-cd-live/framebuffer-at-90s.ppm` in the main repo, `/Users/bagas/Documents/Projects/vphone/vphone-legacy`). The rule must therefore be more than "sample texture 0 with UV 0".

## Do
1. Decode the full state of these draws:
   - fragment program id and its USC code (compare with known programs: vibrancy, `DEST_IN`, colour matrix)
   - **all** samplers/TSWs referenced by the PPP state: there should be two textures, a mask and a backdrop, or a mask plus a vibrancy constant block
   - constants (luma weights, tint, matrix)
   - blend state
   - what was drawn into `0x10618000` earlier in the same or previous frames, and where

   Find which UV set addresses which texture.
2. Work out the shader semantics from the code and constants. iOS 7 `_UIBackdropView` / vibrancy: output = f(backdrop) × mask alpha, with a tint/colour matrix. Validate against the live lock-screen capture `corpus/unlock/probe/framebuffer-at-95s.ppm`, cropping the grabber at about (283..357, 1110..1135) and the camera corner at about (565..630, 1070..1115).
3. Implement the path in C and Python identically, so C/Python parity holds. If part of it cannot be proven, keep the current behaviour for that part and document why.
4. Add unit tests (synthetic buffers; standard library only).

## Measurable target
- The grabber and camera regions in the chained `unlock` lock-screen frames match the live 95 s capture better than baseline. Report the MAE before and after for both crops. There must be no solid black pixels in those boxes.
- No regression: diff every C replay PNG against your baseline. List each changed frame, its bbox and the reason. Save crops under `research/evidence/GE-stride36-dual-texture/`.
- These must pass:
  - `.venv/bin/python -m unittest discover -s tests`
  - `.venv/bin/python backend/agx_contract_test.py`
  - `backend/.venv/bin/python scripts/qemu/build_backend.py`

## Finish
Commit on `wt/gfx-e`, with the message ending `Co-Authored-By: Antigravity <agy@deepmind.com>`. Write the report with: decoded state, semantics, evidence, MAE numbers, corpus totals, changed frames, test output and open items. End your final answer with a short summary and the commit hash.
