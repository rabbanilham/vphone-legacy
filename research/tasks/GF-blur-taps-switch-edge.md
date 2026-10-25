# Task GF (agy): canvas-blur taps from shader constants, and the UISwitch shadow edge

Worktree `/Users/bagas/Documents/Projects/vphone/vphone-wt-gfx-f`, branch `wt/gfx-f`, based on `gfx/fidelity-3`. That base is `gfx/fidelity-2` (PR #6) and already contains tasks GA–GD.

Read first: `research/reports/GA-untextured-strides.md`, `research/reports/GC-ppp-state-blur-signature.md` (including §8, the lead review) and `research/reports/U-uiswitch.md`.

Report: `research/reports/GF-blur-taps-switch-edge.md`. Lead/orchestrator: Claude.

A parallel worker (task GE, `vphone-wt-gfx-e`) is adding a stride-36 textured path at the same time. Keep your changes inside the canvas-backdrop blur and the `ROUNDED_CAPS` textured path. Do not refactor shared helpers, rename things or reformat code you don't need.

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

## 1. Canvas-backdrop blur taps (104 draws)
The Python audit marks 104 draws as `approximated: Gaussian blur (separable 6-tap, canvas backdrop)`. These are stride-12 blur passes, which GC now detects by USC code signature: draw 0 of most `unlock`, `unlock-flyin` and `settings-all` frames, drawn into offscreen caches.
- C uses a fixed 6-tap kernel. Python already extracts tap parameters from the vertex uniforms (`vs_data + 48`) for the 4/7-tap programs.
- Read the actual tap count, offsets, weights and direction for these draws from their shader constants/uniforms. Use the same location rule for every signature-matched blur, and check that it agrees with the known 4-tap and 7-tap programs.
- Implement it identically in C and Python, and make the classification say `replayed exactly` where the parameters are read rather than assumed.

## 2. UISwitch shadow edge
A thin hard edge remains around the knob's drop shadow in `settings-all` near x=508 (the switch at about x 493..594, y 104..243 in frames 1/6/8/9/14/16).

GC tried scaling the tint by red×alpha "coverage" for every `ROUNDED_CAPS` draw. That was **rejected**: the same program draws the lock-screen "slide to unlock" label, which turned pale (MAE against live 1.7 → 16.7).

- Find the real cause: 9-slice UV boundaries, clamp vs the scissor at x=508, how the shadow texture `0x10524000` was produced, or the tint semantics.
- Fix it in a way that keys on the texture/sampler/slice data rather than on the program alone.
- Validate against the live Settings capture in `corpus/settings-all/probe/`.
- The "slide to unlock" label in `settings` lock-screen frames (region about 126..482 × 942..1008) must stay pixel-identical to baseline.

## Measurable target
- Blur: 0 `approximated` blur draws, or each remaining one explained.
- Switch: MAE against live in the switch region is lower than baseline, with no new hard edge.
- In both cases the slider label is unchanged.
- No regression: diff every C replay PNG against your baseline. List each changed frame, its bbox and the reason. Measure changed regions against the time-matched live capture, and report the MAE before and after. Save crops under `research/evidence/GF-blur-taps-switch-edge/`.
- These must pass:
  - `.venv/bin/python -m unittest discover -s tests`
  - `.venv/bin/python backend/agx_contract_test.py`
  - `backend/.venv/bin/python scripts/qemu/build_backend.py`

## Finish
Commit on `wt/gfx-f`, with the message ending `Co-Authored-By: Antigravity <agy@deepmind.com>`. Write the report with: decoded parameters, evidence, MAE numbers, corpus totals, changed frames, test output and open items. End your final answer with a short summary and the commit hash.
