# Task S: "slide to unlock" glint and the black bar artefact

Worktree `/Users/bagas/Documents/Projects/vphone/vphone-wt-slider`, branch `wt/slider`. Renderer scope: per-program blend modes and the span primitive in the AGX replay core of `backend/s5l8960x.c` (between `AGX_REPLAY_CORE_BEGIN`/`END`), `backend/agx_replay_offline.py`, `tests/test_agx_*`. Task K (another agy, `wt/clock-hands`) changes vertex colour byte order and adds a triangle rasteriser for untextured draws: do not touch those parts; keep your diff local to blend selection and the span draws so both merge mechanically.

## Work offline. No QEMU boots.
Set up your own corpus view exactly as task K describes (symlink `build/agx-corpus/*/dumps` and `probe` into `corpus/`, run `.venv/bin/python scripts/agx_corpus.py --corpus corpus`, which writes `corpus/report/index.html` in about 2 s). Never write into `build/`.

## Symptoms (iOS 7 lock screen)
- The "slide to unlock" label renders as flat dark grey (#636363); on a real iPhone it is light, translucent text with a bright glint that sweeps left to right every couple of seconds. The glint never shows.
- A thin black horizontal line above the label (about x 10..370, y 936..946 in framebuffer pixels) and black dashes down its left edge (x 171..181). See `build/agx-corpus/settings/probe/framebuffer-at-90s.ppm`.

## What the lead found (corpus `unlock`, frames 1..90 = every pass from 95.3 s, before and during the swipe)
The label is built every frame by a chain of offscreen passes (repeating pattern frames 2,3,4,5 then 16,17,18,19 …):
1. **640x96, clear** (frame-2, target 0x10618000): draw 0 background; draw 1 stride 20 solid 000000ff over the label; draws 2..8 prog **0x1a20cc29e8fcc15b**, stride 16, n=1024: **horizontal 1-pixel spans**, vertex pairs `(x0+.5,y+.5)-(x1+.5,y+.5)` in screen coords (same matrix as draw 1), extra 8 bytes `ffffffff ffffffXX` = white with coverage alpha XX. These are the antialiased glyph rows of "slide to unlock". The replay skips them ("stride 16 outside the wallpaper program").
2. **640x96** frame-3 (→0x10698000) and frame-4 (→0x10a00000): prog 0xcda5e479d40815f, one textured quad each with UV offsets -9 and -5 (likely blur/shadow passes; replay copies 1:1).
3. **640x416, clear** frame-5 (target 0x10254000): draw 1 = texture 0x10a00000 with prog 0x8eb52cf6fe248b1d over 171..653 x 936..1014; draw 3 and 5 = 768x48 gradient textures (tints e6e6e6e6 / bfbfbfbf) moving across x -137..1154 (the glint); draw 4 = 32x32 twiddled texture tint 59595959 prog 0xf9823a724e1b6286; draw 6 = solid 636363ff prog 0xf137b90c25ab5471 over 181..640 x 946..1004; draw 7 = DEST_IN mask (600x58 text mask); draw 8 = n=30 stride 20 colour 0 (border rectangles) prog 0xe816d6204ce86a4c.
4. The display pass composites the 640x416 layer (prog 0x6c01d033fd937c7c tint white, or 0x5b133ef7e0c3428c).

Hypothesis (verify, don't assume): the glinty label uses CoreAnimation compositing filters (plusL / plusD, i.e. plus-lighter and plus-darker) and the replay draws them as src-over. With plus-lighter, black adds nothing (the black bar disappears) and dark grey text becomes light; src-over of the opaque grey fill (draw 6) currently hides the gradient. Identify which programs are plus-lighter / plus-darker / other by testing against expected appearance, implement those blends (C and Python), and implement the span primitive for 0x1a20cc29e8fcc15b.

Passes depend on each other (frame-5 samples what frames 2..4 produced). `scripts/agx_corpus.py` now replays each scene **chained** by default: `backend/agx_replay_host.c --chain` carries the pages each pass writes into the later dumps, so the lock-screen frame can be checked offline from the earliest offscreen pass to the display pass (`--single` gives the old per-dump replay).

## Measurable target
- Offline, chained replay of one label cycle ending in a display pass: no black pixels in the bar/dash regions; label lighter than the wallpaper; the glint visible at successive positions in at least three consecutive cycles (save a strip of crops as PNG under `research/evidence/S-slider-glint/`).
- C skip count for "stride 16 outside the wallpaper program" goes to 0 for 0x1a20cc29e8fcc15b.
- No regressions elsewhere in the report (list frames whose pixels change and explain each); unit tests pass (`.venv/bin/python -m unittest discover -s tests`), add tests for the span primitive and the new blends; `backend/.venv/bin/python scripts/qemu/build_backend.py` compiles.

The lead (Claude, MBP) does one live boot at merge time. Commit to `wt/slider` and write `research/reports/S-slider-glint.md`.
