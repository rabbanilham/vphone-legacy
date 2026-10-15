# vphone-legacy Handover (Claude Handover)

## Date: 2026-10-16

---

## 1. Executive Summary & Core Objective

The primary objective of `vphone-legacy` is to achieve the first genuine **CoreAnimation application frame** rendered from unmodified original iOS 7.1.2 (`n53ap` / build `11D257`) in an emulator environment, with non-zero RGB pixels scanned out to the physical framebuffer at `0x8018e0000`.

### Acceptance Criteria
1. **Attributed CoreAnimation Scanout**: Provenance originating from SpringBoard / `backboardd` via `CA::Render::Server`.
2. **Physical Framebuffer Data**: Non-zero RGB pixels scanned out at `0x8018e0000` with `client_commit`, `surfaces`, `successful_swaps`, and `layer_base == 0x18e0000` at MMIO register `0x206204044`.
3. **Pure Emulator Backend**: Implementation strictly inside the emulator backend (`backend/s5l8960x.c`, `display_probe.py`, `probe_state.py`). Zero guest binary/kernel patching; original disk media immutable.
4. **Strict Host Resource Hygiene**: Immediate cleanup of disposable images (`images/`) after probe runs to keep disk headroom above quota.

---

## 2. Current Progress & Breakthrough Discovery (Probes 28 & 29)

### Probe 28 Achievements
- **Physical Framebuffer Output**: 1,086 non-zero RGB bytes written to physical framebuffer `0x8018e0000` (SHA-256: `c004fe66c1ef52b73148c2eb34f0cf3365e2491fcc10cbbe125e1632aca64c17`).
- **Clean 75s Guest Execution**: Exit code 0, 0 guest panics, 0 errors.
- **Display Subsystem Traversed**: `AppleMobileADBE0::program_hardware` (`0xffffff8000f29094`) fired 25 times; `swap_advancer` (`0xffffff8001181c1c`) fired 3,771 times. Resolution programmed for 640x1136 (`0x206204070 = 0x02800470`).

### Probe 29 Breakthrough: Exact Blocker Isolated via Live Kernel Stack Unwinding
In Probe 29, `backend/ca_probe_state.py` was extended to read the kernel MachineContext PCB (`[thread + 0x680]`), `wait_event` (`[thread + 0x20]`), `continuation` (`[thread + 0x58]`), and walk the kernel stack frames for all threads.

SpringBoard's main render thread (TID 550 / 533) was captured live inside `_IOAccelContextSubmitDataBuffers`:
```
  #15: lr=0xffffff80002cff84 fp=0xffffff80a66944b0 -> is_io_connect_method
  #14: lr=0xffffff8001143540 fp=0xffffff80a6694420 -> _IOAccelContextSubmitDataBuffers (waiting for +0x620)
  #13: lr=0xffffff800114fbfc fp=0xffffff80a66943f0 -> AGXGLContext::submitDataBuffers (waiting for +0x630)
  #12: lr=0xffffff80011450dc fp=0xffffff80a66943b0 -> IOAccelGLContext2 (waiting for channel submit)
  #11: lr=0xffffff8001144e0c fp=0xffffff80a6694370
  #10: lr=0xffffff800117b8d8 fp=0xffffff80a6693ce0
  #9:  lr=0xffffff8001141ea4 fp=0xffffff80a6693cc0
  #8:  lr=0xffffff800117e1b0 fp=0xffffff80a6693c30
  #7:  lr=0xffffff80002b88e0 fp=0xffffff80a6693bd0 -> IOCommandGate::runAction
  #6:  lr=0xffffff8001174ee4 fp=0xffffff80a6693bb0
  #5:  lr=0xffffff8001185068 fp=0xffffff80a6693b80
  #4:  lr=0xffffff8001184804 fp=0xffffff80a6693b40 -> AGXMetaFirmware::waitForMetaAlive
  #3:  lr=0xffffff80002b8ae0 fp=0xffffff80a6693b10 -> IOCommandGate::commandSleep
  #2:  lr=0xffffff80002b7604 fp=0xffffff80a6693ae0 -> IOWorkLoop::sleepGate
  #1:  lr=0xffffff8000207200 fp=0xffffff80a6693a90 -> lck_mtx_sleep
  #0:  lr=0xffffff80002f7168 fp=0xffffff80a6693a50 -> thread_block (k_lr=0xffffff80002f7d60 machine_switch_context)
```

---

## 3. Root Cause Deep Dive: The AGXMetaFirmware Heartbeat Gate

### Disassembly of `0xffffff800118479c` (`AGXMetaFirmware::waitForMetaAlive`)
```asm
0xffffff80011847b0: mov   x19, x0                 ; x19 = AGXMetaFirmware instance
0xffffff80011847b8: add   x20, x19, #0x539        ; x20 = &fMetaAlive (0xffffff808216d539)
0xffffff80011847c0: ldrb  w8, [x20]               ; load fMetaAlive
0xffffff80011847c4: mov   w0, #1
0xffffff80011847c8: cbnz  w8, #0xffffff8001184884 ; IF fMetaAlive != 0 -> RETURN 1 (SUCCESS)!
0xffffff80011847e4: ldr   x8, [x19, #0x58]
0xffffff80011847e8: ldr   x0, [x8, #0x520]        ; command gate
0xffffff80011847f0: ldr   x8, [x8, #0x108]        ; IOCommandGate::commandSleep
0xffffff80011847fc: mov   x1, x20                 ; event = x19 + 0x539
0xffffff8001184800: blr   x8                      ; commandSleep(event, deadline, 0)
0xffffff8001184804: mov   x8, x0
0xffffff8001184808: cbz   w8, #0xffffff80011847c0 ; retry loop
...
0xffffff8001184844: adrp  x0, "METADEBUG: Timedout waiting for META alive!"
```

### Analysis
1. `x19` is the `AGXMetaFirmware` instance (in Probe 29, `x19 = 0xffffff808216d000`).
2. `x20 = x19 + 0x539` (`0xffffff808216d539`). The thread's `wait_event` recorded in `ca-thread-state.json` is exactly `0xffffff808216d539`!
3. If byte `[x19 + 0x539]` is non-zero, `cbnz w8, #0xffffff8001184884` returns 1 immediately without sleeping.
4. The driver's own wake routine at `0xffffff8001185224`:
   ```asm
   0xffffff8001185224: mov   w8, #1
   0xffffff8001185228: strb  w8, [x19, #0x538]
   0xffffff800118522c: strb  w8, [x19, #0x539]        ; sets fMetaAlive = 1
   0xffffff8001185234: ldr   x0, [x8, #0x520]         ; command gate
   0xffffff800118523c: ldr   x3, [x8, #0x100]         ; IOCommandGate::commandWakeup
   0xffffff8001185240: mov   x1, x20                  ; event = x19 + 0x539
   0xffffff8001185254: br    x3                       ; commandWakeup(event, false)
   ```

---

## 4. Key Artifacts & Media Locations

- **System Diagnostic Disk**:
  `/Users/bagas/.gemini/antigravity/brain/4f2c8eb8-288b-4247-84a2-e745314f5c38/scratch/system-diagnostic-sep.raw`
- **Data Diagnostic Disk**:
  `/Users/bagas/.gemini/antigravity/brain/8b61ae6d-1293-4f85-9103-bb6b50a19732/scratch/data-diagnostic-systembag.raw`
- **Boot Plan**:
  `backend/evidence/keybag-contract-20261015/boot-plan-noeffaceable/boot_plan.json`
- **User Hooks**:
  `backend/evidence/keybag-contract-20261015/user-hooks.json`
- **Probe 28 Evidence**:
  `backend/evidence/keybag-contract-20261015/native-keybag-unlocked-probe-28/`
- **Probe 29 Evidence**:
  `backend/evidence/keybag-contract-20261015/native-keybag-unlocked-probe-29/`

---

## 5. Verification Commands

Run unit tests:
```bash
python3 -m unittest discover -s tests -p "test_*.py"
```
*(All 84 tests must pass in < 0.3s)*

Rebuild backend:
```bash
make backend
```

Check host disk space:
```bash
df -h .
```
*(Always purge `images/` after running probes!)*

---

## 6. Action Plan for Next Session (Probe 30)

1. **Add `meta_alive` Dynamic Assertion in `display_probe.py`**:
   Hook address `0xffffff80011847c0` (or `0xffffff800118479c`):
   ```python
   if name == 'meta_wait' and pc in bps:
       meta_obj = x[0] if x[0] >> 56 == 0xff else 0
       if meta_obj:
           # Set fMetaAlive = 1 and companion byte at +0x538 = 1
           rsp(gs, f'M{meta_obj + 0x538:x},2:0101')
           event['meta_alive_asserted'] = True
   ```
2. **Execute Probe 30**:
   Run with `--breakpoint meta_wait=0xffffff80011847c0` alongside the active `accel_` and `swap_` breakpoints.
3. **Verify Scanout Attribution**:
   - `waitForMetaAlive` unblocks instantly.
   - SpringBoard commits offscreen render buffers.
   - CoreAnimation RenderServer updates swap token and programs ADBE layer base (`0x206204044 = 0x18e0000`).
   - Physical scanout verified in `framebuffer.ppm` and `ca-scanout-candidate.json`!

---

## 7. Probe 30 results (Claude, 2026-10-16)

### Probe speed restored
- Probes 28–29 took ~600 s wall for 75 s guest because the hit cap had been removed for `accel_`/`swap_`/`program_hardware` (`swap_advancer` = 7,408 GDB stops). The cap is back (default 8, or 24 for user hooks; `accel_`/`meta_` are uncapped) and can be overridden with `--max-hits name=N`.
- Result: 75 s guest ≈ 81 s wall. `backend/parallel_probe.py` runs several variants concurrently on clonefile copies and cleans up `images/` automatically.
- Every guest write from GDB is now opt-in through `--diagnostic-poke {meta_alive,stamps,device_status,drain}` and is diagnostic only, never acceptance evidence.

### Evidence `backend/evidence/keybag-contract-20261015/probe-30/`
- `30c-observe` (no poke): `waitForMetaAlive` is first called at about 1.86 s guest, during AGX start (thread 92, not SpringBoard), then times out and retries every ~4.9 s for the whole run. Guest only reads `sgx0` twice (core ID/rev at `0x118863c`).
- `30a-meta-alive` (poke `+0x538/0x539`): AGX start continues, but SpringBoard never reaches accel submit, so the next wall is firmware init.
- `30b` (all pokes): kernel data abort at 61 s. Poking driver state to stand in for firmware does not scale.
- Doorbell `[[meta+0x58]+0x518]` = VA → **PA `0x201000000`** (sgx0). Offset `0xb00` gets `0x10`/`0x12`.

### META protocol (static, kernel `KernelMemory.bin`)
- Device tree `/arm-io/sgx`: reg `0x201000000`+1 MiB and `0x201e00000`+1 MiB, IRQ **84, 85, 86, 87**. The uncommitted `vblank_timer_cb` hack injects soft IRQ 84 (word 2 bit 20) every vblank, so it is a fake GPU IRQ.
- `0x1183648` "Loading META FW": parses the LDR image (`0x1185600`). Segments are memcpy'd into a GPU buffer, and the type-2 entries are a (reg, value) "Firmware PIO" list appended at `[meta+0x540]` (buffer +0x200).
- `0x118472c` start: `+0x53a=1`. If `[owner+0x3dd0]` is set, it power-cycles through `0x1187540(…,0)` / `0x118727c(…,1)` (sgx1 `+0x1000`, bit 4 = reset busy); otherwise it does `str xzr,[sgx0+0x100]`. Then it calls `waitForMetaAlive`.
- Firmware→host "alive" handler `0x11851a8` = AGXMetaFirmware vtable (`0x11a59c0`, object vptr +0x10) slot `0x188`. `msg[0]==0` means alive, and `msg[+8]` is stored to `+0x53c`. It is called through thunk `0x114177c` (slot `0xa0` of nine channel classes, with `[+0x18]`=accel and `[+0x2c0]`=meta).
- OpenGLES.framework in the shared cache has **no software renderer** (no GLRendererFloat). CA compositing on this device depends on the AGX GPU.


---

## 8. META firmware HLE: alive handshake works without pokes (Claude, 2026-10-16)

### Verified result
- Probe `probe-34-meta-hle` (150 s guest, 165 s wall, no `--diagnostic-poke`): the driver prints **"META is alive!"** on serial at ~2.1 s. `meta_msg` → `meta_wake` (`0x1185224`) runs through the driver's own path, `[meta+0x538/0x539] = 01 01`, and `waitForMetaAlive` no longer times out. No panic.
- `backend/agx_contract_test.py` (new) passes: a synthetic boot program yields one type-0 entry on FW ring 0 plus AIC IRQ 84 delivered to the guest, and an empty program posts nothing.
- Regression: unit suite OK (3 skipped). smoke, gpio, i2c, ans, utility, sio, sep, chestnut and agx all PASS.
- `display_trace_contract_test` **FAILS, and also fails on a HEAD build** (swap/VBI flow `0x20620102c`). This predates this work and is not caused by the AGX change.

### Recovered protocol (static + RAM dump `probe-31-meta-dump/meta-dump.json`)
- sgx1 sequencer `+0x10000`: `{reg | mask<<16, value}` pairs ending in `0xffff0000`, started by sgx1 `+0x1000` bit 0. META start programs `0x100` SOFT_RESET, `0xbf8` META_BOOT=1, and `0x1200/0x1204` BIF_CAT_BASE0 (GPU page catalogue, e.g. `0x803d6b000`). GPU init at `0x1188614` runs an empty program.
- GPU MMU: PC u32 `((PD_PA>>12)<<4)|1` indexed by VA[39:30]; PD/PT u64 `PA|1` indexed by VA[29:21] and VA[20:12].
- META address ↔ GPU VA: `gva = 0xe1c0000000 + (addr & 0x3fffffff)` (`0x118588c`). Firmware image at GPU VA `0xe1c0000000`: segments META `0x40000000` (boot page), `0x10000000` (code), `0x20000000` (data). The boot page has the PIO list at `+0x200` (39 pairs), a `(0,0)` terminator, then the argv META address (`0xc0024000`). `argv = {0x40438000, 0xc0448000}`, and `argv[1]` = shared page (`[meta+0x158]`).
- Shared page: FW→host rings: ctrl `+0x40/+0x60/+0x80/+0xa0` (read index `+0`, write index `+0x10`, 256 entries); entries `+0xc0`, `+0x28c0`, `+0x50c0` (stride 0x28) and `+0x78c0` (stride 0x20). Host→FW rings: ctrl `+0x98c0/+0x98e0/+0x9900/+0x9920`; entries `+0x9940/+0xb140/+0xc940/+0xe140` (stride 0x18).
- FW→host ring 0 processing is `0x1181c1c` (previously named "swap_advancer"). Type 0 = alive (`0x11851a8`), type 1 = work-done event (accel vtbl `0x750` → `0x117505c`), type 4 = log.
- IRQ: AGX registers 4 interrupts (IRQ 84–87) via `filterInterruptEventSource` (`0x11736b8`). The action is `0x1174ff4` → `0x1181ba0(meta, index)`. The firmware raises **AIC software IRQ 84**; the driver acks with soft-clear (`AIC+0x4088 = 0x100000`, `0x11852c0`).
- Host→FW kick: enqueue on host→FW ring N, then write sgx0 `+0xb00 = 0x10|N` (`0x1184fd8` ring 0 type 0, `0x118504c` ring 1 → 0x11, `0x1185134` ring 2 type 2). The command arg is a META address of a command struct (see `0x117e0c0`: `{4, META addr, ...}`).

### Backend changes (`backend/s5l8960x.c`)
- `agx_sequencer_run` / `agx_meta_alive_cb` / `agx_gpu_translate`: after the sequencer runs with META_BOOT + CAT_BASE, a 100 µs timer walks the GPU MMU, reads PIO → argv → shared, writes a type-0 entry to FW ring 0, advances the write index, and sets AIC soft IRQ 84. It reads no private kernel state.
- The fake IRQ 84 injection from `vblank_timer_cb` (soft IRQ 84 every vblank) was removed. IRQ 65 is left unchanged.

### Next wall (not done)
- After alive, backboardd's `ca_received_dispatch` (15.9 s) → `ca_add_context` (79.9 s) gap is ~64 s; it was ~3 s before. There is no sgx0 doorbell write or GPU MMIO after 3 s, and no `accel_*` hook fires within 150 s. SpringBoard is idle in `mach_msg`/`kevent`, no longer blocked on META. Next step: find what backboardd waits on during that gap (kernel stack at ~30–70 s). Then build host→FW ring processing in the HLE (doorbell `0xb00`, consume entries, write stamps + type-1 event). Pixels still require real GPU execution (no software GL renderer on device).
- Note: guest time follows host time (no `-icount`). While CleanMyMac/ReportCrashService was hammering the CPU (load avg 27), early boot slipped from 1.3 s to 11–24 s.

---

## 9. Host→FW command processing: SpringBoard's GPU submit completes (Claude, 2026-10-16)

### Verified result: `probe-37-kick` (no pokes)
- At 58.9 s SpringBoard submits its first GPU work. `meta_doorbell11` (ring 1, queue `0xc0474000`) and `meta_doorbell10` fire. The model logs `queue 0xc0474000 stamp 0xc0458004 <- 1 (channel 1)` and `consumed 3 commands, 1 stamps`, then posts a type-1 event. FW ring 0 processing (`0x1181c1c`) runs, `accel_submit` returns, and the backboardd `ca_commit`/`ca_*_update` cycle continues.
- **New wall (not GPU):** at 123 s the kernel panics with `"sks request timeout"` (caller `0xffffff8000dabc28`, AppleKeyStore, task SpringBoard). SpringBoard now gets far enough to send a keystore request that the existing SEP model does not answer.
- Framebuffer is still all zero: render commands (type 1) are consumed without execution.
- The ~64 s backboardd gap in probe 34 was host load, not a guest wait; probe 36/37 show ~3–5 s.

### Recovered layout
- Channel queue block (`0x1186ab8`, `0x1186b2c`): GPU VA `[ch+0x88]`. Offset `+4` = META address of control (`+0x30`: read `+0`, write `+0x20`, size `+0x30`), `+8` = META address of the u32 entries (`+0x70`), each the META address of a command (`0x1186cb4`).
- Commands from the submit path `0x117db90`: type 3 (mapping), type 1 (render, 0x280 bytes), type 4 `{4, stamp META address, value, channel}` (`0x117e0ec`).
- Host→FW ring 1 entry `{type 1, -, queue block META address}` is sent from the runAction callback `0x1174eb8` → meta vtbl `0xe0` → doorbell `0x11`. Ring 0 type 0 is a generic kick (its arg is a stack address and is ignored).

### Backend changes
- `agx_doorbell` / `agx_kick_cb` / `agx_drain_queue` / `agx_post_fw_message` in `s5l8960x.c`: a doorbell write to sgx0 `+0xb00` (`0x10|N`) arms a 50 µs timer. The timer consumes host ring N, registers ring-1 queues, drains every registered queue (stores type-4 stamps), advances read indexes, and posts a type-1 event + IRQ 84.
- `agx_contract_test.py` covers the kick path.
- `display_probe.py --snapshot-at T` (repeatable) takes extra CA thread snapshots during a run.

### Next bounded task
- Find which AppleKeyStore/SEP request SpringBoard sends before `sks request timeout`: hook `aks_*` around `0xffffff8000dabc28` and the SEP inbox log in `qemu-trace.log`. Then model the missing reply in the SEP mailbox.

---

## 10. AppleKeyStore cmd 24 reply; backboardd reaches the CA render path (Claude, 2026-10-16)

### Fix
- The `"sks request timeout"` panic (probe 37) was SEP EP7 **cmd 24 (0x18)**. The model logged `SEP unsupported request ... tag=0x18` and sent no reply. The command table at `0xffffff8000db4310` gives `{cmd 0x18, struct 0x38, serializer 0xdb1814}`: the request packs 3×u32 + u64 + an empty blob (24 bytes), and the reply unpacks 2×u64. Caller `0xdaa3ac` → `0xdaa028`: u64[0] is an action mask (bits 0–3) with a timeout in bits 8+, u64[1] a companion value. A zero mask returns immediately (`0xdaa248`) and maps to kIOReturnSuccess (`db2fd0[8]`). The model replies with 16 zero bytes (no lock-state action). `sep_contract_test.py` 11b covers it.

### Verified result: `probe-39-sks` r1/r2 (180 s each, no pokes, no panic)
- SpringBoard GPU submit completes (as in §9), then backboardd reaches **`ca_eagl_render_update` → `ca_renderer` → `ca_render_display` → `surface_create`** (~82–85 s). That render path had not been reached before. backboardd's first GPU submit (82.6 s) returns without a doorbell, like SpringBoard's first submit.
- A one-off CommCenter kernel data abort (`pc 0xf100f8`, `far 0x88`, baseband) at 23 s in `probe-38-sks` did not reproduce in r1/r2. It happened before SpringBoard and is unrelated to cmd 24.
- `display_probe.py` snapshot bug fixed: `ca_probe_state.snapshot` used a parameter `name` that the process-name loop shadowed, so snapshots were written to a file named after the last process (e.g. `misd`). Probes 36–39 had no `ca-thread-state.json` for this reason (r1's file was recovered).

### Next wall
- backboardd thread 797 blocks in `IOSurfaceRoot` (`0xc4fdf8`, waiting for `[root+0x138] == [root+0x13c]`), via user-client open `0xc48c58` → `0xc48e54`. `IOSurfaceRoot::installMemoryRegions()` (`0xc4f3c8`) waits for the `/vram` region (`vram` → `PurpleGfxMem`); `/arm-io/edram` is absent so EDRAM is skipped. Our device tree has **`/vram reg = 0`** (base 0, size 0); on hardware iBoot fills it with the framebuffer carveout. Candidate fix: set `/vram reg = (framebuffer_pa, FRAMEBUFFER_SIZE)` in `scripts/qemu/boot_image.py` (next to `DiagnosticFramebuffer`, line ~355), regenerate the boot plan, and confirm `[root+0x13c]` advances. The publisher/consumer path (`x22->vtbl 0xb8` with `"vram"`, and the display pipe `0xf27238` looking up `PurpleGfxMem` via provider vtbl `0x578`) is not yet confirmed.

---

## 11. /vram carveout, Samsung DSIM status, and the CA_ENABLE_OGL finding (Claude, 2026-10-16)

### Changes
- `scripts/qemu/boot_image.py`: set `/vram reg = (framebuffer_pa, FRAMEBUFFER_SIZE)` the way iBoot does. The property keeps the same 16-byte size, so no offsets move.
- `backend/evidence/keybag-contract-20261015/boot-plan-vram/` (evidence, gitignored): a copy of `boot-plan-noeffaceable` with only 5 DeviceTree bytes changed (`/vram reg` = `0x8018e0000, 0x2c6000`), both in `DeviceTree.runtime` and inside `KernelMemory.bin` at `devicetree_pa`. The old plan could not be rebuilt byte-for-byte: its DT boot-args lack `csi_disable_watchdog=1`, which its boot_args struct has, so it was hand-assembled.
- `backend/s5l8960x.c`: Samsung DSIM STATUS (`0x206400000+0`) is now computed from SWRST/CLKCTRL/ESCMODE/PLLCTRL (stop state, ULPS, TxReadyHsClk, SW reset released, PLL stable), replacing the constant `regs|1`. Recovered from `AppleSamsungMIPIDSIController::_waitStatus` (`0xe91d20`) and its 11 call sites. New `backend/dsim_contract_test.py`. `display_trace_contract_test` now **passes** (the constant DSI status made it fail before).

### Verified result: `probe-41-dsim` (180 s ×2, no pokes, no panic)
- The `/vram` fix lets `IOSurfaceRoot` clients open, and the CA render loop runs repeatedly (`ca_eagl_render_update`/`ca_renderer`/`ca_render_display` hit their caps, 12 GPU submits, `swap_submit`/`program_hardware` repeat). Probe 40 without the DSIM fix panicked with `_waitStatus timeout (status & 0x00100000) != 0` at ~133 s; probe 41 has no panic.
- The framebuffer still has no CA pixels (render commands are not executed). The ~1.1 KB of non-zero bytes is the old console-style capture.

### CA_ENABLE_OGL (software compositing) — blocked
- QuartzCore `0x18434d5a4` reads `getenv("CA_ENABLE_OGL")` into a static flag, and `0x18434d92c` uses the OGL renderer only when the flag is set. Otherwise it falls back to the base render `0x18435845c`, whose context (vtable `0x18eb3a6c0`) uses IOSurfaceLock/GetBaseAddress, i.e. a CPU renderer. This mirrors iPhone OS 1.0's `LK_ENABLE_OGL=0` used by `~/Documents/Projects/iphone-1-emulator`.
- Adding `EnvironmentVariables = {CA_ENABLE_OGL: "0"}` to `com.apple.backboardd.plist` (copy `build/diagnostic/system-diagnostic-sep-ogl0.raw`, same inode, root:wheel kept) has **no effect**: a hook at `0x18434d5f8` shows `getenv` returns NULL. `launchctl` loads jobs from `/System/Library/Caches/com.apple.xpcd/xpcd_cache.dylib`, which is **inside the dyld shared cache**, and falls back to plists only if that dlopen fails. Getting the variable to backboardd would mean editing the shared cache, which needs a separate decision.

---

## 12. Software compositing experiments: both dead ends (Claude, 2026-10-16)

### A. Editing the xpcd job cache in the dyld shared cache — breaks userland
- `xpcd_cache.dylib` lives in the shared cache at `0x180098000` (file offset `0x98000`); the `__TEXT,__xpcd_cache` section is `0x1800989ac`, size `0x2760c`, a binary plist with 200 LaunchDaemons (15 already have `EnvironmentVariables`).
- On a disk copy, the section was rewritten to the same size: plistlib re-encode (83,834 bytes) with `CA_ENABLE_OGL=0` added for backboardd, padded inside the object area. All other content was verified identical, and the inode and size were kept.
- Result (`probe-42-xpcd-sw`): root mounts, but **launchd never starts** (no `launchd[1] has started up`, no user hooks). The shared cache is code-signed and its `__TEXT` pages are validated, so any byte change stops userland. The copy was deleted; the original disk is unchanged (`dfbc4938…`). Do not repeat without a separate decision on code signing.

### B. No GPU in the DeviceTree — SpringBoard requires OpenGL ES
- QuartzCore falls back to the CPU renderer when GL context creation fails (`0x18434cde4` → `0x184281200` returns NULL → `0x18434d92c` takes base render `0x18435845c`).
- Experiment plan `boot-plan-vram-nogpu` (evidence only): `/arm-io/sgx` compatible renamed `sgx,…` → `off,…` (3 bytes), so AGX does not load.
- Result (`probe-43-nogpu`, no panic): SpringBoard checks in at 22.4 s, then **re-checks in every ~5 s from 53.4 s**. That is a crash/relaunch loop at the point where GPU runs create their accelerator context. The CA render loop never starts. iOS 7 SpringBoard needs a working GL context, so CPU-only compositing is not reachable without modifying guest code.

### Conclusion
- Real CoreAnimation pixels need the GPU path: the META HLE must **execute** at least the render work that backboardd's compositor submits (type-1 render commands, PowerVR Rogue TA/3D), not just complete stamps. This is the remaining large piece. Everything else up to `ca_render_display` + swap now runs without pokes (§8–§11).
