# H-home-live: preserve Home and remove animation ghosts

Branch `wt/home-live`; worker Codex. Resumed on 2026-10-23 with the expanded
Home-ghosting goal. No other worktree edited, no guest-binary changes, no push.

## Integration

Saved the requested WIP first as `3915b66`, then rebased only H's additions onto
`agx-meta-hle` `ae427fc` (rebased WIP `cebf2e0`). Dropped the older duplicate
`01af599` and `bd38b2b` commits. The base supplies the final G2 implementation
(`8ca7caa`), expanded TA history, geometry snapshots, stamp waits, queue cursor,
wallpaper and native scissor/region clipping. H does not reimplement that work.

Read main handover section 24 and `backend/evidence/merge-20261023-live/`.
The merged screenshots show live Home with persistent doubled/offset icons,
labels and a dashed black bar above/left of the lock slider.

Restored the base's **18-frame** scripted release settle before rebuilding.
Earlier H pilot non-unlocks and its experimental 60-frame settle were on an older
candidate and are not proof against the merged build. Any subsequent settle
change requires native SpringBoard scroll-hook evidence, per user instruction.

## Findings

### Previous black-Home problem

The original apparent intermittent success was a capture-time effect.
T-live-swipe boot1's 102-second image shows the unlock animation, but **its final
105-second image is already black with the rectangle**. Boot1's large Home
passes finish near 102 s; the other runs finish near 98 s. All three display
targets receive Home, and display swaps rotate/complete. The already-integrated
G2 clipping fix addresses the later full-screen overwrite from the Clock's
small dirty update. H's retained regression checks that a wallpaper quad under
a tiny scissor or tile region preserves all static content outside coverage.

### Home ghosting: false vibrancy from the next draw's allocation

The new defect is in texture/blend classification, rather than evidence for a
blanket clear of load passes or a scanout workaround.

In an actual full Home pass (`baseline-dumps/frame-8.bin`):

- Draw 0 uses ordinary wallpaper shader `0x8646b974188714ee`.
- Its stand-alone, **16-byte** texture descriptor begins at `0x10200054`.
- The next draw's Rec.709 luma constants begin immediately afterward at
  `0x10200064` (`3d93a000 3e59a000 3f372000`).
- Replay reads 48-byte descriptor windows and previously searched all their
  words/all candidate blocks for those constants. It therefore classified the
  opaque wallpaper, and some ordinary icons, as vibrancy draws.
- `agx_blend_vibrancy` scales source alpha by destination luminance. An opaque
  wallpaper redraw then becomes translucent and retains previous animation
  positions in the display buffer. Later Clock-only dirty passes cannot repair
  that static history. The source wallpaper itself is clean, with alpha 255.
- Genuine vibrancy draw 1 (`0x682361cfceff644a`) has the luma constants **at the
  start of its own selected descriptor**, followed by its sampler/TSW. This
  remains supported.

`ghost-diagnosis.json` preserves the first three draws' shader fingerprints,
addresses, descriptors and corrected classifications. Isolated C replay of the
same full-Home dump now produces single opaque icons/labels instead of retaining
translucent history (`home-vibrancy-fixed.png`; its old dump has a black wallpaper
source because it predates live wallpaper support).

### Damaged labels: false display-strip layout

A second independent decoder bug remained after fixing the translucent trails.
The page-hole heuristic tested `base+0x9000` and `base+0xa000` for every texture,
including glyphs that occupy much less memory. Those probes can hit unrelated
neighbouring allocations and falsely select the display's **2560-byte** pitch.

In the same full Home dump, the 106 x 30 Compass label at `0x1042c800` was
classified as a strip texture, as were several 78-pixel-high label shadows.
Those are uploaded linear glyph images, not display buffers. Their rows then
sample unrelated RAM, producing dashed bars/coloured text. The lock-slider
artifact also varied between captures, so the first apparently clean lock
capture was insufficient proof of its removal.

The recovered strip format is specifically a 640-pixel-wide, 64 KiB-aligned
display surface containing sixteen 2560-byte rows per 64 KiB strip. Restrict the
hole test to that shape/alignment in C and offline replay. Dense offscreen
surfaces and small/offset uploaded glyphs retain their own pitch. Isolated C
replay now shows readable Compass and all dock labels (`home-layout-fixed.png`).
The slider artifact persisted in a subsequent live capture and had the
independent cause below. The native Home fixture adds an unrelated mapped page
at the tiny source texture's `+0x9000`, with `+0xa000` unmapped, and still requires exact linear
pixels. Decoder tests retain a positive real display-strip case.

### Slider bar: transparent source-copy padding was treated as source-over

Captured consecutive, matching-TA passes after 87 s on the merged build:
`slider-consecutive/frame-16.bin` renders a 640 x 416 offscreen target
(`0x10258000`). Its final draw has 48 stride-20 vertices: eight quads around
`(20,946)-(620,1004)`, covering a six-pixel padding ring out to
`(14,940)-(626,1010)`. Every vertex colour is zero. Its USC fingerprint is
`0x99d4fcf52a8c2ef3`. This is the native source-copy padding clear. Treating it
as source-over makes zero-alpha pixels a no-op and preserves the previous
blur/highlight output in the ring. The next display pass copies that whole
612 x 70 region. Subsequent 348 x 58 text-only dirty updates do not cover the
ring, so the bar stays on screen.

Isolated replay of that exact offscreen pass reproduces the white/black dashed
border (`slider-wide.png`). Recognizing this observed solid copy program and
writing the vertex colour, including zero alpha, removes the entire padding
ring while preserving the text (`slider-wide-fixed.png`). The Python mirror
uses the same fingerprint and operation. This is deliberately scoped to the
observed USC program; it does not reinterpret arbitrary transparent src-over
quads or clear all load passes.

The `copy-border` native contract seeds a solid 16 x 16 target, clears eight
padding quads using the captured 64 USC bytes, and checks every output byte,
centre pixel, exterior pixel, padding and guard page. `transparent-over` uses
the same zero-colour vertices with ordinary src-over and must preserve all
seeded pixels. Both also run inline and through the offline mirror.

## H changes

- Live and offline texture decoders classify vibrancy only from the selected
  descriptor's luma prefix. Trailing words or another pointer's luma no longer
  change an ordinary texture's blend mode. No blanket framebuffer clears.
- Native Home pixel contracts seed distinct static content, redraw an opaque
  full-target wallpaper quad under a small indexed scissor/tile region, and
  place another draw's luma constants immediately after its 16-byte TSW. Check
  every physical output byte and guard/padding in threaded and inline replay.
- Offline parity uses those same fixtures through real GPU page tables. Tests
  cover partial PPP inheritance/region disable and deliberately reproduce the
  old un-clipped overwrite. Descriptor tests cover trailing luma and luma in a
  different candidate pointer while retaining genuine prefixed vibrancy.
- Corrected an older test that explicitly required the erroneous trailing-luma
  behavior. The historical 533-draw audit still skips zero draws: 20 formerly
  misclassified vibrancy draws are ordinary exact textured draws (149 instead
  of 129); genuine vibrancy count becomes 21 instead of 41.
- Opt-in `VPHONE_AGX_FRAME_DUMP_AFTER_NS` bounds captures to a transition.

## Validation (final three boots pending)

All probes: own rebuilt QEMU, visible Cocoa, MTTCG, fresh cloned v5 plan/System/
Data, one owned QEMU at a time, load < 5 at launch, clones removed after reaping.
Prescribed swipe starts at 95 s. Acceptance probes append a Settings tap at
145 s, after the 105/140 s Home captures.

- Rebased build with restored 18-frame settle: **230 unit tests OK, 7 skipped**;
  full native AGX, touch_input and spi_touch contracts pass.
- Final copy/blend/layout build: **232 unit tests OK, 7 skipped**
  (`unit-copy-final.log`). Full native AGX, including both Home and both copy/over
  cases threaded/inline (`agx-copy-final.log`), touch_input and spi_touch
  contracts pass (`touch-copy-final.log`, `spi-touch-copy-final.log`).
- `ghost-boot1/`: 155 s, no panic. The 90/105 s screenshots show clean lock
  content and **no dashed slider bar**, but it did not unlock; this is
  **not a Home success**. The native-hook run below separates sleep from a pan
  bounce, so these screenshots alone do not justify increasing settle length.
- `scroll-18/`: verified SpringBoard kernel trigger and executable scroll
  callbacks installed; initial page 1 callback at 72.2 s, **no scroll callbacks
  at the 95 s gesture**. Chestnut powers off at 85.11 s, and the injected touch
  FIFO grows to 63 entries instead of draining. This is input while asleep,
  not a demonstrated 18-frame pan bounce. Kept 18 frames.
- `scroll-18-awake/` adds blank-area taps at 78/86/94 s, then every eight seconds
  after unlock. The prescribed 95 s swipe is unchanged. This is normal native
  input to keep the screen awake, not forced input, a guest patch, or an
  emulator display-power override. `sb_lsv_will_end` targets `(0,0)` at
  96.194 s, followed by page-0 callbacks at 96.500 s. Home is free of fly-in
  ghosts at 105/140 s, and the 145 s tap opens Settings by 152 s.
  `scroll-comparison.json` preserves critical callbacks. **18 frames retained**.

| Goal | Result |
| --- | --- |
| Home without ghosts at 105/140 s, three fresh boots | Final copy/blend/layout build pending |
| Tap opens native Settings | Final copy/blend/layout build pending |
| Dashed lock-slider bar removed | Source-copy fix added; final captures pending |
| Live/offline native pixel contract | PASS, threaded and inline |
| No guest patch / protected source images | PASS so far |

## Evidence and limits

Local evidence: `research/evidence/H-home-live/`. Older pre-rebase H pilots are
retained as historical evidence, not merged-build acceptance. Selected PNGs and
JSON will be committed; large raw dumps/logs remain local. Completed log files
may be gzip-compressed. Disposable image clones are deleted with provenance
checks; the user removed the interrupted old `settle-boot1/images` clone.

Clock hands still use the existing axis-aligned quad approximation. Overall
GPU fidelity is not claimed. Three fresh acceptance boots are being reviewed. The keep-awake taps are a
validation precondition: a sleeping screen ignores injected touches, and a
static QMP framebuffer can still show the last lit frame. This change does not
add a power-button/wake model or override native idle policy.
