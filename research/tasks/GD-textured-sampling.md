# Task GD (agy): exact textured sampling (bilinear scaled quads and vibrancy)

Worktree `/Users/bagas/Documents/Projects/vphone/vphone-wt-gfx-d`, branch `wt/gfx-d` (based on `gfx/fidelity-2`, which already contains GA and GB). Report: `research/reports/GD-textured-sampling.md`. Lead/orchestrator: Claude. A parallel worker (task GC, `vphone-wt-gfx-c`) is changing PPP state retention, blur program detection and the switch-shadow path at the same time. Keep your changes inside the textured-quad sampling path and the vibrancy blend. Do not refactor shared helpers, rename things or reformat code you don't need.

## You own
`backend/s5l8960x.c` ONLY between `AGX_REPLAY_CORE_BEGIN` and `AGX_REPLAY_CORE_END`, `backend/agx_replay_offline.py`, `tests/test_agx_*` (new test files preferred), and your report. Touch nothing else. Never touch other worktrees or `build/agx-corpus/` (it is read-only and shared).

## Work offline. No QEMU boots. Never start qemu-system-aarch64 or display_probe.py.
Your private corpus is in `corpus/`. To replay everything (~6 s):

    .venv/bin/python scripts/agx_corpus.py --corpus corpus

Output goes to `corpus/report/summary.json`. Before changing anything, save a baseline with `cp -R corpus/report corpus/report-baseline`. To replay a single dump: `corpus/.harness/agx_replay_host-* frame.bin out.rgba`. Set `AGX_HOST_VERBOSE=1` to print the replay log.

## Defect
The Python audit on the current base reports two approximated paths:
- `approximated: textured quad (bilinear scaled)`: 97 draws
- `approximated: vibrancy (Rec.709 luma-weighted tint)`: 40 draws

Scaled textures include icons, wallpaper and Home/Settings layers. Vibrancy covers the dock and the Control-Center/folder-style backdrops.

## Do
1. **Group the 97 bilinear draws** by program, TSW/sampler words, texture size and scale factor. Decode each of these from the sampler words:
   - min/mag filter (nearest, linear, mip)
   - wrap/clamp mode
   - texel-centre convention
   - texture format: sRGB or not, premultiplied or not

   Then make the C renderer and the Python mirror sample exactly that: the correct half-texel offset, clamp-to-edge, and the filter the sampler says. Where the hardware filter is linear, compute it in the same fixed-point/rounding way in both C and Python, so that the two implementations are bit-identical. When a group is proven exact, reclassify it as `replayed exactly: ...`.
2. **For the 40 vibrancy draws**, decode the full shader constant block: luma weights, the tint colour/matrix, and any saturation/brightness terms. Compare it with how iOS 7 builds `_UIBackdropView` vibrancy, which is a colour matrix plus tint over a blurred backdrop. Implement the exact maths in C and Python and validate it against the live captures in `corpus/*/probe/framebuffer-at-*.ppm`, for example the dock region in `unlock` at 101 s.
3. Add unit tests for the sampler decode, the filter maths and C/Python parity.

## Measurable target
- Each approximated count reaches 0. Where it does not, every remaining draw has a precise reason.
- Mean absolute error against the live captures, for the dock region and a Home icon row, is lower than baseline. Report the numbers.
- No regression: diff every C replay PNG against your baseline. List each changed frame with its bbox and the reason. Save before/after crops under `research/evidence/GD-textured-sampling/`.
- These must pass:
  - `.venv/bin/python -m unittest discover -s tests`
  - `.venv/bin/python backend/agx_contract_test.py`
  - `backend/.venv/bin/python scripts/qemu/build_backend.py`

## Finish
Commit on `wt/gfx-d`, with a message ending in `Co-Authored-By: Antigravity <agy@deepmind.com>`. Write the report with:
- the decoded sampler rules
- the evidence and error numbers
- corpus totals before and after
- the changed frames
- the test output
- open items

End your final answer with a short summary and the commit hash.
