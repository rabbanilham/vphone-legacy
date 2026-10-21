# T17 — Scripted gesture timelines

Worktree `/Users/bagas/Downloads/vphone-wt-gesture`, branch `wt/gesture`.

## Result and verification boundary

**VERIFIED (host):** the actual C parser and actual timer/HBPP producer now support
multi-segment single-finger gestures and repeated gestures. The six-field swipe
preserves all 21 states, positions, and integer timer intervals for the original
0.3-second example. Contact serialization remains unchanged: path ID1, identity2,
start1/down3/move4/up7, raw `(x,1136-y)`, existing 26+30-byte record, signal/radii
cleared on up. No guest code, disk, or policy changes.

**VERIFIED (existing runtime evidence, independently checked):** probe119's
`breakpoints.json` has 21 `ui_hid_callback` hits, all SpringBoard PID44, runtime
PC `0x18a9b4ac0`. `user-hook-mapping.json` gives cache slide `0x6354000`;
subtracting yields `__UIApplicationHandleHIDEvent` at `0x184660ac0`. Every hit's
first16 instruction bytes equal the supplied cache:
`fd7bbfa9fd030091e80303aa493e05b0`.
This establishes callback delivery, not successful recognizer handling.

**INFERRED:** slower motion, a longer initial contact, another vertical location,
or a different drag direction may distinguish gesture recognition/hit-testing
from already established HID callback delivery. None is a demonstrated fix for
the greeting. I did not build or run QEMU; the user owns those steps.

## Syntax and timing

Existing form:

```sh
VPHONE_TOUCH_SCRIPT='125,60,1018,600,1018,0.3'
```

Segment form (the requested example compiles to 66 frames):

```sh
VPHONE_TOUCH_SCRIPT='at=125;down 100,1018;hold 0.15;move 560,1018 dur=0.8 steps=48;hold 0.1;up'
VPHONE_TOUCH_SCRIPT='at=125;tap 320,568|at=128;down 60,1018;move 600,1018 dur=1;up'
```

- Coordinates are integer top-left framebuffer pixels, X=0..640, Y=0..1136;
  at UIKit's 320x568-point scale, divide by2 to interpret the intended point.
- `at=` is seconds relative to machine reset, 0..86400. Each gesture needs its
  own `at=`, strictly later than the preceding scheduled up. One contact per
  gesture; no overlap or implicit lift. `;` separates commands, `|` gestures.
- `down x,y`: start1 at the current time, down3 one 16,666,667ns tick later.
- `hold seconds`: stationary move4 samples for the duration, including its end.
- `move x,y dur=seconds [steps=N]`: linear movement, exactly N move4 frames,
  first sample one interval after the previous frame, final sample at the
  requested endpoint/duration. Default N=max(1,floor(duration*60)); explicit
  N is an integer 1..3600 and deliberately overrides the default cadence.
- `up`: state7 one tick after the prior frame, at its last coordinates.
- `tap x,y`: down, 0.1s hold (six move4 samples), up; nine frames total.
- Move/hold durations: 0.001..60s. Legacy duration remains 0.016..60s; legacy
  start/down/movement/up spacing retains the old floor(duration_ns/N) interval.
- All times use QEMU virtual time. Gate waits and FIFO backpressure stretch the
  remaining timeline (including later gestures), preserving every transition
  and inter-gesture gap. No overdue burst. A reset cancels/reparses the script.
- The complete script is validated before scheduling; any error cancels all
  injection. Diagnostics identify gesture number, command number, nearby text,
  and reason. NaN/infinity, out-of-bounds coordinates, missing lifecycle steps,
  overlapping/out-of-order gestures, invalid steps, trailing text/separators,
  and capacity overflow are rejected. Capacity is 16384 frames.

`backend/README.md` documents the interface. `VPHONE_TOUCH_FORCE` retains its
existing meaning; normal probes below explicitly disable it.

## Code and tests

`backend/s5l8960x.c`: bounded `TouchScript` compiler and frame timeline, consumed
by the existing QEMU timer. The existing serialization, SPI, IRQ, and reporting
policy functions are reused. Exhausted/disabled scripts do not rearm a timer.
The exact comment `/* The TA command whose parameter buffer` is preserved.

`tests/test_touch_script.py`: extracts and compiles the actual parser under
ASan/UBSan; verifies legacy and segment timelines, interpolation/endpoints,
taps/repeated gestures, nanosecond timing, malformed input, truncated allocations,
frame-capacity rejection, and recovery after errors.

`tests/test_spi_dma.py`: updates the existing actual-C host harness to use the
compiled timeline. Verifies rich timelines through `touch_timer_cb` and
`touch_contact`: gate waits, a full FIFO at start and mid-gesture, timestamp and
coordinate serialization, stable identity/state, up signal clearing, reset,
invalid replacement, force behavior, and exhaustion. Existing transport tests
continue to run. `backend/touch_input_contract_test.py` needs no change: its
original six-field script and initial record remain valid. That QEMU-dependent
contract was not run.

**VERIFIED:** `python3 -m unittest discover -s tests -p 'test_*.py'` passes,
133 tests, 4 skipped. `git diff --check` passes. Host coverage does not establish
UIKit gesture completion or a successful QEMU build.

## Research inputs and addresses

Read `CLAUDE_HANDOVER.md` section20, T5/T6 from the scratchpad reports directory,
and the T15 cache-index warning. Cache mappings were read from the supplied
`cache-index.json`; instructions were independently decoded with Capstone in
`/Users/bagas/Downloads/vphone-legacy/.venv/bin/python`.

Additional **VERIFIED byte checks** against
`/Users/bagas/Downloads/vphone-legacy/backend/evidence/ca-surface-20261014-214745/dyld_shared_cache_arm64`:

| Unslid VA | First16 bytes | Checked instruction/evidence scope |
|---|---|---|
| `0x187cd2028` | `a80640391f05007181010054190340b9` | `ldrb w8,[x21,#1]; cmp w8,#1; b.ne 0x187cd2060` (state1 branch cited by T5) |
| `0x187cd1d34` | `8806403948010035290d00513f050071` | `ldrb w8,[x20,#1]; cbnz w8,0x187cd1d60` (raw state decode cited by T5) |
| `0x184664a50` | `fd7bbfa9fd030091f44fbfa9f657bfa9` | existing UIKit sendEvent hook prologue |
| `0x1846943c8` | `fd7bbfa9fd030091f44fbfa9f657bfa9` | existing gesture setState hook prologue |

These checks confirm instruction bytes at those addresses; they do not
independently establish every semantic claim in the earlier reports.

At the final research-input check, the requested `G1-unlock-gesture.md`,
`G2-uikit-touch.md`, and `G3-touch-geometry.md` did not exist in the specified
scratchpad `reports/` directory. The unrelated `G1-pbe-search.md` is not one of
those inputs. Consequently there is no new G3 contact defect to verify/fix and
no G2 hook set to integrate as `backend/touch_gesture_hooks.json` in this commit.
Existing `backend/touch_ui_hooks.json` can capture the next probes. A later
SpringBoard executable hook must use its executable slide, never the shared-cache
slide; cache and executable bytes must each be checked against their own file.

## Exact next probe commands (user runs after rebuilding)

Run from `/Users/bagas/Downloads/vphone-wt-gesture` after making the usual plan,
binary, evidence, and diagnostic disks available in this worktree. Each command
starts a fresh boot with the SMP plan and 180 seconds of guest-running capture
budget. Output directories are separate. The display window uses Cocoa.

A: original bottom Y, slower rightward movement with initial/final holds:

```sh
env VPHONE_TCG_THREAD=multi VPHONE_DISPLAY=cocoa VPHONE_TOUCH_FORCE=0 \
  VPHONE_TOUCH_SCRIPT='at=125;down 60,1018;hold 0.15;move 600,1018 dur=0.8 steps=48;hold 0.1;up' \
  python3 backend/display_probe.py \
  --plan backend/evidence/keybag-contract-20261015/boot-plan-smp/boot_plan.json \
  --seconds 180 --stop-condition duration --output /tmp/t17-probe-a \
  --user-hooks backend/touch_ui_hooks.json \
  --max-hits ui_=0 --max-hits hid_client_=0
```

B: center-screen rightward drag to test a different hit-test region:

```sh
env VPHONE_TCG_THREAD=multi VPHONE_DISPLAY=cocoa VPHONE_TOUCH_FORCE=0 \
  VPHONE_TOUCH_SCRIPT='at=125;down 60,568;hold 0.15;move 600,568 dur=0.8 steps=48;hold 0.1;up' \
  python3 backend/display_probe.py \
  --plan backend/evidence/keybag-contract-20261015/boot-plan-smp/boot_plan.json \
  --seconds 180 --stop-condition duration --output /tmp/t17-probe-b \
  --user-hooks backend/touch_ui_hooks.json \
  --max-hits ui_=0 --max-hits hid_client_=0
```

C: bottom-screen leftward drag, testing scroll direction rather than assuming
that the rightward label motion proves the accepted pan direction:

```sh
env VPHONE_TCG_THREAD=multi VPHONE_DISPLAY=cocoa VPHONE_TOUCH_FORCE=0 \
  VPHONE_TOUCH_SCRIPT='at=125;down 600,1018;hold 0.15;move 60,1018 dur=0.8 steps=48;hold 0.1;up' \
  python3 backend/display_probe.py \
  --plan backend/evidence/keybag-contract-20261015/boot-plan-smp/boot_plan.json \
  --seconds 180 --stop-condition duration --output /tmp/t17-probe-c \
  --user-hooks backend/touch_ui_hooks.json \
  --max-hits ui_=0 --max-hits hid_client_=0
```

All three are **INFERRED diagnostic candidates**, not verified unlock recipes.
Compare SpringBoard identity/PID on `ui_hid_callback`, queue/dispatch gates,
`ui_send_event`, `ui_gesture_state`, and screenshots. Callback count can change
through UIKit coalescing; producer frame count alone is not completion evidence.
For an unhooked visual run, use the same environment/script with
`python3 backend/live_run.py --plan backend/evidence/keybag-contract-20261015/boot-plan-smp/boot_plan.json`.
