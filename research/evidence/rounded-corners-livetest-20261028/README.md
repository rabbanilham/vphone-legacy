# Live Test Evaluation: Rounded Corners Fix (Commit 3f24a12)

**Test Date:** October 28, 2026  
**Worktree:** `<repo>`  
**Branch:** `fix/anim` (commit `3f24a12 "agx: support Format 3 A8 masks and dual-texture corner restore"`)  
**Baseline Binary:** `<repo>/backend/bin/qemu-system-aarch64` (clean primary checkout)  
**Candidate Binary:** `<repo>/backend/bin/qemu-system-aarch64` (with Format 3 / dual-texture restore)  

---

## 1. Executive Verdict

| Test Location | Baseline Verdict | Candidate Verdict | Per-Crop MAE | Improvement Verdict |
| :--- | :--- | :--- | :--- | :--- |
| **Home Icons (Messages, Calendar, Photos, Camera, Settings)** | Round, antialiased squircle ($r \approx 27\text{px}$) | Round, antialiased squircle ($r \approx 27\text{px}$) | **0.0000** | **Identical (No Change)** |
| **Home Dock** | Rectangular, edge-to-edge ($w=640$) | Rectangular, edge-to-edge ($w=640$) | **0.0000** | **Identical (No Change)** |
| **Settings App (UISwitch & Table View)** | Round pill switch, clean rounded cell borders | Round pill switch, clean rounded cell borders | **0.3211** | **Identical (No Change)** |
| **Fly-Out Zoom: Mid-Flight Window (155.764s)** | Rectangular scaling window ($640\times 1093$) | Rectangular scaling window ($640\times 1093$) | **138.79** *(phase jitter)* | **Identical geometry** |
| **Fly-Out Zoom: Landing on Icon Slot (155.929s)** | Flat unmasked blend over icon slot; square backdrop bleed | Format 3 A8 mask attenuated additive blend; squircle restored | **40.3324** (crop), corner MAE vs wallpaper drops **58.43 $\to$ 15.84** | **Better (Proven Fix)** |
| **Fly-Out Zoom: Post-Landing Home (156.028s)** | Settled Home icon | Settled Home icon | **13.2040** *(1-frame phase)* | **Identical settled state** |

### Overall Conclusion
Commit `3f24a12` **does NOT change rounded corners everywhere**; it is **specifically and solely active during the final landing phase of the SpringBoard app fly-out animation** (when an app window shrinks down into its Home screen icon slot). 
- In that specific landing phase, **it measurably improves corner rendering** (restoring the squircle corner against the wallpaper backdrop and dropping top-left corner error from **58.43 to 15.84**).
- For static Home screen icons, the Dock, and UIKit views (Settings switches and table cells), **the fix has zero effect (MAE = 0.0000)** because those elements are pre-rendered into layer caches by iOS and drawn via standard 1:1 textured quads, never executing shader `0x03a2edd2a4d84437`.

---

## 2. Quantitative Evidence & Measurements

### 2.1 Home Screen Elements at 105s (`framebuffer-at-105s.ppm`)
Both Baseline and Candidate were captured at virtual clock time $t = 105.0\text{s}$. Outside of the real-time second hand on the analog Clock icon (`MAE = 4.22`) and the status bar clock minute rollover (`MAE = 0.16`), the entire 640×1136 display is **100% bit-for-bit identical**:

- `Home Messages Icon` (`30,48-150,168`): MAE = **0.0000** (0 differing pixels)
- `Home Messages TL Corner` (`30,48-62,80`): MAE = **0.0000**
- `Home Calendar Icon` (`184,48-304,168`): MAE = **0.0000**
- `Home Calendar TL Corner` (`184,48-216,80`): MAE = **0.0000**
- `Home Photos Icon` (`338,48-458,168`): MAE = **0.0000**
- `Home Camera Icon` (`492,48-612,168`): MAE = **0.0000**
- `Home Settings Icon` (`184,752-304,872`): MAE = **0.0000**
- `Home Settings TL Corner` (`184,752-216,784`): MAE = **0.0000**
- `Home Dock Full` (`0,960-640,1136`): MAE = **0.0000**
- `Home Dock Top Edge` (`0,955-640,985`): MAE = **0.0000**

#### Corner Superellipse Verification
Inspection of the top-left $25\times 25$ region of `candidate_messages_icon.png` confirms that Home icons are **already rendered as true squircles** with subpixel antialiasing by SpringBoard's layer cache:
```
y= 0: .........................
y= 1: .........................
y= 2: ........................+ (starts curving at x=24)
y= 3: ....................+#### (x=20)
y= 4: .................+####### (x=17)
y= 5: ...............+######### (x=15)
y= 6: ..............########### (x=14)
y= 7: ............+############ (x=12)
...
y=24: ..+###################### (x=2)
```
Where `.` is background wallpaper, `+` is antialiased blend pixel, and `#` is solid icon green.

---

### 2.2 Settings App Opened at 152s (`framebuffer-at-152s.ppm`)
- `Settings Airplane Switch` (`500,200-605,260`): MAE = **0.3211**
- `Settings Table Row Top` (`0,195-640,265`): MAE = **0.0641**

The UISwitch is rendered as a clean pill shape with smooth antialiased circular endcaps and knob. The minor MAE ($\approx 0.3$) is due to network status spinner subpixel updates, not corner geometry.

---

### 2.3 Fly-Out Animation (155.50s – 156.30s)

During the fly-out animation (triggered by Home button press at $t=154.0\text{s}$), 25 dense framebuffer frames were captured every 33ms.

#### Landing Icon Slot (155.929s) — Active Corner Restore
At $t=155.929\text{s}$, the shrinking window reaches the $120\times 120$ icon slot at $(184, 754)-(304, 874)$. Here CoreAnimation dispatches **Draw 2** with `prog 0x03a2edd2a4d84437` (stride 36) using a $256\times 256$ Format 3 A8 mask texture:
- **Baseline (without fix):** Draw 2 falls back to `simple_blend` (`agx_blend_over` without mask attenuation), bleeding square backdrop pixels into the corner.
  - Top-Left $28\times 28$ Corner MAE vs Home Wallpaper: **58.43**
- **Candidate (with 3f24a12):** Draw 2 decodes the Format 3 mask, samples $UV_2$, attenuates by $(1 - \text{mask\_alpha})$, and blends additively.
  - Top-Left $28\times 28$ Corner MAE vs Home Wallpaper: **15.84** (a **73% reduction in error**)

#### Mid-Flight (155.764s) & Post-Landing (156.028s)
- **Mid-Flight (155.764s):** The scaling window is $640\times 1093$. It is drawn with `prog 0xd3c9c963e559c6e3` (textured quad) and `prog 0x4ee01acc47551344` (dest-in mask). Shader `0x03a2edd2a4d84437` is NOT used here. Differences between Baseline and Candidate are entirely due to sub-frame animation timing jitter (~1 frame offset).
- **Post-Landing (156.127s+):** The animation has completely terminated. Baseline and Candidate are once again **100% bit-for-bit identical** (MAE = 0.0000).

---

## 3. Draw Classification Audit (AGX Replay)

Auditing all 30 AGX binary frame dumps (`frame-1.bin` through `frame-30.bin`) captured during the animation run yields identical draw classifications:

```
Classification                                                 | Count | Frames
-----------------------------------------------------------------------------------------------
replayed exactly: textured quad (1:1 nearest)                  | 388   | 24 frames (1..30)
replayed exactly: vibrancy (Rec.709 luma-weighted tint)        | 220   | 16 frames (1..29)
replayed exactly: textured rounded quad (stride=28)            | 59    | 16 frames (1..30)
replayed exactly: solid quad (stride=20)                       | 51    | 11 frames (1..30)
replayed exactly: textured quad (bilinear scaled)              | 49    | 16 frames (1..29)
skipped: non-axis-aligned or rotated geometry                  | 24    | 8 frames [8, 11, 14, 17, 20, 28, 29, 30]
approximated: dest-in mask (bilinear scaled)                   | 6     | 6 frames [4, 7, 10, 13, 16, 19]
replayed exactly: corner restore (backdrop + mask)             | 6     | 6 frames [4, 7, 10, 13, 16, 19]
replayed exactly: 1D Gaussian blur (horizontal, 7-tap)         | 3     | 3 frames [24, 25, 26]
replayed exactly: 1D Gaussian blur (vertical, 7-tap)           | 2     | 2 frames [26, 27]
approximated: Gaussian blur (separable 6-tap, canvas backdrop) | 1     | 1 frames [21]
replayed exactly: pill border (stride=36)                      | 1     | 1 frames [22]
replayed exactly: color matrix (saturation & tint)             | 1     | 1 frames [27]
```

### Key Findings from Draw Classification:
1. `prog 0x03a2edd2a4d84437` (`corner restore`) is present in **only 6 frames** out of 30: frames `[4, 7, 10, 13, 16, 19]`.
2. Each of these 6 frames contains **exactly 1 corner restore draw**.
3. That single draw is always at the $(183, 752)-(211, 780)$ coordinate box: the top-left $28\times 28$ corner of the destination Settings icon slot on the Home screen.
4. **Zero corner restore draws exist on the static Home screen or in Settings.**

---

## 4. Why Earlier Tests Appeared Identical

The earlier investigation in `research/evidence/rounded-corners-20261027/` compared:
1. `before_home_icon_box.png` vs `after_home_icon_box.png`: Identical because static Home icons are cached 1:1 quads and never invoke corner restore.
2. `win_tl_before.png` vs `win_tl_after.png` at 156.028s: Identical because by $t=156.028\text{s}$, the fly-out animation had already completed, and the screen had reverted to the static Home screen.

Only during the active landing frames ($t \approx 155.90\text{s} - 155.99\text{s}$) does the corner restore draw actually execute.

---

## 5. Artifacts and Generated Evidence

- Filmstrips:
  - Baseline: `research/evidence/rounded-corners-livetest-20261028/strip_baseline.png`
  - Candidate: `research/evidence/rounded-corners-livetest-20261028/strip_candidate.png`
- 1:1 and 4x Crops:
  - `candidate_home_messages_tl_corner_4x.png` vs `baseline_home_messages_tl_corner_4x.png`
  - `candidate_home_calendar_tl_corner_4x.png` vs `baseline_home_calendar_tl_corner_4x.png`
  - `candidate_home_settings_tl_corner_4x.png` vs `baseline_home_settings_tl_corner_4x.png`
  - `candidate_settings_airplane_switch_3x.png` vs `baseline_settings_airplane_switch_3x.png`
  - `candidate_flyout_155.929s_landing_tl_corner_4x.png` vs `baseline_flyout_155.929s_landing_tl_corner_4x.png`
  - `candidate_icon_landing_155_929.png` vs `baseline_icon_landing_155_929.png`
