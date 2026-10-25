# Task GB (agy): Clock-app rotated geometry and antialiased edges

Worktree `/Users/bagas/Documents/Projects/vphone/vphone-wt-gfx-b`, branch `wt/gfx-b` (based on `gfx/fidelity-2` = main 539270b). Report: `research/reports/GB-clock-rotated-aa.md`. Lead/orchestrator: Claude. A parallel worker (task GA, worktree `vphone-wt-gfx-a`) is adding a new path for untextured draws with unknown stride at the same time, so keep your changes inside the triangle / wide-line rasterisers and the rotated textured path; do not refactor shared helpers, rename things or reformat code you don't need.

## You own
`backend/s5l8960x.c` ONLY between `AGX_REPLAY_CORE_BEGIN` and `AGX_REPLAY_CORE_END`, `backend/agx_replay_offline.py`, `tests/test_agx_*` (new test files preferred), your report. Touch nothing else. Never touch other worktrees or `build/agx-corpus/` (read-only, shared).

## Work offline. No QEMU boots. Never start qemu-system-aarch64 or display_probe.py.
Your private corpus is already set up in `corpus/` (symlinked dumps, excluded from git). Replay everything (C renderer QEMU runs + Python audit, ~6 s):

    .venv/bin/python scripts/agx_corpus.py --corpus corpus

Output: `corpus/report/summary.json` (+ PNGs per frame, chained per scene). Single dump: `corpus/.harness/agx_replay_host-* frame.bin out.rgba`, `AGX_HOST_VERBOSE=1` prints the replay log. Save the baseline report before you change anything (`cp -R corpus/report corpus/report-baseline`). Background: `research/reports/K-clock-hands.md` (how triangles and wide lines were added).

## Defect 1: rotated geometry in the Clock app (24 skips)
Python audit: `"skipped: non-axis-aligned or rotated geometry": 24`, all in scene `clock`, draw indices 6, 7, 9, 15, 16, 17, 18, 20. Decode them (textured or not, stride, program, what they are on screen: likely the world-clock analog faces / hands, or rotated text). Implement them in C and Python: for textured rotated quads, map pixel centres back through the inverse of the quad's affine transform and sample the texture with the same filter the axis-aligned path uses; untextured ones go to the existing triangle/wide-line paths if that's what they are. Check whether the C renderer also drops them (C currently reports no rotated skip; find out what C actually does with them, e.g. a bounding box, and fix it to match).

## Defect 2: jagged clock hands (no antialiasing)
The triangle and wide-line rasterisers use binary coverage at pixel centres, so the Home Clock icon hands (and the n=48 cap fans) are jagged. iOS draws them antialiased (CoreGraphics stroke). Add analytic edge coverage: for wide lines, coverage = clamp(width/2 + 0.5 - distance, 0, 1) (butt ends likewise along the axis); for triangles, per-edge signed distance clamp or 4x4 subsample, whichever matches the live captures better. Multiply the vertex colour alpha by the coverage, same src-over blend. Apply AA only to these untextured triangle/wide-line paths, not to axis-aligned quads (they must stay pixel exact).

## Measurable target
- rotated-geometry skips = 0 (or each remaining one explained with a precise reason).
- Crop strip of the Home clock icon (x 190..300, y 250..360) from chained `unlock` frames 91, 120, 140, 161, before vs after, plus Clock-app crops of the fixed draws, saved as PNG in `research/evidence/GB-clock-rotated-aa/`. Hands smooth, widths roughly hour 3 px, minute 2 px, second 1 px.
- No regression: diff every C replay PNG of the whole report against your baseline; list each frame whose pixels change, the changed bbox, and why. Glyph spans, lock-screen digits, Settings, slider and UISwitch must not change except where the AA paths draw.
- `.venv/bin/python -m unittest discover -s tests` passes (add tests for inverse-affine sampling and AA coverage, C/Python parity); `.venv/bin/python backend/agx_contract_test.py` passes; `backend/.venv/bin/python scripts/qemu/build_backend.py` compiles.

## Finish
Commit on `wt/gfx-b` (message ending with `Co-Authored-By: Antigravity <agy@deepmind.com>`), write the report with: decoded draws, evidence, corpus before/after totals, list of changed frames, test output, open items. End your final answer with a short summary of results and the commit hash.
