# Task P: CPU and GPU performance

**Branch**: `perf/integrate` (merges `perf/gpu-lat`, `perf/gpu-raster`, `perf/gpu-present`; `perf/cpu` kept separate)
**Workers**: Antigravity (`agy`) for the three GPU workstreams, Codex for CPU/TCG
**Lead / Orchestrator**: Claude (baseline, review, integration fixes, live verification)
**Host**: M4 Pro, 12 cores, with background load (load average about 4.6) during all runs

## 1. Baseline: where the time went

Measured on main `cfa2d0d`. Tools: `scripts/agx_bench.py` (offline renderer timing and pixel diff) and
`scripts/perf_live.sh` (a 160 s merge-live style boot with host `sample` of QEMU at 30, 100 and 145 s).

- **Offline renderer**: all 265 corpus frames take about 250 ms in `agx_replay_frame` (worst frame 26 ms). Raster math is not the main cost.
- **Live boot (t=30 s)**: both vCPUs are about 80% busy in TCG. Boot is TCG-bound.
- **Live UI (t=100 and 145 s)**:
  - Both vCPUs are about 80% halted (WFI).
  - `agx-render` is about 95% idle.
  - The main loop is about 96% in `select()`.
  - The UI is therefore limited by latency and pacing, not by throughput. A doorbell took about 5 ms (p50) to reach completion, because of the 50 µs virtual-clock kick timer, the 1 ms stamp re-poll and the guest-memory walks.
- **Launch flags**:
  - The GUI/CLI path (`scripts/qemu/runtime.py`) ran with `-d guest_errors,unimp,int` (about 100 MB of trace per boot) and with `thread=single` for 2 vCPUs.
  - The native player path drew every update through `[UInt8]` → `Data` → `CGImage` → `NSImage.draw`.

## 2. Changes

| Area | Change | Commit(s) |
| --- | --- | --- |
| Launch | `runtime.py` drops `-d int,unimp` (re-enable with `VPHONE_QEMU_DEBUG`) and defaults to MTTCG (`VPHONE_TCG_THREAD` overrides). The per-frame `[DISP0]` swap prints move to `LOG_UNIMP`. | `af765b1` |
| Tooling | The harness reports `replay_ms`. `agx_bench.py` compares a base and a candidate core pixel by pixel. `perf_live.sh` serializes live runs with a host-wide lock. | `af765b1`, `a191cca` |
| AGX latency | Doorbell kick runs as an immediate QEMUBH. The completion BH re-drains stalled queues at once. Stamp re-poll is 200 µs. `VPHONE_PERF_STATS=1` prints pipeline latency every 5 s. `VPHONE_AGX_ASYNC=1` is experimental and off by default. | `7a1fa9e`, `53ce9cc` |
| AGX memory | Per-thread 1024-entry GPU-VA translation cache with direct host-pointer reads, and a direct page-table walk for RAM-backed tables. | `53ce9cc`, fixes `0dccef1`, `11afbc2` |
| Rasterizer | Morton LUT, hoisted bilinear weights, single-pass triangles, pills and lines, persistent scratch buffers, and a blur fast path. Bit-exact. | `f09afeb` |
| Display | `display_update` caches the display MMU translation, skips unchanged rows and reports tight dirty rects to the console and VNC. | `0c57813` |
| Player | RFB rects stream into a persistent IOSurface, presented through `CALayer.contents`. `VPHONE_PLAYER_STATS=1` prints FPS. | `4f3fe04` |

### Integration fixes found in review and live verification

1. **Stale translations on the main loop** (`0dccef1`).
   - The translation cache is thread-local, but it was only reset on the render thread.
   - `agx_capture_ta` and `agx_trace_rt` run on the main loop, so they could reuse old entries after the guest remapped GPU pages.
   - Fix: both functions now reset the cache on entry.
2. **Stale TBs from GPU stores** (`11afbc2`).
   - In three live runs that included the gpu-lat branch (including that agent's own run, which its report described as clean), Settings was still blank at 152 s. It was drawn only by the end of the run.
   - An env-toggle bisect pinned the cause to the cache: with the cache off, Settings drew on time; with the old kick timing, it was still blank.
   - Cause: stores were done with `memcpy` plus `memory_region_set_dirty`. That marks code pages dirty without invalidating their translation blocks (TBs), so a page recycled from a GPU surface into code could run stale translated code.
   - Fix: stores now go through `address_space_write` using the cached physical address. Reads keep the direct host pointer.

## 3. Results

Offline (`agx_bench.py --base a191cca`, 265 frames): **250 → 150 ms (1.73×), 0 differing frames.**

Live, one run each, `scripts/perf_live.sh` (same touch script):

| Metric | Baseline (`live-baseline2`) | Integrated (`live-fix1`) |
| --- | --- | --- |
| AGX passes completed in 160 s | 21.8k | **27.2k (+25%)** |
| Display swaps | 4.26k | **4.75k (+12%)** |
| Doorbell → completion p50, lock-screen animation | about 5,000 µs | about 300 µs (gpu-lat stats) |
| Lock / Home / Settings at 90 / 105 / 152 s | yes / yes / yes | yes / yes / yes, no panic |
| Player CPU at Home (gpu-present) | 0.72 s | 0.26 s (−64%) |

`make test`: 310 tests OK (13 skipped).

Note: an earlier failure in `test_agx_stride36_dual_texture` came from a stale local `corpus/report/summary.json`. It passes after `agx_corpus.py replay --corpus corpus`.

## 4. Not merged and open items

- **`perf/cpu` (Codex)**:
  - Adds TCG flush/MMIO profiling (`VPHONE_TCG_PROFILE`, `VPHONE_CPU_PROFILE`) and patch 0007, which tracks up to eight disjoint large-page regions per MMU index.
  - One profiled pair showed −19% large-page escalations and no measured boot-time gain.
  - Not merged: patch 0007 changes QEMU's TLB invalidation logic for a gain that has not been shown.
  - Boot remains TCG-bound: `helper_lookup_tb_ptr` is about 23% of vCPU samples at t=30 s.
- **Single runs**: live numbers come from one run each on a loaded host. Repeat on a quiet host before quoting them as a release metric.
- **Async completion** (`VPHONE_AGX_ASYNC=1`): not validated beyond the worker's run, and off by default.
- **SwiftUI GUI path** (`runtime.py`, Cocoa): not re-run live after the launch-flag change. `merge-live.sh` already uses MTTCG with the same machine.
