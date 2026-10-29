# Folder rendering: offline verification, 2026-10-29

All work used existing AGXF dumps, read-only via local build-directory symlinks.
No guest boot, live capture, timing, vblank, VNC or player changes.

## Findings and evidence

* **Proved addressing bug:** 640×640 folder scratch allocations have missing linear pages but all 400 logical 32×32 Morton tiles mapped. The existing sparse-layout recognizer excluded this extent. Before the fix, frames 18, 19, 21 each reported 155 missing target rows. After the fix, all three report zero. Both target staging/flush and later TSW reads now select Morton. No change to chain page persistence was necessary.
* **Proved independent-UV bug:** Home frame 1 backdrop is 512×1024, mask is A8 256×256. UV0 spans atlas coordinates while UV1 uses .25/.5/.75 nine-slice coordinates. Reading the mask with UV0's texel index produces the grey square. UV1 now supplies independent mask coordinates and dimensions. Corner (33,51) changes from RGB (80,90,123) to (24,33,66), continuous with outside wallpaper (31,51)=(24,33,64).
* **Shader interpretation, inferred:** b618's observed descriptor contains two TSWs and two samplers, with no luma constants. Treating the blurred colour texture as a vibrancy glyph retained sharp wallpaper and introduced a discontinuity at the heuristic light/dark threshold. Alpha compositing through the second sampler produces the expected smooth blue rounded panel. Exact USC instruction semantics were not independently disassembled; the audit marks this composite as approximated.
* **Corner approximation:** e344 uses signed radial UVs; ab6 carries the same radial coordinates plus saved-background UVs. Coverage clipping plus complementary additive restore preserves the Contacts icon and the panel corners. Coverage is sampled on a 4×4 grid, not claimed GPU-exact. Before this support the unmasked ab6 restore overwrote part of Contacts with a rectangular patch.

## Frame 18–22 draw trace

| Frame | Target / size | Program / stride / count | Source | Operation |
|---|---|---|---|---|
| 18 | 113fc000 / 640×640 | 1c64df22f7b8b60e / 12 / 4 | none | transparent uniform region clear, previously misclassified as canvas blur |
| 18 | same | a224932ddfd2b37d / 24 / 6 | 10334000 / 186×348 / pitch 768 | bilinear scaled preblurred wallpaper, src-over |
| 19 | 117fc000 / 640×640 | 67fb767bf2cc1d68 / 12 / 4 | none | transparent uniform region clear |
| 19 | same | 8eb52cf6fe248b1d / 24 / 6 | 109f4000 / 608×608 / pitch 2432 | rounded black alpha mask, src-over |
| 20 | 4008e0000 / 640×1136 | b61824c33264c501 / 36 / 6 | 113fc000 + mask 117fc000 / both 640×640 Morton | masked backdrop composite at (15,263)–(625,873) |
| 21 | 117fc000 / 640×640 | 8646b974188714ee / 24 / 24 | 4008e0000 / display strips | four 78×78 corner snapshots, target load + dirty-tile flush |
| 22 | 4008e0000 / 640×1136 | e344a0f081cc4997 / 24 / 24 | radial UVs | clip destination corner coverage |
| 22 | same | ab6fdb6c250c32c0 / 36 / 24 | saved corners 117fc000 | complementary radial background restore |

Frame 22 includes the ordinary icon, label and status-bar draws too. It now has 14 replayed draws, zero skips, zero guest errors, zero missing rows. The empty panel middle (320,600) is identical in frames 20 and 22; the Contacts pixel (80,330) remains opaque grey instead of being replaced by background. The source wallpaper is already a smooth guest-provided low-resolution texture; this fix does not invent a new Gaussian blur.

## Validation

* `make test PYTHON=python3`: 362 tests, 28 skipped, no assertion failures. Four environmental errors remain: three sandbox socket-bind denials and the allowed IOSurface SIGTRAP. Plain `make test` initially could not find `.venv/bin/python`; the system Python override runs the same target.
* New `tests/test_agx_folder.py`: synthetic 640×640 sparse Morton transfer/partial staged flush/readback; reject dense/incomplete allocations; mirrored radial AA/complementary restoration; optional full 22-frame captured chain checks for persistent panel, intact Contacts and Home mask.
* Both complete benchmarks replay 698 installed dumps. Against pre-change HEAD be8126a, **only folder changes (17/22 frames)**; all 676 other frames remain byte-identical. No new non-folder differences are hidden as accepted clock/switch changes.
* Against a8c7498, 373 frames differ. The 356 non-folder differences are already present in HEAD. Full frame/pixel/channel counts are in [all-diffs-vs-a8c7498.csv](../evidence/folder-render-offline-20261029/all-diffs-vs-a8c7498.csv). No dumps exist for safari-google, so that scene was not tested.

| Scene | Changed / total | Max channel delta | All changed frame numbers |
|---|---:|---:|---|
| calendar | 0/2 | 0 | none |
| clock | 0/10 | 0 | none |
| folder | 17/22 | 255 | 1–10, 13, 16, 18–22 |
| keyboard | 106/149 | 213 | 1, 6–7, 12–13, 18–20, 24–25, 29–30, 34–35, 40–41, 46–47, 52–53, 58–59, 63–64, 68–149 |
| notification-center | 92/105 | 255 | 1–2, 4–12, 14–22, 24–32, 34–42, 44–52, 54–60, 62–68, 70–76, 78–85, 87–95, 97–98, 100–101, 103–105 |
| safari | 10/33 | 64 | 1, 7–8, 14, 16, 22, 24, 27, 31, 33 |
| safari-round2 | 12/30 | 64 | 1–2, 4–5, 7, 13–14, 16, 22, 25, 28–29 |
| settings | 0/20 | 0 | none |
| settings-all | 11/40 | 25 | 1–2, 6, 8–14, 16 |
| switch | 24/74 | 179 | 1, 6, 8, 11, 16–17, 22–23, 28, 31, 36, 39, 44–45, 50–51, 56, 58, 61, 65, 67–68, 72, 74 |
| unlock | 83/161 | 255 | 9, 13, 23, 27, 37, 41, 51, 55, 65, 69, 79, 83, 91–161 |
| unlock-flyin | 0/32 | 0 | none |
| unlock-real | 18/20 | 214 | 3–20 |

## Artifacts and limitations

[Before](../evidence/folder-render-offline-20261029/before.png), [frame 22 after](../evidence/folder-render-offline-20261029/after.png), [Home after](../evidence/folder-render-offline-20261029/home-after.png), [chain metadata](../evidence/folder-render-offline-20261029/folder-chain.jsonl), [baseline benchmark](../evidence/folder-render-offline-20261029/bench-a8c7498.txt), [HEAD benchmark](../evidence/folder-render-offline-20261029/bench-head.txt).

No live VM validation or real-device image oracle was performed. Exact shader algebra and GPU antialiasing remain unverified; missing atlas contents in other captures may still cause artifacts. Sparse detection deliberately retains the existing size allowlist and complete-footprint checks. Future fully mapped Morton allocations are outside this fix's scope. Timing numbers are offline measurements, not a live performance claim.
