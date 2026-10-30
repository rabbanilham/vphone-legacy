# Testing

## Firmware-free checks

```sh
make setup
make test
.venv/bin/python scripts/check_docs.py
zsh -n scripts/setup_venv.sh scripts/build_app.sh vphone-legacy
bash -n research/tasks/merge-live.sh
```

Default discovery runs Python tests and small compiled C harnesses. A host compiler is needed for those harnesses. It does not boot iOS or require a private VM. Missing optional firmware, dump corpora, or backend build tools produce explicit skips; inspect the skip summary before interpreting coverage. CI runs this path on macOS, with no firmware or private artifacts.

To run the three probe-164 dump test modules against an external corpus, set `VPHONE_AGX_DUMPS=/absolute/path/to/dumps`. Keep the original `frame-N.bin` files and sidecars together. Other historical corpus tests use their documented local `build/` or `backend/evidence/` locations. Never commit memory dumps.

## Backend checks

After building the project QEMU:

```sh
.venv/bin/python backend/smoke_test.py
.venv/bin/python backend/agx_contract_test.py
.venv/bin/python backend/touch_input_contract_test.py
.venv/bin/python backend/spi_touch_contract_test.py
.venv/bin/python backend/smp_contract_test.py
```

These execute synthetic guest contracts. UART/framebuffer success is not native iOS acceptance. QMP requires local Unix sockets. Native boot acceptance must independently record source/backend/input hashes, serial output, screenshots, timing, and panic/exit status.

## Optional integration

`VPHONE_TEST_BUNDLE=/absolute/path/to/bundle` enables lifecycle integration in `test_backend_lifecycle.py`. It launches QEMU and force-stops the guest, modifying runtime/session state: use a disposable prepared bundle. `VPHONE_REAL_FIRMWARE_TEST=1` enables real-firmware tests. `VPHONE_TEST_IPSW` and `VPHONE_TEST_ANS_SOURCE` override the local IPSW and extracted ANS file paths respectively; absent overrides use the historical checkout layout. Inspect `test_firmware_gate.py` and `test_ans_image.py` before enabling it. These tests are not part of firmware-free CI.

See [publication audit](publication-audit.md) for checks actually performed during repository preparation and their limitations.

## Performance suite

Only the orchestrator runs live measurements, sequentially, on an otherwise quiet
macOS host. Close other guests, builds, screen capture and CPU/GPU-heavy apps;
keep power and thermal conditions consistent. Guest time follows host time.
The suite uses the host-wide `/tmp/vphone-perf-live.lock` directory; do not remove
an active lock. New Python runners record a PID and recover a dead PID under an
advisory guard. Legacy owner-only locks require manual inspection/removal; an
unknown owner is never assumed stale. PID reuse can prevent automatic recovery.
Keep renderer concurrency k=1; the suite does not alter renderer scheduling,
guest timing, interrupt order or pixel output. Do not run it in `make test`.

Prepare the virtual-activated profile and build the backend first (see
[firmware.md](firmware.md) and [setup.md](setup.md)). A native macOS player,
Swift compiler, codesign, `ps`, and a VNC-enabled QEMU are required. No firmware
or disk images are downloaded by the suite. Disk clones are temporary and
removed after each run. The actual Swift player is built via
`backend/live_run.build_player`; a second Raw RFB client observes updates only.

```sh
python3 scripts/perf_suite.py --list-steps
# Orchestrator only; default repeat=3, sequential, separate disposable disks:
python3 scripts/perf_suite.py --qemu-binary backend/bin/qemu-system-aarch64
python3 scripts/perf_suite.py --repeat 3 --baseline latest
python3 scripts/perf_suite.py --out build/perf/candidate --baseline build/perf/BASELINE
```

`--out` must be a new directory. Default output is
`build/perf/<git-hash>-<UTC timestamp>/`. Runs remain sequential (k=1), with
unchanged guest actions and disposable disk clones. The suite refuses a start
when the 1-minute host load exceeds 3 or `ps` finds another QEMU/player. The
lock is retained. `caffeinate -dims` runs internally and is cleaned up on exit;
load averages at start/end are recorded, including aborted measurement data.
Both processes receive `VPHONE_PERF_STATS=1` and `VPHONE_PLAYER_STATS=1`.

The player is activated with the macOS application API before measurement and
at each step. Activation failure aborts. Keep an unlocked, awake desktop with
the player visible throughout; foreground activation cannot prove continuous
visibility if the user changes windows. The suite preserves `player-input.log`
in addition to both stderr logs and serial output, reads player JSON from both
locations, and deduplicates mirrored records. No player stats for 15 seconds
during the action phase invalidates the run. No steady-state receive window
with fps>1 or no presentation samples also invalidates it, with explicit issues.

Schema **2** rejects old baselines. `perf.json` retains raw windows, process
samples, markers, attempted input events, per-step metrics and run aggregates.
Missing values stay `null`, with `available_runs` and `total_runs`; a partial
aggregate must not be read as all repetitions succeeding. Aborted runs preserve
`run-N/measurement-raw.json` and logs, and have status `failed`; missing player
coverage yields `invalid`. Neither is eligible for `--baseline latest`.

The fixed sequence unlocks, swipes Home left/right, opens Settings, toggles
Airplane Mode, returns Home, swipes left and opens Extras. The existing unlock
script ends at virtual 103s. Boot includes all windows through the first pacing
report at/after virtual 105s. Entire windows crossing that boundary or an action
marker are retained as `boundary` and excluded from steady/per-step aggregates.
Steps are dispatched at pacing reports (5 host seconds resolution); a missed
schedule by >10 virtual seconds aborts rather than replaying actions rapidly.
Each producer clock is anchored to its first log receipt on the suite clock
(approximately 20 ms polling resolution); raw timestamps and offsets remain
available. A suite polling gap or pacing backend-clock gap >15 seconds, or
backend/suite elapsed-time disagreement >10 seconds, aborts, preserving
evidence. Large stalls are **not** deleted and then reported as a healthy run.

Measurement definitions:

- Pacing: vblank and swap-complete **host** intervals; callback lateness relative
  to the **virtual** deadline. Merge 100 us histogram counts for p95/p99/max;
  overflow uses its observed maximum, hence tail quantiles are upper bounds.
  Count windows whose metric maximum exceeds 50 ms as `jank_windows` (a long
  swap interval can represent idle display time, not necessarily animation jank).
  New records include the window start and first interval start; intervals
  originating in boot or across a marker are excluded per metric. For legacy
  records, use the previous report as window start and conservatively bound
  interval start by window start minus its maximum; ambiguous intervals stay
  excluded. Old ANS lifecycle totals still cannot yield steady ANS metrics.
- `swap_request_ms.p95`: histogram of the oldest pending DISP0 commit to its
  modeled swap completion on the host clock. Both FIFO and direct commit paths
  stamp the request without changing guest decisions or timer scheduling.
  Repeated requests while pending are coalesced. Histogram name is
  `metrics.swap-request`; the legacy top-level `swap_request_ms:null` remains
  for compatibility and is not used by the parser.
- Player: `fps` means **received** frames/second. `dropped` counts overwritten
  pending frames in that receive window. Report their window means, and maximum
  reported window p50/p95/p99/max for recv/update/present/arrival-to-present,
  explicitly named `window_*_max`; these are not pooled frame quantiles.
  `present_ms` is the interval between actual present callbacks, including idle
  gaps, not rendering cost. `arrive_to_present_ms` starts at RFB surface commit,
  not network packet arrival or physical display scanout. The minimal Swift
  instrumentation fix adds sample counts, host uptime/window duration, and uses
  monotonic time for stats. Empty sample arrays and old JSON without counts
  produce unavailable timings rather than invented zero milliseconds.
- ANS: periodic pacing JSON now includes window request/fsync counts and a
  log2-nanosecond handler histogram plus exact observed maximum. Percentiles
  ending `_le` are inclusive bucket upper bounds. `background_fsync_bql_ns=0`
  describes only background worker fsync, not explicit barriers. Lifecycle
  `ANS perf:` lines are parsed separately as `ans-lifetime`; cumulative boot/
  shutdown totals are never attributed to a step or silently summed twice.
- CPU/RSS: per-process mean/p95 of approximately 1 Hz `ps %cpu` samples and
  sampled maximum RSS, for each steady step and the steady run. `%cpu` retains
  macOS averaging semantics, so this is not instantaneous per-step CPU time.
  Missing process samples remain unavailable; RSS peaks between samples can
  be missed. `step.<name>.*` keys participate in aggregation/comparison.
- Boot time remains the first >=30 changed icon-area rows, a framebuffer proxy,
  not semantic lock-screen recognition. It is reported separately from steady
  metrics.

**Input latency is currently unavailable**, with `valid=false`, reason and
valid/attempted counts for every step. No pooled tap/swipe number is emitted.
The required definition is host monotonic time from the submitted touch-begin
(tap/toggle/folder) or first submitted touch-move (swipe) to the first meaningful
RGB framebuffer change in that action's relevant region. Preconditions must
identify the current screen and show a quiet region before injection. Proposed
regions and screen preconditions are recorded in `INPUT_CONTRACTS`: Settings
content, the Airplane switch, Home icon grid, and expanded folder content.
These regions are definitions pending live validation, not active validators.
A digitizer-gated action is recorded and skipped, not silently woken and timed.

The old suite started before the whole blocking gesture and accepted **one
changed row anywhere in y=120..899**, without quieting, screen identification,
RGB magnitude/area threshold, or causal validation. Settings, swipes, switch
and folder shared that detector. It could therefore time unrelated animation,
queued RFB work or a missing response's next update. `input_latency.py` uses a
similar icon-area detector but starts at first pointer move, waits for quiet,
and uses VNC pointer gestures; its numbers are not interchangeable with QMP
suite numbers. Synthetic tests confirm unvalidated 1.6/1766 ms observations
cannot become official latency metrics. End-to-end validation still requires
orchestrator runs with known delayed visual responses, unrelated/status-bar
animation, no-op input, timeout and per-action screen/ROI checks. Do not enable
latency reporting until those cases distinguish real responses from noise.

A/A calibration (orchestrator only):

```sh
python3 scripts/perf_suite.py --self-test --repeat 3 --qemu-binary /path/to/base --out build/perf/aa
python3 scripts/perf_suite.py --qemu-binary /path/to/candidate --baseline build/perf/aa
```

`--self-test` uses the **same binary** at least twice, sequentially, and records
per-metric maximum relative deviation from the median as `aa_noise`. Thresholds
are the greater of the existing 15% (20% input) floor and twice that A/A
variation; lower receive FPS is a regression, higher times/resources are
regressions. All available metrics are compared, including per-step fields.
Same-binary baselines can supply variation from their repeated runs. Metrics
without calibration are `uncalibrated`; missing metrics are `unavailable`.
Neither is falsely labeled a regression or a pass. Two runs only give a coarse
noise estimate; prefer >=3, on the same profile/host/load conditions. Counts and
idle-sensitive metrics still require workload inspection.

W0-c investigation of supplied six runs (read-only log excerpts in
`tests/fixtures/perf/`):

- **Proven:** all runs contain a huge simultaneous host/virtual report gap.
  Base run 3 jumps 25.04→953.13 virtual seconds, with vblank-late max
  926027.369 ms; candidate run 3 jumps 25.02→952.19, max 925837.014 ms.
  However perf.json suite receipt time advances only 25.15→27.47s (base) and
  25.11→26.55s (candidate). Thus the backend clock domain diverges from Python
  monotonic time; a 15-minute process suspension is **not proven**. This is not
  merely an incorrectly initialized virtual deadline. It occurs
  during boot, before the gesture schedule, and makes that schedule unusable.
  There is no SIGSTOP/SIGCONT in the suite. Host sleep with differing clock
  semantics, or a backend clock discontinuity, is plausible; logs do not
  establish which. Ordinary process suspension alone does not explain the
  different elapsed times.
  Sampling via `ps` and the RFB observer do not intentionally suspend QEMU.
- **Proven:** candidate player logs have only 0–1 JSON receive windows per run,
  with no fps>1; base run 1 receives frames while dropping pending frames with
  empty presentation quantiles. Stats environment did reach at least those
  players; it cannot explain their empty present samples. A non-running display
  link due to hidden/occluded/sleeping display is a hypothesis requiring live
  verification. The original scratchpad contains no saved `player-input.log`,
  so alternate-file coverage cannot be established retroactively.
- **Proven:** the old four aggregate metrics included empty-array zeros and
  causally unvalidated input, making the reported base/candidate comparison
  unsuitable for performance conclusions. New parser tests use exact small
  source-log excerpts; firmware-free C and Swift tests use known samples.

No VM was booted and no live suite was run for W0-c. Foreground/awake behavior,
A/A noise on real hardware, input causality and the rebuilt backend's live
windows remain for orchestrator verification. Instrumentation adds histogram
updates and periodic logging; its overhead and the extra RFB observer are
shared measurement costs, not established as negligible.

For an offline, pixel-checked renderer comparison:

```sh
python3 scripts/agx_bench.py --base main --repeat 3 --jobs 1 --max-slowdown 1.15
```

`--max-slowdown R` fails if total candidate/base replay time exceeds R, even
with `--allow-diff`. Pixel differences still fail independently. `--jobs 1` is
the default; retain it for stable timing. `--repeat N` preserves the existing
fastest-per-frame estimator, rather than reporting a median. Use the same
corpus and host conditions for both builds. This command replays an existing
corpus; it does not capture or boot a guest.
