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
`build/perf/<git-hash>-<UTC timestamp>/`. `latest` resolves the newest completed
`perf.json` before creating the candidate directory. Both processes always get
`VPHONE_PERF_STATS=1` and `VPHONE_PLAYER_STATS=1`. Each repetition preserves
`qemu-stderr.log`, `player-stderr.log`, and `serial.log`. Versioned `perf.json`
contains metadata (git/dirty state, binary SHA256, profile/plan, OS/model, TCG
thread mode and VPHONE/QEMU environment), raw process samples, input events,
virtual markers, pacing windows, per-step distributions, run aggregates and
comparison rows. `summary.txt` reports median and min/max across repetitions.
Do not share captured environment metadata without checking it for secrets.

The fixed sequence unlocks, swipes Home left and right, opens Settings, toggles
Airplane Mode, returns Home, swipes left and opens Extras. Settings/switch/folder
coordinates come from `agx_corpus.SCENES`. Unlock uses the established backend
virtual-time script; later gestures use the player's QMP `input-send-event/mtt`
events and scaling. No Python RFB pointer input substitutes for those gestures.
Markers dispatch at the next 5-second pacing report, so actual virtual start
and host start are recorded alongside each target. Whole log windows are
attributed to the step active when read; boundary windows can straddle steps.
This is sampling granularity, not an exact virtual-time scheduler.

Metric definitions and limits:

- Pacing histograms: vblank/swap-complete host intervals and vblank callback
  lateness against its virtual deadline. Nearest-rank p50/p95/p99/max use 100 us
  upper-bound bins; overflow uses the observed maximum. JSON includes counts
  for correct histogram merging; legacy `[pacing 5s]` text remains available.
- Player `present_ms`: existing p50/p95/p99/max per log window. The comparison
  uses the **maximum reported window p95**, not a pooled end-to-end percentile.
  Raw per-frame timestamps are necessary for a true pooled percentile.
- Input latency: host send time to first changed icon-area row in a separate
  RFB stream; tap/swipe distributions and timeouts are retained. Unrelated
  animations can cause false attribution. This measures framebuffer delivery,
  not physical display scanout or Swift presentation completion.
- CPU/RSS: per-process `ps %cpu,rss` at approximately 1 Hz; per-step mean/p95
  and sampled RSS maximum. `%cpu` has the host's averaging semantics. Short
  RSS peaks can be missed. Overall CPU is the sum of process sample means.
- Boot: host launch to first update changing at least 30 icon-area rows, plus
  the latest sampled virtual time (5-second granularity). This is a first
  meaningful framebuffer proxy, **not verified lock-screen recognition**.
- Swap-request to completion is explicitly `null`: request timestamps require
  changes to the display MMIO request sites outside this task's permitted C
  timer/pacing region. No proxy duration is reported as request latency.

A candidate fails if median across runs exceeds baseline by more than 15% for
present window p95, 20% for median input latency, 15% for combined CPU, or 15%
for host boot time. Missing/zero baseline values fail comparison; missing
required candidate metrics or unanswered measured gestures fail completeness.
These coarse metrics must be inspected with raw windows before claiming a
performance improvement. Schema mismatches are rejected. Parser, histogram,
percentile and comparison tests run without firmware or booting QEMU.

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
