# GH — Notification Center backdrop blur, round 2

Branch: `gfx/nc-blur`. Baseline: `main` (`a0e0c1a`). No live iOS boot, commit, push, merge or rebase was attempted in round 2. The orchestrator will commit the working-tree changes using the prepared patch/message.

## Result

Round 1's broad layout change was a regression. It is removed, along with the offline legacy-cache environment switch and all changes to unrelated blur/clear behavior. The default, unmodified benchmark command now produces **zero differing frames outside Notification Center**, including Clock status text, the Settings unlock label, unlock 154/161 previously differing frames, and Settings-all.

The NC grey rectangles in frames **60, 79 and 83 are fixed**, through a separate, evidenced texture-coordinate correction. Complete NC composites have smooth wide blur; the colour-matrix alpha rule is unchanged. This is offline evidence from emulator dumps, not a device ground-truth comparison. Live texel lifetime and visual acceptance still need the orchestrator's verification.

- Graphics suite: **133 tests, OK**, 17 skipped because old optional evidence is absent.
- `make backend`: **PASS**. The built binary's recorded source hash matches this worktree.
- Corpus: **390 dumps**, 0 C harness errors, 0 explicit C skips, 0 Python dump errors. The existing Python geometry audit still lists 17 non-blur geometry skips (11 in NC); it is not an instruction-level GPU oracle.
- Timing: **320.2 → 354.3 ms overall (+10.7%)** and **119.9 → 137.0 ms for NC (+14.2%)**, fastest per-frame result from three repetitions. All other scenes are also within 15% in this run.

## 1. Layout scope and non-NC regressions

The old selector, `(TSW[0] & 0x8007) == 0x8001`, remains authoritative for existing textures. The only additional Morton case must meet **all** of these conditions:

1. GPU layout-1 descriptor with the uploaded-twiddle bit absent; an aligned base; no display-strip mapping.
2. Extent exactly 128×96, 640×224, 640×288 or 640×320.
3. At least one linear row-start page is unmapped.
4. Every logical 32×32 Morton tile page is mapped.

The 640×224 extent is the same proved sparse case in NC's horizontal intermediate (frames 8/18); it is included to keep that part of the chain valid. The installed non-NC corpus contains none of these matched allocations. A dense allocation of the same dimensions is rejected. 640×96 and 640×416 unlock/glyph caches are rejected regardless of their holes. The tests cover positive, dense, incomplete and excluded-extent cases in C/Python. No texture address or scene-name whitelist is used.

| Extent | Logical Morton pages | Missing logical Morton pages | Missing linear page starts (unique pages) | NC passes |
|---|---:|---:|---:|---:|
| 128×96 | 12 | 0 | 2 | 1 |
| 640×224 | 140 | 0 | 16 | 2 |
| 640×288 | 180 | 0 | 56 | 23 |
| 640×320 | 200 | 0 | 52 | 27 |

For frame 6 at `0x10484000`, padding pages `+0x5000,+0x7000,+0xd000,+0xf000` are absent. Main's linear row writes hit holes at reduced y=40..47 and 56..63. The sparse Morton round-trip test writes and samples every logical pixel without accessing those holes. The addressing predicate is applied consistently to the target and subsequent texture reads. Its small topology-result cache lives only for one replay call; the translation cache and `agx_ctx_read/write` paths are unchanged.

The clear fix and strict blur-uniform rule are also restricted to a matched NC source/target context. Generic replay paths and their existing blur heuristics retain main's behavior. This is why the default benchmark, without a special environment flag, now preserves all unrelated frames.

Review crops (main | round 2, asserted byte-identical across each replayed chain) are in `build/gfx/nc-blur/round2-review/`:

- `clock-frame-1-comparison.png`: bbox 13,1–629,42; readable “Searching…” and time.
- `settings-frame-2-comparison.png`: bbox 130,946–478,1004; readable unlock label.
- `unlock-frame-40-comparison.png` and `settings-all-frame-10-comparison.png`.

## 2. Grey rectangles: exact cause and correction

These rectangles were **opaque BGRA `(140,140,140,255)`**, not transparent holes or a clear-colour/alpha workaround. The offending draw copies the linear **4×2048 strip at `0x104e0020`, pitch 32**, using fragment ID `0x8eb52cf6fe248b1d`.

NC frame 53 draw 2 has geometry `(94,7)–(624,309)` and UVs `U=0..132.5`, `V=102/2048..404/2048`. U repeats across the strip while V is normalized. The preceding copy draw uploads **`texmat0=(1,1,0,0)` at vertex uniform words 24..27**. Draw 2 reuses that upload with the same vertex-code key and no new uniform pointer.

Main's `max(U,V)>1` heuristic classifies both axes as texel units. It consequently samples V≈0.05..0.20 from **row 0**, which is opaque `#8c8c8c`, across the entire quad. That cache is composited by the following display pass and remains outside later damage scissors. In frame 60, for example, the saturation draw only covers **x=130..376, y=148..400**; frame 83 covers **x=130..378, y=150..659**. Those small redraws cannot repair the earlier grey regions.

The NC normalized-copy path now reads the explicit texmat and retains it only when the subsequent PDS packet has the same vertex-code key. Identity texmat means both coordinates remain normalized, even when repeated U exceeds 1. At panel pixel y=100, the source V is **195.5**, so the transparent row 195 is sampled rather than opaque row 0. All four columns of this specific strip are identical in every row, verified by a test; horizontal repeat versus clamp does not alter its colours. There is no general sampler-wrap change.

This change is scoped to stride-24 copy draws in the proved NC context. It does not reinterpret status/glyph textures elsewhere. C and Python have tests for explicit upload, inheritance, rejection across a changed vertex-code key, and the actual frame-53 texture/UV data. The uploaded strip and its first 420 rows are preserved as `round2-review/nc-strip-4x2048.png` and `nc-strip-rows-0-419.png`.

Grey-pixel counts in panel bbox `(0,0)–(640,660)`:

| Frame | Main `#8c8c8c` pixels | Round 2 | Pixel (400,100), main → round 2 |
|---|---:|---:|---|
| 60 | 87,210 | 1,219 | `(140, 140, 140, 255)` → `(65, 58, 49, 255)` |
| 79 | 87,217 | 964 | `(140, 140, 140, 255)` → `(65, 58, 49, 255)` |
| 83 | 87,213 | 960 | `(140, 140, 140, 255)` → `(65, 58, 49, 255)` |

The remaining `#8c8c8c` pixels are thin rules/glyph content from the strip, not flat panels. Side-by-side crops: `round2-review/nc-frame-{60,79,83}-comparison.png`. **Frame 79 is the partial Home producer pass**, before its backdrop reduction/blur; its sharp Home strip is expected at that stage. Frame 83 is the subsequent blurred composite.

## 3. Filter chain, parameters and capture coverage

A complete late chain is Home draw **88**, reduction **89** to `0x11400000` (640×288 allocation; roughly 80×155 active), copy/extrusion **90**, horizontal blur **91**, vertical blur **92**, then saturation/tint upscale **93**. The blur intermediates alternate `0x11800000` (640×320) and `0x11600000` (640×288). Frame 103 adds NC text/content. The broad radius comes from the known seven-pair shader at 1/8 scale; these dumps do not show a new wide-convolution USC variant.

| Shader/rule | Replay |
|---|---|
| `0x00e3b3d44520e23f` | Eight samples at ±offset[0,3,5,6], weight 1/8; two PDS uniform chunks. All ten NC captures contain zero offsets. |
| `0xf82f9a96aff1e034` | Seven symmetric bilinear pairs; direct vertex offsets/direction and fragment-weight pointer, validated without an adjacent-allocation search in NC. |
| Short 41-byte export signature + PDS `0x90000004` | Own four-float RGBA region clear in matched NC contexts; not a convolution. PDS high-nibble flags vary. |
| `0x8646b974188714ee` | Copy and edge extrusion, using the established sampler. |
| `0x2353a3fe1ca2b71b` | Existing RGB saturation/tint matrix, bias about 0.0803. Alpha handling unchanged. |

Seven-pair offsets are `[0.662207,2.475021,4.455121,6.435364,8.415810,10.396517,12.377541]`; weights per signed sample are `[0.118916,0.141265,0.107065,0.069256,0.038236,0.018016,0.007245]`, summing to about 0.5. H/V direction is explicit in the vertex offset vectors. At 8× upscale, the last offset spans about 99 display pixels.

The reduction shader evidence is the eight-sample USC body and matching QuartzCore GLSL string in the installed arm64 dyld cache at offset `0x439a9c1`. The captured zero offsets are preserved; they are not replaced by invented taps. Nonzero synthetic offsets exercise the C/Python eight-sample rule. Fresh live uploads still need validation.

TA capture retains the 192-byte vertex blocks, fragment weights/downsample constants, colour-matrix metadata and fingerprint code pages it reads. A C test zeroes live uniform/weight pages after capture and still recovers the original H/V kernel. **GPU-produced texels remain live** because their producers may execute after TA submission. This work does not prove all uploaded/input texel lifetimes or remove the existing 1,024-page cap; that is an explicit live verification item, not a claim of complete snapshot coverage.

The full current NC audit is uncommitted under `build/gfx/nc-blur/round2-inventory.{json,md}`: **105 passes and 915 draw rows**, with target address/extent/layout, draw count/stride, program ID, source/layout, filter, blend/PPP words, colour matrix, coordinate state and outcome. NC has 47 scoped region clears, ten H and ten V seven-pair draws, ten reductions, and ten matrix composites. Five unmatched/dense initial canvas passes retain the old approximation. Eleven existing NC geometry-audit skips are listed there; none is a blur draw. Other unknown fragment IDs still use the generic high-level textured/solid/vibrancy rules. “Replayed exactly” in the old auditor is a semantic classification, not GPU instruction-level verification.

## 4. Performance

The slow round-1 full-surface policy is removed. Current optimizations stay inside the replay core:

- NC destinations use a linear tile cache: load a touched 4 KiB tile once and flush dirty tiles at pass end. Dense/display targets use the original I/O path.
- Decode Morton sources in tile order once per draw; reuse the decoded source for an immediately following immutable-source copy/extrusion draw.
- Prefilter only the source UV rectangle plus a `ceil(max_offset)+2` halo, rather than every pixel of 640-wide scratch padding. Tests compare H/V interior pixels and fractional bilinear samples against the existing full-source convolution, bit for bit.
- Eight zero-offset reduction samples collapse to the ordinary bilinear path, avoiding a full-resolution source preload.

The existing 8-bit intermediate/bilinear arithmetic model is retained. Python uses the same full-source reference convolution; the padded C ROI excludes only values that cannot affect samples.

Command, **without environment overrides**:

```sh
python3 scripts/agx_bench.py --base main --allow-diff --repeat 3 \
  --json build/gfx/nc-blur/round2-bench-final.json
```

Fastest timing per frame across three repetitions (pixel comparison is performed for every output):

| Scene | Frames | Main ms | Round 2 ms | Change | Differing frames |
|---|---:|---:|---:|---:|---:|
| calendar | 2 | 29.1 | 32.8 | +12.7% | 0 |
| clock | 10 | 32.9 | 36.6 | +11.3% | 0 |
| notification-center | 105 | 119.9 | 137.0 | +14.2% | 89 |
| settings | 20 | 1.9 | 2.1 | +12.7% | 0 |
| settings-all | 40 | 25.4 | 27.8 | +9.5% | 0 |
| unlock | 161 | 49.6 | 53.0 | +7.0% | 0 |
| unlock-flyin | 32 | 10.4 | 11.0 | +5.9% | 0 |
| unlock-real | 20 | 51.1 | 54.0 | +5.8% | 0 |
| **Total** | **390** | **320.2** | **354.3** | **+10.6%** | **89 (all NC)** |

The stock host harness still reads offscreen outputs back as linear rows; some NC offscreen differences are consequently layout differences rather than decoded pictures. Final display comparisons are valid. The build-only evidence harness uses `agx_target_read` for correct offscreen readback; no tracked host harness or corpus-script change was made. Timings measure the renderer, not image export. They are one benchmark run and do not replace live latency acceptance.

## 5. Verification, artifacts and handoff

- `.venv/bin/python -m unittest discover -s tests -p 'test_agx*.py'`: **133 tests, OK, 17 skipped**; see `round2-graphics-tests.log`.
- `make backend`: **PASS**; see `round2-backend.log`. Source/binary manifest match checked.
- `python3 scripts/agx_corpus.py replay --jobs 4`: 390 dumps, 0 C/Python errors; see `round2-corpus.log` and `build/agx-corpus/report/summary.json` (offscreen viewer caveat above).
- New tests cover positive/negative layout scope, sparse row/tile transfers and five-pixel partial clears, direct kernel recovery, snapshot mutation, eight-sample reduction, inherited normalized UVs, and ROI/full-prefilter equivalence.
- Round-1 full `make test` encountered four sandbox Unix-socket failures. Round 2 ran the required graphics suite and backend build; a socket-enabled full suite remains an orchestrator check.

Current visual evidence:

- `build/gfx/nc-blur/after/contact.png`, `round2-panels.png` and `after/notification-center-frame-103.png`.
- `round2-review/`: restored text and grey-panel comparison crops, plus the uploaded strip.
- `before/`: unchanged main reference replays; these are not device ground truth.
- `round2-grey-proof.json`, `round2-bench-final.{json,log}`, `round2-inventory.{json,md}`.
- `export-evidence.py` regenerates correct target readback/images; `round2-artifacts.py` regenerates the full audit and review crops. Both are build artifacts. Older round-1 report/source copies are under `round1/` for provenance, not current results.

Live verification should pull NC over Home with this binary, repeat the capture windows while moving and settled, check y=250..410 and 660..820, inspect actual nonzero reduction uploads and snapshot-cap cost, and verify texture/input lifetimes through the render thread. Keep GPU intermediates live until their producers have executed. Confirm non-NC text/shadows and interactive latency, and run full tests where Unix sockets are permitted.

No commit was attempted in round 2, as requested. Working-tree changes are ready for the orchestrator; `build/gfx/nc-blur/GH-nc-blur.patch` and `commit-message.txt` are updated, ending with the requested Codex co-author trailer.
