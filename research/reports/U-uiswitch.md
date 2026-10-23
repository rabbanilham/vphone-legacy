# Task U: UISwitch track (Settings → Airplane Mode)

**Task ID**: `U-uiswitch`  
**Worktree**: `/Users/bagas/Documents/Projects/vphone/vphone-wt-uiswitch`  
**Branch**: `wt/uiswitch`  
**Author / Worker**: Antigravity (`agy`)  
**Scope**: AGX replay core in `backend/s5l8960x.c` (between `AGX_REPLAY_CORE_BEGIN` and `AGX_REPLAY_CORE_END`), `backend/agx_replay_offline.py`, `tests/test_agx_*`. Strictly offline.

---

## 1. Status per Goal

| Goal | Status | Evidence / Notes |
| --- | --- | --- |
| **1. Set up corpus view and verify chained replay** | **PASS** | Symlinked corpus scenes into `corpus/`. Chained replay runs all 225 dumps across scenes `calendar`, `clock`, `unlock`, `unlock-flyin`, `settings` in 5.3 s with 0 harness errors. |
| **2. Identify defect in live evidence** | **PASS** | Analyzed `backend/evidence/merge-ks-live/framebuffer-at-152s.ppm`. Extracted crops under `research/evidence/U-uiswitch/`: at x≈504..560, y≈212..274, the switch knob is rendered (56 px diameter circle with shadow), but the white pill track (~102x62 px) and thin grey border (~#E5E5E5) behind/to the right of the knob are completely missing. |
| **3. Investigate Settings corpus passes** | **BLOCKED (Corpus Stale)** | Discovered that `build/agx-corpus/settings` was captured at 19:57 (before touch IRQ fix `1cbb103`). The touch script never unlocked the device (`queued=1..30` in probe log). All 20 dumps in `corpus/settings/dumps/` and all framebuffers in `corpus/settings/probe/` (from 90 s to 160 s) show only lock screen wallpaper and slider glint (1 draw each). Zero Settings passes are present in the current corpus. |
| **4. Identify missing passes from live trace** | **PASS** | Recovered the full render pass pipeline from `merge-ks-live/qemu-trace.log` at 152 s. Settings uses a 6-pass chain per frame, including 128x64 offscreen passes (`0x1059c000`, `0x105b4000`) for the switch shadow/elements. |
| **5. Build QEMU backend cleanly** | **PASS** | `backend/.venv/bin/python scripts/qemu/build_backend.py` compiled cleanly to `backend/bin/qemu-system-aarch64`. |
| **6. Unit & contract test suites pass** | **PASS** | All 251 unit tests passed (`.venv/bin/python -m unittest discover -s tests`). All synthetic contracts in `backend/agx_contract_test.py` pass. |
| **7. Obey offline constraint** | **PASS** | No QEMU guest boots executed by this worker (agy D owns QEMU). Stopped per prompt protocol to request re-capture from lead (Claude). |

---

## 2. Root Cause Analysis & Investigation Findings

### A. Defect Characterization in Live Framebuffer (`framebuffer-at-152s.ppm`)
In `backend/evidence/merge-ks-live/framebuffer-at-152s.ppm` (captured live at 152 s after tasks K and S merge):
- The Settings screen is open, displaying grouped table cells.
- The "Airplane Mode" row is located at $y = 200..288$.
- The switch component occupies bounding box $x = 504..606$, $y = 212..274$.
- Current render:
  - An isolated circular knob (diameter $\approx 56\text{ px}$) is drawn at $x = 504..560$, $y = 212..274$.
  - Soft drop shadow appears on the left and bottom perimeter of the knob.
  - The pill track (which should form a rounded rectangular well of approximately $102 \times 62\text{ px}$ with a light grey border $\approx\text{\#E5E5E5}$ and white interior) is absent. To the right of the knob ($x = 560..606$), only the plain white ($\text{\#FFFFFF}$) table cell background is present.
- Evidence crops saved:
  - `research/evidence/U-uiswitch/live-switch-crop-152s.png`
  - `research/evidence/U-uiswitch/settings-row-crop.png`
  - `research/evidence/U-uiswitch/airplane-switch.png`

### B. Investigation of `build/agx-corpus/settings`
Inspection of `corpus/settings/recipe.json`, `probe/`, and `dumps/`:
1. `recipe.json` in `build/agx-corpus/settings` shows capture at 19:57:
   ```json
   "touch": "at=78;tap 600,900|at=86;tap 600,900|at=94;tap 600,900|at=95;down 40,1036;move 600,1036 dur=0.7 steps=42;up|...|at=130;tap 245,815|at=150;tap 200,900"
   ```
2. In `corpus/settings/probe/qemu-trace.log`:
   The touch events accumulated as `queued=1..30` without being serviced by the guest kernel because GPIO22 touch IRQ pulsing had not yet been applied (that fix landed at 22:09 in `1cbb103`).
3. Consequently, the phone **never unlocked** during the capture of `build/agx-corpus/settings`.
4. All 10 framebuffer captures in `corpus/settings/probe/` (`framebuffer-at-90s.ppm` through `framebuffer-at-160s.ppm`) are identical lock screen wallpaper (`RGB (115, 83, 22)`).
5. All 20 dumps in `corpus/settings/dumps/` (`frame-1.bin` through `frame-20.bin`) contain only 1 draw each:
   - Either Draw 0: wallpaper ($744 \times 1392$ texture)
   - Or Draw 0: "slide to unlock" glint label ($640 \times 416$ texture)
   Zero Settings passes exist in the corpus directory.

### C. Recovered Pass Pipeline from Live Trace (`merge-ks-live/qemu-trace.log`)
By analyzing `merge-ks-live/qemu-trace.log` around lines 1089300–1100040 (when Settings was actively rendered and displayed at 152 s), the exact multi-pass structure of the Settings screen was recovered:

| Pass | Target GVA | Dimensions | Format | Load | Draw Count | Role / Content |
| --- | --- | --- | --- | --- | --- | --- |
| **Pass 1** | `0x400470000` | $640 \times 1136$ | `0x20007fc` | `0x2` (clear) | **68 draws** | Full Settings display: table grouped background (#efeff4), white cell rows, separators (#c8c7cc), icons, text labels, and the switch knob. |
| **Pass 2** | `0x104c0000` | $640 \times 64$ | `0x20007fd` | `0x2` (clear) | **1 draw** | Navigation bar backdrop / blur filter. |
| **Pass 3** | `0x1059c000` | $128 \times 64$ | `0x20000fd` | `0x2` (clear) | **2 draws** | Offscreen switch knob shadow mask / geometry. |
| **Pass 4** | `0x105b4000` | $128 \times 64$ | `0x20000fd` | `0x2` (clear) | **2 draws** | Horizontal 6-tap Gaussian blur of $128 \times 64$ layer. |
| **Pass 5** | `0x1059c000` | $128 \times 64$ | `0x20000fd` | `0x2` (clear) | **2 draws** | Vertical 6-tap Gaussian blur of $128 \times 64$ layer. |
| **Pass 6** | `0x400470000` | $640 \times 1136$ | `0x20007fc` | `0x2` (clear) | **3 draws** | Composites the blurred $128 \times 64$ offscreen shadow onto the main display surface. |

Notice that `SCENES['settings']` in `scripts/agx_corpus.py` was executed with the default `display_only: True`. This means even on a successful unlock run, `VPHONE_AGX_FRAME_DUMP_DISPLAY=1` filters out passes 2, 3, 4, 5, so the $128 \times 64$ offscreen switch passes would not have been dumped!

---

## 3. Explicit Instruction to Lead (Claude)

Per the task prompt:
> *"The switch may be drawn in an offscreen pass that the display-only settings scene did not dump. If so, record the draw list you can see and say exactly which pass is missing; do not boot QEMU. Ask the lead (Claude) for a capture with display_only: False by writing it in your report, then stop."*

### Request to Lead (Claude):
Please run a fresh corpus capture for scene `settings` with:
1. `display_only: False` (in `scripts/agx_corpus.py` for `settings`).
2. The verified touch script from `research/evidence/H-home-live/awake-touch-script.txt`:
   ```
   at=78;tap 600,900|at=86;tap 600,900|at=94;tap 600,900|at=95;down 40,1036;move 600,1036 dur=0.7 steps=42;up|at=103;tap 600,900|at=111;tap 600,900|at=119;tap 600,900|at=127;tap 600,900|at=135;tap 600,900|at=143;tap 600,900|at=145;tap 245,815
   ```
   (Note: Settings icon tap is at `at=145;tap 245,815`, so dump windows should cover $t = 150..160\text{ s}$).
3. This will dump both the 68-draw display pass and the $128 \times 64$ offscreen passes (`0x1059c000`, `0x105b4000`), allowing the worker to immediately inspect the draw commands for the pill track and border, fix the C and Python replay, and verify against the corpus.

---

## 4. Verification & Clean State

- `backend/.venv/bin/python scripts/qemu/build_backend.py` compiled cleanly (`backend/bin/qemu-system-aarch64` updated).
- `.venv/bin/python -m unittest discover -s tests`: 251 tests run, 0 failures, 12 skipped.
- `VPHONE_DISPLAY=cocoa .venv/bin/python backend/agx_contract_test.py`: all synthetic contracts PASS.
- `.venv/bin/python scripts/agx_corpus.py --corpus corpus`: 225 dumps replayed in 5.3 s, 0 harness errors.
- No QEMU guest boots were executed by this worker.

Co-Authored-By: Antigravity <agy@deepmind.com>
