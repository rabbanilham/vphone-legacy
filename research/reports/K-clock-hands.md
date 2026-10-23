# Task K: Clock icon hands (vertex colour order and triangle rasterisation)

**Task ID**: `K-clock-hands`  
**Worktree**: `/Users/bagas/Documents/Projects/vphone/vphone-wt-clock-hands`  
**Branch**: `wt/clock-hands`  
**Author / Worker**: Antigravity (`agy`)  
**Scope**: AGX replay core in `backend/s5l8960x.c` (between `AGX_REPLAY_CORE_BEGIN` and `AGX_REPLAY_CORE_END`), `backend/agx_replay_offline.py`, `backend/agx_contract_test.py`, and `tests/test_agx_*`.

---

## 1. Status per Goal

| Goal | Status | Evidence / Notes |
| --- | --- | --- |
| **Clock icon hands visually resolved** | **PASS** | Bounding box blocks eliminated. Settled Home frame 91 renders thin dark hour/minute hands meeting at center and thin red second hand. Before/after crops saved in `research/evidence/K-clock-hands/`. |
| **Vertex colour byte order (untextured)** | **PASS** | Untextured vertex colors (stride 20 and 28, offset +16) are decoded as RGBA in memory. Second hand `0xff1c3edc` renders as #DC3E1C (red); Clock app grouped background `0xfff4efef` renders as #EFEFF4 (iOS 7 grey), not pinkish #F4EFEF. |
| **Untextured triangle rasteriser** | **PASS** | Added triangle rasteriser evaluating edge functions at pixel centres `(x + 0.5, y + 0.5)` with CW/CCW winding invariance, span clamping, and scissor/region clipping. |
| **Python audit: 0 untextured rotated skips** | **PASS** | "skipped: non-axis-aligned or rotated geometry" for untextured draws dropped from 3/frame to exactly 0 across all 225 dumps. The 27 remaining skips in the entire corpus are all textured rotated draws in `clock` and `calendar`. |
| **No regressions elsewhere in corpus** | **PASS** | All 225 corpus dumps replayed cleanly. All 167 non-Clock images are byte-identical. Only `unlock` settled Home frames 91..161 (Clock icon) and `clock` frames 3..10 (table background color) changed. Lock screen clock digits, Calendar, and Settings remain 100% identical. |
| **Unit tests pass** | **PASS** | 242 passed, 12 skipped in test suite (`.venv/bin/python -m unittest discover -s tests`). Added 8 dedicated rasteriser and colour order unit tests in `tests/test_agx_triangle_raster.py`. |
| **QEMU backend compiles** | **PASS** | `backend/.venv/bin/python scripts/qemu/build_backend.py` compiles cleanly to `backend/bin/qemu-system-aarch64`. |
| **Offline constraint obeyed** | **PASS** | No QEMU guest boots executed. All validation conducted offline via `corpus/` and test harnesses. |

---

## 2. Root Cause Analysis

### A. Non-Axis-Aligned Triangles Rendered as Filled Bounding Boxes
- **Root Cause**:
  In `unlock/dumps/frame-91.bin` (and all settled Home frames 91..161), the Clock icon hands are rendered via:
  - Draws 2, 4, 6, 8: $n=48$, stride 28, untextured (prog `0x6c01d033fd937c7c`), rounded caps / path fan triangle lists.
  - Draws 3, 5, 7, 9: $n=6$, stride 28, untextured (prog `0xba8c50a79810e866`), thin rotated hands (two triangles per hand, rotated by hand angle).
  - Draw 10: $n=6$, stride 20 (prog `0x2f70323ca222288c`), center dot.
  - Draw 11: $n=48$, stride 20 (prog `0x5fe3ceea98f5b523`), center pivot.
  Previously, both `backend/s5l8960x.c` and `backend/agx_replay_offline.py` only supported axis-aligned quads by computing the axis-aligned bounding box `[minx, miny, maxx, maxy]` and filling the entire rectangle. For rotated hands, this filled the entire diagonal bounding box (~225 pixels instead of ~40 pixels), creating a massive dark square block for the minute hand and a thick rectangle for the second hand.

### B. Vertex Colour Byte Order: RGBA vs BGRA
- **Root Cause**:
  In guest memory, untextured vertex colours at `stride 20` and `stride 28` (offset +16) are packed in **RGBA** byte order:
  - Byte 0: Red ($R$)
  - Byte 1: Green ($G$)
  - Byte 2: Blue ($B$)
  - Byte 3: Alpha ($A$)
  When loaded as a 32-bit little-endian integer in memory, `0xff1c3edc` has byte 0 = `0xdc`, byte 1 = `0x3e`, byte 2 = `0x1c`, byte 3 = `0xff`.
  The original code treated this value as BGRA:
  - Blue was assigned `0xdc` (220) and Red was assigned `0x1c` (28), rendering the second hand as a thick bright blue bar instead of red.
  - Similarly, the Clock app table background `0xfff4efef` (byte 0 = `0xef`, byte 2 = `0xf4`) was assigned Red = 244, Blue = 239, giving a pinkish hue instead of iOS 7 grouped background grey `#EFEFF4`.
  - Textured tint check: In textured draws with stride 28, the tint is almost exclusively monochrome/white `0xffffffff` or dark vibrancy `0xc0204060`. The red "Edit"/"+" buttons and Calendar "Friday" use un-tinted stride 24 textures, so leaving textured tint unchanged preserved red UI elements.

---

## 3. Implementation Details

### A. Live C Replay (`backend/s5l8960x.c`)
- Located strictly between `AGX_REPLAY_CORE_BEGIN` and `AGX_REPLAY_CORE_END`:
  1. **Colour Conversion**:
     When reading untextured vertex colour from guest memory (`agx_ctx_read(..., buf + 16, &colour, 4)`), the $R$ and $B$ channels are swapped:
     ```c
     colour = (colour & 0xff00ff00u) | ((colour >> 16) & 0xff) | ((colour & 0xff) << 16);
     ```
     This translates guest RGBA vertex colour into the internal BGRA framebuffer word used by scanout.
  2. **Triangle Rasteriser & Axis-Aligned Fast Path**:
     - Quad draws with count 4, 6, or 12 are first checked for axis-alignment (`is_axis_aligned`). If all vertices align to horizontal and vertical edges, the existing fast-path rectangle scanline loop is preserved.
     - For non-axis-aligned geometry, consecutive vertex triples $P_0, P_1, P_2$ are processed as triangles:
       - Degenerate triangles ($|\det| < 10^{-5}$) are skipped.
       - Edge functions $E(P_i, P_{i+1}, P) = (P_{i+1,x} - P_{i,x})(P_y - P_{i,y}) - (P_{i+1,y} - P_{i,y})(P_x - P_{i,x})$ are evaluated at pixel centers $(x + 0.5, y + 0.5)$.
       - Both CW ($\det > 0$) and CCW ($\det < 0$) windings are supported:
         `inside = (det > 0.0f) ? (e0 >= 0.0f && e1 >= 0.0f && e2 >= 0.0f) : (e0 <= 0.0f && e1 <= 0.0f && e2 <= 0.0f);`
       - Per-scanline horizontal spans `[first_in, last_in]` are determined to minimize framebuffer memory operations.
       - Blending (`prog == AGX_USC_COPY ? colour : agx_blend_over(...)`) and scissor/region clipping are applied identically to quad draws.

### B. Python Offline Mirror (`backend/agx_replay_offline.py`)
- In `classify_draw()`:
  - Non-axis-aligned untextured draws with stride 20 or 28 are now classified as:
    `f'replayed exactly: solid triangles (stride={stride})'`
  - Textured non-axis-aligned draws continue to be reported as `'skipped: non-axis-aligned or rotated geometry'`.
- In `main()`:
  - Added the exact triangle rasteriser mirroring the C implementation.
  - Vertex colors are decoded directly as RGBA `r, g, b, a = raw_col[0], raw_col[1], raw_col[2], raw_col[3]` into the RGBA PNG framebuffer `fb`.

### C. Synthetic Contract Fixtures (`backend/agx_contract_test.py`)
- Updated `replay_blob()` oracle generator to reflect that untextured vertex colors in memory are RGBA, so the expected BGRA framebuffer words contain `swap_rb(colour)`.

---

## 4. Evidence

All crops saved under `research/evidence/K-clock-hands/`:
- **`before-clock-icon-91.png`**: Original C replay crop (120x120, x: 185..305, y: 245..365) from `build/agx-corpus/report/unlock-frame-91.png`. Shows minute hand rendered as a large dark square block and second hand as a thick blue bar.
- **`live-probe-clock-icon-101s.png`**: Reference live QEMU guest display probe crop from `build/agx-corpus/unlock/probe/framebuffer-at-101s.ppm`.
- **`after-clock-icon-91.png`**: New C replay crop from `corpus/report/unlock-frame-91.png`.
  - Dark hour and minute hands are thin, cleanly rendered lines meeting at the center dot.
  - The second hand is a thin, crisp red line (#DC3E1C).
  - Exactly 113 red pixels ($R > 180, B < 60$) on the second hand; exactly 0 blue second-hand pixels ($B > 180, R < 60$).

---

## 5. Corpus & Report Verification

Executed: `.venv/bin/python scripts/agx_corpus.py --corpus corpus`  
Result: 225 dumps replayed in 5.2s.

### Draw Classification Breakdown:
- C replay: 1184 draws drawn, 0 harness errors.
- Python audit:
  - 306: `replayed exactly: solid triangles (stride=28)`
  - 302: `replayed exactly: solid quad (stride=28)`
  - 226: `approximated: textured quad (bilinear scaled)`
  - 197: `replayed exactly: textured quad (1:1 nearest)`
  - 170: `skipped: untextured with stride not in (20, 28)`
  - 152: `replayed exactly: solid quad (stride=20)`
  - 80: `replayed exactly: solid triangles (stride=20)`
  - 40: `approximated: vibrancy (Rec.709 luma-weighted tint)`
  - 27: `skipped: non-axis-aligned or rotated geometry` (all 27 are textured in clock/calendar)
  - 12: `replayed exactly: dest-in mask (1:1)`

### Before vs After Frame Comparison:
- **Total images in corpus report**: 246 PNGs.
- **Identical frames**: 167 frames (100% byte-identical).
  - All Lock Screen frames 1..90: identical (no regression on clock digits or date).
  - All Settings frames 1..20: identical.
  - All Calendar frames 1..2: identical.
  - All Unlock-flyin frames: identical.
- **Changed frames**: 79 frames.
  - `unlock-frame-91.png` .. `unlock-frame-161.png` (71 frames): Settled Home passes. Changed exclusively due to Clock icon hands being correctly rasterised instead of blocked.
  - `clock-frame-3.png` .. `clock-frame-10.png` (8 frames): Clock app. Changed exclusively due to table background color correcting from pinkish #F4EFEF to genuine iOS 7 grouped grey #EFEFF4.

---

## 6. Unit Tests & Static Analysis

1. **New Unit Tests (`tests/test_agx_triangle_raster.py`)**:
   - `test_vertex_colour_rgba_order`: Verifies untextured vertex colours decode as RGBA (second hand #DC3E1C, grouped background #EFEFF4).
   - `test_c_replay_vertex_colour_rb_swap`: Verifies C replay byte swap produces correct BGRA framebuffer words.
   - `test_triangle_pixel_center_coverage`: Verifies edge function evaluation at $(x + 0.5, y + 0.5)$ produces mathematically exact coverage.
   - `test_winding_invariance`: Verifies CW and CCW windings produce bit-identical raster output.
   - `test_degenerate_triangles_skipped`: Verifies collinear and zero-area triangles produce 0 pixels.
   - `test_triangle_scissor_clipping`: Verifies pixels outside scissor bounds are never written.
   - `test_rotated_clock_hand_not_bounding_box`: Verifies rotated hands occupy ~40 pixels instead of ~225 bounding box pixels.
   - `test_c_rasteriser_extracted_compilation`: Compiles C rasteriser routine with host `cc` and asserts parity with Python.

2. **Full Test Suite**:
   ```bash
   .venv/bin/python -m unittest discover -s tests
   # Ran 242 tests in 2.257s -> OK (skipped=12)
   ```

3. **Backend Build Verification**:
   ```bash
   backend/.venv/bin/python scripts/qemu/build_backend.py
   # Built experimental backend: backend/bin/qemu-system-aarch64 (clean build, exit code 0)
   ```

---

## 7. Open Items

- None for Task K. Untextured rotated triangle rasterisation and vertex colour byte order are fully resolved both live (C) and offline (Python).
- Textured rotated draws (27 remaining in corpus) remain as future work if needed.
