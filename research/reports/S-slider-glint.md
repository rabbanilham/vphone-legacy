# S-slider-glint: "slide to unlock" Glint and Black Bar Artefact Fix

**Task ID**: `S-slider-glint`
**Worktree**: `${WORKTREES_ROOT}/vphone-wt-slider`
**Branch**: `wt/slider`
**Worker**: Antigravity (`agy`)
**Scope**: Per-program blend modes and horizontal span primitive in the AGX replay core of `backend/s5l8960x.c` (between `AGX_REPLAY_CORE_BEGIN` and `AGX_REPLAY_CORE_END`), `backend/agx_replay_offline.py`, and `tests/test_agx_slider_spans.py`. Strictly decoupled from Task K (`wt/clock-hands`).

---

## 1. Status per Goal

| Goal | Status | Evidence / Notes |
| --- | --- | --- |
| 1. Eliminate black bar and dash artefacts | **PASS** | 0 black pixels in bar/dash regions across all display passes (was 172 in `unlock-frame-6.png`). Fixed via CoreAnimation `kCAFilterPlusL` additive blend mode on display composite. |
| 2. "slide to unlock" label lighter than wallpaper | **PASS** | Label average luminance = 160.3..176.0 vs wallpaper average = 139.1 (delta +21.3..+36.9), with glint peak reaching 255.0 pure white. |
| 3. Glint visible at successive positions across consecutive cycles | **PASS** | Glint sweep confirmed moving across frames 48, 62, 76, 90 (shifts right by ~130–150 px per cycle). Evidence strip generated under `research/evidence/S-slider-glint/glint_sweep_strip.png`. |
| 4. C skip count for `stride 16 outside the wallpaper program` drops to 0 | **PASS** | C skips dropped from **73 to 0**. All 73 glyph span draws successfully rasterised (37 in `unlock`, 36 in `unlock-flyin`). |
| 5. No regressions elsewhere in corpus report | **PASS** | All 225 dumps in `corpus/report/index.html` replay in 3.9 s with 0 harness errors. Settings, Calendar, Clock verified clean; lock-screen frames in Settings also cured of black bar artefact. |
| 6. Unit tests pass & new tests added | **PASS** | 240 unit tests pass (`.venv/bin/python -m unittest discover -s tests`). Added `tests/test_agx_slider_spans.py` covering span classification, rasterisation, blend arithmetic, C execution, and zero-skip assertions. |
| 7. QEMU backend compiles cleanly | **PASS** | `backend/.venv/bin/python scripts/qemu/build_backend.py` compiles without warnings or errors; binary output at `backend/bin/qemu-system-aarch64`. |

---

## 2. Root Cause Analysis

### A. The Black Horizontal Bar and Vertical Dashes Artefact
- **Symptom**: On the iOS 7 lock screen, a dark horizontal bar appeared above the slider label (framebuffer $x=10..370, y=936..946$) along with vertical black dashes down its left edge ($x=171..181$).
- **Mechanism in Pass Chain**:
  1. Offscreen pass 2 (640x96, target `0x10618000`) renders glyph rows of the label and chevron into a clear surface.
  2. Passes 3 and 4 (640x96, targets `0x10698000` and `0x10a00000`) apply horizontal/vertical blur filters to generate the label glow/shadow.
  3. Offscreen pass 5 (640x416, target `0x10254000`) composites the blurred texture at $(-4, -4)..(478, 74)$ (draw 1). While draw 7 applies a `DEST_IN` text mask over rect $(6, 6)..(465, 64)$, the outer perimeter retains black padding pixels.
  4. In the display pass (frame 6, target framebuffer), draw 0 composites the 640x416 layer over $x=175..640, y=940..1010$.
  5. The previous emulator implementation treated all textured draws as standard `src-over` alpha blending. Because the border pixels in the source surface had non-zero alpha but black color $(0, 0, 0, 255)$, `src-over` overwrote the underlying wallpaper with pure black!
- **True Blend Mode**:
  The USC program hash for the display composite draw is `0x5b133ef7e0c3428c`. In QuartzCore / CoreAnimation, this corresponds to `kCAFilterPlusL` (`plusL` / plus-lighter):
  $$\text{Color}_{\text{out}} = \min(255, S + D)$$
  Because black source pixels have value 0:
  $$0 + D = D$$
  Black adds zero contribution to the destination. The black bar and vertical dashes disappear completely, allowing the underlying wallpaper to shine through intact.

### B. Flat Grey Label (#636363) and Missing Glint Sweep
- **Symptom**: The "slide to unlock" label rendered as a flat solid grey `#636363` without the characteristic shimmering glint sweeping across it.
- **Root Cause 1 (Skipped Spans)**:
  In offscreen pass 2, draws 2..8 use USC program `0x1a20cc29e8fcc15b` with `stride = 16` and up to 1024 vertices. These draws were being skipped by the AGX replay core as `"stride 16 outside the wallpaper program"`.
  Investigation showed that `0x1a20cc29e8fcc15b` encodes **horizontal 1-pixel spans**:
  - Each span is defined by a pair of consecutive vertices: $(x_0+0.5, y+0.5)$ and $(x_1+0.5, y+0.5)$.
  - Stride 16 layout: bytes 0..7 are 32-bit float $(X, Y)$ coordinates; bytes 8..15 contain coverage alpha at byte 15 (`buf + k*16 + 15`).
  - Only draw 2 specifies the vertex matrix in `vs_data`; subsequent draws 3..8 retain the matrix in USC registers across the pass.
- **Root Cause 2 (Src-Over Overwrite of Moving Gradients)**:
  In offscreen pass 5 (640x416):
  - Draws 3 and 5 draw a 768x48 periodic gradient texture (with bright peaks every 384 pixels) moving across the label (the glint). These draws use USC programs `0x5b133ef7e0c3428c` and `0xa225a18725e48d5c`.
  - Draw 6 draws a solid `#636363` quad over $(6, 6)..(465, 64)$ with USC program `0xf137b90c25ab5471`.
  - Because draw 6 was rendered with `src-over`, it completely covered the moving gradient quads below it with opaque grey.
  - In reality, `0x5b133ef7e0c3428c`, `0xa225a18725e48d5c`, and `0xf137b90c25ab5471` all use `plus-lighter` additive blending. Draw 6 provides the base grey luminance, while draws 3 and 5 add the sweeping gradient luminance on top, culminating in draw 7 masking the composite with the glyph mask.

---

## 3. Implementation Details

### A. C Hardware Replay Core (`backend/s5l8960x.c`)
All changes are strictly contained between `AGX_REPLAY_CORE_BEGIN` and `AGX_REPLAY_CORE_END`:
1. **Shader Program Identifiers**:
   ```c
   #define AGX_USC_SPANS        UINT64_C(0x1a20cc29e8fcc15b)
   #define AGX_USC_PLUS_L       UINT64_C(0x5b133ef7e0c3428c)
   #define AGX_USC_PLUS_L2      UINT64_C(0xa225a18725e48d5c)
   #define AGX_USC_SOLID_PLUS_L UINT64_C(0xf137b90c25ab5471)
   ```
2. **Blend Implementations**:
   ```c
   static inline uint32_t agx_blend_plus_lighter(uint32_t dst, uint32_t src)
   {
       uint32_t out = 0;
       for (unsigned c = 0; c < 32; c += 8) {
           uint32_t v = ((src >> c) & 0xff) + ((dst >> c) & 0xff);
           out |= MIN(v, 255u) << c;
       }
       return out;
   }

   __attribute__((unused))
   static uint32_t agx_blend_plus_darker(uint32_t dst, uint32_t src)
   {
       uint32_t out = 0;
       for (unsigned c = 0; c < 32; c += 8) {
           int v = (int)((src >> c) & 0xff) + (int)((dst >> c) & 0xff) - 255;
           out |= (uint32_t)MAX(v, 0) << c;
       }
       return out;
   }
   ```
3. **Pass-Level Matrix Retention**:
   Maintains `float last_m4[4]` across the pass in `agx_replay_frame` so that untextured spans in draws 3..8 correctly inherit the transformation matrix from draw 2.
4. **Horizontal Span Rasteriser**:
   - For `is_span = stride == 16 && prog == AGX_USC_SPANS`:
     - Reads vertex pairs $(x_0, y_0)$ and $(x_1, y_1)$ in screen coordinates.
     - Transforms to pass space using $m_4$ and viewport $vp$.
     - Extracts coverage alpha from byte 15 (`buf + k*stride + 15`).
     - Blends premultiplied white $(a, a, a, a)$ across the scanline with `agx_blend_over`.
5. **Blend Application**:
   - Untextured draws with `prog == AGX_USC_SOLID_PLUS_L` use `agx_blend_plus_lighter(row[x - x0], colour)`.
   - Textured draws with `prog == AGX_USC_PLUS_L || prog == AGX_USC_PLUS_L2` use `agx_blend_plus_lighter(dst, src)`.

### B. Python Offline Mirror (`backend/agx_replay_offline.py`)
- Registered `SPANS`, `PLUS_L`, `PLUS_L2`, and `SOLID_PLUS_L` program constants.
- Updated `classify_draw()` to identify `stride == 16 && prog == SPANS` as `'replayed exactly: glyph spans (stride=16)'`.
- Updated `main()` with span rasterisation loop, matrix retention, and `PLUS_L` / `SOLID_PLUS_L` additive blending.

---

## 4. Evidence and Verification

### A. Artifact Directory and Files
Evidence files are persisted in `research/evidence/S-slider-glint/`:
- `glint_sweep_strip.png`: Horizontal strip displaying the slider across 4 consecutive cycles (Frames 48, 62, 76, 90).
- `glint_sweep_vertical_strip.png`: Vertical comparative strip with labels.
- `unlock_frame_6_crop.png` .. `unlock_frame_90_crop.png`: High-resolution crops of individual cycles.

### B. Quantitative Measurements

#### 1. Black Pixel Elimination (Bar & Dash Region: $x=10..370, y=936..1010$)
| Frame | Prior Implementation | With Plus-Lighter Fix |
| --- | :---: | :---: |
| Frame 6 | 172 black pixels | **0** |
| Frame 20 | 172 black pixels | **0** |
| Frame 34 | 172 black pixels | **0** |
| Frame 48 | 172 black pixels | **0** |
| Frame 62 | 172 black pixels | **0** |
| Frame 76 | 172 black pixels | **0** |
| Frame 90 | 172 black pixels | **0** |

#### 2. Label Luminance vs Background Wallpaper ($y=960..980, x=200..500$)
| Display Frame | Wallpaper Average Luma | Label Average Luma | Label Peak (Glint) | Relative Delta |
| --- | :---: | :---: | :---: | :---: |
| Frame 6 | 139.1 | 176.0 | 255.0 | **+36.9** |
| Frame 20 | 139.1 | 174.8 | 255.0 | **+35.8** |
| Frame 34 | 139.1 | 173.8 | 255.0 | **+34.7** |
| Frame 48 | 139.1 | 171.4 | 255.0 | **+32.3** |
| Frame 62 | 139.1 | 167.7 | 255.0 | **+28.6** |
| Frame 76 | 139.1 | 166.0 | 255.0 | **+26.9** |
| Frame 90 | 139.1 | 160.3 | 255.0 | **+21.3** |

#### 3. Gradient Glint Sweep Progression Across Consecutive Cycles
| Pass (Dump) | Display Frame | Gradient Texture $x$ Coverage | Peak Position on Text |
| --- | :---: | :---: | :---: |
| Pass 47 | Frame 48 | $-481.3 \to 810.7$ | $x \approx 420..480$ |
| Pass 61 | Frame 62 | $-358.0 \to 934.0$ (+123 px) | $x \approx 460..480$ |
| Pass 75 | Frame 76 | $-204.1 \to 1087.9$ (+154 px) | $x \approx 460..500$ |
| Pass 89 | Frame 90 | $-58.2 \to 1233.8$ (+146 px) | $x \approx 500..540$ |

---

## 5. Regression & Compatibility Testing

1. **Unit Test Suite**:
   ```
   .venv/bin/python -m unittest discover -s tests
   Ran 240 tests in 3.598s -> OK (skipped=12)
   ```
2. **Corpus Replay Summary (`scripts/agx_corpus.py --corpus corpus`)**:
   - 225 dumps replayed in 3.9 s.
   - C replay: 1257 draws drawn, 0 harness errors.
   - Python audit: 1512 draws, 0 dump errors.
   - Glyph spans drawn: 73 (37 in `unlock`, 36 in `unlock-flyin`).
   - `"stride 16 outside the wallpaper program"`: **0**.
3. **Non-Slider Scenes**:
   - `calendar`: 0 target shader occurrences, 0 pixel changes.
   - `clock`: 0 target shader occurrences, 0 pixel changes. (Ready for mechanical merge with Task K's triangle rasteriser).
   - `settings`: Lock-screen frames (dumps 5, 9, 15, 17, 20) also cured of black bar/dash artefacts; subsequent Settings app screens completely unaffected.
4. **Backend Compilation**:
   - `backend/.venv/bin/python scripts/qemu/build_backend.py` finished with exit code 0.

---

## 6. Open Items / Next Steps
- Offline targets fully achieved and verified against the corpus.
- Ready for lead worker (Claude, MBP) live boot verification at merge time.
