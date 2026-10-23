# Task U: UISwitch track (Settings → Airplane Mode)

**Task ID**: `U-uiswitch`  
**Worktree**: `/Users/bagas/Documents/Projects/vphone/vphone-wt-uiswitch`  
**Branch**: `wt/uiswitch`  
**Author / Worker**: Antigravity (`agy`)  
**Scope**: AGX replay core in `backend/s5l8960x.c` (strictly between `AGX_REPLAY_CORE_BEGIN` and `AGX_REPLAY_CORE_END`), `backend/agx_replay_offline.py`, `tests/test_agx_*`. Strictly offline (no QEMU boots).

---

## 1. Status per Goal

| Goal | Status | Evidence / Notes |
| --- | --- | --- |
| **1. Set up corpus view and verify chained replay** | **PASS** | Symlinked corpus scenes into `corpus/` including new scene `settings-all` (40 dumps across display and offscreen passes, live capture `probe/framebuffer-at-145s.ppm`). Chained replay of 265 dumps across 6 scenes runs in 4.6 s with 0 harness errors. |
| **2. Identify defect and missing/skipped switch draw** | **PASS** | Switch geometry isolated at $x \in [508, 610], y \in [177, 239]$ ($102 \times 62\text{ px}$ pill). Draw 63 renders the untextured pill border (`prog 0x62a99af8d8beb332`, stride 36, 48 vertices / 8 quads, color `#E6E6E6`). Both C replay (`AGX_REPLAY_SKIP("untextured with unsupported stride")`) and Python audit (`'skipped: untextured with stride not in (20, 28)'`) skipped this draw entirely. |
| **3. Implement distance-field pill border rasterizer in C and Python** | **PASS** | Implemented analytic dual-radius distance field rasterizer in `backend/s5l8960x.c` (lines 5515 & 5985..6110, within `AGX_REPLAY_CORE`) and in `backend/agx_replay_offline.py`. Supports bilinear UV interpolation, subpixel anti-aliased edge coverage, scissor clipping, and alpha compositing. |
| **4. Verify chained replay and crop visual fidelity** | **PASS** | Chained replay of `settings-all` display passes (e.g. Frame 1 at $t=152.98\text{ s}$ and Frame 9 at $t=153.36\text{ s}$) now renders the complete white pill track with the subtle light grey border (`#E6E6E6`) seamlessly connected around the knob. Evidence crops saved in `research/evidence/U-uiswitch/`. |
| **5. Zero regressions across full corpus** | **PASS** | Replayed all 265 dumps across 6 scenes (`calendar`, `clock`, `settings`, `settings-all`, `unlock`, `unlock-flyin`). C skips dropped from 118 to 116 (Frame 1 and Frame 9 in `settings-all` dropped to 0 skips). C draws increased from 1775 to 1777 (+2 draws). Zero pixel differences outside the switch bounding box. |
| **6. Unit tests pass & new tests added** | **PASS** | Added `tests/test_agx_pill_border.py` (5 test cases covering classification, radial math, C source boundary checks, compiled C standalone execution, and corpus validation). Full test suite passes: 256 passed, 0 failed, 12 skipped. |
| **7. Clean backend build** | **PASS** | `backend/.venv/bin/python scripts/qemu/build_backend.py` compiled cleanly to `backend/bin/qemu-system-aarch64`. |
| **8. Obey offline constraint** | **PASS** | Strictly offline; zero QEMU guest boots executed by this worker (agy D owns QEMU). |

---

## 2. Root Cause Analysis & Technical Findings

### A. UISwitch Architecture in iOS 7 (Settings)
On iOS 7 (Retina 2x display), an off-state `UISwitch` has a standard size of $51 \times 31\text{ pt}$ ($102 \times 62\text{ px}$). It consists of:
1. **White pill track well**: A rounded capsule (straight top and bottom edges of width $40\text{ px}$ with semicircular end caps of radius $R_{out} = 31\text{ px}$).
2. **Light grey track border**: An outer stroke of width $1.5\text{ pt}$ ($3\text{ px}$ at 2x) in `#E6E6E6` / `#E5E5E5` (`0xffe6e6e6`), running around the entire perimeter of the pill.
3. **Circular white knob**: A solid white circular thumb (radius $\approx 28\text{ px}$, diameter $\approx 56\text{ px}$) positioned on the left side ($x \in [508, 564]$).
4. **Soft drop shadow**: A blurred radial shadow cast below and around the knob.

### B. Defect Isolation in Settings Render Passes
In `settings-all/frame-1.bin` (pass 1, display pass at $t = 152.98\text{ s}$, target `0x400470000`, 68 draws):
- **Draw 62** (solid quad, stride 20): Background fill behind table rows.
- **Draw 63** (untextured, **stride 36**, 48 vertices, prog `0x62a99af8d8beb332`):
  - Vertex format: 8 quads ($8 \times 6 = 48$ vertices).
  - Vertex layout:
    - `+0`: $(x, y)$ screen coordinate (float32).
    - `+8`: $(z, w)$ depth / clip coordinates.
    - `+16`: RGBA color `0xffe6e6e6` (#E6E6E6 light grey).
    - `+20`: $(u_1, v_1)$ float32 (coordinates for outer pill radius $R_{out} = 31\text{ px}$, normalized such that $r_1^2 = u_1^2 + v_1^2 \le 1.0$).
    - `+28`: $(u_2, v_2)$ float32 (coordinates for inner pill radius $R_{in} = 28\text{ px}$, normalized such that $r_2^2 = u_2^2 + v_2^2 \ge 1.0$).
  - Scissor: `scissor[0]: x = 539..610, y = 177..239` (exact right half of the switch track).
  - Defect: **Skipped by C replay** as `AGX_REPLAY_SKIP("untextured with unsupported stride")` and Python audit as `'skipped: untextured with stride not in (20, 28)'`.
- **Draw 64** (untextured, stride 20, prog `0x160032e53f191b40`):
  - Scissor: `scissor[1]: x = 508..539, y = 177..239` (left half of switch well under the knob).
  - Replayed as solid triangles, rendering the white interior and grey left border under the knob.
- **Draw 65** (textured, stride 28, prog `0xd097b6a125ab5471`):
  - Composites the circular white knob and blurred drop shadow from texture `0x10524000` ($114 \times 87\text{ px}$).

Because Draw 63 was skipped, only Draw 64 (under the knob) and Draw 65 (the knob and shadow) rendered. The entire right half of the pill track ($x \in [539, 610]$) was completely unpainted, leaving only the bare table cell background.

---

## 3. Mathematical Model & Implementation

### A. Distance-Field Anti-aliasing Formulation
Each quad in Draw 63 represents a segment of the pill border. The USC shader computes coverage based on two concentric radius thresholds:
- Outer radius limit: $r_1 = \sqrt{u_1^2 + v_1^2} \le 1.0$
- Inner radius limit: $r_2 = \sqrt{u_2^2 + v_2^2} \ge 1.0$

Subpixel anti-aliasing is achieved by computing smooth transition margins $\text{scale}_1$ and $\text{scale}_2$:
$$\text{scale}_1 = \max\left(\frac{\Delta u_1}{q_w}, \frac{\Delta v_1}{q_h}\right) \approx \frac{1}{31}$$
$$\text{scale}_2 = \max\left(\frac{\Delta u_2}{q_w}, \frac{\Delta v_2}{q_h}\right) \approx \frac{1}{28}$$

The coverage factor $\text{cov} \in [0.0, 1.0]$ at screen pixel center $(c_x, c_y) = (x + 0.5, y + 0.5)$ is:
$$\text{cov}_{outer} = \text{clamp}\left(0.5 - \frac{r_1 - 1.0}{\text{scale}_1}, 0.0, 1.0\right)$$
$$\text{cov}_{inner} = \text{clamp}\left(0.5 + \frac{r_2 - 1.0}{\text{scale}_2}, 0.0, 1.0\right)$$
$$\text{cov} = \text{cov}_{outer} \times \text{cov}_{inner}$$

When $\text{cov} > 0$, the base border color `0xffe6e6e6` is premultiplied by $\text{cov}$ and composited onto the framebuffer via standard `agx_blend_over(dst, src)`.

### B. Code Changes
1. **`backend/s5l8960x.c`**:
   - Defined `#define AGX_USC_PILL_BORDER UINT64_C(0x62a99af8d8beb332)` between `AGX_REPLAY_CORE_BEGIN` and `AGX_REPLAY_CORE_END`.
   - In untextured dispatch: checked `bool is_pill = stride == 36 && prog == AGX_USC_PILL_BORDER;` to bypass unsupported stride skip.
   - Implemented quad corner identification, bilinear UV interpolation, distance field coverage calculation, scissor clipping, and row span writing.
2. **`backend/agx_replay_offline.py`**:
   - Added `PILL_BORDER = 0x62a99af8d8beb332`.
   - Updated `classify_draw()` to return `'replayed exactly: pill border (stride=36)'`.
   - Added corresponding distance-field rasterizer loop in `main()`.
3. **`tests/test_agx_pill_border.py`**:
   - New unit tests verifying classification, coverage arithmetic, boundary placement in `s5l8960x.c`, standalone C execution, and corpus validation.

---

## 4. Evidence & Comparison

### Evidence Files
- Before fix (Draw 63 skipped): `research/evidence/U-uiswitch/before-switch-crop.png`
- After fix (C replay): `research/evidence/U-uiswitch/after-switch-crop.png`
- Frame 1 full switch crop: `research/evidence/U-uiswitch/c-replay-frame-1-crop.png`
- Frame 9 (scrolled) switch crop: `research/evidence/U-uiswitch/c-replay-frame-9-crop.png`
- Knob + shadow texture: `research/evidence/U-uiswitch/tex_10524000.png`
- Live reference capture at 145 s: `research/evidence/U-uiswitch/live-145s-crop.png`

### Visual Analysis
- **Before**: The switch appears truncated; the white circular knob stands on the left, but to its right ($x = 539..610$) is pure blank white with no border, looking like an incomplete or "headless" control.
- **After**: The light grey `#E6E6E6` border curves smoothly from the knob edge ($x = 539$), extends horizontally along the top ($y = 177$) and bottom ($y = 239$) edges, and rounds out at the right cap ($x = 610$), forming the classic iOS 7 off-state switch pill.
- **Pixel difference**: Exactly 606 pixels within the switch bounding box changed, with maximum RGB change of 25 (white `#FFFFFF` (255) to light grey `#E6E6E6` (230)). Zero pixels changed anywhere else in the frame.

---

## 5. Corpus & Verification Summary

- **Corpus replay**:
  ```
  265 dumps in 4.6 s -> corpus/report/index.html
  C replay: 1777 draws drawn, 0 harness errors
    C skipped   116  untextured with unsupported stride
  Python audit: 2046 draws, 0 dump errors
      429  replayed exactly: textured quad (1:1 nearest)
      389  replayed exactly: solid quad (stride=20)
      308  replayed exactly: solid triangles (stride=28)
      302  replayed exactly: solid wide line (stride=28)
      247  approximated: textured quad (bilinear scaled)
      116  skipped: untextured with stride not in (20, 28)
       80  replayed exactly: solid triangles (stride=20)
       73  replayed exactly: glyph spans (stride=16)
       48  skipped: non-axis-aligned or rotated geometry
       40  approximated: vibrancy (Rec.709 luma-weighted tint)
       12  replayed exactly: dest-in mask (1:1)
        2  replayed exactly: pill border (stride=36)
  ```
- **Settings frames**:
  - `settings-all/frame-1 t=152.98s`: C skipped **0**, Python skipped 1
  - `settings-all/frame-9 t=153.36s`: C skipped **0**, Python skipped 1
- **Backend build**: `backend/.venv/bin/python scripts/qemu/build_backend.py` -> exit code 0 (`backend/bin/qemu-system-aarch64` built).
- **Unit test suite**: `.venv/bin/python -m unittest discover -s tests` -> 256 tests, 0 failures, 12 skipped.

Co-Authored-By: Antigravity <agy@deepmind.com>
