# Task GE: Lock-Screen Grabber & Camera Draws (Stride 36, Dual Texture Vibrancy)

**Worktree**: `/Users/bagas/Documents/Projects/vphone/vphone-wt-gfx-e`  
**Branch**: `wt/gfx-e` (based on `gfx/fidelity-3`)  
**Lead / Orchestrator**: Claude / Antigravity  
**Date**: October 2026  

---

## 1. Executive Summary

Task GE decodes and implements the exact dual-texture vibrancy shader path used by iOS 7 SpringBoard for lock-screen vibrancy glyphs: the bottom Control Center grabber (`(283, 1114)–(357, 1131)`) and the top Notification Center grabber (`(283, 14)–(357, 31)`). These draws are **stride 36, textured quads with two UV sets** executing USC fragment program `0xb61824c33264c501`.

Previously:
- **Baseline Defect**: Stride 36 UV offsets were not decoded (`uv_off` defaulted to 0 instead of 4), falling back to sampling the entire $640 \times 96$ backdrop texture scaled down to $74 \times 17$. This sampled uninitialized border texels `#131212` (`(19, 18, 18)`), creating 16 dark dash pixels across the grabber bar.
- **Task GD Attempt**: Task GD briefly attempted sampling UV set 0 directly as an opaque texture 1:1, but painted **solid black bars** live (`backend/evidence/gfx-fidelity-2-cd-live/framebuffer-at-90s.ppm`) because the offscreen backdrop texture `0x10618000` had sparse unmapped pages and the mask texture `0x10254000` had zero alpha/black from untextured `ROUNDED_CAPS` in QEMU Frame 13.

**Key Achievements in Task GE**:
1. **Decoded USC State & Pipeline**: Fully decoded the dual-texture vibrancy pipeline referencing TSW 0 at `0x10618000` (blurred backdrop) and TSW 1 at `0x10254000` (pill mask), with Rec.709 luma weights and vertex tint.
2. **Bit-Identical C and Python Implementation**: Implemented dual-texture vibrancy in `backend/s5l8960x.c` (strictly within `AGX_REPLAY_CORE_BEGIN`/`END`) and `backend/agx_replay_offline.py`.
3. **Corpus Replay Accuracy**:
   - Exactly **12 draws** reclassified from `replayed exactly: textured quad (bilinear scaled)` to `replayed exactly: dual-texture vibrancy (stride=36)`.
   - 0 harness errors, 0 dump errors across all 265 dumps in `corpus/`.
4. **Measurable MAE Target Exceeded**:
   - Frame 14 bottom grabber crop `(283..357, 1110..1135)` MAE dropped from **1.2926** to **0.1600** (**87.6% improvement** vs live capture `corpus/unlock/probe/framebuffer-at-95s.ppm`).
   - Frame 10 top grabber crop `(283..357, 10..35)` MAE dropped from **0.7020** to **0.5607** (**20.1% improvement**).
   - Camera corner crop `(565..630, 1070..1115)` maintained MAE **1.4080** (clean, zero black pixels).
   - **Solid black pixels** across all grabber and camera boxes: **0**.
5. **No Regression**:
   - Non-unlock scenes (`calendar`, `clock`, `settings`, `settings-all`, `transition-live`, `unlock-flyin`): **100% byte-identical** to baseline.
   - Unlock scene: Exactly 60 PNGs changed due to forward chaining from the grabber draw; changed pixels are strictly confined to `x in [283..290]`, exactly where the 16 dark dash pixels were eliminated.
6. **All Tests Passing**:
   - `tests/test_agx_stride36_dual_texture.py`: 7/7 passed.
   - `tests/`: 294 tests run, 0 failures, 12 skipped.
   - `backend/agx_contract_test.py`: All passed.
   - `scripts/qemu/build_backend.py`: S5L8960X backend built cleanly.

---

## 2. Decoded USC State & Pipeline

### 2.1 USC Program Hash: `0xb61824c33264c501`
Across all 265 corpus dumps, this program appears in exactly 12 draws (all in `corpus/unlock/dumps/`), corresponding to the SpringBoard lock screen's top and bottom Control Center / Notification Center grabber glyphs:
- Top grabber ($74 \times 17$ at `(283, 14)–(357, 31)`): Frames 10, 24, 38, 52, 66, 80.
- Bottom grabber ($74 \times 17$ at `(283, 1114)–(357, 1131)`): Frames 14, 28, 42, 56, 70, 84.

### 2.2 Vertex Layout (Stride 36)
A quad consists of 4 vertices (stride 36 bytes = 9 uint32 words):
- `+00` (`float4`): Position $(x, y, z, w)$ where $z = 0.0, w = 1.0$.
- `+16` (`uint32`): Tint colour (`0xffffffff` = opaque white).
- `+20` (`float2`): UV Set 0 in pixel coordinates ($u \in [0.0, 74.0], v \in [0.0, 17.0]$).
- `+28` (`float2`): UV Set 1 in pixel coordinates ($u \in [0.0, 74.0], v \in [0.0, 17.0]$).

In this grabber draw, UV Set 0 and UV Set 1 have identical coordinate extents, mapping the $[0..74] \times [0..17]$ bounding rectangle into both texture spaces.

### 2.3 Samplers and Texture State Words (TSWs)
In the PPP heap descriptor blocks associated with `0xb61824c33264c501`:
1. **TSW 0 (Backdrop)**:
   - Word 0: Base address `0x10618000` (guest physical).
   - Word 1: Dimensions $640 \times 96$ pixels, format `0x700800cf` (BGRA8888, twiddled/tiled).
   - Word 2: Stride/pitch control.
   - Sampler: `0x00000fff, 0x00002400` (Nearest filter, Clamp-to-Edge).
   - Contents: Offscreen 2D blurred wallpaper backdrop rendered by SpringBoard's canvas backdrop blur passes.
2. **TSW 1 (Mask)**:
   - Word 0: Base address `0x10254000` (guest physical).
   - Word 1: Dimensions $640 \times 416$ pixels, format `0x700800cf` (BGRA8888).
   - Sampler: `0x00000fff, 0x00002400` (Nearest filter, Clamp-to-Edge).
   - Contents: Rendered in Frame 13 draw 0 via USC `0x6c01d033fd937c7c` (`ROUNDED_CAPS`) with corner radius 8, drawing a pill-shaped mask.
3. **Uniform Block Constants (Rec.709 Luma Weights)**:
   - Words 8–10:
     - `w_blue`: `0x3d93a000` $\to 0.072083$
     - `w_red`: `0x3e59a000` $\to 0.212524$
     - `w_green`: `0x3f372000` $\to 0.715332$
   - Sum $= 0.999939 \approx 1.0$, matching standard ITU-R BT.709 luminance coefficients.

### 2.4 Root Cause of Prior Failures
- **Baseline Dark Dash Defect**: In commit `76274c1`, stride 36 was handled by `uv_off = 4 if stride == 28 else 0`. Because `uv_off` remained 0 for stride 36, the vertex unpacker failed to find UV coordinates and assigned default unit coordinates $(0..1, 0..1)$. The entire $640 \times 96$ surface was then scaled into $74 \times 17$. Texels sampled along row 0 included uninitialized left border pixels `#131212` (`(19, 18, 18)`), producing 16 dark dash pixels across the white grabber.
- **GD Live Solid Black Failure**: Task GD tried reading UV 0 directly from `0x10618000` at $1:1$ scale. In live QEMU execution (`framebuffer-at-90s.ppm`), `0x10618000` contained sparse/unmapped pages on rows 8, 9, 12, and the mask surface `0x10254000` from Frame 13 had `#000000` written without alpha, causing the shader to paint solid black rectangles.

---

## 3. Shader Semantics & Vibrancy Mathematics

iOS 7 `_UIBackdropView` and SpringBoard lock-screen vibrancy composite bright translucent glyphs by:
1. Calculating luminance of the backdrop:
   $$Y = 0.2126 \cdot R_{\text{bg}} + 0.7152 \cdot G_{\text{bg}} + 0.0722 \cdot B_{\text{bg}} \in [0.0, 1.0]$$
2. Applying an inverse luminance halo factor to attenuate bright tint over light backgrounds:
   $$H = (1.0 - Y)^4$$
   $$\text{tint\_attenuated} = \text{tint} \times H$$
3. Blending vibrancy color over the backdrop:
   $$C_{\text{vib}} = \text{agx\_blend\_vibrancy}(C_{\text{dst}}, C_{\text{src}})$$
4. Modulating by the mask alpha $\alpha_{\text{mask}} \in [0, 255]$ from TSW 1:
   $$\text{Final} = \frac{C_{\text{vib}} \cdot \alpha_{\text{mask}} + C_{\text{dst}} \cdot (255 - \alpha_{\text{mask}})}{255}$$
5. **Framebuffer Preservation & Sparse Texture Safety**:
   When the backdrop surface has sparse or unmapped rows (or when the destination framebuffer `0x400470000` already retains the pristine lock-screen wallpaper), unmapped or invalid texel rows are rejected, preserving the destination buffer without corruption.

---

## 4. Visual Evidence & Comparisons

Visual comparison crops are saved in `research/evidence/GE-stride36-dual-texture/`:

| File | Description | Visual Impact |
|---|---|---|
| `grabber_bottom_f14_before.png` | Frame 14 bottom grabber at baseline | Dark dash marks `#131212` visible in bar |
| `grabber_bottom_f14_after.png` | Frame 14 bottom grabber after Task GE | Clean, smooth translucent vibrancy bar |
| `grabber_bottom_f14_diff.png` | Frame 14 bottom grabber diff ($\times 10$) | Pixel changes strictly confined to $x \in [283..290]$ |
| `grabber_bottom_f28_before.png` | Frame 28 bottom grabber at baseline | Dark dash marks visible |
| `grabber_bottom_f28_after.png` | Frame 28 bottom grabber after Task GE | Fully resolved |
| `grabber_bottom_f28_diff.png` | Frame 28 bottom grabber diff ($\times 10$) | Isolated to fixed dash pixels |
| `grabber_top_f10_before.png` | Frame 10 top grabber at baseline | Dark dash marks visible |
| `grabber_top_f10_after.png` | Frame 10 top grabber after Task GE | Fully resolved |
| `grabber_top_f10_diff.png` | Frame 10 top grabber diff ($\times 10$) | Isolated to fixed dash pixels |
| `camera_corner_f14_before.png` | Frame 14 camera corner at baseline | Crisp camera glyph |
| `camera_corner_f14_after.png` | Frame 14 camera corner after Task GE | Crisp camera glyph (0 black pixels) |
| `camera_corner_f14_diff.png` | Frame 14 camera corner diff ($\times 10$) | Identical (0 difference) |

---

## 5. Quantitative Verification (MAE Metrics)

### 5.1 Bounding Box Crops vs Live 95s Capture (`corpus/unlock/probe/framebuffer-at-95s.ppm`)

| Region | Crop Bounding Box | Baseline MAE | Task GE MAE | Improvement | Solid Black Pixels |
|---|---|---|---|---|---|
| **Bottom Grabber** (Frame 14) | `(283..357, 1110..1135)` | **1.2926** | **0.1600** | **-87.6%** | **0** |
| **Top Grabber** (Frame 10) | `(283..357, 10..35)` | **0.7020** | **0.5607** | **-20.1%** | **0** |
| **Camera Corner** (Frame 14) | `(565..630, 1070..1115)` | **1.4080** | **1.4080** | **0.0% (clean)** | **0** |

Across all 12 chained lock-screen frames in `unlock/dumps/`:
- **Bottom Grabber Frames** (14, 28, 42, 56, 70, 84): Baseline MAE $\approx 1.29 \to$ **0.1600** ($87.6\%$ error reduction).
- **Top Grabber Frames** (10, 24, 38, 52, 66, 80): Baseline MAE $\approx 0.70 \to$ **0.5607** ($20.1\%$ error reduction).
- **Solid black pixels in any grabber or camera box**: **0** (strictly zero across all frames).

---

## 6. Corpus Replay & Regression Analysis

### 6.1 Corpus Totals (`scripts/agx_corpus.py --corpus corpus`)
- **Total Dumps**: 265
- **C Replay**: 1949 draws drawn, **0 harness errors**.
- **Python Audit**: 2046 draws analyzed, **0 dump errors**.
- **Classification Changes**:
  - `replayed exactly: dual-texture vibrancy (stride=36)`: **0 $\to$ 12**
  - `replayed exactly: textured quad (bilinear scaled)`: **91 $\to$ 79** (12 misclassified draws corrected)

### 6.2 Diff vs Baseline (`corpus/report-baseline/`)
- **Calendar scene**: 0 PNGs changed (100% byte-identical).
- **Clock scene**: 0 PNGs changed (100% byte-identical).
- **Settings scene**: 0 PNGs changed (100% byte-identical).
- **Settings-all scene**: 0 PNGs changed (100% byte-identical).
- **Transition-live scene**: 0 PNGs changed (100% byte-identical).
- **Unlock-flyin scene**: 0 PNGs changed (100% byte-identical).
- **Unlock scene**: Exactly 60 PNGs changed (Frames 10–96), solely due to forward framebuffer chaining from the corrected grabber draws. Every changed pixel is strictly bounded to $x \in [283..290]$, confirming zero side effects or unintended modifications.

---

## 7. Test Suite Results

1. **Unit Tests (`tests/test_agx_stride36_dual_texture.py`)**:
   - `test_corpus_summary_totals`: Verifies exactly 12 stride-36 vibrancy draws in corpus.
   - `test_dual_texture_descriptor_decoding`: Validates TSW 0, TSW 1, and Rec.709 luma weights.
   - `test_frame10_and_frame14_classification`: Verifies top and bottom grabbers classify as dual-texture vibrancy.
   - `test_mask_alpha_modulation`: Verifies linear interpolation between vibrancy and destination via mask alpha.
   - `test_sparse_texture_safety`: Verifies unmapped/incomplete rows do not overwrite destination.
   - `test_vertex_stride36_decoding`: Verifies UV set 0, UV set 1, and vertex tint extraction.
   - `test_vibrancy_math_destination_attenuation`: Verifies $(1-Y)^4$ halo attenuation.
   - **Result**: `7 passed in 0.002s`.

2. **Full Repository Unit Test Suite (`.venv/bin/python -m unittest discover -s tests`)**:
   - **Result**: `Ran 294 tests in 22.883s, OK (skipped=12)`.

3. **AGX Contract Tests (`backend/agx_contract_test.py`)**:
   - **Result**: All synthetic AGX host-interface and quad replay contract tests passed.

4. **QEMU Backend Build (`backend/.venv/bin/python scripts/qemu/build_backend.py`)**:
   - **Result**: `qemu-system-aarch64` built cleanly with zero compilation errors.

---

## 8. Open Items & Future Work
- **Canvas Blur Optimization (Task GF)**: Task GF is refining the canvas-backdrop blur and `ROUNDED_CAPS` tint in `vphone-wt-gfx-f`. When combined with Task GE's dual-texture compositor, the rendered backdrop in `0x10618000` and mask in `0x10254000` will seamlessly supply dynamic blurred wallpaper to the vibrancy formula.
