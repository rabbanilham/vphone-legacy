# vphone-legacy Handover (Claude Handover)

## Date: 2026-10-20

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

---

## 13. GPU Hang Eliminated via TA Queue Draining (Probe 44); Display Hardware Programming & DataMigration Analysis (Claude, 2026-10-20)

### Diagnostic Disks Automated & Permanent
- Stale scratch paths from expired chat sessions were replaced with reproducible permanent builder `scripts/prepare_diagnostic_disks.py` creating permanent copies in `build/diagnostic/`:
  1. `build/diagnostic/system-diagnostic-sep.raw` (SHA256: `8918b9b0c4fcbfee47f52b26f9a0de2103b8a1b6e6feefb662ec8687241bdae1`):
     - Freed 22MB by removing unused `/usr/standalone/update/ramdisk/H6SURamDisk.dmg`.
     - Placed `research/firmware-artifacts/sep-firmware.n53.RELEASE.im4p` at `/usr/standalone/firmware/sep-firmware.img4`.
  2. `build/diagnostic/data-diagnostic-systembag.raw` (SHA256: `a8df5e090c8a172bdaeb79b11c56c1c71794a295aa6561b727cac306cfa7d1d3`):
     - Populated with `/private/var` template from `system.raw` (ensuring `/var/db/` exists so `cc_fips_test` passes without halting).
     - Created `/keybags/systembag.kb` containing unencrypted binary plist `{"KeyBagKeys": b"\x00" * 32}` for `no-effaceable-storage` keybag loading.
- Replaced stale absolute paths pointing to `/Users/bagas/Documents/Projects/vphone/vphone-legacy` with the active workspace `/Users/bagas/Downloads/vphone-legacy` across all boot plans (`boot_plan.json`).

### GPU Hang Bug Root Cause & Backend HLE Fix
- **Root Cause**: In Probe 41, the guest logged `GPU hang: Trying restart GPU ... LastSubmittedStamp[4]=8 stampsPtr[4]=4`. Channel 4 is the Tile Acceleration (TA) queue `0xc0488000`, kicked on Host Ring 0 (doorbell `0x10`). `backend/s5l8960x.c` line 2840 previously only registered queues when `ring == 1`, ignoring Ring 0 TA queue kicks, which caused channel 4 to stall at stamp 4 while 3D (channel 5) advanced to stamp 8.
- **Fix**: In `backend/s5l8960x.c`, updated line 2840 from `if (ring == 1)` to `if (entry[2])` to register and drain queues across all host rings (Ring 0 TA channel queue `0xc0488000`, Ring 1 3D channel queue `0xc0474000`, Ring 2 compute queue). Added logging for non-stamp commands (Type 1 render commands).

### Verified Results: `probe-44-ta` (180s guest duration, zero panics, exit code 0)
- **GPU Hang Completely Gone**: Zero `GPU hang: Trying restart GPU ...` occurrences in `serial.log`!
- **Both Host Rings Draining**: Host Ring 0 and Host Ring 1 both successfully drained commands and updated stamps (`consumed 3 commands, 1 stamps` on both rings).
- **CoreAnimation Active**: CoreAnimation rendered continuously in `backboardd`: `ca_renderer` (7 hits), `ca_render_display` (6 hits), `ca_eagl_render_update` (7 hits), `ca_finish_update` (16 hits), and `swap_submit` (16 hits).
- **Disk Hygiene**: `probe_cleanup.py` executed successfully, verified source hashes and purged disposable cloned disks in `images/`.

### Genuine Application Swap Payloads Recovered
- In `breakpoints.json`:
  * Hits 683, 699, 715 in `swap_submit` passed 312-byte swap payloads with active layer mask `0x03` (both layer 0 and layer 1 active!), dimensions `0x280` x `0x470` (640 x 1136 Retina display for iPhone 5s), advancing swap tokens `0xb`, `0xc`, `0xd`.

### Display Hardware Programming Disassembly (`AppleMobileADBE0::program_hardware` `0xffffff8000f29094`)
- Disassembly revealed the exact mechanism for framebuffer base register `0x206204044`:
  * Lines `0xffffff8000f29608` - `0xffffff8000f29734`: Checks active layer bit `x24` in `[x12, #0x1c8]`.
  * If layer 0 is inactive: branches to `0xffffff8000f29728` which writes `0` to MMIO `0x206204044` (`layer0 base`). This explains why `layer0 base off=0x4044 val=0x0` was logged during early boot swaps.
  * When layer 0 IS active: calls `[surface, vtable + 0xd8]` to retrieve the physical buffer base address and writes it to `0x206204044` at `0xffffff8000f29e9c`!

### Live Thread State Analysis (`ca-thread-state-150s.json`) & Current Blocker
- `installd` (PID 32), `lsd` (PID 26), `backboardd` (PID 98), and `SpringBoard` (PID 31) are alive and active.
- `backboardd` has `CA::Render::Server::server_thread` (TID 860) running and waiting on mach messages.
- `SpringBoard` (TID 507) is waiting on `_BKSSystemAppCheckIn+0xb8` (waiting for backboardd system app check-in acknowledgment).
- `backboardd` worker thread (TID 869) is waiting on a semaphore in `DataMigration.framework` (`0x1869110c8` in `-[DMConnection ...]`, `dispatch_semaphore_wait`), waiting for migration status check before completing system app check-in.

### Next Bounded Steps
1. Trace `DataMigration.framework` in `backboardd` / `data_migrator` daemon to unblock system app checkin completion.
2. Hook the render target surface to scanout at `0x8018e0000`.


---

## 14. DataMigration wall cleared; parallel worker workflow (Claude, 2026-10-21)

### Workflow
- Claude orchestrates; Codex CLI (`codex exec --dangerously-bypass-approvals-and-sandbox -C <worktree>`, stdin from `/dev/null`) implements in git worktrees (`../vphone-wt-<task>`, branch `wt/<task>`); agy (`agy --dangerously-skip-permissions --model … -p="<brief>"`, prompt attached to `-p=`) does read-only RE. Only Claude runs QEMU probes and commits to `agx-meta-hle`.
- `backend/parallel_probe.py` now waits for host load (`--max-load`, default 6) and runs at most `--max-parallel` (2) variants. Each finished variant gets `summary.json`/`summary.txt` from `backend/probe_summary.py`.
- **Always cap noisy hooks.** Uncapped `accel_*` hooks now produce ~7,400 GDB stops (~10 min wall for 60 s guest). Pass `--max-hits <hook>=12` for every `accel_*`/`meta_*` breakpoint; with caps, 180 s guest takes ~4 min wall.
- `VPHONE_AGX_DUMP=1` logs raw type-1 (0x280 B) and type-3 (0x40 B) commands as `AGX DUMP …` lines; decode them with `backend/agx_cmd_decode.py <qemu-trace.log> [--type N] [--json out]`.

### DataMigration root cause (correcting §13)
- §13's addresses were runtime addresses: thread-state frames are slid (`cache_slide`, 0x58000 in probe 44, random per boot). The blocked frame is `DMPerformMigration` (`0x186910f68`, DataMigration.framework / AppSupport `DMMessaging.m`), not `-[DMConnection …]`. It sends `migrateWithCompletion:` to XPC service `com.apple.datamigrator` (launched through xpcproxy; it is not a LaunchDaemon) and then calls `dispatch_semaphore_wait(FOREVER)` at `0x1869110cc`.
- The caller `DMPerformMigrationIfNeeded` compares `CFPreferencesCopyAppValue("LastSystemVersion", "com.apple.migration")` with the running build (`0x186910dd4`/`0x186910dec`); the skip path is `0x186910ebc`. The data disk had no such preference, so migration ran on every boot.
- Probe 45 (`probe-45-dm/`, 180 s, no pokes, both variants exit 0, no panic or GPU hang):
  - `base`: the migrator replies only at ~141 s (`dm_wait_done`), and the second `bks_checkin` follows at 147 s.
  - `seeded` (`com.apple.migration.plist` with `LastSystemVersion=11D257` in `mobile` and `root` Library/Preferences): `dm_skip` fires at 57.4 s, `bks_checkin` completes at 58.4 s, and no backboardd/SpringBoard thread is blocked at 170 s.
- `scripts/prepare_diagnostic_disks.py` now writes that plist. `build/diagnostic/data-diagnostic-systembag.raw` was replaced by the seeded disk (SHA-256 `39a30ccc…c8777`). The previous disk is kept as `data-diagnostic-systembag-pre-dm.raw`.

### First type-1 render command (probe 45 seeded, queue `0xc0474000`, cmd META `0x40404000`)
- `+0x0c/+0x10` = META `0xc0460000`/`0xc04600a0`; `+0x18`, `+0x40`, `+0x50` = `0x101c4000`/`0x101ac000`/`0x101b0000`; `+0x68` = META `0x60a00000`; `+0x6c` = `0x020007fc`; `+0x70/+0x74` = `0x46f`/`0x27f` (1136−1 × 640−1, a full-screen render target).
- Queues seen: `0xc0474000` (types 3, 1), `0xc048c000` (types 3, 1), `0xc0470000`/`0xc0488000` (types 7, 0).

### Next
- Blocker B (pixels): work out which type-1 field points to the colour attachment (agy B1 report pending at the time of writing), translate it through the GPU MMU (`agx_gpu_translate`), and implement a minimal clear/resolve into the IOSurface backing so `program_hardware` scans out non-zero CA pixels.

---

## 15. Scanout follows the CA surface; render target is in a per-context GPU address space (Claude, 2026-10-21)

### Display register model (corrects §1/§13 acceptance wording)
- `0x206204044` is `DPU0SRCBASE`, the plane offset inside the surface. It is always 0 for single-plane IOSurfaces, so `0x4044 == 0x18e0000` can never hold. The surface's physical page is written through the DPFIFO to **`0x4098` `DPU0MMUPGADDR0` (PA >> 4)**, with `0x4090 DPU0MMUCNTL = 1` (`AppleMobileADBE0` helper `0xf2896c`, write at `0xf28a98`, PA from the IOSurface memory descriptor).
- Layer 0 is enabled by backboardd from ~64 s (swap tokens 11–13, surface ID 3, 640×1136, `payload[0x10c]` mask 1). Earlier swaps are bootstrap swaps without a surface.
- Backend: `0x4098` now sets `framebuffer = (val << 4) + [0x4044]`, and FIFO writes to `0x4044/0x4070/0x4098` are traced as `DISPLAY_FIFO` lines.
- **Acceptance should read:** `0x4098` holds CA's surface page and that surface contains non-zero pixels rendered by the HLE.

### Probe 46 (`probe-46-scanout/plain`, `probe-46-scanout-fill/rtfill`; 180 s each, no panic or GPU hang)
- `0x4098` = `0x803cbd00` (plain) / `0x803ce000` (rtfill), so scanout PA is `0x803cbd000` / `0x803ce0000`. The QMP screendump now has 790 k non-zero bytes, but the content is noise and `deadbeef` patterns: the CA surface was never rendered. The surface may also be scatter-gather (only page 0 is known).
- `AGX RT` lines: rt0 GVA `0x60a00000` (backboardd, 640×1136, fmt `0x20007fc`, load Clear) and `0x68800000` (SpringBoard wallpaper 744×1392) are **unmapped in BIF_CAT_BASE0** (the firmware page catalogue). User render targets live in a per-context GPU page catalogue, so `VPHONE_AGX_RT_FILL` stopped at the first page.

### Next
1. Find where the AGX driver keeps each GPU context's page catalogue PA and how a channel/command references it (the context ID at type-1 `+0x04`, the parameter buffers at `+0x0c/+0x10`, or the FW memory-context structure). Teach `agx_gpu_translate` to walk that catalogue.
2. Re-run with `VPHONE_AGX_RT_FILL` to confirm that rt0 maps to the surface at `0x4098` (same PA).
3. Then decide how much of the fragment pipeline to emulate (CA composites textured quads, so the minimum useful step is a textured blit from the source IOSurface into rt0).

### Update (probes 47–50, 2026-10-21)
- Host→FW ring 3 binds are now consumed: context 1 (backboardd) → slot 4, context 2 (SpringBoard) → slot 5, each with its page-catalogue PA (the format is the same 3-level PC/PD/PT as the firmware). Type-3 entries with slot 0/1 and catalogue 0 also appear (probably unbinds).
- **`0x4098` is the display MMU page table, not a pixel page.** Each u32 is `(page PA >> 4) | 1`, one per 4 KiB page of the plane. `0x4040` is the stride (0x1000 = 4096 B), `0x4044` the plane offset. `display_update` now gathers through that table. Probe 50 scanout is clean black (727,040 zero bytes) instead of noise: the plane is genuinely unrendered.
- backboardd's display surfaces are triple-buffered. In context 1's GPU address space they sit at GVA `0x400000000`, `0x400470000` and `0x4008e0000` (0x470000 = 4096 × 1136 each). One of them has page 0 = `/vram` `0x8018e0000`.
- **Type-1 `+0x68` (`0x60a00000`) is not the render target.** It is identical in all 200 distinct type-1 commands although the target rotates across three surfaces, and it is unmapped in every catalogue. The real colour target address must be in the fragment / PBE state referenced by the command (`+0x40` `0x101ac000`, `+0x50` `0x101b0000`, `+0x1b0/+0x1bc` `0x80004000` …), which is mapped in the context catalogue (e.g. `0x101c4000` = PC[0]/PD[128]).
- Next: search those context-mapped buffers for the three surface GVAs (several shift encodings) to locate the PBE address word, then fill/resolve into that surface.

### Update (probes 51–56): colour target located, fill reaches the screen
- The render-target address is **not** in the type-1 command. Path, verified at runtime:
  1. The end-of-tile PDS data lives at `0x6_0000_0000 | cmd[+0x1bc]` (= `0x680004000`, size `cmd[+0x1c0]`, PC index 26). The PDS heap base is `0x6_0000_0000`. Beware: `ldl_le_p` sign-extends, so cast to `uint32_t`.
  2. EOT data word 2 (`0x101a4000`) points at the PBE state words.
  3. The surface GVA is `A = ((W1 & 0x3f) << 34) | (W0 << 2)` (`PBEEncoderG3::encodeStateWords` `0x1802b217c`, agy report C4).
- Probe 56: the decoded targets rotate over the three display surfaces (`0x400000000` / `0x400470000` / `0x4008e0000`, about 611 commands each). `agx_trace_rt` now uses that target, with +0x68 kept as fallback; +0x68 is PBE register word 0 (swizzle/format).
- `VPHONE_AGX_RT_FILL=ff2080e0` (diagnostic only) paints the **top 10 rows of the scanned-out plane blue**. The command → PBE → surface → DPU MMU → screen chain is closed. The fill stopped at `0x40000a000` because only 10 surface pages were GPU-mapped at that moment.
- Open question: five sparse grey glyph clusters appear around y ≈ 355 in the rtfill screendump (colours 9d9d9d/636363), which were not written by the fill. They may be CPU-written content in a tiled/twiddled layout. Not yet explained.
- Remaining for genuine CA pixels: execute what the fragment pass does. That means at least the clear (load action) and the textured blits/composites CA issues, i.e. a USC/PDS interpreter subset or a high-level "CA composite" replay from the TA/3D streams. This is the large remaining piece; scope it before starting.
- Clear (agy report C5, 2026-10-21): every CA 3D pass uses load action Clear (`+0x1d0 = 2`) through the ISP background object (`+0x178` = background PDS data `0x6_8000_4020`, `+0x180/+0x184` flags, depth 1.0 at `+0x1c8`, stencil `+0x1cc`). The colour clear is **transparent black**, so executing it changes nothing visible. All visible CA content comes from textured quads drawn in the TA/3D streams (VDM control stream at `0x1019c000`, vertex PDS/USC state, texture state words). Explicit `glClear` with a colour or scissor goes through a TA clear quad, whose RGBA floats are in fragment PDS data (`AGXHalClearFragmentShader::emitConstants` `0x18029751c`).

---

## 16. First CoreAnimation frames on screen via high-level AGX replay (Claude, 2026-10-21)

### Result
- With `VPHONE_AGX_REPLAY=1`, the emulator shows backboardd's real boot UI: a black background, the Apple logo, and later the boot progress bar (`probe-60-replay/r1`: 2,030 replayed frames, no panic, `screen.png`). Every pixel comes from backboardd's own GPU submissions (vertex buffers, texture IOSurface, PBE target), executed by the backend's HLE and scanned out through the DPU MMU table. There are no guest patches and no pokes.
- `probe-59-replay` hit the intermittent CommCenter kernel data abort (`pc 0xf100f8`, `far 0x88`, baseband; first seen in probe 38, §10). Two follow-up runs (probe 60 r1/r2) had no panic. It is unrelated to replay but still needs its own fix.
- r2 replayed only 2 frames. Run-to-run variance in how long the boot UI keeps animating is not yet understood.

### Recovered model (verified against `probe-57-frames/dumps/frame-1.bin`)
- **VDM block type = bits [31:29]**: 0 PPP state (`count<<8 | gva_hi`, then gva_lo), 1 PDS state (3 words), 2 VDM state (word 0, plus 1 word if bit 28, 2 if bit 27 (vertex PDS data/code, PDS heap relative), 2 if bit 26), 3 index list / draw (`0x6a000600`: count, first), 4 link, 6 terminate. agy report C6 missed type 2.
- **Vertex data**: VDM-state vertex PDS data at `0x6_0000_0000 | word`: `+0` stride, `+8` buffer GVA. CA quads are 6 vertices of `float x,y,z,w` in pixels, followed by `u,v` (stride 24, textured) or a BGRA colour (stride 20, solid).
- **Texture**: a PPP word `0x8xxxxxxx` → PDS heap → u64 pointer → TSW `{w0 format, w1 = (h-1)<<16 | (w-1)<<2, w2/w3: addr = ((q>>16) & 0x3ffffffff) << 2}`. The texture is linear BGRA, stride `align16(w) * 4`. The Apple logo is at `0x101d8000`, 129×158.
- **Colour target**: the display surface is physically linear (stride 2560, the `/vram` carveout). The GPU maps it in 64 KiB strips of 16 rows: `gva = base + (y>>4)*0x10000 + (y&15)*2560 + x*4`. DPU scanout therefore uses a 2560-byte pitch; `0x4040 = 0x1000` is not the pitch.

### Code
- `backend/s5l8960x.c`: `agx_replay_frame` / `agx_replay_texture` / `agx_surface_gva` (axis-aligned quads, nearest sampling, premultiplied src-over). It is called from `agx_trace_rt` for context-slot commands with a PBE target. TA commands are retained per context when replay or frame dump is enabled.
- `backend/agx_replay_offline.py` renders a frame dump to PNG; `backend/agx_vdm_walk.py` prints the VDM stream.

### Replay is the default (2026-10-21)
- `agx_replay_enabled()` is on unless `VPHONE_AGX_REPLAY=0`. `backend/agx_contract_test.py` covers it: a VDM stream with one solid and one textured quad must produce exact pixels, and `VPHONE_AGX_REPLAY=0` must leave the target untouched.
- Probe 61 (no env vars, 2 runs, no panic): d2 replays 2,052 frames (logo + progress bar). d1 replays only 1 because **the guest stalls**: backboardd stops submitting after 3 kicks. In d1 at 170 s there is no `CA::Render::Server` thread, `IOMFBServer::set_next_update` is blocked in `kern_EnableVSyncNotifications` (IOMobileFramebuffer user client), and `CA::Context::commit_transaction` is blocked sending to the render server. The same run-to-run variance (1 to ~2,000 type-1 commands) existed before replay, so this is a separate vsync/IOMFB problem.

### Render stall = backboardd crash loop in CompassPlugIn (2026-10-21, open)
- The stalled runs are **backboardd crash-loops**: pid 17 → 98 → 104 → … about every 10 s. After the last respawn the render server wedges in `kern_EnableVSyncNotifications`.
- Real iOS crash reports, read from the data disk, are kept in `probe-62-crash/c1/crashlogs/` and `probe-64-nooscar-crash/k2/crashlogs/`. Run `display_probe.py` directly (not `parallel_probe.py`) so `images/` survives, read `/var/mobile/Library/Logs/CrashReporter` from `images/data.raw` (read-only attach), then run `probe_cleanup.py`.
- Every backboardd crash is thread 0 in `/System/Library/HIDPlugins/CompassPlugIn.plugin`, called from `_IOHIDEventSystemCreate → __IOHIDServiceCreate → _IOHIDServiceFilterCreate → CFRelease → __IOHIDServiceFilterRelease → CompassPlugIn::Release`:
  - Crash A: `objc_msgSend` with a garbage isa; the faulting address always ends in `…beb8`. This fits `CFRelease(this->factoryID)` at `+0x3440` on a corrupted instance.
  - Crash B: `this == -1` in `setMagnetometerDac` (`+0x3958`).
  - The plugin is an IOHIDServiceFilter: it is instantiated for every HID service and matches `0xff00/0x0a` with `IONameMatch == "compass,akm8973s"`. agy report C7 decodes its layout.
- **Hiding the M7 'oscar' node does not fix it.** `boot-plan-vram-nooscar` (name/compatible `oscar` → `oscax`, evidence only): 3 of 4 runs still restart backboardd once, with the same crash A. agy C7's Oscar/UART2 root cause is wrong.
- **The problem predates this session.** Across all probes, backboardd respawns appear in probe 30a, 36, 39r1, 41, 44, 45–58, 60r2, 61d1, 62c1 and 63; many other runs are clean. SpringBoard also crashes in some runs (thread 4 SIGSEGV, `probe-64…/k1`). The pattern (deterministic code, about 50 % failure, a corrupted object) suggests memory corruption or a race exposed by emulator timing.
- Probe 65 (`user-hooks-hidf.json`, hooks `hidf_*` at `_IOHIDServiceFilterCreate` `0x1825dff28`, `+0x2ac` `0x1825e01d4`, and `__IOHIDServiceFilterRelease` `0x1825e09e0` / `0x1825e0a00` / `0x1825e0a14`; `display_probe.py` dumps the filter, plugin instances, and CompassPlugIn's `__la_symbol_ptr`, stubs and stub helper). 1 of 4 runs crashed (h1).
  - The `__ZdlPv` lazy pointer is bound correctly to libc++ in both crashing and clean runs, so dyld binding is ruled out.
  - Plugin instances look sane right up to the last Release (refcount 2 → 0, valid vtable and factoryID).
  - Clean runs create 288 filters for 21 services by about 54 s. h1's pid 17 died at about 39 s after 75 creates, in the middle of enumeration.
- Symbolised crash A: `objc_msgSend` ← `_CFNumberGetValue+0x134` ← CompassPlugIn ← `Release` ← `CFRelease` ← `_IOHIDServiceFilterCreate+0x2b4` ← `__IOHIDServiceCreate(Virtual)` ← `_IOHIDEventSystemCreate`. CompassPlugIn only calls `CFNumberGetValue` in `setPropertyForClient` (`+0x3b84`), and crash B is in `setMagnetometerUpdateInterval`, so plugin methods run on a just-released or corrupted object. The bad isa values (`0x1_5f67_bea8`, `0x1_37cb_bea8`, `0x1_fd65_bea8`) keep the low 16 bits and corrupt bits 16–31, which looks like partial heap corruption.
- **Statistical bisect (2026-10-21, `bisect/`, `backend/crash_batch.py`, 6 runs × 75 s per configuration):**
  - baseline: 2/6 crash. `VPHONE_AGX_HLE=0` (new switch: META stays silent, no AGX guest-RAM writes): 2/6, one of them a 5× crash loop. **The AGX HLE is not the cause.**
  - Shared-cache slide against crash across ~80 historical runs: no correlation. Crashes go back to native-keybag probe 15, so the failure rate is about 50 % and long-standing.
  - Hooks on `__IOHIDEventSystemAddService` (`0x1825ba758`), `__IOHIDEventSystemRemoveService` (`0x1825ba944`) and `_IOHIDSessionRemoveService` (`0x1825c345c`): never hit in 6 runs (1 crash, r4, after 51 filter creates). There is no evidence of HID service removal during enumeration.
- Remaining hypotheses: (1) a race between backboardd threads that the emulator's much slower HID enumeration (≈20 s versus milliseconds on hardware) makes likely; (2) guest-RAM writes by the SEP mailbox / SIO AES models; (3) a CPU/MMU emulation bug. A deterministic `-icount` run that reproduces the crash would allow watchpoints on the corrupted object.
- Next: (a) hook the remaining IOKit filter call sites (`_IOHIDServiceFilterOpen` `0x1825e05cc`, `_IOHIDServiceFilterSetPropertyForClient`, `_IOHIDServiceFilterMatch` `0x1825e0270`) to catch the exact call that enters the plugin on the bad object; (b) bisect emulator writes to guest RAM statistically (AGX stamp/ring/replay writes, SEP mailbox, ANS DMA) with batches of 6+ runs per configuration, since the failure rate is about 30–50 %.

### Next
1. Root-cause the backboardd CompassPlugIn crash above.
2. Generalise beyond axis-aligned quads: transforms, texcoord sub-rects, A8 glyph textures, per-draw opacity, twiddled textures. Use frame dumps from later in boot (SpringBoard UI) to find what is missing.
3. Fix the intermittent CommCenter baseband data abort (`0xf100f8`).


---

## 17. Root cause of the backboardd/SpringBoard crash loops: stale QEMU TLB after TLBI VAE1IS (Claude, 2026-10-21)

### Finding
- With `-icount shift=N,sleep=off` (via `display_probe.py --qemu-arg`) runs are fully deterministic (identical slide, pids, registers). `shift=1` reproduces the CompassPlugIn crash every time (`bisect/icount-s1/{a,b}`: identical crash report and registers).
- backboardd loads and unloads its HID filter plugins (CompassPlugIn, CompassBacklightCompensation) alternately **at the same VA** (`0x102aa0000`). This is normal CFPlugIn behaviour.
- Single-stepping `CompassPlugIn::Release` (`--user-va-hook cp_release=0x102aa3414 --step-count 24`, `bisect/icount-s1-trace`) shows the CPU **executing CompassBacklightCompensation instructions while the guest page tables map CompassPlugIn** at that VA. Example: `0x341c → 0x343c`, which is CBC's `cbnz`. Execution follows a stale translation.
- iOS does flush correctly: `TLBI VAE1IS` is issued for every plugin page (54× each in the trace). The kernel helpers at `0xffffff8000227040–0x2270c4` are standard.
- Experiments on the deterministic crash (flush mode → result): per-4K page flush (QEMU's bits/range path or a plain page flush) → crash; 16 KiB block → crash; 2 MiB block → OK; all VAs of the EL1&0 mmu indexes → OK; full flush → OK. Why QEMU's per-page flush misses entries more than 16 KiB away but within 2 MiB is not yet understood.

### Fix
- `backend/patches/0001-tlbi-vae1-flush-regime.patch`: `tlbi_aa64_vae1is_write` / `tlbi_aa64_vae1_write` flush every VA of the EL1&0 regime (`vae1_tlbmask`). `scripts/qemu/build_backend.py` now applies every `backend/patches/*.patch` to the pinned upstream files (restoring them from the pinned commit first). Boot speed is unchanged (`bisect/perf`: bks_checkin 23.3 s vs 22.2 s, wall 77.7 s vs 77.9 s).
- Verification:
  - Deterministic shift=1: 0/1 backboardd crash (was 1/1).
  - `bisect/fixed`, 8 normal runs × 75 s: **0/8 backboardd crashes (was 30–50 %), SpringBoard never restarts (it used to restart in most runs), no panics, 474–841 replayed CA frames per run.**
- Remaining: SpringBoard still restarts once in the deterministic icount shift=1 run (`fixed-s1`, pids 47 → 107). It may be timing-related under icount and should be re-checked. The intermittent CommCenter `0xf100f8` panic (§10/§16) may also have come from this bug; watch for it.

### Tools added
- `backend/crash_batch.py`: N identical probes; counts backboardd respawns.
- `display_probe.py`: `--qemu-arg` (e.g. `-icount`), `--user-va-hook name=0xVA` (absolute user breakpoints), `hidf_*` IOHIDServiceFilter capture; steps record x16/x19–x21/lr.
- `VPHONE_AGX_HLE=0` switch in `s5l8960x.c`.

### 180 s after the fix (`probe-66-fixed`)
- Run a: no panic; 6,947 replayed frames with up to 56 draws each. The screen leaves the boot logo and shows a SpringBoard UI: a white background, a blue ⓘ button at the bottom right, black blocks at the top, and the text drawn as streaks (`a/screen.png`). It looks like the iOS 7 setup / "slide to set up" UI. Text and some elements are wrong because the replay does not yet handle A8 glyph textures, transforms or texcoord sub-rects (next step 2).
- Run b: kernel panic `"SEP/OS failed to boot"` (caller `0xffffff8000d47a30`), not seen before. It is probably SEP-model boot timing and needs its own investigation.

### SEP "SEP/OS failed to boot" panic fixed (2026-10-21)
- Cause: the SEP model sends OS-active (`ep0 op 0x0d`) 2 ms of virtual time after the AP pops the boot-finish reply. Virtual time follows host time, so when the kernel thread is delayed the message arrives before `AppleSEPManager` enters its booting state. The kernel logs `received OS Active message in unexpected state 2`, drops it, and later panics `"SEP/OS failed to boot"` (caller `0xffffff8000d47a30`; `probe-66-fixed/b`).
- Fix (`sep_os_active_timer_cb`): after sending OS-active, the model resends it every 250 ms of virtual time, up to 8 times, until the AP sends any non-boot endpoint traffic (`ep != 0xff/0xfe`). That traffic only starts once the kernel has accepted OS-active.
- Verified by forcing the race with `VPHONE_SEP_OSACTIVE_DELAY_NS=0` (test-only knob; `probe-67-sep-race` a/b): both runs hit "unexpected state 2", resent once, then logged `SEP booting` and `SEP/OS is alive`, with no panic. `sep_contract_test.py` still passes.


---

## 18. Replay extended for SpringBoard UI (Claude, 2026-10-21)

### Recovered rules (verified on frame dumps `probe-68-ui-frames`, `probe-71-late-frames`, `probe-72-full-frames`)
- **VDM_STATE size**: bit 28 → +1 word (cut index), bit 27 → +1 word (vertex PDS data), bit 26 → +3 words (vertex PDS code + 2 state words). `0x48002f84` carries data only. The old rule (2 + 2) desynchronised the stream and lost most draws.
- **Fragment data**: PPP words `0x8xxxxxxx` → PDS heap (`0x6_0000_0000|w`) → up to four u64 pointers. Each points to blocks made of a sampler header (`00000fff 0000240x 0 0`), TSWs (`w0 >> 28 == 6`) and shader constants. Draws whose constants start with the Rec.709 luma weights (`0x3d93a000 0x3e59a000 0x3f372000`) are CA vibrancy/desaturation passes; the replay skips them, since painting them as solid quads produced the black blocks.
- **TSW**: `w1 = (h-1)<<16 | (w-1)<<2`; `addr = ((q>>16) & 0x3ffffffff)<<2` with `q = w2 | w3<<32`.
  - Linear textures (`w0 & 7 != 1`): **pitch (pixels − 1) is in `w2 & 0xffff`** (logo 129 px → 144; labels align8). This matches 50/51 textures by a row-continuity score.
  - `w0 & 7 == 1`: a GPU-rendered IOSurface, with rows 64-pixel aligned in 16-row / 64 KiB strips like the display (`base + (y>>4)*0x10000 + (y&15)*pitch + x*4`).
- **Vertices**: stride 20 = position + BGRA colour; 24 = position + normalised u,v; 28 = position + BGRA tint + u,v in texels (rectangle textures). The replay now uses vertex u,v (sub-rects) and tints.
- **Pass dims**: type-1 `+0x70/+0x74` carry flags above the max y/x in bits [15:0] (e.g. `0x6000003f` / `0xa000025f` = 608×64).

### Code
- `backend/s5l8960x.c`: `agx_replay_texture` (multi-block TSW search, vibrancy skip), `agx_replay_decode_tsw`, `agx_texel_gva`, u,v/tint sampling, the VDM_STATE size fix, masked pass dims. **Only 640×1136 passes are replayed**: offscreen passes such as the 608×64 blur backdrops position screen-space geometry through vertex-shader uniforms that are not modelled yet.
- `VPHONE_AGX_FRAME_DUMP_SKIP`, `_EVERY` and `_MIN_DRAWS` select which frames are dumped; `agx_vdm_draw_count` counts draws for `MIN_DRAWS`.
- `backend/agx_replay_offline.py` mirrors the C rules.

### Results
- Offline replay of `probe-72-full-frames/frame-6` (an offscreen full-size pass SpringBoard renders) reproduces the **complete iOS 7 home screen**: icons, labels, dock, wallpaper, "9:41 AM" (`probe-72-full-frames/homescreen-offline.png`).
- Live (`probe-73-ui`, 180 s × 2, no panic): the scanout shows the setup screen with a correct status bar ("Searching…", battery) and the ⓘ button, and no black blocks or streaks (`a/screen.png`). The setup greeting and the bottom "slide to set up" are still missing, because they go through vibrancy/blur shaders and 608×64 offscreen passes.

### Next
1. Recover the vertex-shader transform (uniforms reachable from the vertex PDS data) so offscreen passes can be replayed into their own coordinate space.
2. Approximate the vibrancy passes (luma-weighted tint of the backdrop) instead of skipping them, so text drawn through them appears.
