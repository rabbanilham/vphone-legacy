# Task GC (agy): PPP state inheritance and signature-based blur detection

Worktree `/Users/bagas/Documents/Projects/vphone/vphone-wt-gfx-c`, branch `wt/gfx-c` (based on `gfx/fidelity-2`, which already contains GA and GB: read `research/reports/GA-untextured-strides.md` and `research/reports/GB-clock-rotated-aa.md` first). Report: `research/reports/GC-ppp-state-blur-signature.md`. Lead/orchestrator: Claude. A parallel worker (task GD, `vphone-wt-gfx-d`) is working on textured sampling (bilinear and vibrancy) at the same time. Keep your changes to PPP state decoding, program identification and the switch-shadow path. Do not refactor shared helpers, rename things or reformat code you don't need.

## You own
`backend/s5l8960x.c` ONLY between `AGX_REPLAY_CORE_BEGIN` and `AGX_REPLAY_CORE_END`, `backend/agx_replay_offline.py`, `tests/test_agx_*` (new test files preferred), and your report. Touch nothing else. Never touch other worktrees or `build/agx-corpus/` (it is read-only and shared).

## Work offline. No QEMU boots. Never start qemu-system-aarch64 or display_probe.py.
Your private corpus is in `corpus/`. To replay everything (~6 s):

    .venv/bin/python scripts/agx_corpus.py --corpus corpus

Output goes to `corpus/report/summary.json`. Before changing anything, save a baseline with `cp -R corpus/report corpus/report-baseline`. To replay a single dump: `corpus/.harness/agx_replay_host-* frame.bin out.rgba`. Set `AGX_HOST_VERBOSE=1` to print the replay log.

## 1. PPP incremental state retention (12 skips)
GA left 12 draws skipped as `stride 24 untextured with program 0x0`: draw 2 in `unlock` frames 5/19/33/47/61/75/89 and in `unlock-flyin` frames 5/11/17/23/29. Their PPP packet is incremental (`hdr = 0xc3f`). It updates the scissor and viewport but has no fragment-program word, and it follows draw 1 (prog `0x8eb52cf6fe248b1d`, textured).
- Model PPP state retention in both C and Python: a state word that an incremental header does not emit keeps its value from the previous draw in the same control stream. That covers the fragment program, TSW/samplers and blend.
- Prove the rule from the header bits. Check every other incremental header in the corpus, and make sure the rule does not change draws that already replay correctly.
- Then decide what these draws really are and replay them. Note that the UVs are far out of range, v ∈ [-871,-870], so check the sampler wrap/clamp mode.

## 2. Blur detection by USC code signature, not by hash list
GA added 22 FNV hashes (`AGX_USC_BLUR_10..31`, `BLUR_PROGRAMS_12`). GA's report says they all share an identical 41-byte USC preamble and differ only in trailing bytes that depend on heap layout. The hashes will therefore change between boots and builds.
- Replace the list with a signature match on the program's USC code (the preamble, plus whatever distinguishes the 4/6/7-tap variants), in both C and Python.
- Keep the existing exact paths for `AGX_USC_BLUR_4TAP` and `AGX_USC_BLUR_7TAP` working. Verify that no non-blur program in the corpus matches the signature.
- Python currently classifies these 104 draws as `approximated: Gaussian blur (separable 6-tap, canvas backdrop)`. If the tap count and weights can be read from the shader constants, replay them exactly and make the classification say so.

## 3. UISwitch shadow edge
docs/status.md lists "a thin switch-shadow edge" as open. GA's blur now draws into the switch shadow/knob caches (`settings-all` frames 5..37, targets `0x1071c000`, `0x1076c000`, `0x10554000`).
- Compare the chained `settings-all` C replay of the switch region against the live capture in `corpus/settings-all/probe/` (and `research/evidence/U-uiswitch/`), and find whether a thin hard edge remains around the knob shadow.
- If it does, fix the cause, for example the 9-slice sampling at slice boundaries, the clamp, or blur radius/edge handling, in C and Python.
- Save before/after crops.

## Measurable target
- 0 skips in C and Python, or each remaining skip explained by a precise reason.
- No hard-coded blur hash list remains for these programs.
- No regression: diff every C replay PNG of the whole report against your baseline. List each changed frame with its bbox and the reason. Save evidence crops under `research/evidence/GC-ppp-state-blur-signature/`.
- These must pass:
  - `.venv/bin/python -m unittest discover -s tests`
  - `.venv/bin/python backend/agx_contract_test.py`
  - `backend/.venv/bin/python scripts/qemu/build_backend.py`

## Finish
Commit on `wt/gfx-c`, with a message ending in `Co-Authored-By: Antigravity <agy@deepmind.com>`. Write the report with:
- the decoded rules
- the evidence
- corpus totals before and after
- the changed frames
- the test output
- open items

End your final answer with a short summary and the commit hash.
