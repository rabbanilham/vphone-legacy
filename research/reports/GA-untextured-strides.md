# Task GA: Untextured Draws with Unknown Vertex Stride

**Task ID**: `GA-untextured-strides`  
**Worktree**: `/Users/bagas/Documents/Projects/vphone/vphone-wt-gfx-a`  
**Branch**: `wt/gfx-a`  
**Author / Worker**: Antigravity (`agy`)  
**Lead / Orchestrator**: Claude  
**Scope**: `backend/s5l8960x.c` strictly between `AGX_REPLAY_CORE_BEGIN` and `AGX_REPLAY_CORE_END`, `backend/agx_replay_offline.py`, `tests/test_agx_untextured_stride.py`, `research/reports/GA-untextured-strides.md`. Strictly offline (no QEMU boots).

---

## 1. Executive Summary

At baseline, the offline AGX replay corpus reported **116 dropped untextured draws** across 265 frames:
- C renderer: `c_skips = {"untextured with unsupported stride": 116}`
- Python audit: `"skipped: untextured with stride not in (20, 28)": 116`

Disassembly and layout decoding revealed that the 116 draws decompose into **two distinct subgroups**:
1. **Group A (104 draws)**: Draw index 0 across 64 `unlock`, 21 `unlock-flyin`, and 19 `settings-all` frames. These are **stride-12 axis-aligned quad backdrop Gaussian blur passes** targeting offscreen render targets. All 22 program hashes share the exact 41-byte bytecode preamble of CoreAnimation narrow Gaussian blur shaders (`CA::OGL::BlurState::narrow_blur`). The C replay core and Python mirror already possessed the separable 6-tap backdrop blur path (`agx_blur_texture`), but failed to dispatch it because the 22 corpus-specific 64-byte hashes were not registered in `agx_is_blur_program` / `ALL_BLUR_PROGRAMS`.
2. **Group B (12 draws)**: Draw index 2 across 7 `unlock` frames and 5 `unlock-flyin` frames. These are **stride-24 multi-quad draws** (`count = 12`) driven by incremental PPP packets (`hdr = 0xc3f`) that omit the fragment shader word (`prog = 0x0`, `tex = None`) and contain out-of-bounds negative UV coordinates ($v \in [-871, -870]$). Per task specification, because shader state cannot be proven without pipeline state inheritance, this subgroup is precisely classified as `"stride 24 untextured with program 0x0"` in both C and Python.

### Measurable Results
- **Target `untextured with unsupported stride`**: **0** in C and **0** in Python.
- **Corpus Skips**: Exactly 12 remaining untextured skips across the entire corpus, fully explained and identical in C and Python:
  - C: `12  stride 24 untextured with program 0x0`
  - Python: `12  skipped: stride 24 untextured with program 0x0`
- **Replayed Draws**: 104 draws now replayed as separable 6-tap Gaussian backdrop blur.
- **Full Corpus Replay**: 265 dumps replayed in 3.8 s with 0 harness errors.
- **Pixel Diff Verification**: Diffed all 287 baseline PNGs. Exactly 12 frames changed (all offscreen switch shadow/knob caches and their chained composites in `settings-all`). Representative comparison crops saved under `research/evidence/GA-untextured-strides/`.
- **Test Suite**:
  - `tests/test_agx_untextured_stride.py`: 6 tests passing.
  - `.venv/bin/python -m unittest discover -s tests`: 270 tests passing (12 skipped).
  - `.venv/bin/python backend/agx_contract_test.py`: all contract tests pass.
  - `backend/.venv/bin/python scripts/qemu/build_backend.py`: compiles cleanly to `backend/bin/qemu-system-aarch64`.

---

## 2. Decoded Geometry & Attribute Layouts

### Group A: 104 Draws (Draw Index 0)
- **Distribution**: 64 in `unlock`, 21 in `unlock-flyin`, 19 in `settings-all`.
- **Topology**: Axis-aligned quad, `count = 4`, `step = 4`.
- **Vertex Stride**: `12` bytes.
- **Attribute Layout**:
  - Bytes 0..3: `float` $X$ coordinate (in target surface pixels).
  - Bytes 4..7: `float` $Y$ coordinate (in target surface pixels).
  - Bytes 8..11: `uint32_t` `0x00000000` (padding / $Z = 0.0$).
  - Vertices define an axis-aligned bounding rectangle: $(x_0, y_0)$ to $(x_1, y_1)$ (e.g. $[0, 0]$ to $[91, 39]$ or $[3, 3]$ to $[107, 55]$).
- **Target Surfaces**: Offscreen render targets:
  - `0x10618000` ($640 \times 96$ px)
  - `0x10698000` ($640 \times 96$ px)
  - `0x10a00000` ($640 \times 96$ px)
  - `0x10254000` ($640 \times 416$ px)
  - `0x1071c000` ($384 \times 64$ px, switch shadow cache)
  - `0x1076c000` ($640 \times 64$ px, switch knob cache)
  - `0x10554000` ($640 \times 96$ px, switch composite)
- **Shader Identity & Disassembly**:
  All 104 draws use one of 22 distinct 64-byte FNV-1a hashes:
  - `0x13a51b8bc1b85b4c` (6 draws)
  - `0x23e324c0933a2344` (1 draw)
  - `0x28463bd14bffa040` (4 draws)
  - `0x3568dc9c7476346b` (12 draws)
  - `0x4251b7edb10c7be4` (1 draw)
  - `0x48870313e0aa10e8` (12 draws)
  - `0x604b25e32d1eca5c` (2 draws)
  - `0x66287bbe34a68c71` (1 draw)
  - `0x664855a92cde7229` (6 draws)
  - `0x6a2a1d789db87995` (2 draws)
  - `0x6af3b793cd19efb3` (3 draws)
  - `0x95450769f4fd347b` (12 draws)
  - `0x9606b672a1c89485` (3 draws)
  - `0xa112190af2d46c9e` (1 draw)
  - `0xa7d834f91cf06600` (1 draw)
  - `0xa82d25f9c6abcf1f` (1 draw)
  - `0xc449a4cb19584ccf` (1 draw)
  - `0xc8fbd825d2fea18d` (4 draws)
  - `0xcbf57b156b6423ec` (6 draws)
  - `0xe8b7f35f72f5737f` (12 draws)
  - `0xefc984709b59caeb` (1 draw)
  - `0xf5f55b3c7127e3f3` (12 draws)

  Binary inspection of the USC fragment shader code showed that **all 22 programs share the identical 41-byte preamble**:
  ```hex
  2a0a949999af1b800033000080408001880020ff3a8a80949999af1b80003300008240800388002100
  ```
  This preamble is identical to `AGX_USC_BLUR_1` through `AGX_USC_BLUR_9` (`CA::OGL::BlurState::narrow_blur` in `QuartzCore`). The shader body is 44 bytes; the 64-byte FNV window incorporates 20 trailing bytes that vary based on the heap layout, creating 22 distinct hashes for the same shader family.

### Group B: 12 Draws (Draw Index 2)
- **Distribution**: 7 in `unlock` (frames 5, 19, 33, 47, 61, 75, 89) and 5 in `unlock-flyin` (frames 5, 11, 17, 23, 29).
- **Topology**: Multi-quad, `count = 12` ($2 \times 6$ vertices).
  - Quad 1: $1 \times 865$ px vertical strip ($x \in [640, 641], y \in [1011, 1876]$).
  - Quad 2: $295 \times 1$ px horizontal strip ($x \in [346, 641], y \in [1010, 1011]$).
- **Vertex Stride**: `24` bytes.
- **Attribute Layout**:
  - Bytes 0..15: `(float x, float y, float z=0.0, float w=1.0)`
  - Bytes 16..23: `(float u, float v)`
  - Texture coordinates in Quad 2 have $v \in [-871, -870]$, far out-of-bounds for the active $640 \times 96$ render target.
- **Packet State**:
  - Preceded by Draw 1 (`prog = 0x8eb52cf6fe248b1d`, textured quad).
  - Draw 2 is emitted with an incremental PPP state packet (`hdr = 0xc3f`, 15 words) that updates scissor and viewport, but has bit 7 cleared (no fragment program offset).
  - Without a fragment shader or texture state word, `prog = 0x0` and `tex = None`.
  - Left skipped as `"stride 24 untextured with program 0x0"` per instructions.

---

## 3. Implementation Summary

### `backend/s5l8960x.c` (between `AGX_REPLAY_CORE_BEGIN` and `AGX_REPLAY_CORE_END`)
- Defined `AGX_USC_BLUR_10` through `AGX_USC_BLUR_31` for the 22 recovered backdrop blur program hashes.
- Updated `agx_is_blur_program()` to include all 31 numbered blur shaders, `AGX_USC_BLUR_4TAP`, and `AGX_USC_BLUR_7TAP`.
- Updated skip classification in `AGX_REPLAY_SKIP` to emit `"stride 24 untextured with program 0x0"` when `stride == 24 && prog == 0`.
- Stride-12 blur draws route directly into the existing C backdrop blur implementation:
  ```c
  if (stride == 12 && is_blur_prog && !textured) {
      /* Untextured Gaussian blur backdrop pass on canvas */
      ...
      agx_blur_texture(raw_tex, tmp_tex, bw, bh);
      ...
  }
  ```

### `backend/agx_replay_offline.py`
- Preserved `BLUR_PROGRAMS` with the 9 legacy hashes (preserving test invariants in `tests/test_agx_accuracy.py`).
- Defined `BLUR_PROGRAMS_12` with the 22 new program hashes.
- Expanded `ALL_BLUR_PROGRAMS = BLUR_PROGRAMS | {BLUR_4TAP, BLUR_7TAP} | BLUR_PROGRAMS_12`.
- Updated `classify_draw()`:
  ```python
  if stride == 24 and prog == 0:
      return 'skipped: stride 24 untextured with program 0x0', {'stride': stride, 'prog': hex(prog)}
  ```

---

## 4. Corpus Before & After Totals

### C Replay
| Category | Baseline | Current | Delta |
|---|---|---|---|
| Replayed draws | 1777 | 1881 | +104 |
| `untextured with unsupported stride` | 116 | **0** | -116 |
| `stride 24 untextured with program 0x0` | 0 | 12 | +12 |
| Total skipped draws | 116 | 12 | -104 |
| Replay execution time | 5.5 s | 3.8 s | -1.7 s |

### Python Audit
| Category | Baseline | Current | Delta |
|---|---|---|---|
| `approximated: Gaussian blur (separable 6-tap, canvas backdrop)` | 0 | 104 | +104 |
| `skipped: untextured with stride not in (20, 28)` | 116 | **0** | -116 |
| `skipped: stride 24 untextured with program 0x0` | 0 | 12 | +12 |
| `skipped: non-axis-aligned or rotated geometry` | 24 | 24 | 0 |
| Total audited draws | 2046 | 2046 | 0 |

---

## 5. Changed Frame Analysis & Pixel Diff

Every PNG in `corpus/report/` was diffed against `corpus/report-baseline/` (287 frames total). Exactly 12 frames changed:

| Frame PNG | Changed Bounding Box $(x_0, y_0, x_1, y_1)$ | Diff Pixels | Explanation |
|---|---|---|---|
| `settings-all-frame-5.png` | $(0, 15) - (90, 38)$ | 375 | Draw 0 separable blur rendered onto offscreen switch shadow cache `0x1071c000`. |
| `settings-all-frame-12.png` | $(96, 3) - (105, 48)$ | 404 | Draw 0 separable blur rendered onto offscreen switch shadow cache `0x1071c000`. |
| `settings-all-frame-13.png` | $(60, 0) - (90, 38)$ | 244 | Draw 0 separable blur rendered onto offscreen switch knob cache `0x1076c000`. |
| `settings-all-frame-20.png` | $(99, 3) - (106, 54)$ | 352 | Draw 0 separable blur rendered onto offscreen switch shadow cache `0x1071c000`. |
| `settings-all-frame-21.png` | $(79, 0) - (98, 46)$ | 660 | Draw 0 separable blur rendered onto offscreen switch knob cache `0x1076c000`. |
| `settings-all-frame-28.png` | $(95, 3) - (106, 63)$ | 554 | Draw 0 separable blur rendered onto offscreen switch shadow cache `0x1071c000`. |
| `settings-all-frame-29.png` | $(79, 0) - (98, 46)$ | 724 | Draw 0 separable blur rendered onto offscreen switch composite `0x10554000`. |
| `settings-all-frame-30.png` | $(637, 112) - (639, 127)$ | 24 | Chained composite: blurred offscreen switch from frame-29 composited onto framebuffer `0x400470000`. |
| `settings-all-frame-32.png` | $(637, 112) - (639, 127)$ | 24 | Chained composite: blurred offscreen switch from frame-29 composited onto framebuffer `0x400470000`. |
| `settings-all-frame-35.png` | $(81, 0) - (98, 46)$ | 406 | Draw 0 separable blur rendered onto offscreen switch composite `0x10554000`. |
| `settings-all-frame-36.png` | $(96, 3) - (106, 63)$ | 392 | Draw 0 separable blur rendered onto offscreen switch shadow cache `0x1071c000`. |
| `settings-all-frame-37.png` | $(80, 0) - (98, 46)$ | 668 | Draw 0 separable blur rendered onto offscreen switch composite `0x10554000`. |

Representative before/after crops and side-by-side comparison images have been saved to:
`research/evidence/GA-untextured-strides/`
- `settings-all-frame-12_before.png`, `settings-all-frame-12_after.png`, `settings-all-frame-12_compare.png`
- `settings-all-frame-21_before.png`, `settings-all-frame-21_after.png`, `settings-all-frame-21_compare.png`
- `settings-all-frame-29_before.png`, `settings-all-frame-29_after.png`, `settings-all-frame-29_compare.png`
- `settings-all-frame-30_before.png`, `settings-all-frame-30_after.png`, `settings-all-frame-30_compare.png`

---

## 6. Verification & Test Output

1. **`tests/test_agx_untextured_stride.py`**:
   ```
   ......
   ----------------------------------------------------------------------
   Ran 6 tests in 0.931s
   OK
   ```
2. **Full Test Suite (`.venv/bin/python -m unittest discover -s tests`)**:
   ```
   ----------------------------------------------------------------------
   Ran 270 tests in 4.900s
   OK (skipped=12)
   ```
3. **Contract Tests (`.venv/bin/python backend/agx_contract_test.py`)**:
   ```
   PASS: AGX replay pairing: exact guest physical pixels and padding
   ...
   PASS: AGX doorbell 0x11 -> channel queue registered, type-4 stamp stored, type-1 completion event and IRQ 84
   Synthetic AGX host-interface and quad replay contract only; no iOS rendering inferred.
   ```
4. **QEMU Backend Build (`backend/.venv/bin/python scripts/qemu/build_backend.py`)**:
   ```
   [7/7] Generating qemu-system-aarch64 with a custom command
   Built experimental backend: /Users/bagas/Documents/Projects/vphone/vphone-wt-gfx-a/backend/bin/qemu-system-aarch64
   No iOS boot compatibility is claimed.
   ```

---

## 7. Open Items

1. **Incremental PPP State Inheritance for Draw 2**:
   Draw 2 in `unlock` / `unlock-flyin` uses incremental header `0xc3f` which does not emit fragment program words. In hardware SGX/Rogue pipelines, state registers not touched by an incremental PPP update retain values from the prior draw (Draw 1). Future work can model pipeline state retention across consecutive draws to determine whether Draw 2 inherits Draw 1's textured program (`0x8eb52cf6fe248b1d`) and sampler state.
