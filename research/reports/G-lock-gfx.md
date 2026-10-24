# G-lock-gfx: full lock screen and live Home (final report)

Workers: Codex (initial WIP, commit `a77b241`), Antigravity / `agy` (Task G2 completion).
Branch `wt/lock-gfx` (worktree `/Users/bagas/Downloads/vphone-wt-lock-gfx`).
See full Task G2 report in `research/reports/G2-lock-gfx.md`.

## Status per goal

| Goal | Result |
| --- | --- |
| Lock screen: wallpaper | **PASS (live)**: star-field wallpaper plus slide text (`live-probe-g2-r2/lock-75.png`). Channel ordering verified BGRA matching source and framebuffer. |
| Lock screen: clock and date | **PASS (live)**: clock digits, colon, and date string clearly visible live (`live-probe-g2-r2/lock-75.png`, `lock-90.png`). |
| Home after unlock, live | **PASS (live)**: full Home screen rendered live with all 16 app icons, labels, dock, and status bar (`live-probe-g2-r2/home-115.png`, `home-140.png`). |
| Home in offline C replay | **PASS**: verified offline mirror matches live C replay. |
| Scissor & Region clipping | **PASS**: PPP header `scenable`, `scindex`, and `rc0/rc1` decoded and applied. |
| Untextured stride-28 draws | **PASS**: vector clock digits enabled with color at offset 16. |
| Unit tests | **PASS**: 227 tests OK (7 skipped). |
| AGX pixel contracts | **PASS**: All 15 cases pass including new `'scissor-clip'` contract. |

## Key Findings & Resolutions
1. **Clock and Home Screen Erased by Redraws Missing Scissor Clipping**:
   - 3D command `+0x50` specifies the base GVA of the scissor table.
   - PPP state word `pres_ispctl` contains `scenable` (bit 16), `pres_ispctl_dbsc` contains `scindex` (low 16 bits), and `pres_region_clip` defines region clip bounds.
   - Without scissor clipping, the wallpaper passes scissored to the dock (y: 944-1136 on Home screen) or bottom slide (y: 1034-1136 on Lock screen) were drawn over the entire 0-1136 screen, erasing all icons and clock digits rendered earlier.
   - With scissor table decoding and clipping implemented in `s5l8960x.c` and `agx_replay_offline.py`, both the clock/date on lock screen and the full Home screen after unlock are preserved and rendered live.
2. **Missing Clock Characters**:
   - Clock digits are untextured quads with `stride == 28`. Enabling `stride == 28` untextured draws with vertex color at offset 16 renders these digits cleanly.
3. **Evidence**:
   - `backend/evidence/G-lock-gfx/live-probe-g2-r2/` contains `lock-75.png`, `lock-90.png`, `home-115.png`, `home-140.png`, and logs.
