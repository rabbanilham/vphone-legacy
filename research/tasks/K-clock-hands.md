# Task K: Clock icon hands (vertex colour order and triangle rasterisation)

Worktree `${WORKTREES_ROOT}/vphone-wt-clock-hands`, branch `wt/clock-hands`. You own the AGX replay renderer for this task: `backend/s5l8960x.c` (only the AGX replay core between `AGX_REPLAY_CORE_BEGIN` and `AGX_REPLAY_CORE_END`), `backend/agx_replay_offline.py`, `tests/test_agx_*`. Touch nothing else in `s5l8960x.c` (touch, Wi-Fi, USB regions belong to others).

## Work offline. No QEMU boots.
The corpus is already recorded in `build/agx-corpus/` (read-only; do not delete or re-capture). One command replays every dump with the exact C renderer QEMU runs plus the Python audit in about 2 s:

    .venv/bin/python scripts/agx_corpus.py --corpus build/agx-corpus

That writes `build/agx-corpus/report/` though, which is shared. **Copy first** and use your copy: `mkdir -p corpus && for d in build/agx-corpus/*/; do mkdir -p corpus/$(basename $d) && ln -s $PWD/$d/dumps corpus/$(basename $d)/dumps; cp $d/recipe.json corpus/$(basename $d)/ 2>/dev/null; ln -s $PWD/$d/probe corpus/$(basename $d)/probe; done`, then run with `--corpus corpus` (output in `corpus/report/`; scenes replay chained, pass by pass). Add `corpus/` to `.git/info/exclude`. A single dump: compile the harness once (the script prints nothing extra; it is `corpus/.harness/agx_replay_host-*`) and run `agx_replay_host frame.bin out.rgba`; `AGX_HOST_VERBOSE=1` prints the replay log.

## The defect
On Home the Clock icon's minute hand is a big dark square block and the second hand is a thick blue bar. Evidence: `build/agx-corpus/unlock/probe/framebuffer-at-101s.ppm` (icon at x 190..300, y 250..360). Settled Home passes are `unlock` frames 91..161 (display passes, all draws). In `unlock/dumps/frame-91.bin` the draws over the icon are (Python walk, screen coords):

- draw 1: icon base, textured, stride 24, prog 0x8eb52cf6fe248b1d
- draws 2,4,6,8: n=48, stride 28, untextured, prog 0x6c01d033fd937c7c (rounded caps / path fan, triangle list)
- draws 3,5,7,9: n=6, stride 28, untextured, prog 0xba8c50a79810e866: thin rotated hands (two triangles, not axis-aligned)
- draw 10: n=6 stride 20 prog 0x2f70323ca222288c (centre dot); draw 11: n=48 stride 20 prog 0x5fe3ceea98f5b523 colour 0x40070f37
- colours: 0xff333333 (hour/minute, dark grey) and **0xff1c3edc** (second hand).

Two root causes, both confirmed by the lead:
1. **Vertex colour byte order.** Untextured vertex colours (stride 20 and 28, +16) are stored **RGBA** in memory, but the replay treats them as BGRA. 0xff1c3edc read as RGBA is #DC3E1C (the red second hand); as BGRA it is blue. The same swap shows elsewhere: the Clock app's table background 0xfff4efef renders pinkish, while the real iOS 7 grouped background is #EFEFF4. Check whether the textured-draw tint (stride 28 +16) has the same order before changing it: the "Edit"/"+" labels in the Clock app (clock frames 7..10) and the red "Friday" on the Calendar icon must stay red. Fix in C and Python.
2. **Non-axis-aligned triangles.** The replay rasterises only axis-aligned quads (bounding box), so rotated hands and the n=48 triangle lists become filled rectangles. Add a proper triangle rasteriser for untextured draws (each consecutive vertex triple is one triangle; coverage by edge functions at pixel centres; src-over with the vertex colour, same blending as the existing solid path, same scissor/region clipping). Keep the existing axis-aligned fast path for the quads that already work. Textured rotated draws may stay as they are unless trivial.

## Measurable target
- Python audit: "skipped: non-axis-aligned or rotated geometry" for untextured draws goes to 0, and C replay draws for these programs are no longer bounding boxes.
- `unlock` frame-91 replay crop of the icon: thin dark hour/minute hands and a thin red second hand meeting at the centre; no blocks. Save before/after crops as PNG under `research/evidence/K-clock-hands/`.
- No regression elsewhere: compare the whole report before and after (the C replay images of every dump; list frames whose pixels change and explain each). The lock screen clock digits, the Settings, Clock and Calendar screens must not get worse.
- `.venv/bin/python -m unittest discover -s tests` passes; add unit tests for the rasteriser and the colour order. `backend/.venv/bin/python scripts/qemu/build_backend.py` still compiles.

Do not boot QEMU; the lead (Claude, MBP) does one live boot at merge time. Commit to `wt/clock-hands` and write `research/reports/K-clock-hands.md`.

## Follow-up 1 (lead review of f6b18c0, 2026-10-23)
Your colour order fix and the triangle rasteriser are right (frames 91 and 120: thin red second hand). But **diagonal hands are still blocks**: replay the corpus chained (the default) and crop x 190..300, y 250..360 of `unlock-frame-161.png`: a dark square (minute hand) and a red square (second hand). Your before/after crops used frame 91, where the hands are nearly vertical, so a bounding box looks thin.

Cause: the hands (prog 0xba8c50a79810e866, n=6, stride 28) are not triangles. Every vertex comes in identical pairs, e.g. frame-161 draw 7: A,A,B,B,B,A with A=(264.58,260.85), B=(245.58,284.07), so every triangle has zero area and your code falls back to the box. The attribute after the colour is a cross-width coordinate: u=0.375 / 0.625 on the minute hand (v=0.5), u=0.5 everywhere on the second hand. This is a **line primitive with a width** (CoreGraphics antialiased stroke): the segment A→B is drawn with a width from pipeline state, and u runs across the width.

Do:
1. Find where the primitive type and line width come from: the VDM index-list word (w[0]/w[1] bits, primitive topology), the PPP state words of that draw (look for a float or fixed-point width that differs between hour, minute and second hand), or the u range. Compare the hour (draw 3/5), minute (draw 7) and second (draw 9) hands in frames 91..161.
2. Rasterise these as wide lines: distance from pixel centre to segment A→B ≤ width/2, with flat (butt) ends; the n=48 caps (prog 0x6c01d033fd937c7c) already round the ends. Same colour order, blending and clipping as your triangles. Keep the generic triangle path for real triangles.
3. Target: chained `unlock-frame-161.png` and every settled Home frame 91..161 show thin hands at their angle (hour about 3 px, minute about 2 px, second about 1 px wide), no blocks; save a strip of crops (frames 91, 120, 140, 161) to `research/evidence/K-clock-hands/`. The same regression rules apply. Amend the report, commit to `wt/clock-hands`.
