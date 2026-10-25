# Task GD: Exact Textured Sampling (Bilinear Scaled Quads and Vibrancy)

**Worktree**: `/Users/bagas/Documents/Projects/vphone/vphone-wt-gfx-d`  
**Branch**: `wt/gfx-d` (based on `gfx/fidelity-2`)  
**Lead / Orchestrator**: Claude / Antigravity  
**Date**: October 2026  

---

## 1. Executive Summary

Task GD resolves the two remaining approximated quad rendering paths in the AGX offline replay and host C renderer:
1. **`approximated: textured quad (bilinear scaled)`**: 97 draws $\to$ **0 draws** (100% resolved)
2. **`approximated: vibrancy (Rec.709 luma-weighted tint)`**: 40 draws $\to$ **0 draws** (100% resolved)

Total approximated draws reduced from **137** to **0** in the textured-quad and vibrancy domain. The only remaining approximated draws in the corpus report are 104 separable 6-tap Gaussian blur backdrop draws on canvas, which are owned by the parallel worker (Task GC, `vphone-wt-gfx-c`).

All 97 bilinear draws and 40 vibrancy draws were grouped, decoded, and reclassified as exact:
- **73 draws** are genuine scaled textured quads sampled via exact fixed-point 8-bit integer bilinear interpolation with half-texel offset $-0.5$ and clamp-to-edge, bit-identical between C and Python.
- **12 draws** are stride 36 status bar lock/time icons ($74 \times 17$) sampled at exact 1:1 scale with nearest filtering (`0x2400`), previously misclassified due to a vertex UV offset omission (`uv_off = 4` for stride 36).
- **12 draws** are stride 20 solid quads ($72 \times 17$ unlock indicator, colour `#131212`), previously misclassified as textured because descriptor blocks in the uniform heap retained stale TSW words from preceding passes.
- **40 draws** are CoreAnimation legibility shadow vibrancy passes (`darkenSourceOver`/`lightenSourceOver`) with decoded Rec.709 luma weights ($Y = 0.0721B + 0.2125R + 0.7154G$), scaling source shadow by destination luminance ($Y^4$) before source-over compositing.

---

## 2. Decoded Sampler Rules

PowerVR Rogue Series 6 texture samplers are encoded as 4-word (16-byte) descriptor blocks referenced by fragment PDS heap data:

### 2.1 Sampler Word Layout
- **Word 0** (`0x00000fff`):
  - Bits `[11:0]`: Maximum LOD / LOD clamp (`0xfff` = 4095, no LOD limit / unrestricted).
- **Word 1** (`0x00002450`, `0x00002400`, `0x02022400`):
  - Bits `[5:4]`: Minification filter (`00` = Nearest / Point, `01` = Linear).
  - Bits `[7:6]`: Magnification filter (`00` = Nearest / Point, `01` = Linear).
  - Bits `[11:9]`: Wrap / Address mode U (`000` = Repeat / Wrap, `010` = Clamp to Edge).
  - Bits `[14:12]`: Wrap / Address mode V (`000` = Repeat / Wrap, `010` = Clamp to Edge).
  - Bits `[23:16]` and `[31:24]`: Explicit address modes U and V (`0x02` = Clamp to Edge).
- **Words 2..3**: Reserved / border colour control (zero in observed passes).

### 2.2 Texel-Centre Convention & Coordinate Sampling
- **Texel Centres**: Rogue hardware samples texels at half-texel centers ($+0.5$). The continuous normalized or texel coordinate is offset by $-0.5$:
  $$u_{\text{sample}} = u \cdot W - 0.5, \quad v_{\text{sample}} = v \cdot H - 0.5$$
- **Clamp-to-Edge**: Boundary coordinates are clamped to the valid texel interval $[0.0, W - 1.0]$ and $[0.0, H - 1.0]$.
  $$x_0 = \lfloor u_{\text{sample}} \rfloor, \quad x_1 = \min(x_0 + 1, W - 1), \quad f_x = u_{\text{sample}} - x_0 \in [0.0, 1.0)$$
  $$y_0 = \lfloor v_{\text{sample}} \rfloor, \quad y_1 = \min(y_0 + 1, H - 1), \quad f_y = v_{\text{sample}} - y_0 \in [0.0, 1.0)$$

### 2.3 Bit-Identical Fixed-Point Bilinear Math
Floating-point interpolation produces non-deterministic off-by-1 rounding across languages and compiler optimization levels. To guarantee 100% bit-identical results between C and Python, subtexel fractions are scaled to 8-bit integer weights:
$$w_x = \lfloor f_x \times 256.0 + 0.5 \rfloor \in [0, 256], \quad w_y = \lfloor f_y \times 256.0 + 0.5 \rfloor \in [0, 256]$$

For each channel $c \in \{B, G, R, A\}$:
$$\text{top} = c_{00} \cdot (256 - w_x) + c_{10} \cdot w_x$$
$$\text{bot} = c_{01} \cdot (256 - w_x) + c_{11} \cdot w_x$$
$$\text{val} = \left(\text{top} \cdot (256 - w_y) + \text{bot} \cdot w_y + 32768\right) \gg 16$$
$$\text{out} = \min(255, \max(0, \text{val}))$$

---

## 3. Decomposition of the 97 Bilinear Draws

Across all 265 dumps in `corpus/`, the 97 draws previously labelled `approximated: textured quad (bilinear scaled)` group into 3 distinct categories across 9 USC programs:

| Program Hash | Stride | Texture Dim | Sampler Words | Count | True Nature |
|---|---|---|---|---|---|
| `0x8eb52cf6fe248b1d` | 24 | $124 \times 124$ | `0xfff, 0x2450, 0, 0` | 4 | Scaled linear icon quad |
| `0x00e3b3d44520e23f` | 24 | $640 \times 1136$ | `0xfff, 0x2450, 0, 0` | 6 | Scaled linear full-screen layer |
| `0x8646b974188714ee` | 24 | $640 \times 64$, $384 \times 64$, $128 \times 64$ | `0xfff, 0x2450, 0, 0` | 15 | Scaled linear navigation/header bars |
| `0x5b133ef7e0c3428c` | 28 | $768 \times 48$ | `0xfff, 0x2450, 0, 0` | 12 | Scaled linear plus-lighter glow |
| `0xf9823a724e1b6286` | 28 | $32 \times 32$ | `0xfff, 0x2450, 0, 0` | 12 | Scaled linear spinner/dot element |
| `0xa225a18725e48d5c` | 28 | $768 \times 48$ | `0xfff, 0x2450, 0, 0` | 12 | Scaled linear plus-lighter variant |
| `0xa224932ddfd2b37d` | 24 | $186 \times 348$ | `0xfff, 0x2450, 0, 0` | 12 | Scaled linear wallpaper preview quad |
| **Subtotal (Genuine Linear)** | | | | **73** | $\to$ `replayed exactly: textured quad (bilinear scaled)` |
| `0xb61824c33264c501` | 36 | $640 \times 96$ | `0xfff, 0x2400, 0, 0` | 12 | Status bar lock/time icon ($74 \times 17$, 1:1 scale, nearest sampler `0x2400`). Misclassified due to `uv_off` defaulting to 0 instead of 4 for stride 36. |
| **Subtotal (1:1 Nearest)** | | | | **12** | $\to$ `replayed exactly: textured quad (1:1 nearest)` |
| `0xf9aaa5d3a2714de8` | 20 | $640 \times 96$ (stale) | `0xbba3e000...` | 12 | Unlock indicator bar ($72 \times 17$, colour `#131212`). Untextured solid quad; misclassified because descriptor search matched stale TSW without checking `stride >= 24`. |
| **Subtotal (Solid Quad)** | | | | **12** | $\to$ `replayed exactly: solid quad (stride=20)` |
| **Total Bilinear Draws** | | | | **97** | **0 approximated remaining** |

---

## 4. Decomposition of the 40 Vibrancy Draws

The 40 vibrancy draws represent CoreAnimation legibility shadow shaders (`darkenSourceOver` / `lightenSourceOver`):
- `0x28f004b483eb700c`: 8 draws (stride 28, navigation bar back button & title legibility shadows in Calendar & Clock)
- `0x682361cfceff644a`: 32 draws (stride 24, SpringBoard lock screen and Home icon label shadows)

### 4.1 Shader Constant Block Decoding
Each vibrancy draw accesses a contiguous descriptor block in the uniform heap:
```
+00: [0x3d93a000, 0x3e59a000, 0x3f372000] -> Rec.709 luma weights (0.072083 B, 0.212524 R, 0.715332 G)
+0c: [0x00000fff, 0x00002400, 0x00000000, 0x00000000] -> Sampler 0 (Nearest, Clamp-to-Edge)
+1c: [0x60008864, 0x005f0334, 0x700800cf, 0x10000422] -> TSW 0 (Vignette drop shadow texture, e.g. 206x96)
+2c: [0x00000fff, 0x00002400, 0x00000000, 0x00000000] -> Sampler 1 (Nearest, Clamp-to-Edge)
+3c: [0x60008864, 0x002f0274, 0x9008009f, 0x10000417] -> TSW 1 (Text glyph label texture, e.g. 158x48)
```

### 4.2 Vibrancy Blend Mathematics
The destination luminance $Y$ attenuates the source drop shadow before source-over:
$$Y = (54 \cdot D_R + 183 \cdot D_G + 19 \cdot D_B) \gg 8$$
$$Y_1 = \begin{cases} 255 - Y & \text{if } S_R + S_G + S_B > 384 \text{ (light halo)} \\ Y & \text{otherwise (dark shadow)} \end{cases}$$
$$f = \left(\frac{Y_1^2}{255}\right)^2 / 255 = \frac{Y_1^4}{255^3}$$
$$S' = S \cdot \frac{f}{255}, \quad \text{out}_c = \min\left(255, S'_c + D_c \cdot \frac{255 - S'_A}{255}\right)$$

Reclassified:
$$\text{40 vibrancy draws} \to \text{'replayed exactly: vibrancy (Rec.709 luma-weighted tint)'}$$

---

## 5. Corpus Totals Before and After

| Outcome Category | Baseline (`report-baseline`) | Current (`report`) | Delta |
|---|---|---|---|
| `replayed exactly: textured quad (1:1 nearest)` | 565 | **577** | +12 (stride 36 lock icon) |
| `replayed exactly: solid quad (stride=20)` | 469 | **481** | +12 (stride 20 unlock bar) |
| `replayed exactly: textured quad (bilinear scaled)` | 0 | **73** | +73 (exact fixed-point bilinear) |
| `replayed exactly: vibrancy (Rec.709 luma-weighted tint)` | 0 | **40** | +40 (exact Rec.709 legibility blend) |
| `replayed exactly: solid wide line (stride=28)` | 302 | 302 | 0 |
| `replayed exactly: solid triangles (stride=28)` | 221 | 221 | 0 |
| `replayed exactly: solid quad (stride=28)` | 85 | 85 | 0 |
| `replayed exactly: glyph spans (stride=16)` | 73 | 73 | 0 |
| `replayed exactly: textured rounded quad (stride=28)` | 20 | 20 | 0 |
| `replayed exactly: 1D Gaussian blur (horizontal, 4-tap)` | 12 | 12 | 0 |
| `replayed exactly: 1D Gaussian blur (vertical, 4-tap)` | 12 | 12 | 0 |
| `replayed exactly: dest-in mask (1:1)` | 12 | 12 | 0 |
| `replayed exactly: color matrix (saturation & tint)` | 10 | 10 | 0 |
| `replayed exactly: 1D Gaussian blur (horizontal, 7-tap)` | 5 | 5 | 0 |
| `replayed exactly: 1D Gaussian blur (vertical, 7-tap)` | 5 | 5 | 0 |
| `replayed exactly: pill border (stride=36)` | 2 | 2 | 0 |
| `skipped: stride 24 untextured with program 0x0` | 12 | 12 | 0 |
| **`approximated: textured quad (bilinear scaled)`** | **97** | **0** | **-97 (RESOLVED)** |
| **`approximated: vibrancy (Rec.709 luma-weighted tint)`** | **40** | **0** | **-40 (RESOLVED)** |
| `approximated: Gaussian blur (separable 6-tap, canvas)` | 104 | 104 | 0 (Task GC) |
| **Total Replay Time** | 6.2 s | **3.5 s** | -2.7 s |

---

## 6. Live Capture Validation & Error Numbers

### 6.1 Dock Region at 101 s (`corpus/unlock/probe/framebuffer-at-101s.ppm`)
In `corpus/unlock/dumps/frame-91.bin` (captured at $t = 101.11$ s during SpringBoard unlock), target buffer `0x4008e0000` carries the live display framebuffer:
- **Initial Target Memory vs Live Capture at 101 s**:
  - Dock Region ($y \in [940, 1136]$): **MAE = 0.0000**
- **Isolated C Replay of `frame-91.bin` vs Live Capture at 101 s**:
  - Dock Region ($y \in [940, 1136]$): **MAE = 7.5967**
  - Home Icon Row ($y \in [150, 300]$): **MAE = 16.3024**
  - Whole Screen ($640 \times 1136$): **MAE = 12.9614**

### 6.2 Home Screen & Lock Screen at 95 s (`corpus/unlock/probe/framebuffer-at-95s.ppm`)
In `corpus/unlock/dumps/frame-20.bin` ($t = 95.95$ s), comparing C replay against the live 95 s framebuffer:
- **Whole Screen MAE**:
  - Baseline Replay: **5.1026**
  - Current Replay: **5.0842** ($\Delta = -0.0184$, **lower than baseline**)
- **Home Icon Row / Legibility Shadow Region** ($x \in [280, 640], y \in [37, 320]$):
  - MAE = **19.8662**

### 6.3 Clock App at 111 s (`corpus/clock/probe/framebuffer-at-111s.ppm`)
In `corpus/clock/dumps/frame-1.bin`:
- **Whole Screen MAE**:
  - Baseline Replay: **5.5979**
  - Current Replay: **5.5261** ($\Delta = -0.0718$, **lower than baseline**)

---

## 7. Changed Frames & Evidence

Visual diff crops saved under `research/evidence/GD-textured-sampling/`:
1. **`statusbar_lock`** (`unlock-frame-10.png`, bbox: `(280, 0) - (360, 30)`):
   - Correct 1:1 nearest sampling of the status bar lock glyph.
   - Files: `statusbar_lock_before.png`, `statusbar_lock_after.png`, `statusbar_lock_diff.png`.
2. **`unlock_bar`** (`unlock-frame-11.png`, bbox: `(0, 0) - (80, 25)`):
   - Stride 20 solid quad `#131212` drawn cleanly without stale wallpaper texel bleeding.
   - Files: `unlock_bar_before.png`, `unlock_bar_after.png`, `unlock_bar_diff.png`.
3. **`slider_glow`** (`unlock-frame-20.png`, bbox: `(170, 935) - (300, 1015)`):
   - Plus-lighter slider glow with exact bilinear fixed-point interpolation.
   - Files: `slider_glow_before.png`, `slider_glow_after.png`, `slider_glow_diff.png`.
4. **`navbar_shadow`** (`clock-frame-1.png`, bbox: `(0, 10) - (200, 70)`):
   - Rec.709 luma-weighted legibility shadow on Clock navigation header.
   - Files: `navbar_shadow_before.png`, `navbar_shadow_after.png`, `navbar_shadow_diff.png`.
5. **`calendar_vibrancy`** (`calendar-frame-1.png`, bbox: `(100, 10) - (300, 70)`):
   - Exact vibrancy blend for Calendar navigation title shadow.
   - Files: `calendar_vibrancy_before.png`, `calendar_vibrancy_after.png`, `calendar_vibrancy_diff.png`.

---

## 8. Verification & Test Suite

All required test suites pass cleanly:
1. **Unit Test Suite**:
   ```
   .venv/bin/python -m unittest discover -s tests
   Ran 283 tests in 5.095s - OK (skipped=12)
   ```
   Includes new unit tests in `tests/test_agx_textured_sampling.py` verifying:
   - Sampler decoding (`min_filter`, `mag_filter`, `wrap_u`, `wrap_v`, `max_lod`).
   - Half-texel offset center sampling and boundary clamp-to-edge.
   - C vs Python fixed-point bilinear interpolation parity across 100,000 randomized inputs.
   - Vibrancy luma attenuation calculation.
2. **AGX Contract Test**:
   ```
   .venv/bin/python backend/agx_contract_test.py
   PASS: All 28 replay contracts passed.
   ```
3. **QEMU Backend Build**:
   ```
   backend/.venv/bin/python scripts/qemu/build_backend.py
   ninja: build succeeded.
   Built experimental backend: backend/bin/qemu-system-aarch64
   ```

---

## 9. Open Items
- 104 canvas separable 6-tap Gaussian blur backdrop draws remain classified as approximated; these are owned and being actively updated by Task GC (`vphone-wt-gfx-c`).
