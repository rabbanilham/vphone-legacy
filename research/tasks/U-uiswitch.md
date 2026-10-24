# Task U: UISwitch track (Settings → Airplane Mode)

Worktree `${WORKTREES_ROOT}/vphone-wt-uiswitch`, branch `wt/uiswitch`. Renderer scope: the AGX replay core of `backend/s5l8960x.c` (between `AGX_REPLAY_CORE_BEGIN`/`END`), `backend/agx_replay_offline.py`, `tests/test_agx_*`. Work offline only: no QEMU boots (agy D owns QEMU on this Mac). The lead does one live boot at merge time.

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

## Follow-up 2 (lead, after the U and B merge da8d57e)
`wt/uiswitch` is merged. Your branch is now at `agx-meta-hle` da8d57e, which includes agy B's 1-D blur (`wt/blur`): read `research/reports/B-gaussian-blur.md` first. Two targets, offline only, on `settings-all`:
1. **Knob shadow.** Live boot `backend/evidence/merge-ub-live/framebuffer-at-152s.ppm` (crop x 380..640, y 140..300): the knob's shadow is a **grey square** behind the knob instead of a soft round shadow. The shadow chain per frame (from your earlier trace): 128x64 pass at 0x1059c000 (shape, 2 draws), 0x105b4000 (horizontal blur), 0x1059c000 again (vertical blur), then a display composite (3 draws). Find which step produces a square: the shape draw (a circle drawn as a quad, i.e. a missing distance-field or texture alpha), the blur, or the composite (alpha or blend). Fix it so the chained replay shows a soft round shadow offset below the knob.
2. **Navigation bar saturation/tint.** Agy B identified prog 0x2353a3fe1ca2b71b (`settings-all` frame-14 draw 0), which reads a 3x3 colour matrix at 0x101a493c to boost saturation and tint the blurred navigation bar backdrop. Implement it in C and Python (decode the matrix and any offset or bias exactly from the draw's constants), and check that the navigation bar keeps the iOS 7 look (near-white translucent, slightly saturated content behind it).
Same rules: chained corpus before/after crops in `research/evidence/U-uiswitch/`, no unexplained pixel changes elsewhere, tests pass, backend compiles. Commit to `wt/uiswitch`, amend the report, and always commit before you stop.
