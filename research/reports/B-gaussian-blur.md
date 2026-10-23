# B-gaussian-blur: CoreAnimation Gaussian Blur Accuracy and 1D Directional Replay

**Task ID**: `B-gaussian-blur`  
**Worktree**: `/Users/bagas/Documents/Projects/vphone/vphone-wt-blur`  
**Branch**: `wt/blur`  
**Worker**: Antigravity (`agy`)  
**Scope**: Blur code in AGX replay core of `backend/s5l8960x.c` (between `AGX_REPLAY_CORE_BEGIN` and `AGX_REPLAY_CORE_END`: `AGX_USC_BLUR_*`, `agx_blur_taps_*`, `agx_blur_1d`, `agx_extract_blur_params`, `agx_blur_texture`, and blur branches in `agx_replay_frame`), `backend/agx_replay_offline.py` blur handling and draw classification, and `tests/test_agx_gauss_blur.py`. Decoupled from concurrent work by agy U (`wt/uiswitch`).

---

## 1. Status per Goal

| Goal | Status | Evidence / Notes |
| --- | --- | --- |
| 1. Inventory & parameter recovery from shader code and constants | **PASS** | Sliced iOS 7.1.2 QuartzCore binary; disassembled `CA::OGL::BlurState::narrow_blur` and extracted GLSL shaders. Discovered all blur shaders are 1D convolutions with bilinear tap pairs. Decoded vertex uniform buffer (`vs_data + 48`) for directional offsets and fragment uniform buffer for weights. |
| 2. Replace 2-D blur with 1-D directional blur passes | **PASS** | Implemented `agx_blur_1d` in C and Python offline replay. Horizontal passes convolve strictly along $X$; vertical passes convolve strictly along $Y$. Retained 2-D fallback for legacy 6-tap shaders and stride-12 backdrops. |
| 3. Handle downsampled sources | **PASS** | Offsets in `offset[N]` are scaled by source texel dimensions `texmat0.xy`, sampled via hardware bilinear interpolation (`agx_sample_bilinear`). Correctly handles 640x64 -> 384x64 downsampling (navbar) and 128x64 passes (switch shadow). |
| 4. Identify post-blur saturation & tint passes | **PASS** | Discovered and analyzed `0x2353a3fe1ca2b71b` in `settings-all/frame-14 d0`: reads a 3x3 color matrix at `0x101a493c` to boost saturation and tint the blurred navbar backdrop before compositing to screen. |
| 5. Python audit reports blur draws as "replayed exactly" | **PASS** | Audit reports **0 approximated blur draws** (was 100% approximated). 34 blur draws replayed exactly: 12 horizontal 4-tap, 12 vertical 4-tap, 5 horizontal 7-tap, 5 vertical 7-tap. |
| 6. Chained replay smoothness & evidence crops | **PASS** | Verified chained replay of Settings navbar, switch shadow, and unlock glow. Saved before/after crops and diff images in `research/evidence/B-gaussian-blur/`. |
| 7. Unit tests pass & new tests added | **PASS** | All unit tests pass across test suites: `test_agx_gauss_blur.py`, `test_agx_navbar_blur.py`, `test_agx_accuracy.py`, `test_agx_draw_coverage.py`. Added tests for 1D directional smoothing and parameter extraction. |
| 8. QEMU backend compiles cleanly | **PASS** | `backend/.venv/bin/python scripts/qemu/build_backend.py` compiles with 0 errors and 0 unused function warnings; binary at `backend/bin/qemu-system-aarch64`. |

---

## 2. Root Cause Analysis of Previous Model

### A. The Fixed 2-D Separable Blur Defect
Previously, `backend/s5l8960x.c` and `backend/agx_replay_offline.py` modeled Gaussian blur as a fixed 2D blur (`agx_blur_texture` / `gaussian_blur_texture`) applying both horizontal and vertical passes within a single draw using hardcoded 6-tap-pair weights from QuartzCore's `BLUR_TAPS_3_5`.

In Apple's CoreAnimation pipeline, Gaussian blur is **never rendered as a 2D convolution in a single draw**. Instead, CoreAnimation executes two separate 1D passes:
1. **Pass 1 (Horizontal)**: Samples the source texture with tap offsets $(+ox_k, 0.0)$ and $(-ox_k, 0.0)$, writing into an intermediate offscreen render target.
2. **Pass 2 (Vertical)**: Samples the intermediate texture with tap offsets $(0.0, +oy_k)$ and $(0.0, -oy_k)$, writing into the final backdrop or destination surface.

When the emulator ran a full 2D blur on Pass 1 and then ran another full 2D blur on Pass 2:
- The image was blurred **4 times** (2x horizontally and 2x vertically).
- Intermediate details were obliterated by severe over-blurring.
- Radii and weights were wrong: QuartzCore selected 4-tap or 7-tap kernels depending on layer blur radius, but the emulator forced 6 taps with sigma 3.5 onto every draw.

### B. Unrecognized 1D Blur Shaders in the Corpus
The nine programs hardcoded in `BLUR_PROGRAMS` (`0x115ac3fe908dec8a`, `0x90d330877114e752`, etc.) were legacy untextured stride-12 hashes from an older probe (probe-164).
In the actual 265 corpus dumps, the textured 1D blur draws were executed with:
- `0xcda5e479d40815f` (4-tap pair 1D blur, used for label glow and status bar)
- `0xf82f9a96aff1e034` (7-tap pair 1D blur, used for navbar and switch shadow)

Because neither hash was in `BLUR_PROGRAMS`:
- The emulator did not recognize them as blur shaders at all.
- It fell through to standard textured quad rendering without any blur, or classified them in the audit as `"approximated: textured quad (bilinear scaled)"`.

---

## 3. Native QuartzCore Architecture & Shader Recovery

By slicing the guest's iOS 7.1.2 `QuartzCore` binary (`/Users/bagas/Documents/Projects/vphone/vphone-wt-blur/build/virtual-activated-profile-v5/system.raw`), we disassembled `CA::OGL::BlurState::narrow_blur` and extracted the native GLSL shaders directly from binary strings (`0x184397c08` through `0x18439c042`):

### A. The 7-Tap 1D Blur Shader (`0xf82f9a96aff1e034`)
```glsl
// Vertex Shader:
uniform highp mat4 vertex_matrix;
uniform highp vec4 texmat0;
varying highp vec2 texcoord0, texcoord1, texcoord2, texcoord3;
varying highp vec2 texcoord4, texcoord5, texcoord6, texcoord7;
varying highp vec2 texcoord8, texcoord9, texcoord10, texcoord11;
varying highp vec2 texcoord12, texcoord13;
uniform highp vec2 offset[7];

void main() {
  highp vec2 t0 = vertex_texcoord0 * texmat0.xy + texmat0.zw;
  gl_Position = vertex_matrix * vertex_position;
  texcoord0 = t0 - offset[6] * texmat0.xy;
  texcoord1 = t0 - offset[5] * texmat0.xy;
  texcoord2 = t0 - offset[4] * texmat0.xy;
  texcoord3 = t0 - offset[3] * texmat0.xy;
  texcoord4 = t0 - offset[2] * texmat0.xy;
  texcoord5 = t0 - offset[1] * texmat0.xy;
  texcoord6 = t0 - offset[0] * texmat0.xy;
  texcoord7 = t0 + offset[0] * texmat0.xy;
  texcoord8 = t0 + offset[1] * texmat0.xy;
  texcoord9 = t0 + offset[2] * texmat0.xy;
  texcoord10 = t0 + offset[3] * texmat0.xy;
  texcoord11 = t0 + offset[4] * texmat0.xy;
  texcoord12 = t0 + offset[5] * texmat0.xy;
  texcoord13 = t0 + offset[6] * texmat0.xy;
}

// Fragment Shader:
uniform lowp sampler2DRect texture0;
uniform mediump vec4 weight[2];
void main() {
  lowp vec4 q0..q13;
  // Samples q0..q13 from texcoord0..13
  mediump vec4 acc;
  acc  = (q6 + q7)   * weight[0].x; // tap pair 0 (offset[0])
  acc += (q5 + q8)   * weight[0].y; // tap pair 1 (offset[1])
  acc += (q4 + q9)   * weight[0].z; // tap pair 2 (offset[2])
  acc += (q3 + q10)  * weight[0].w; // tap pair 3 (offset[3])
  acc += (q2 + q11)  * weight[1].x; // tap pair 4 (offset[4])
  acc += (q1 + q12)  * weight[1].y; // tap pair 5 (offset[5])
  acc += (q0 + q13)  * weight[1].z; // tap pair 6 (offset[6])
  gl_FragData[0] = acc;
}
```

### B. The 4-Tap 1D Blur Shader (`0xcda5e479d40815f`)
Operates symmetrically with 4 tap pairs ($q_3..q_{10}$):
$$\text{acc} = (q_6 + q_7) w_0 + (q_5 + q_8) w_1 + (q_4 + q_9) w_2 + (q_3 + q_{10}) w_3$$

### C. Locating Blur Constants in GPU Buffers
1. **Direction and Offsets**:
   Located in the vertex uniform buffer referenced by `vs_data + 48` (`u_ptr = vsd[12] | (vsd[13] << 32)`):
   - Words 0..15: 4x4 orthographic projection matrix.
   - Words 16..: Array of 16-byte aligned `highp vec2 offset[N]` entries `(ox, oy, 0.0, 0.0)`:
     - If $|ox| > 0.01$ and $|oy| < 0.001$: **Horizontal 1D blur**.
     - If $|oy| > 0.01$ and $|ox| < 0.001$: **Vertical 1D blur**.
   - Word $16 + 4N$: `texmat0.xy = (1/width, 1/height)` (the source texel dimensions).
2. **Weights**:
   Located in the fragment PDS uniform buffer (referenced from `ppp` pointers with bit `0x80000000`):
   - $N$ IEEE 754 32-bit floats summing to $0.5$ (since each tap pair contributes 2 symmetric samples, total weight $= 2 \times 0.5 = 1.0$).

---

## 4. Complete Inventory of Blur Draws in the Corpus

Across all 265 dumps in `corpus/`, there are **34 active 1D Gaussian blur draws**:

| Scene | Frame | Pass Target | Source Tex | Stride | Program Hash | Direction | Taps | Tap Offsets ($ox_k$ or $oy_k$) | Tap Weights ($w_k$) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `settings-all` | 4 | 384x64 (`0x1053c000`) | 384x64 | 24 | `0xf82f9a96aff1e034` | Horizontal | 7 | 0.662, 2.475, 4.455, 6.435, 8.416, 10.397, 12.378 | 0.1189, 0.1413, 0.1071, 0.0693, 0.0382, 0.0180, 0.0072 |
| `settings-all` | 5 | 128x64 (`0x1071c000`) | 128x64 | 24 | `0xf82f9a96aff1e034` | Vertical | 7 | 0.662, 2.475, 4.455, 6.435, 8.416, 10.397, 12.378 | 0.1189, 0.1413, 0.1071, 0.0693, 0.0382, 0.0180, 0.0072 |
| `settings-all` | 12 | 384x64 (`0x1071c000`) | 640x64 | 24 | `0xf82f9a96aff1e034` | Horizontal | 7 | 0.662, 2.475, 4.455, 6.435, 8.416, 10.397, 12.378 | 0.1189, 0.1413, 0.1071, 0.0693, 0.0382, 0.0180, 0.0072 |
| `settings-all` | 13 | 640x64 (`0x1076c000`) | 384x64 | 24 | `0xf82f9a96aff1e034` | Vertical | 7 | 0.662, 2.475, 4.455, 6.435, 8.416, 10.397, 12.378 | 0.1189, 0.1413, 0.1071, 0.0693, 0.0382, 0.0180, 0.0072 |
| `settings-all` | 20 | 384x64 (`0x1071c000`) | 640x64 | 24 | `0xf82f9a96aff1e034` | Horizontal | 7 | 0.662, 2.475, 4.455, 6.435, 8.416, 10.397, 12.378 | 0.1189, 0.1413, 0.1071, 0.0693, 0.0382, 0.0180, 0.0072 |
| `settings-all` | 21 | 640x64 (`0x1076c000`) | 384x64 | 24 | `0xf82f9a96aff1e034` | Vertical | 7 | 0.662, 2.475, 4.455, 6.435, 8.416, 10.397, 12.378 | 0.1189, 0.1413, 0.1071, 0.0693, 0.0382, 0.0180, 0.0072 |
| `settings-all` | 28 | 128x64 (`0x1071c000`) | 128x64 | 24 | `0xf82f9a96aff1e034` | Horizontal | 7 | 0.662, 2.475, 4.455, 6.435, 8.416, 10.397, 12.378 | 0.1189, 0.1413, 0.1071, 0.0693, 0.0382, 0.0180, 0.0072 |
| `settings-all` | 29 | 128x64 (`0x10554000`) | 128x64 | 24 | `0xf82f9a96aff1e034` | Vertical | 7 | 0.662, 2.475, 4.455, 6.435, 8.416, 10.397, 12.378 | 0.1189, 0.1413, 0.1071, 0.0693, 0.0382, 0.0180, 0.0072 |
| `settings-all` | 36 | 128x64 (`0x1071c000`) | 128x64 | 24 | `0xf82f9a96aff1e034` | Horizontal | 7 | 0.662, 2.475, 4.455, 6.435, 8.416, 10.397, 12.378 | 0.1189, 0.1413, 0.1071, 0.0693, 0.0382, 0.0180, 0.0072 |
| `settings-all` | 37 | 128x64 (`0x10554000`) | 128x64 | 24 | `0xf82f9a96aff1e034` | Vertical | 7 | 0.662, 2.475, 4.455, 6.435, 8.416, 10.397, 12.378 | 0.1189, 0.1413, 0.1071, 0.0693, 0.0382, 0.0180, 0.0072 |
| `unlock` | 3, 17, 31, 45, 59, 73, 87 | 640x96 (`0x10698000`) | 640x96 | 24 | `0xcda5e479d40815f` | Horizontal | 4 | 0.638, 2.349, 4.245, 6.165 | 0.2758, 0.1857, 0.0358, 0.0027 |
| `unlock` | 4, 18, 32, 46, 60, 74, 88 | 640x96 (`0x10a00000`) | 640x96 | 24 | `0xcda5e479d40815f` | Vertical | 4 | 0.638, 2.349, 4.245, 6.165 | 0.2758, 0.1857, 0.0358, 0.0027 |
| `unlock-flyin` | 3, 9, 15, 21, 27 | 608x64 / 640x96 | 608x64 | 24 | `0xcda5e479d40815f` | Horizontal | 4 | 0.638, 2.349, 4.245, 6.165 | 0.2758, 0.1857, 0.0358, 0.0027 |
| `unlock-flyin` | 4, 10, 16, 22, 28 | 640x96 (`0x10a00000`) | 640x96 | 24 | `0xcda5e479d40815f` | Vertical | 4 | 0.638, 2.349, 4.245, 6.165 | 0.2758, 0.1857, 0.0358, 0.0027 |

Total: **34 draws** (17 horizontal, 17 vertical).

---

## 5. Post-Blur Saturation and Tint Passes

Following Pass 2 of the blur, CoreAnimation applies backdrop saturation adjustment and tinting:
- In `settings-all/frame-14 d0`:
  - **Target**: 640x1136 display framebuffer (`0x4008e0000`)
  - **Source**: 640x64 blurred navbar texture (`0x1076c000`)
  - **Program**: `0x2353a3fe1ca2b71b`
  - **Function**: Reads a 3x3 color matrix at fragment uniform `0x101a493c`:
    $$\begin{bmatrix} 0.3259 & -0.1144 & -0.0116 \\ -0.0340 & 0.2456 & -0.0116 \\ -0.0340 & -0.1144 & 0.3484 \end{bmatrix}$$
    This matrix multiplies RGB colors to boost color saturation and apply the iOS 7 translucent frosted-glass tint.

---

## 6. Implementation & Code Scope

All changes in `backend/s5l8960x.c` were kept strictly between `AGX_REPLAY_CORE_BEGIN` and `AGX_REPLAY_CORE_END` inside the blur functions to prevent merge conflicts with concurrent work (e.g. agy U):
1. **Programs Added**:
   `AGX_USC_BLUR_4TAP` (`0x0cda5e479d40815f`) and `AGX_USC_BLUR_7TAP` (`0xf82f9a96aff1e034`).
2. **Convolution Engine**:
   Replaced hardcoded 2D convolution with `agx_blur_1d(buf_in, buf_tmp, w, h, offsets, weights, n_taps, is_vertical)`.
   `agx_blur_texture` remains as a convenience 2-D wrapper for tests and legacy fallbacks.
3. **Parameter Extraction**:
   Implemented `agx_extract_blur_params` in C and `extract_blur_params` in Python: reads offsets and direction from `vs_data + 48`, weights from fragment PDS uniform buffers, and falls back to program defaults if unmapped.
4. **Drawing Loops Updated**:
   Both textured blur draws and untextured stride-12 backdrop blur draws invoke `agx_extract_blur_params` and execute the 1-D convolution pass in its native direction.

---

## 7. Evidence & Verification

All evidence artifacts are stored under `research/evidence/B-gaussian-blur/`:
- `settings_all_frame12_before.png` vs `settings_all_frame12_after.png` (diff: `settings_f12_diff.png`, 1,345 pixels smoothed by 7-tap H-blur)
- `settings_all_frame13_before.png` vs `settings_all_frame13_after.png` (diff: `settings_f13_diff.png`, 3,094 pixels smoothed by 7-tap V-blur)
- `settings_navbar_before.png` vs `settings_navbar_after.png` (navbar frosted composite)
- `settings_switch_before.png` vs `settings_switch_after.png` (switch shadow blur)
- `unlock_statusbar_before.png` vs `unlock_statusbar_after.png` (status bar backdrop)
- `unlock_slider_before.png` vs `unlock_slider_after.png` (label glow)

### Audit Comparison
- **Before**:
  `40  approximated: vibrancy (Rec.709 luma-weighted tint)`
  `247 approximated: textured quad (bilinear scaled)`
- **After**:
  `12  replayed exactly: 1D Gaussian blur (horizontal, 4-tap)`
  `12  replayed exactly: 1D Gaussian blur (vertical, 4-tap)`
  ` 5  replayed exactly: 1D Gaussian blur (horizontal, 7-tap)`
  ` 5  replayed exactly: 1D Gaussian blur (vertical, 7-tap)`
  ` 0  approximated blur draws` (all 34 active blur draws replayed exactly).

---

## 8. Test Execution Results

```text
$ .venv/bin/python -m unittest tests/test_agx_gauss_blur.py tests/test_agx_navbar_blur.py tests/test_agx_accuracy.py tests/test_agx_draw_coverage.py
Ran 19 tests in 0.340s
OK (skipped=2)

$ backend/.venv/bin/python scripts/qemu/build_backend.py
[7/7] Generating qemu-system-aarch64 with a custom command
Built experimental backend: /Users/bagas/Documents/Projects/vphone/vphone-wt-blur/backend/bin/qemu-system-aarch64
```
All unit tests pass and QEMU compiles cleanly.
