# Task GA (agy): untextured draws with unknown vertex stride

Worktree `/Users/bagas/Documents/Projects/vphone/vphone-wt-gfx-a`, branch `wt/gfx-a` (based on `gfx/fidelity-2` = main 539270b). Report: `research/reports/GA-untextured-strides.md`. Lead/orchestrator: Claude. A parallel worker (task GB, worktree `vphone-wt-gfx-b`) is changing the triangle / wide-line rasterisers and the rotated textured path at the same time, so keep your changes inside the untextured-stride dispatch and a NEW primitive function; do not refactor shared helpers, rename things or reformat code you don't need.

## You own
`backend/s5l8960x.c` ONLY between `AGX_REPLAY_CORE_BEGIN` and `AGX_REPLAY_CORE_END`, `backend/agx_replay_offline.py`, `tests/test_agx_*` (new test files preferred), your report. Touch nothing else. Never touch other worktrees or `build/agx-corpus/` (read-only, shared).

## Work offline. No QEMU boots. Never start qemu-system-aarch64 or display_probe.py.
Your private corpus is already set up in `corpus/` (symlinked dumps, excluded from git). Replay everything (C renderer QEMU runs + Python audit, ~6 s):

    .venv/bin/python scripts/agx_corpus.py --corpus corpus

Output: `corpus/report/summary.json` (+ PNGs per frame, chained per scene). Single dump: `corpus/.harness/agx_replay_host-* frame.bin out.rgba`, `AGX_HOST_VERBOSE=1` prints the replay log. Save the baseline report before you change anything (`cp -R corpus/report corpus/report-baseline`).

## The defect
Baseline totals: C `c_skips = {"untextured with unsupported stride": 116}`; Python `"skipped: untextured with stride not in (20, 28)": 116`. Per scene: draw index 0 in 64 `unlock`, 21 `unlock-flyin`, 19 `settings-all` frames; draw index 2 in 7 `unlock` + 5 `unlock-flyin` frames. Draw 0 of nearly every Home/Settings frame is being dropped, so whatever it paints (likely a background/backdrop fill, a dimming layer or a gradient) is missing, and later draws composite over stale pixels.

## Do
1. Decode these draws: vertex stride, attribute layout (position, colour, any extra per-vertex attribute such as gradient/coverage/u), program id, blend state, region/scissor, how many vertices/primitive topology. Compare across scenes; group by program id. Use the existing walkers in `agx_replay_offline.py` and the C log.
2. Determine the correct semantics (solid fill? per-vertex colour gradient? a mask?) with evidence: compare against the live framebuffer captures in `corpus/*/probe/framebuffer-at-*.ppm` and the visual content of later draws. Don't guess: write the evidence in the report.
3. Implement it in C AND the Python mirror identically (same colour byte order as the stride-20/28 paths: vertex colour is RGBA in memory), same blending/clipping as the existing solid paths. If a subgroup cannot be proven, leave it skipped with a precise reason string.
4. Unit tests for the new path (C/Python parity like the existing `tests/test_agx_*`).

## Measurable target
- `untextured with unsupported stride` = 0 in C and Python (or each remaining skip explained by program id with a new specific reason).
- No regression: diff every C replay PNG of the whole report against your baseline; list every frame whose pixels change, the changed bbox, and why. Save representative before/after crops (PNG) under `research/evidence/GA-untextured-strides/`.
- `.venv/bin/python -m unittest discover -s tests` passes; `.venv/bin/python backend/agx_contract_test.py` passes; `backend/.venv/bin/python scripts/qemu/build_backend.py` compiles.

## Finish
Commit on `wt/gfx-a` (message ending with `Co-Authored-By: Antigravity <agy@deepmind.com>`), write the report with: decoded layout, evidence, corpus before/after totals, list of changed frames, test output, open items. End your final answer with a short summary of results and the commit hash.
