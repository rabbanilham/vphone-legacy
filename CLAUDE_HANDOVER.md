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
