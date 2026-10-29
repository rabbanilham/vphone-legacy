# Folder corner investigation — 2026-10-29

Status: **partial implementation; acceptance not achieved**. The new procedural
pair renders instead of skipping, but the supplied replay still has black lower
corner pixels and the early folder group remains square. Do not describe this
as live-equivalent or ready for release.

## Findings and implementation

`backend/s5l8960x.c` recognizes the word-6 TSW only for USC
`e344a0f081cc4997`, first descriptor block, sampler `fff 2400 0 0`.
It decodes 160x160 BGRA at 0x10758000 with pitch 640 in frames 13/16;
frame 22 instead references the later 640x640 backdrop allocation.
General descriptor offsets remain unchanged.

The following `ab6fdb6c250c32c0` stride-36 draw covers the same four corners.
Its UV0 indexes a four-slot backdrop atlas, while UV1 duplicates the preceding
draw's mirrored [-1,0] coordinates. An inferred circle coverage implements
D*m followed by additive backdrop*(1-m), sharing the same derivative-based
one-pixel AA transition. This is an approximation, not a USC disassembly or
proof of the exact shader curve. Python classification explicitly says
"approximated" and its renderer mirrors the two new legs.

Sampler `2400` is clamp-to-edge under the existing decoder, not evidence of
repeat/mirror sampling. The signed UVs describe procedural geometry; treating
them as normalized texture UVs clamps all corners to texel zero. Mirroring is
encoded by the vertex UV orientation.

## Evidence limits / unresolved problems

The read-only source was
`/Users/bagas/Documents/Projects/vphone/vphone-legacy/build/agx-corpus`.
Local `build/folder-fix/corpus` contains symlinks to its scene directories;
binaries, JSON and RGBA outputs live only in this worktree. No VM was booted.

Contrary to an assumption of an intact external texture, the texture address
is present in frames 13/16 but only partly recorded. Frames 12/15 are its
producer passes. They pack screen backdrop corners into a 160x160 atlas.
The harness reports **41 missing target rows** on each producer. Frames 18/19/21
have **155 missing rows** for the 640x640 targets. Replayed producer writes
cannot recover absent target pages with the existing host memory contract.
The resulting restore has black strips at the lower corners. An experimental
host materialization change did not resolve the result and was reverted;
`backend/agx_replay_host.c` is unchanged.

For the earlier group icon, frames 4/7/10 already execute the old Format-3
DEST_IN / CORNER_RESTORE pair. Their backdrop atlas in the chain contains the
folder-colored rectangle, so adding its complement restores a square again.
This work does not establish a safe general correction for that captured
initial/backdrop state. It deliberately does not replace source pixels with a
wallpaper heuristic.

The supplied live-folder-113s.png itself shows the square Extras group, and
live-folder-114s.png has no visible panel backdrop. They document the buggy
renderer state, not a clean rounded reference. The panel's top corners in
`build/folder-fix/after-frame-16.png` become curved, but lower black artifacts
and the unchanged group mean visual acceptance is **not passed**.

## Validation

- New `tests/test_agx_folder_corners.py`: executes production C descriptor
  decode, rejects offset-6 for unrelated programs and malformed sampler,
  checks mirrored AA and complementary blending; optional actual corpus chain
  asserts frames 13/16/22 render 17/17/14 draws with no skips or guest errors.
- AGX suite: **153 tests, OK, 23 skipped** (includes both new tests).
- `make test PYTHON=python3`: **359 tests, 32 skipped, 4 errors**. Three are
  sandbox-denied AF_UNIX binds. The fourth is
  `test_surface_ring_multibuffer_fidelity_and_isolation`, where the Swift
  IOSurface test crashes with SIGTRAP / nil unwrap. Its source was unchanged.
  This is not counted as a passing full suite or silently waived.
- `git diff --check`: passes.

## Pixel benchmark

Command: `python3 scripts/agx_bench.py --base a8c7498 --corpus
build/folder-fix/corpus --json build/folder-fix/bench-final.json --allow-diff
--jobs 4`. Allow-diff is used to collect all differences; it is not an
acceptance waiver. 624 frames total, 308 differing.

| Scene | Frames | Diff vs a8c7498 | Diff vs initial HEAD 18d6ef7 |
|---|---:|---:|---:|
| calendar | 2 | 0 | 0 |
| clock | 10 | 0 | 0 |
| folder | 22 | 9 | 9 |
| keyboard | 149 | 106 | 0 |
| notification-center | 105 | 92 | 0 |
| safari | 33 | 0 | 0 |
| safari-google | 0 | 0 | 0 |
| safari-round2 | 30 | 0 | 0 |
| settings | 20 | 0 | 0 |
| settings-all | 40 | 0 | 0 |
| unlock | 161 | 83 | 0 |
| unlock-flyin | 32 | 0 | 0 |
| unlock-real | 20 | 18 | 0 |

Every differing frame, pixel count and maximum channel delta is listed in
[folder-corners-20261029-diffs.tsv](folder-corners-20261029-diffs.tsv).
The initial-HEAD comparison proves no newly introduced non-folder pixel diff.
However, the requested stricter a8c7498 condition is not satisfied by the
starting checkout: keyboard/notification-center/unlock-real already differ.
No existing changes were reverted to conceal those differences.

Folder differences vs both baselines: frames 5,6,7,8,9,10,13,16,22. The first six
precede the new procedural shaders and remain unexplained (possible existing
undefined/uninitialized state exposed by compilation); they are explicitly not
claimed as intended corner fixes. They need investigation before release.

## Risks and unverified work

Exact shader transfer function/AA, sampler semantics beyond the current
decoder, a clean full four-corner restore, group-icon rounding, and equivalence
to a correct native/live reference are unverified. No live/performance capture,
guest timing, vblank, VNC or player changes were made. The patch remains a
reviewable partial investigation, not a completed fix.
