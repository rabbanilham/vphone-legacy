# Safari rendering, second round (2026-10-28)

Worktree: `vphone-wt-safari`, baseline `6ad9f6d`, branch `fix/safari`. Code commits: `86d7b31`, `e5b2180`.

## Findings

The supplied `research/evidence/safari-user/google-live-20261027.png` was inspected. A fresh `live_run.py --wifi` guest reproduced the failure on google.com after joining the synthetic Wi-Fi network. The connected baseline screenshot is `../evidence/safari-render-round2-20261028/google-connected-before.png`.

1. **Hatched, word-shaped blocks:** WebKit uses a format-0 U8 coverage atlas with `TSW0=0x00016d01`, not the format-3 A8 descriptor handled in the first round. Both decoders rejected it; live replay treated the stride-28 glyph geometry as solid triangles. A captured 512×512 atlas contains readable glyphs when decoded in Y-first Morton order. The new format-0 recognition is limited to the observed alpha-replicating swizzle, so arbitrary format-0 words cannot steal a valid descriptor.
2. **Colored horizontal bands / collapsed raster content:** the format-6 WebKit upload atlas was being read linearly. Morton decoding reveals the actual Google logo. This was an addressing-layout error, rather than evidence of a linear pitch off by a few texels. The uploaded images are RGBA; the software framebuffer is BGRA. Both layout and channel order now follow the observed non-CA, flipped-matrix path for padded power-of-two upload atlases; the generated 96×96 mask remains linear/BGRA. CA software-produced layout-1 render targets retain their existing representation.
3. **Black rectangles and orange rules:** the captured 2048×16 image source is a clean rule under rectangular Morton decoding and stripes under linear decoding. The WebKit upload correction and the rounded-mask span/strip correction apply. The source dimensions exceed 256; the existing rectangular Morton address calculation already handles the tail coordinate bits correctly.
4. **UIKit remains intact:** its render-target path differs from WebKit's uploaded source atlases. No general reinterpretation of every layout-1 surface was made. All 265 available pre-existing UIKit corpus frames remain byte-identical to baseline.

Two further defects became visible after decoding the glyph texture: live C replay capped all draws at 64 vertices, truncating a captured 150-vertex glyph batch; and later glyph draws reused the projection matrix without repeating it in their vertex PDS data. The corrected paths stream up to 16,380 textured vertices and retain the matrix for the observed glyph/upload pipelines. RGBA glyph tints and white coverage are handled explicitly.

## Changes

- `backend/s5l8960x.c`: format-0 coverage, WebKit uploaded Morton/RGBA sources, glyph tint and matrix retention, larger streamed textured batches.
- `backend/agx_replay_offline.py`: matching Python behavior and per-draw diagnostics.
- `scripts/agx_corpus.py`: `--texture-audit FILE` prints format, layout, dimensions, pitch word, stride, layout flags, sampler words, vertex stride/count, and RGBA-upload selection.
- `tests/test_agx_safari_render.py`: linear/Morton U8 decoding, rejection of unrelated format-0 descriptors, rectangular addressing, a 150-vertex white glyph batch retaining its Y flip, actual rasterization of the rounded-mask span variant, and preservation of generated linear masks.
- `backend/agx_replay_host.c` requires no separate patch: its harness compiles the exact C replay core extracted from `s5l8960x.c`.

The first successful rebuild repaired the visible geometry/layout problems. Live inspection then exposed the RGBA/BGRA channel mismatch (orange first G), requiring a second successful rebuild. A further live residual exposed a skipped rounded-mask span shader (`0x84102d511f2a41bb`) and reused strip matrix (`0x4eb8837dc2079f95`); those were corrected in a third successful rebuild. A fourth build preserved the generated 96×96 mask as linear when consumed by the copy shader, while retaining Morton/RGBA decoding for the captured power-of-two upload atlases. Thus this run exceeded the brief's single-rebuild target to correct a measured residual defect.

## Evidence and measurements

Evidence directory: `research/evidence/safari-render-round2-20261028/`.

`webkit-footer.agxf.gz` preserves the complete 6,790,256-byte context-2 submission, compressed to 75,185 bytes. It contains six draws, including four coverage-atlas draws and the 150-vertex batch. `capture-provenance.json` records hashes and the bounded capture settings. No source profile images were modified. Larger intermediate dumps remain local and are excluded by the evidence directory's `.gitignore`.

Reproduce the offline comparison:

```sh
gunzip -c research/evidence/safari-render-round2-20261028/webkit-footer.agxf.gz > /tmp/webkit-footer.bin
.venv/bin/python scripts/agx_corpus.py --texture-audit /tmp/webkit-footer.bin
.venv/bin/python backend/agx_replay_offline.py /tmp/webkit-footer.bin /tmp/footer.png
```

The texture audit records format 0, layout `0x1`, 512×512, pitch word `0xf0000001`, logical stride 512, Morton=true, strip=false, nc_sparse=false, sampler `[4095,9216,0,0]`, vertex stride 28, and counts 60/150/54/60. Format-3 Morton decoding was separately inspected and already produced a smooth shape; changing it to linear would have introduced bands.

RGB MAE against the corrected **Python replay of the same submission**:

| Crop | Baseline C | Corrected C |
|---|---:|---:|
| Entire footer `(0,0)-(640,122)` | 12.8887 | 0.0000 |
| Upper text `(180,0)-(460,40)` | 21.1124 | 0.0000 |
| Lower text `(90,55)-(550,105)` | 17.6176 | 0.0000 |

These numbers measure C/Python replay agreement, not physical-device fidelity. A synchronized post-submission live framebuffer for this exact offscreen pass was not obtained, so no pixel-aligned offline-versus-live MAE is claimed. There is no synchronized clean physical-iPhone framebuffer for a ground-truth live-page MAE. The live QMP captures establish visible before/after behavior; they should not be mistaken for device ground truth.

## Validation

- `.venv/bin/python -m unittest discover -s tests`: 342 tests, OK, 25 skipped. Tests requiring local sockets were rerun with socket access after the sandbox prevented their first run.
- `backend/agx_contract_test.py`: all synthetic AGX contracts passed on the final binary.
- 265 available UIKit corpus frames, chained C baseline versus final: zero changed frames, zero guest errors, zero C skips. `transition-live/dumps` was unavailable; the other six recorded scenes were compared. See `corpus-comparison.json`.
- Final live google.com capture: `google-after.png`. Text and logo are readable, the logo channels are correct, the search button is blue, and the earlier checkerboards/black-orange stripes are absent. Small stylistic differences remain (for example, the Login button shading); this is not a claim of pixel-perfect WebKit.
- Live Home and Settings were captured from the final binary and inspected for new solid black bars.
- Final native backend build succeeded. Existing compiler warnings are retained in `build-final.log`.

## Manual verification

1. Ensure no other QEMU guest is running, then start `.venv/bin/python backend/live_run.py --wifi`.
2. Unlock, join an exposed network in Settings → Wi-Fi, and open https://google.com in Safari. Allow the request to finish.
3. Check that menu labels and language links are readable, the Google logo has a blue G/red o/yellow o, and the search glyph is recognizable. The previous hatched blocks, collapsed bands, and black/orange striped rectangles should be absent.
4. Open Home and Settings. Check icon labels, navigation bars, and switches for newly introduced black bars.
5. Close the player when finished so another shared-Mac measurement can run.

The decoder remains a measured high-level renderer: the upload-path selection is intentionally limited to the observed non-CA flipped-matrix pipeline and the coverage shader fingerprint. This is not general support for every PowerVR format, shader, or application.
