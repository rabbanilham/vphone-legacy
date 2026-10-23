# Task U: UISwitch track (Settings → Airplane Mode)

Worktree `/Users/bagas/Documents/Projects/vphone/vphone-wt-uiswitch`, branch `wt/uiswitch`. Renderer scope: the AGX replay core of `backend/s5l8960x.c` (between `AGX_REPLAY_CORE_BEGIN`/`END`), `backend/agx_replay_offline.py`, `tests/test_agx_*`. Work offline only: no QEMU boots (agy D owns QEMU on this Mac). The lead does one live boot at merge time.

## Defect
In Settings the Airplane Mode switch (off state) draws only the white knob and part of its shadow. The track is missing. A real iOS 7 off switch is a white rounded track (pill, about 102x62 px at 2x) with a thin light grey border (about #E5E5E5) and a white round knob with a soft shadow on the left. Live evidence: `backend/evidence/merge-ks-live/framebuffer-at-152s.ppm` (switch around x 500..620, y 150..215).

## Corpus
Set up your corpus view as tasks K/S did (symlink `build/agx-corpus/*/dumps` and `probe` into `corpus/`, then `.venv/bin/python scripts/agx_corpus.py --corpus corpus`; scenes replay chained). Settings dumps are in scene `settings` (display passes from 131 s on show the Settings screen). The switch may be drawn in an offscreen pass that the display-only `settings` scene did not dump. If so, record the draw list you can see and say exactly which pass is missing; do not boot QEMU. Ask the lead (Claude) for a capture with `display_only: False` by writing it in your report, then stop.

## Do
1. Find the draws over the switch rectangle (stride, program hash, texture, vertex count, colours), and which of them the C replay skips (`AGX_REPLAY_SKIP` reasons in the harness output) or draws wrongly. Likely candidates: a path or stroke primitive (the pill border), an untextured draw with an unsupported stride, or a blend mode not modelled yet.
2. Fix it in C and Python, reusing the existing paths (K's triangle and wide-line rasteriser, S's spans and plus-lighter blends) where they fit.
3. Target: the chained replay of the Settings frames shows a white pill track with a light grey border behind the knob. Save before/after crops under `research/evidence/U-uiswitch/`. No pixel changes elsewhere without an explanation (compare the whole report before and after). Unit tests pass, add tests for anything new, and `backend/.venv/bin/python scripts/qemu/build_backend.py` compiles.
4. Commit to `wt/uiswitch` and write `research/reports/U-uiswitch.md`.

## Follow-up 1 (lead, after 21b08da)
You were right: the old `settings` scene never unlocked. New scene **`settings-all`** (`build/agx-corpus/settings-all/`, recipe in `scripts/agx_corpus.py`): every pass, display and offscreen, from 153 s on, with Settings open (live capture `probe/framebuffer-at-145s.ppm`). It holds 40 dumps, including the 128x64 and 384x64 offscreen passes. Symlink it into your `corpus/` like the others, then do steps 1–4 of the brief on it. Do not boot QEMU.
