# H-home-live: preserve live Home during Clock dirty redraws

Task H, branch `wt/home-live`, based on `wt/live-swipe` `d096333`.
Worker: Codex. No guest binaries or source images changed. No other worktree
was edited, no branches merged elsewhere, and nothing pushed.

## Root cause and comparison

The apparent intermittent success in T-live-swipe was a capture-time effect.
`boot1-scripted/framebuffer-at-102s.png` shows Home during its unlock animation;
**that same run's final `framebuffer.png` is already black with the rectangle**.
In the adjacent MMIO timestamp ranges, its large Home draws continue until
about 102.25 s. Boots 2/3 finish those draws around 97.8 s. Thus all four
original runs lose settled Home, even though one briefly displays it at 102 s.

Original replay/RT queue totals:

| Original run | AGX replay target | AGX RT queue |
| --- | ---: | ---: |
| boot1-scripted | 20741 | 21166 |
| boot2-scripted | 18956 | 19356 |
| boot3-scripted | 20368 | 20852 |
| mouse-drag | 21981 | 22299 |

All three display targets (`0x400000000`, `0x400470000`, `0x4008e0000`) receive
large Home draws in failed runs too. `[DISP0]` stderr logs rotate the MMU tables,
keep layer0 base zero and complete swap tokens. The isolated replay of the
fresh baseline's full Home dump also draws icons, labels and dock. This is
not simply a GPU stall or Home permanently confined to an offscreen surface.

The decisive fresh settled-Home capture is
`research/evidence/H-home-live/prerequisite-dumps/frame-1.bin`:

- 12 submitted draws; first draw samples the 744 x 1392 wallpaper into a
  full-display quad with shader fingerprint `0x8646b974188714ee`.
- Type-1 command `+0x50` is scissor-table GVA `0x101d8000`.
- PPP enables scissoring, index 0. Table words `0x00d700f6`, `0x011c0128`
  restrict coverage to **[215,246) x [284,296)**.
- Region words restrict the draw to **[192,256) x [256,320)** in 32-pixel tiles.
- These are the Clock icon's moving-hand update. Ignoring both clips redraws
  wallpaper over the entire screen while only the Clock fragment is restored.
  On the original renderer that wallpaper is black; with the wallpaper fix it
  is visible wallpaper, still without Home icons.

This agrees with G's independently found lock-clock erasure. It is a shared
missing coverage contract, rather than evidence for a scanout workaround.

## Changes and coordination

1. Cherry-picked permitted prerequisite `a77b241` as local `01af599`: expanded
   TA history, TA-consumed geometry snapshots, stamp waits, private queue cursor,
   stride-16 wallpaper and PBE pitch. Read G's report first.
2. Applied a **read-only snapshot of G2's in-progress clipping candidate** in
   this worktree. The other worktree was never modified. Snapshot saved as
   `research/evidence/H-home-live/G2-candidate.patch`, SHA-256
   `bf1d85cf59b10ec8f7032825b65f5d91bc5c69c031b6e19616878b8d8e164d33`.
   G2 owns the clipping discovery/implementation: PPP scissor/index state,
   tile-region bounds, replay job scissor address/snapshot, and offline mirror.
   The snapshot also includes G2's untextured stride-28 path and context-selective
   dump option. This is not a claim that G2's concurrent work is finalized.
   Merge/rebase should retain one copy of that shared change.
3. Added Home-specific native pixel contracts: first seed a full target with
   distinct static content, then redraw a full-size textured wallpaper quad
   under a tiny indexed scissor plus tile region, or under tile region alone.
   Compare every physical target byte including guards/padding. Both threaded
   and inline replay are exercised.
4. Added offline parity tests using those same fixtures through real GPU page
   tables; verify partial PPP state inheritance/region disable. A negative
   oracle deliberately disables clipping and proves that the old behavior
   replaces the entire target with wallpaper.
5. Added `VPHONE_AGX_FRAME_DUMP_AFTER_NS` for bounded transition captures.
   It is opt-in and does not change ordinary replay.

6. The faster clipped renderer exposed the short swipe's release heuristic:
   the original 18 stationary release frames bounced in two clipping runs,
   although the same gesture did unlock in another. Increase scripted drag
   release settling to 60 frames (one virtual second). Movement remains the
   prescribed 0.7 s / 42 steps; taps get no extra settle. SPI tests verify all
   60 stationary reports, up ordering, and a subsequent tap. Mouse behavior
   is unchanged. This addresses a measured validation regression, rather than
   claiming that the supplied short swipe was already reliable on this build.

No display timing, swap token or framebuffer routing changes were made.

## Validation

All launches use Cocoa, this worktree's rebuilt QEMU, MTTCG, and cloned fresh
v5 plan/System/Data. Each timed launch waits for host load < 5. Script begins
with the prescribed gesture:

```
at=95;down 40,1036;move 600,1036 dur=0.7 steps=42;up
```

Validation runs append `|at=145;tap 245,815` to launch Settings only after the
140-second Home capture. No forced input or guest patch is used.

- Fresh rebuilt baseline: `baseline/`, 145 s, no panic; live Home erased at
  102/140 s, isolated full-Home dump draws Home (`baseline-offline.png`).
- TA/fence/wallpaper prerequisite alone: `prerequisite/`, 155 s, no panic;
  wallpaper and rectangle at 102/140 s; captures identify the missing clips.
- First clipping pilot: `clipping-boot1/`, 155 s, no panic; the short swipe
  bounced and stayed locked. **Not a Home success.** The fixed lock clock/date
  remain visible. This is an observed input/timing limitation and is retained.
- Second clipping pilot: `clipping-hooked/`, 155 s, no panic; Home visible at
  102 and 140 s; tap opens native Settings by 152 s. Although a bounded hook
  configuration was supplied, no hooks installed; screenshots and touch logs
  are the evidence, not a native gesture-hook claim.

- First pre-settle validation: `final-boot1/`, short swipe bounced and remained
  locked at 102/140 s. Stopped the waiting validation loop before boot 2; no
  QEMU was interrupted. This run is not included as a Home success.

Final three-boot acceptance on the 60-frame build: pending.

Tests: full native AGX contract passed with Cocoa, including both new Home
cases; separate inline Home contracts passed. Final unit/native rerun pending.

## Evidence and remaining limits

All H evidence is local at `research/evidence/H-home-live/`. Disposable image
clones are removed after their process-exit record confirms reaping. Original
profile hashes are checked by `probe_cleanup.py`. Large dumps/logs are local
artifacts, not staged wholesale into git.

This fixes static Home erasure, not overall GPU fidelity. Visible animation
trails, distorted Clock hands, icon/label sampling artifacts, and some Settings
text remain. Do not describe these screenshots as pixel-correct iOS rendering.
Final goal status and test totals will be filled after the three fresh boots.
