# Safari & WebKit AGX GPU Render Evidence (2026-10-27)

## 1. Problem Description
In Safari and WebKit HTML rendering on iOS 7.1.2 under the emulated AGX GPU, text glyphs, layer textures, and UI elements rendered corrupted, missing, or erased against light backgrounds (whereas SpringBoard/Settings rendered acceptably).

## 2. Root Cause Analysis
Two primary rendering pipeline defects were identified in `backend/s5l8960x.c` and `backend/agx_replay_offline.py`:

1. **PowerVR Rogue Format 3 (8-bit A8 / L8 / R8) Texture Rejection**:
   - WebKit and CoreGraphics text rendering frequently uses 8-bit glyph atlas textures (`fmt = tsw[0] >> 28 == 3`).
   - The renderer previously asserted `fmt == 6` (32-bit BGRA), completely skipping format 3 textures.
   - Furthermore, `agx_texel_gva`, `agx_texture_row`, and `texel` assumed 4 bytes per pixel everywhere. For format 3, bytes per pixel is 1 (`bpp = 1`), with Morton twiddling indexing single-byte values rather than 4-byte words.

2. **Alpha-Mask / Coverage Glyph Tint Modulation Defect**:
   - In Safari Favorites and CoreGraphics glyph drawing, text labels are rendered with textures where `RGB == 0` and `Alpha = coverage` (or format 3 alpha coverage unpacked to alpha channel).
   - The vertex color carries the tint (e.g. `(128, 128, 128, 255)` for secondary text).
   - The tint modulation path previously computed `dst = (src_channel * tint_channel) / 255`. When `src_rgb == 0`, this multiplied to 0, outputting `(0, 0, 0, alpha)` instead of modulating the vertex color by coverage. Under standard premultiplied alpha src-over blending, `(0, 0, 0, a)` drew black silhouettes or erased text over light backgrounds.
   - For alpha-mask textures (`(src & 0x00ffffff) == 0 && (src >> 24) != 0`), the output RGB must be computed as:
     ```c
     a = ((src >> 24) * tint_a) / 255;
     r = (tint_r * a) / 255;
     g = (tint_g * a) / 255;
     b = (tint_b * a) / 255;
     ```

## 3. Evidence Images
- `safari_apple_label_before.png`: Baseline rendering with format 3 skipped (text missing, background (241, 241, 241)).
- `safari_apple_label_after.png`: Replay with format 3 A8 texture decode and coverage tint modulation (clean gray text).
- `safari_apple_label_live.png`: Ground truth framebuffer from live guest probe at 121s.
- `safari_apple_label_diff.png`: Absolute difference magnified 4x (region MAE: **0.4383**).
- `safari_frame27_after.png`: Full frame 640x1136 emulated GPU replay.
- `safari_frame27_live.png`: Full frame 640x1136 live guest framebuffer (full-frame MAE: **2.36**).
- `safari_a8_atlas.png`: Decoded and untwiddled 256x256 format 3 glyph atlas.
