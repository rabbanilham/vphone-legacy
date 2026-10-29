# Closed folder icon: offline repair, 2026-10-29

Root cause proved by captured descriptors, UVs, decoded alpha image and isolated
replay change: commit 25873a6 correctly switched b618's second sampler to UV1,
but omitted the A8 256x256 atlas V transform already used by the single-texture
path. This atlas contains a half ellipse (opaque below its curved edge), not a
complete rounded rectangle. CA's nine-slice UV1 coordinates are .25/.5/.75;
using them directly samples empty upper rows and the curved lower region,
producing the bright dome. Using `2 * min(v, 1-v)` mirrors both halves around
UV1=.5, producing the rounded square. No source colour, blend algebra, sparse
layout detection, timing or player changes are needed.

## Draw evidence

[Every draw in frames 1–5](../evidence/folder-icon-20261029/draws-1-5.jsonl)
records program, stride, count, selected texture/mask descriptors and per-vertex
positions/UV0/UV1 (draw indices start at zero).

* Frame 1 draw 9 and frame 2 draw 8: b61824c33264c501, stride 36, 54 vertices
  (nine quads), rect (32,50)–(152,170), splits at x=59/125, y=77/143.
  UV0 x=.21875–.3359375, y=.2470703125–.3056640625 samples colour
  0x111f8020, 512x1024, pitch 2048. UV1=.25/.5/.75 samples mask
  0x10d54000, A8 256x256, Morton, pitch 256.
* Frame 3 draw 0: 8646b974188714ee, stride 24, 24 vertices saves four display
  corners. Its 462 changed pixels are inherited from the corrected Home draw.
* Frame 4 draws 1/2 use the same A8 mask with 4ee01acc47551344 (stride 24,
  24 vertices) and 03a2edd2a4d84437 (stride 36, 24 vertices), clipping and
  restoring corners around the inherited backdrop. Frame 4 has no b618 draw.
* Frame 5 copies texture state and performs ab6fdb6c250c32c0 restoration
  (stride 36, 54 vertices); final output is byte-identical to HEAD.

The fix copies the existing atlas transform into b618's second-sampler path in
both C and Python. It applies only to A8 256x256 masks there. The 640x640 open
folder mask is unaffected. Rendered shape follows guest nine-slice geometry;
no hardcoded icon location or synthetic rounded rectangle was introduced.

## Verification

* [Home result](../evidence/folder-icon-20261029/home.png) and
  [open folder](../evidence/folder-icon-20261029/open.png).
* `python3 -m unittest discover -s tests -p test_agx_folder.py`: 3 tests pass,
  including real 22-frame chained C replay and existing sparse/corner tests.
  New captured assertions compare blank interior rows and both sides, require
  a grey interior, and retain wallpaper at the outer corner for frames 1/2/4.
* `make test PYTHON=python3`: 363 tests, 28 skipped; no assertion failures.
  Only four allowed environmental errors: three socket bind denials and
  IOSurface SIGTRAP.
* `python3 scripts/agx_bench.py --base HEAD --json build/folder-icon-bench.json`:
  [full output](../evidence/folder-icon-20261029/bench-head.txt), 698 frames.
  Only folder frames 1/2/3/4 change: 6402/8704/462/8704 pixels, max channel
  deltas 102/115/94/177. Frames 5–22, including all 16–22 open-folder frames,
  are byte-identical. All 676 non-folder frames are identical, including all
  74 UISwitch frames. Exit 1 is expected for these four intentional diffs.

## Limits

No VM boot, live capture, guest timing, vblank, VNC or player work. Corpus
was read only. Exact USC semantics and hardware AA remain inferred rather
than disassembled; this repair matches the existing atlas interpretation and
captured guest geometry. Live appearance and uncaptured atlas variants remain
unverified. The installed corpus has no safari-google frames. Tests requiring
the private corpus skip when it is absent. Offline timings are not live perf
claims.
