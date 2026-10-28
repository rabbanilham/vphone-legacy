# Workstream A: CPU, timer and host scheduling

2026-10-28, branch `perf/stab-cpu`. No live VM boot, `perf_live.sh`, push,
or access to another checkout was performed. This is implementation and
firmware-free evidence, not a measured improvement in native frame pacing.

## Changes

- Patch 0007 requests Darwin `QOS_CLASS_USER_INTERACTIVE` on MTTCG `CPU N/TCG`
  and single-thread `ALL CPUs/TCG` threads before their start routines. The
  main loop requests the same QoS inside `qemu_main_loop`, including when Cocoa
  runs it on a separate thread. Errors are reported; non-Darwin builds retain
  their scheduling policy. Existing App Nap protection (0003/0005) remains.
- `runtime.py`: optional `VPHONE_TASKPOLICY=foreground` prefixes the actual
  recorded launch argv with `/usr/bin/taskpolicy -B` on macOS. Default `inherit`
  preserves the caller's task policy. `-B` clears inherited background policy;
  it does not grant thread QoS. Unknown policy/non-macOS foreground requests
  fail explicitly. No shell or post-launch PID race is involved.
- Vblank retains a virtual-clock deadline and advances it by 16,666,667 ns,
  rather than adding that period to callback arrival time. Reset establishes
  a fresh deadline. Overdue ticks are retained, with the same drain, swap-token,
  IRQ-status and IRQ assertion sequence. There is no new renderer skip.
- Patch 0006 replaces the unconditional 8 ms VNC polling override with a
  board-invoked refresh after each vblank. Only connected listeners displaying
  this console are refreshed. The existing VNC surface dirty comparison,
  client requests, throttling and worker locking remain authoritative. The
  upstream adaptive refresh remains as a fallback (including while paused).
  Socket input handlers are untouched. This is vblank-driven presentation,
  not a promise to deliver every rendered intermediate surface over RFB.
- `VPHONE_PERF_STATS=1` also reports monotonic host-time intervals for vblank
  callbacks and completed swaps, every approximately 5 seconds. p50/p95/p99
  use nearest-rank upper bounds of 100 us bins; intervals >=102.3 ms share an
  overflow bin whose reported bound is the observed maximum. `max` is exact.
  Last timestamps survive reporting windows; reset clears them. A paused VM
  can therefore appear as a long wall-clock interval after resume. Stats do
  not affect guest scheduling and are disabled by default.

## Timer review and invariants

The pinned QEMU virtual clock follows host monotonic time while the VM is
running, including CPU WFI, and stops for VM pause. WFI alone does not require
converting device timers to `QEMU_CLOCK_REALTIME`. Moving vblank or the stamp/
meta timers to real time would let them mutate guest state during pause and
change their ordering relative to virtual-clock IRQs. Keep the clock domains.
See pinned [cpu-timers.c](https://github.com/qemu/qemu/blob/4fc49f46dc95d4a27de2509e7fceb2931e91faeb/system/cpu-timers.c).

Stamp re-poll remains 200 us and meta retry remains 100 us. They are relative
one-shot dependency retries, not a periodic display cadence. They can still
wake late when the host/main loop is delayed; neither their requested interval
nor a nanosecond timer API guarantees host wake-up precision. QoS targets that
host-side delay without changing their guest-visible timing. No changes were
made in original `s5l8960x.c` lines 5300–9600; byte comparison passed.

k=1, guest clock rate, CPU timebase, draw order, interrupt bit definitions,
AGX queue processing and TB invalidation remain unchanged. No `tb-size` tuning
was made. The existing performance report's boot-time TB lookup/flush findings
do not establish TB-cache capacity pressure during WFI-heavy UI operation.
Larger TB caches cannot be assumed to fix UI pacing; measure cache exhaustion/
flush reasons separately before changing them. Keep the GPU-store TB invalidation
fix described in P-performance.md.

## Validation and limits

- `make test PYTHON=python3`: 352 tests, 317 passed, 32 skipped, 3 errors.
  All three errors are sandbox `PermissionError: Operation not permitted` at
  local socket bind in existing tests: QMP partial frames, player Control Center,
  and player fragmented RFB. The suite is not fully green in this environment.
- Four new tests pass: structured taskpolicy argv/validation; Darwin QoS patch
  additions compiled with host SDK and `cc -Werror -fsyntax-only`; VNC patch
  function compiled/executed against mock listener lists; actual board vblank
  callback and histogram helpers compiled/executed with mocked QEMU services,
  checking late deadlines, swap token/IRQ and overflow statistics.
- `python3 -m py_compile scripts/qemu/runtime.py tests/test_cpu_pacing.py` and
  `git diff --check` pass. Both patch files parse with `git apply --stat`.
- Full backend compilation and full patch application remain unverified:
  `build_backend.py` reports missing pinned source, and fetching QEMU failed
  because the sandbox could not resolve github.com. No other checkout was used.
- `agx_bench.py` was not run: renderer core is unchanged and no corpus is present
  in this worktree. Firmware/corpus/native UI skips remain unverified.

Orchestrator follow-up: build the pinned backend, verify patch application,
then compare vblank/swap p50/p95/p99/max with identical k=1 input and host load.
Check WFI wakeups, pause/resume, startup VNC connection, worker contention,
and guest IRQ acknowledgement after a long host stall. Catch-up can produce
back-to-back overdue callbacks; existing level-triggered IRQ status can coalesce
before the guest acknowledges it. No claim is made that queued historical
interrupts become distinct guest-observed edges. VNC encoding at vblank adds
work under BQL; QoS may increase host energy use/competition. Native validation
must determine whether either tradeoff improves or regresses pacing.

Commit limitation: `git add` and `git commit` were attempted for the focused
QoS/launcher change. Both failed creating the shared worktree `index.lock`
(`Operation not permitted`); Git metadata is outside the writable sandbox.
All changes remain uncommitted. Suggested commit split: QoS/launcher, then
vblank/VNC/statistics/tests/report. No push was attempted.
