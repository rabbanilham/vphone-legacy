# G2-lock-gfx: Lock-screen graphics, clock/date, and live Home screen

**Task ID**: `G2-lock-gfx`  
**Worktree**: `/Users/bagas/Downloads/vphone-wt-lock-gfx`  
**Branch**: `wt/lock-gfx`  
**Worker**: Antigravity (`agy`) continuing Codex WIP (commit `a77b241`)  
**Base Commit**: `a77b241` ("WIP from Codex: AGX replay snapshot, queue cursor, wallpaper pitch, and stamp waits")  

---

## 1. Status per Goal

| Goal | Status | Evidence / Notes |
| --- | --- | --- |
| Lock screen: wallpaper | **PASS (live)** | Star-field wallpaper properly rendered live (`live-probe-g2-r2/lock-75.png`, `lock-90.png`). |
| Lock screen: clock and date | **PASS (live)** | Clock digits, colon, and date string ("Thursday, October 23") render cleanly on live lock screen (`live-probe-g2-r2/lock-75.png`, `lock-90.png`). |
| Home after unlock, live | **PASS (live)** | Full Home screen renders live with all 4 rows of apps, labels, dock icons, dock divider, and status bar (`live-probe-g2-r2/home-115.png`, `home-140.png`). |
| Clipping / Scissor decoding | **PASS** | Decoded 3D command `+0x50` scissor table base, PPP ISP control `scenable`, DB scissor index `scindex`, and region clip `rc0/rc1` bounds. Implemented in live C replay (`s5l8960x.c`) and offline mirror (`agx_replay_offline.py`). |
| Untextured `stride == 28` draws | **PASS** | Allowed `stride == 28` untextured draws with vertex color at offset 16, which renders vector clock characters and UI elements. |
| AGX Scissor Contract test | **PASS** | Added synthetic `'scissor-clip'` case in `backend/agx_contract_test.py`. Verifies clipped redraws touch only pixels within the scissor region. |
| Wallpaper colour channel order | **PASS** | Verified 32bpp BGRA little-endian channel order matching between source texture and display framebuffer. |
| Unit tests & Regression tests | **PASS** | 227 passed, 7 skipped (`python -m unittest discover -s tests`). |
| Contract test suite | **PASS** | All 15 AGX replay contract cases passed with Cocoa display (`VPHONE_DISPLAY=cocoa`). |
| Probe disk clone cleanup | **PASS** | Disposable disk images (~3 GB) deleted immediately after run; verified by `diagnostic-disk-cleanup.json`. |

---

## 2. Root Cause Analysis

### A. Clock and Home Screen Erased by Redraws Missing Scissor Clipping
- **Root Cause**:
  In iOS 7 rendering passes (e.g. `frame-3.bin` for Lock Screen and `frame-6.bin` for Home Screen), the compositor divides the frame into multiple draws sharing the same background wallpaper:
  - In Lock Screen: Draw 0 draws wallpaper scissored to `y=[0, 1034]`. Later draws render the bottom slide area scissored to `y=[1034, 1136]`.
  - In Home Screen: Draw 0 draws wallpaper scissored to `y=[0, 944]`. Draws 1..68 draw the 16 Home screen icons and text. Draw 69 draws the dock wallpaper scissored to `y=[944, 1136]`.
  Because the previous replay implementation ignored scissor state, Draw 69 drew wallpaper over the full `0..1136` screen, completely erasing all 16 Home screen icons! Similarly on the lock screen, full-screen redraws wiped out the clock digits.
- **Hardware Mechanism**:
  1. The 3D command word at offset `+0x50` specifies the guest virtual address (`scissor_base`) of the scissor table. Each entry consists of two 32-bit words packing `(x0 << 16) | x1` and `(y0 << 16) | y1`.
  2. The PPP (Pixel Processing Pipeline) block header contains:
     - `pres_ispctl` (bit 0): bit 16 is `scenable` (scissor enable).
     - `pres_ispctl_dbsc` (bit 5): low 16 bits is `scindex` (scissor table entry index).
     - `pres_region_clip` (bit 10): two words `rc0` and `rc1` defining region clip bounding box coordinates in 32×32 pixel blocks.
- **Fix**:
  - In `backend/s5l8960x.c`:
    - Decoded `scissor_base` at `raw + 0x50` in `agx_trace_rt`, snapshot-cached the 4KB scissor page in `snapshot->pages` to guarantee geometry lifetime.
    - Decoded PPP words in `agx_replay_frame`: extracted `scenable`, `scindex`, and region clip bounds (`rc_x0, rc_x1, rc_y0, rc_y1`).
    - Clamped quad bounding boxes to `[clip_x0, clip_x1]` and `[clip_y0, clip_y1]` before rasterisation.
  - In `backend/agx_replay_offline.py`:
    - Added `decode_ppp_clipping()` and clamped draw rects accordingly.

### B. Missing Clock Numbers and Vector Elements
- **Root Cause**:
  Clock digits and certain UI elements use untextured draws with `stride == 28`. Previously, line 5828 in `s5l8960x.c` discarded all untextured draws unless `stride == 20`.
- **Fix**:
  - Allowed `stride == 28` when `found == AGX_TEX_NONE`.
  - Read the 32-bit vertex colour from offset 16 (`buf + (uint64_t)q_start * stride + 16`) for `stride == 28 && !textured`.

### C. Wallpaper Channel Ordering
- Checked source texture vs framebuffer formats:
  - Both use 32bpp BGRA little-endian format (`B` at byte 0, `G` at byte 1, `R` at byte 2, `A` at byte 3).
  - Background deep blue night sky pixels decode to `(R~0x11, G~0x15, B~0x35)`, matching expected iOS 7 lock screen wallpaper.

---

## 3. Evidence

Live probe output directory: `backend/evidence/G-lock-gfx/live-probe-g2-r2/`
- `lock-75.png` & `framebuffer-at-75s.ppm`: Lock screen at 75s guest running time. Shows star-field wallpaper, "slide to unlock" chevron, clock digits ("12:xx" / "9:xx"), and date string ("Thursday, October 23"). Clock region bright pixels: 1995.
- `lock-90.png` & `framebuffer-at-90s.ppm`: Lock screen at 90s guest running time. Consistent clock and date.
- `home-115.png` & `framebuffer-at-115s.ppm`: Home screen at 115s guest running time after scripted unlock swipe at 95s (`at=95;down 40,1036;hold 0.3;move 600,1036 dur=3 steps=180;hold 1.0;up`). All 16 Home icons (Messages, Calendar, Photos, Camera, Weather, Clock, Maps, Videos, Notes, Reminders, Stocks, Game Center, Newsstand, iTunes Store, App Store, Settings), icon text labels, dock icons (Phone, Mail, Safari, Music), and status bar are fully visible. Icon region non-background pixels: 416,135.
- `home-140.png` & `framebuffer-at-140s.ppm`: Home screen at 140s guest running time. Stable icon presentation. Icon region non-background pixels: 420,681.
- `qemu-trace.log`: Exactly 12,522 / 12,522 frames replayed (100% completion between `AGX RT queue` and `AGX replay target`).
- `diagnostic-disk-cleanup.json`: Verifies disk image cleanup of ~3 GB disposable clones.

---

## 4. Verification Commands

1. Rebuild backend:
   ```bash
   backend/.venv/bin/python scripts/qemu/build_backend.py
   ```
2. Run unit tests:
   ```bash
   .venv/bin/python -m unittest discover -s tests
   # 227 tests OK (7 skipped)
   ```
3. Run contract test suite:
   ```bash
   VPHONE_DISPLAY=cocoa PYTHONPATH=backend .venv/bin/python backend/agx_contract_test.py
   # 15 tests OK (including scissor-clip)
   ```
4. Run live probe:
   ```bash
   VPHONE_DISPLAY=cocoa VPHONE_TCG_THREAD=multi \
   VPHONE_TOUCH_SCRIPT='at=95;down 40,1036;hold 0.3;move 600,1036 dur=3 steps=180;hold 1.0;up' \
   .venv/bin/python backend/display_probe.py \
     --plan build/virtual-activated-profile-v5/boot/boot_plan.json \
     --diagnostic-system build/virtual-activated-profile-v5/system.raw \
     --diagnostic-data build/virtual-activated-profile-v5/data.raw \
     --seconds 150 --stop-condition duration \
     --framebuffer-at 75 --framebuffer-at 90 --framebuffer-at 115 --framebuffer-at 140 \
     --output backend/evidence/G-lock-gfx/live-probe-g2-r2
   .venv/bin/python backend/probe_cleanup.py backend/evidence/G-lock-gfx/live-probe-g2-r2
   ```
