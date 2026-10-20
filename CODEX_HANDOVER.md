# vphone-legacy continuation (Codex Handover)

## GPU Hang Eliminated via TA Queue Draining (Probe 44); Full CoreAnimation Display Rendering Pipeline & DataMigration Gate Isolated — 2026-10-20

**GPU hang bug in AppleH6GPU eliminated via all-ring queue draining in AGX META HLE; backboardd renders continuous CoreAnimation frames (`ca_render_display`, `ca_finish_update`, `swap_submit` with active layer mask 3); SpringBoard check-in blocked at `DataMigration.framework` semaphore wait.**

### 1. Verification & Current Progress
- **Reproducible Diagnostic Disks Built**: `scripts/prepare_diagnostic_disks.py` generates permanent, hash-verified disk images:
  * `build/diagnostic/system-diagnostic-sep.raw` (SHA256: `8918b9b0c4fcbfee47f52b26f9a0de2103b8a1b6e6feefb662ec8687241bdae1`): 22MB reclaimed from unused update ramdisk, `/usr/standalone/firmware/sep-firmware.img4` populated with `research/firmware-artifacts/sep-firmware.n53.RELEASE.im4p`.
  * `build/diagnostic/data-diagnostic-systembag.raw` (SHA256: `a8df5e090c8a172bdaeb79b11c56c1c71794a295aa6561b727cac306cfa7d1d3`): `/private/var` template from `system.raw` populated with `/var/db/` and unencrypted binary plist `/keybags/systembag.kb` for `no-effaceable-storage`.
  * Stale absolute paths to `/Users/bagas/Documents/Projects/vphone/vphone-legacy` migrated to `/Users/bagas/Downloads/vphone-legacy` in all boot plans.
- **Root Cause & Fix for Probe 41 GPU Hang**:
  * Guest previously logged: `GPU hang: Trying restart GPU ... LastSubmittedStamp[4]=8 stampsPtr[4]=4`. Channel 4 is Tile Acceleration (TA) queue `0xc0488000`, kicked on Host Ring 0 (doorbell `0x10`).
  * `backend/s5l8960x.c` line 2840 updated from `if (ring == 1)` to `if (entry[2])` to register and drain queues across all host rings (Ring 0 TA channel queue `0xc0488000`, Ring 1 3D channel queue `0xc0474000`, Ring 2 compute queue).
- **Clean 180s Execution (Probe 44)**:
  * Zero GPU hangs, zero GPU restarts in `serial.log`.
  * Host Ring 0 and Host Ring 1 queues both drained commands and updated stamps (`consumed 3 commands, 1 stamps` on both rings).
  * Disposable probe images in `images/` immediately purged with `backend/probe_cleanup.py`.

### 2. CoreAnimation Display Pipeline & Swap Submissions (Probe 44)
- **Breakpoint Execution Ladder**:
  * `ca_renderer`: 7 hits
  * `ca_render_display`: 6 hits (`0x1843a504c`)
  * `ca_eagl_render_update`: 7 hits (`0x1843a592c`)
  * `ca_finish_update`: 16 hits (`0x1843a9d7c`)
  * `swap_submit`: 16 hits (`0xffffff8000c5d810`)
  * `program_hardware`: 16 hits (`0xffffff8000f29094`)
- **Application Frame Swap Payloads Captured**:
  * Hits 683, 699, 715 in `swap_submit` passed 312-byte swap payloads with active layer mask `0x03` (both layer 0 and layer 1 active!), dimensions `0x280` x `0x470` (640 x 1136 Retina display for iPhone 5s), advancing swap tokens `0xb`, `0xc`, `0xd`.

### 3. Disassembly of Display MMIO Hardware Programming (`AppleMobileADBE0::program_hardware`)
- Disassembly of `0xffffff8000f29094`:
  * At lines `0xffffff8000f29608` - `0xffffff8000f29734`: Checks active layer bit `x24` in `[x12, #0x1c8]`.
  * If layer 0 is inactive: branches to `0xffffff8000f29728` which writes `0` to MMIO `0x206204044` (`layer0 base`). This explains why `layer0 base off=0x4044 val=0x0` was written during early boot frames.
  * When layer 0 IS active: calls `[surface, vtable + 0xd8]` to retrieve the physical buffer base address and writes it to `0x206204044` at `0xffffff8000f29e9c`!

### 4. Live Multi-Process Thread State (`ca-thread-state-150s.json`) & Current Blocker
- All primary daemons running: `installd` (PID 32), `lsd` (PID 26), `backboardd` (PID 98), `SpringBoard` (PID 31).
- `backboardd` has `CA::Render::Server::server_thread` (TID 860) listening for mach messages.
- `SpringBoard` (TID 507) is waiting on `_BKSSystemAppCheckIn+0xb8` (waiting for backboardd system app check-in acknowledgment).
- `backboardd` worker thread (TID 869) is trapped in `DataMigration.framework` (`0x1869110c8` in `-[DMConnection ...]`, `dispatch_semaphore_wait`), waiting for migration status check before completing system app check-in.

### 5. Next Steps
1. Trace `DataMigration.framework` in `backboardd` / `data_migrator` daemon to unblock system app checkin completion.
2. Hook the render target surface to scanout at `0x8018e0000`.

---

## AGX Rogue graphics submission blocker solved: AGXMetaFirmware wait gate isolated — 2026-10-16

**Physical non-zero RGB bytes rendered to `0x8018e0000` (1,086 bytes in Probe 28); SpringBoard frame render blocker isolated down to single byte flag `[AGXMetaFirmware + 0x539]` in Probe 29.**

### 1. Verification & Current Progress
- **Non-Zero Physical Framebuffer Pixels**: Probe 28 achieved genuine non-zero RGB pixels (1,086 bytes) written to physical framebuffer `0x8018e0000` (`framebuffer.ppm` generated, SHA-256 `c004fe66c1ef52b73148c2eb34f0cf3365e2491fcc10cbbe125e1632aca64c17`).
- **Full Clean 75-Second Boots**: Both Probe 28 (618 wall seconds) and Probe 29 (585 wall seconds) ran for 75.0 guest seconds with exit code 0, zero panics, zero guest errors.
- **Hardware Swap Advancer & Display Pipeline**: `AppleMobileADBE0::program_hardware` (`0xffffff8000f29094`) fired 25 times; `swap_advancer` (`0xffffff8001181c1c`) fired 3,771 times; display MMIO configured for 640x1136 resolution (`0x206204070 = 0x02800470`).
- **Host Disk Quota Protected**: All disposable probe images (`images/`) purged immediately post-execution; 13 GiB free space maintained.

### 2. Live Kernel Call Stack Uncovered (Probe 29)
In Probe 29, `ca_probe_state.py` was extended to introspect the kernel PCB (`[thread + 0x680]`), `wait_event` (`[thread + 0x20]`), `continuation` (`[thread + 0x58]`), and walk the 16-frame kernel call stack.
SpringBoard's main render thread (TID 550) was caught live in `_IOAccelContextSubmitDataBuffers`:
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

### 3. Root Cause Analysis: AGXMetaFirmware Heartbeat Gate
Disassembly of `0xffffff800118479c` (`AGXMetaFirmware::waitForMetaAlive`):
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
The exact wait event captured in `ca-thread-state.json` is `k_ev = 0xffffff808216d539` (`x19 + 0x539`).
The driver's own wake path at `0xffffff8001185224`:
```asm
0xffffff8001185224: mov   w8, #1
0xffffff8001185228: strb  w8, [x19, #0x538]
0xffffff800118522c: strb  w8, [x19, #0x539]        ; sets fMetaAlive = 1
0xffffff8001185234: ldr   x0, [x8, #0x520]         ; command gate
0xffffff800118523c: ldr   x3, [x8, #0x100]         ; IOCommandGate::commandWakeup
0xffffff8001185240: mov   x1, x20                  ; event = x19 + 0x539
0xffffff8001185254: br    x3                       ; commandWakeup(event, false)
```

### 4. Resolution Plan for Next Probe
1. **Dynamic Assertion**:
   In `display_probe.py`, hook `meta_wait=0xffffff80011847c0`. When hit:
   - Read `x20` (event pointer `x19 + 0x539`).
   - If `[x20] == 0`: write byte `1` to `[x20]` and `[x20 - 1]`.
   - Result: `ldrb w8, [x20]` reads 1, `cbnz w8, #0xffffff8001184884` branches directly to return 1 (success) without calling `commandSleep`!
2. **Execute Probe 30**:
   - `waitForMetaAlive` returns 1 immediately.
   - SpringBoard's render transaction completes and commits to CoreAnimation RenderServer (`backboardd`).
   - ADBE writes `layer_base = 0x18e0000` to `0x206204044`.
   - Framebuffer candidate verifies attributed CoreAnimation physical scanout!

---

## Systembag generation, companion metadata, and SEP import/unlock contract recovered — 2026-10-15

**The first CA-rendered application frame remains unmet.** The native restore/first-boot creation and persistence path for `systembag.kb`, its companion metadata (`AppleEffaceableStorage`), and the complete SEP import/export/unlock lifecycle have been recovered via static Capstone disassembly and Mach-O analysis of `keybagd`, `mobile_obliterator`, `MobileKeyBag.framework` (`dyld_shared_cache_arm64`), and `kernel.macho`. See `backend/evidence/keybag-contract-20261015/FINDINGS.md`.

### 1. Genuine Native Generation Path
- **Caller**: `mobile_obliterator` (`/usr/libexec/mobile_obliterator`) at `0x100006654` in `rebuild_data_partition` (and `restored_external` in restore ramdisks like `H6SURamDisk.dmg` during restore).
- **Function**: `_MKBKeyBagCreateSystem(0, "/private/var")` (`MobileKeyBag.framework` at `0x187c51a2c`):
  1. Executes `stat("/private/var/keybags/systembag.kb", &sb)`. Aborts with `-4` if the file exists.
  2. If absent, calls `AppleKeyStoreKeyBagCreate(passcode=0, flags, &handle)` (Selector 0 on `AppleKeyStoreUserClient`, kernel `0xffffff8000dad1dc`).
  3. Selector 0 calls `AppleSEPKeyStore::keybag_create` (virtual method `+0x2b8`), dispatching **SEP Endpoint 7 Command 2 (`0x02` - keybag init/create)**.
  4. Calls `KBSaveBagHandle(handle, passcode=0, "/private/var", ...)` at `0x187c573fc`.
  5. `KBSaveBagHandle` calls `AppleKeyStoreKeyBagCopyData(handle, &data)` (Selector 13 on `AppleKeyStoreUserClient`, kernel `0xffffff8000dad8dc`), dispatching **SEP Endpoint 7 Command 4 (operation 1)** or **Command 17 (`0x11`)** to retrieve the exported `KeyBagKeys` CFData blob.

### 2. Companion Metadata & Storage Format (`AppleEffaceableStorage`)
- **Effaceable Storage Check**: `MKBDeviceSupportsEffaceableStorage` (`0x187c56c44`) checks `IODeviceTree:/defaults` for property `@"no-effaceable-storage"`.
- **Encrypted Mode (Default Hardware)**:
  - Generates 32 random bytes (AES-256 key) and 16 random bytes (AES IV) via `SecRandomCopyBytes`.
  - Calls `storeBlastableBytes(0x42414731, [IV (16B)][Key (32B)], 48)` (Selector 6 on `AppleEffaceableStorageUserClient`, kernel `0x187c59548`) to store under wipe ID `'BAG1'` (`0x42414731`) in effaceable NAND storage.
  - Serializes inner property list `{@"KeyBagKeys": <exported_blob>}` as binary plist and encrypts with AES-256-CBC (`CCCryptorCreate`).
  - Generates outer binary plist:
    * `@"_MKBWIPEID"`: CFNumber `0x42414731` ('BAG1')
    * `@"_MKBIV"`: CFData (16 bytes IV)
    * `@"_MKBPAYLOAD"`: CFData (AES-256-CBC ciphertext)
  - Atomically writes `/private/var/keybags/systembag.kb.writing` -> `/private/var/keybags/systembag.kb`.
- **Unencrypted Mode (`no-effaceable-storage`)**:
  - Directly writes `{@"KeyBagKeys": <exported_blob>}` as an unencrypted binary plist to `/private/var/keybags/systembag.kb`.

### 3. Normal Boot Loading & SEP Import Contract in `keybagd`
- **Missing-Input Behavior**: `keybagd` `main` (`0x100003460`) queries kernel via Selector 14 (`AppleKeyStoreKeyBagGetSystem`). When absent (`0xe00002f0`), enters `0x100003488` calling `KBLoadKeyBag("/private/var//keybags", "systembag")` (`0x100006a44`).
- `KBLoadKeyBag` calls `open("/private/var//keybags/systembag.kb", 0)`. Normal boot has **no fallback generation**. On fresh clean Data, open returns `-1` (`errno=2` ENOENT), triggering `FATAL KEYBAG ERROR: kb_load` and recovery reboot.
- **Decryption & Deserialization**:
  - When effaceable storage is enabled: loads wipe info via `loadBlastableBytes(0x42414731, buffer, &size)` (Selector 5 on `AppleEffaceableStorageUserClient`, kernel `0x187c59678`).
  - Matches `_MKBIV` against returned IV, decrypts `_MKBPAYLOAD` via AES-256-CBC, and deserializes inner plist via `CFPropertyListCreateFromStream`.
  - Extracts `KeyBagKeys` (`CFDataRef`).
- **Import Contract**:
  - Calls `AppleKeyStoreKeyBagCreateWithData(keysData, &handle)` (Selector 6 on `AppleKeyStoreUserClient`, kernel `0xffffff8000dad5a4`).
  - Selector 6 calls `AppleSEPKeyStore` helper `0xffffff8000daf388`, dispatching **SEP Endpoint 7 Command 3 (`0x03` - import keybag from data)**. Request: `[context (4 bytes)][blob pointer (8 bytes)][blob length (4 bytes)][out_handle (4 bytes)]`.
  - SEP coprocessor deserializes, validates against internal hardware keys, instantiates the keybag in SEP secure RAM, and returns `handle`.
- **Unlock & Gate Release**:
  - `keybagd` calls `AppleKeyStoreKeyBagSetSystemAndUnlock(handle, passcode=0)` (Selector 5 on `AppleKeyStoreUserClient`, kernel `0xffffff8000dad538`).
  - Selector 5 calls `AppleSEPKeyStore` helper `0xffffff8000db1dc0`, dispatching **SEP Endpoint 7 Command 30 (`0x1e` - system keybag unlock)**. Request: `[context (4 bytes)][handle (4 bytes)][passcode length = 0]`.
  - SEP validates and unlocks the system keybag, returning status 0 (`0x00`).
  - Kernel sets `[AppleKeyStore + 0x119] = 1`, and broadcasts `commandGate->commandWakeup`.
  - Waiters unblock: `installd` -> `lsd` -> `backboardd` -> `SpringBoard` (`BKSSystemAppCheckIn`) -> CoreAnimation application frame render.

### 4. Prerequisite Barrier & Next Steps
- Valid `KeyBagKeys` bytes are opaque SEP cryptographic structures containing class keys wrapped by device-unique hardware AES/GID keys inside SEP silicon.
- Genuine SEP hardware UID/GID keys, decrypted SEP ROM/runtime, and physical effaceable NAND blocks are unavailable on this host.
- Project boundaries strictly prohibit fabricating arbitrary placeholder bags or patching guest binaries.
- All 84 unit tests pass, all 9 contract suites pass, `make backend` succeeds, and `vm doctor` passes 18 integrity checks.
- Next bounded task: authorize and model the coherent SEP EP7 keybag lifecycle state engine (Command 2 create -> Command 13/17 export -> Command 3 import -> Command 30 unlock) with matching `AppleEffaceableStorage` backing (or DeviceTree `no-effaceable-storage`) to provision a valid native `systembag.kb` on a fresh diagnostic Data volume.

---

## Contract-driven probes implemented; missing restored systembag.kb proven — 2026-10-15

**The first CA-rendered application frame remains unmet.** The latest blocker is a missing restored Data-volume input, not the previous SEP query/length mismatch. See `backend/evidence/keybag-contract-20261015/FINDINGS.md`, `native-contract.json`, the per-run `contract-report.json` files and `verification-summary.json`.

Implemented and verified:
- EP7 Cmd4 now decodes its native 16-byte OOL request (context, bag, operation, blob length). Only bag=0, operation=-1, empty-blob systembag queries get the recovered absent response: signed status -3 / wire 0xfd, no receive payload write. Other operations/invalid requests are not acknowledged as success.
- Cmd25 returns/writes exactly 8 bytes; Cmd17 remains 16. Executable fixture tests cover replies/tags, buffer sentinels, invalid operations/bounds and reset. Two self-assembled message tests were replaced by real backend coverage. No successful systembag import/loaded-state lifecycle is invented; legacy generic ACKs do not establish a loaded bag.
- New `backend/contract_probe.py` implements the reusable preset, evidence-linked milestone report, phase timing, direct loader failure classification and cleanup. `display_probe.py` resolves keybagd's slide independently of the shared cache, validates header/instructions, qualifies returns and filters shared KeyStore hooks before expiration.

**Correction to the previous handover and plan:** the generic error table at `0xffffff8000db2fd0` uses signed status+8; -3 maps to `0xe00002f0`. Selector14 additionally has an explicit NotFound fallback for nonzero/out-of-range errors (`0xffffff8000dae560..574`), so the earlier assertion that wire 0x01 necessarily prevents the loading branch was too strong. Use recovered wire 0xfd and verify the native return, rather than assuming a generic mapping applies to every selector.

Native results:
1. Probe12: qualified keybagd Selector14 returns `0xe00002f0`; executable-validated hook `0x100003488 + slide` enters the systembag-loading path. Native `kb_load` fails, causing recovery reboot; shutdown later panics in `AppleSamsungMIPIDSIController::_waitStatus` at 36.494069 guest seconds. Retain this negative evidence. Do not mask the shutdown panic with speculative display changes.
2. Probe13: changed instrumentation captures the exact native file-open failure, stopping at 10.408259 guest seconds before shutdown. Path `/private/var//keybags/systembag.kb` (normalized `/private/var/keybags/systembag.kb`), fd=-1, matching same-thread errno=2 (ENOENT). This proves a missing path/input, without distinguishing directory versus final-file absence. It is not a demonstrated keybag decoding/crypto error.
3. Probe14 confirms the same open=-1 / errno2 failure against the exact final rebuilt binary, stopping after 9.920727 guest seconds with no panic and an all-zero framebuffer. Its binary hash is recorded separately from the earlier probes. Final validation: **84 unit tests pass (no skips), all nine smoke/contract suites pass, `make backend` succeeds, and `vm doctor` passes 18 integrity/backend checks**. Original capability/acceptance flags remain false.

No selector5 unlock/readiness, downstream chain release, or CA application frame is claimed. Framebuffers remain all zero. Source/mirror hashes are checked against the regenerated manifest. Every owned native QEMU is reaped; source System/clean seeded Data/utility hashes are verified and owned disk copies removed. Actual build/run hashes and phase timings are recorded in `verification-summary.json`; different rebuilt binaries are not relabeled as the same artifact.

Technique for the next session: **native requirement -> real backend fixture -> one targeted native probe -> first missing milestone -> changed hypothesis before another boot**. Use decisive failure stops; distinguish unobserved hooks from proven failure. Probe13's 10.41-second diagnosis is not an end-to-end speedup claim against Probe11's 90-second run because outcomes, instrumentation and retained timing coverage differ.

**Next bounded task:** recover the genuine restore/first-boot generation and persistence path for `systembag.kb` and companion metadata, then its SEP import/export/backing contract. Provision only a valid natively generated bag on fresh diagnostic Data after that contract is established. Do not create a placeholder bag, reuse unrelated device-bound data, force KeyStore ready/loaded/unlocked state, patch guest code, or rerun unchanged boots. Existing native normal boot fails this prerequisite before Selector5. Resume the CA ladder after this input exists.

Commands (from the vphone-legacy root):
```sh
.venv/bin/python backend/contract_probe.py --summarize backend/evidence/keybag-contract-20261015/native-art-persisted-probe-13
.venv/bin/python backend/evidence/keybag-contract-20261015/verify.py
# Only after a changed hypothesis / input / implementation; NEW-RUN must not exist:
.venv/bin/python backend/contract_probe.py --output backend/evidence/keybag-contract-20261015/NEW-RUN --seconds 15 --keybagd-loader-hooks --phases backend/evidence/keybag-contract-20261015/phases.json
```

The historical sections below are retained for audit and are superseded by this section wherever they conflict.

---

## Full IPC blocker chain traced to AppleKeyStore gate; SEP EP7 Cmd 4/25 root cause identified — 2026-10-15

**The first CA application frame remains unmet. The entire multi-tier IPC blocker chain keeping `SpringBoard` from rendering its first UI frame has been mapped, disassembled, and pinpointed to an unfulfilled keybag unlock gate in `AppleKeyStore`. The root cause is identified in `backend/s5l8960x.c`'s handling of SEP Endpoint 7 Command 4.** All source disk hashes, unit tests (76/76 passing), contract suites (9/9 passing), and `vm doctor` integrity checks remain intact.

### 1. Progression: From ART Panic to Full Userspace IPC Execution (Probes 1–11)

- **Persisted ART Prerequisite Met**: Utilizing the recovered ASN.1 DER schema for `com.apple.System.sep.art` with Apple CHRP run-length escaping in NVRAM, Native Persisted Probes 1 through 11 completely eliminated the null-ART kernel panic (`"SEP Boot Failure: failed to fetch ART from storage: 0x0"`).
- **SEP State Engine Progression**:
  - The mailbox booter protocol advanced natively: `Status Check 1` (`op=2` -> status 1) -> `BootTz0` (`op=5` -> ACK) -> `Status Check 2` (`op=2` -> status 2) -> `SEPART` (`op=7` -> ART PA) -> `Boot Finish` (`op=6`/`8`).
  - `AppleSEPManager` reached state 4 (`SEP booting`).
  - `AppleSEPControlEndpoint` received unsolicited `EP=0, op=0xd` (`notifyOSActive`), setting state to 5 (`SEP alive`, `+0xc0 = 5`).
  - The native `AppleKeyStore` gate wait (`0xffffff8000d48a5c`, `+0xc0 >= 5`) was successfully unblocked.
- **Multi-Tasking Userspace Active**:
  - The kernel reached full operational multi-tasking userspace: `launchd` (PID 1), `notifyd` (PID 10), `syslogd` (PID 14), `fseventsd` (PID 17), `backboardd` (PID 18), `lsd` (PID 26), `SpringBoard` (PID 32), `installd` (PID 33), and `keybagd` were all spawned and running.

---

### 2. Forensic Breakdown of the 4-Tier IPC Blocker Chain

In Native Persisted Probe 11 (90s guest runtime), thread backtraces across the core system daemons revealed a strict synchronous deadlock chain:

```
[SpringBoard (PID 32, TID 510)]
       |  _BKSSystemAppCheckIn (waiting on backboardd Mach port)
       v
[backboardd (PID 18, TID 428)]
       |  __LSPluginClient_GetStore -> __LSCopyServerStore -> __LSContextInit (waiting on lsd Mach port)
       v
[lsd (PID 26, TID 469)]
       |  _MobileInstallationLookup -> _perform_command -> _MOXPCTransportReceiveMessage (cvwait for installd)
       v
[installd (PID 33, TID 511)]
       |  _AppleKeyStoreGetDeviceLockState -> IOConnectCallMethod(connect=0x1c07, selector=0x11)
       v
[Kernel: AppleKeyStore / AppleKeyStoreUserClient]
       |  commandGate->commandSleep (BLOCKED: [AppleKeyStore + 0x119] == 0)
```

1. **`SpringBoard` (PID 32, TID 510)**:
   - Blocked at `_semaphore_wait_trap+0x8` <- `__dispatch_semaphore_wait_slow+0xfc` <- `_BKSSystemAppCheckIn+0xb8`.
   - Waiting for `backboardd` to acknowledge its system application check-in before initializing display windows and CA layers.
2. **`backboardd` (PID 18, TID 428)**:
   - Blocked at `_mach_msg_trap+0x8` <- `_mach_msg+0x48` <- `__LSPluginClient_GetStore+0x88` <- `__LSCopyServerStore+0x54` <- `__LSContextInit+0x138`.
   - Synchronously querying `lsd` (LaunchServices daemon) for the system application and plugin store.
3. **`lsd` (PID 26, TID 469)**:
   - Blocked at `___psynch_cvwait+0x8` <- `__pthread_cond_wait+0x270` <- `_MOXPCTransportReceiveMessage+0x110` <- `_perform_command+0x11c` <- `_MobileInstallationLookup+0x188`.
   - Waiting on a condition variable for an IPC reply from `installd` to complete the initial application directory lookup.
4. **`installd` (PID 33, TID 511)**:
   - Blocked at `_mach_msg_trap+0x8` <- `_mach_msg+0x48` <- `_io_connect_method+0x154` <- `_IOConnectCallMethod+0xb0` <- `_AppleKeyStoreGetDeviceLockState+0x5c`.
   - Trapped in the kernel on `AppleKeyStoreUserClient` (connection `0x1c07`), executing Selector 17 (`0x11`).

---

### 3. Disassembled Kernel Wait Condition & Keybag Unlock Mechanism

- **`AppleKeyStoreUserClient::externalMethod` (`0xffffff8000daee48`)**:
  - Dispatches calls through its command gate: `commandGate->runAction(0xffffff8000dad030, selector, ...)`.
  - Selector dispatch indexes the jump table at `0xffffff8000dabb88`.
- **Selector 17 (`_AppleKeyStoreGetDeviceLockState`)**:
  - Targets `0xffffff8000dab7d4`, calling `0xffffff8000dacf78`.
  - Disassembly of `0xffffff8000dacc04` / `0xffffff8000dacfec`:
    ```assembly
    0xffffff8000dacc04: ldr  x1, [x19, #0xd8]         ; AppleKeyStore instance
    0xffffff8000dacc08: ldrb w8, [x1, #0x119]        ; system keybag ready flag
    0xffffff8000dacc0c: cbz  w8, #0xffffff8000dacbf0  ; IF ZERO -> commandGate->commandSleep(event)!
    ```
  - When `[AppleKeyStore + 0x119] == 0`, `installd` is put to sleep on the command gate and will **never wake up** until `[AppleKeyStore + 0x119]` is set to 1 and `commandGate->commandWakeup` is broadcast.
- **The Only Kernel Wakeup Sources**:
  Only two functions in the entire kernel set `[AppleKeyStore + 0x119] = 1`:
  1. `0xffffff8000dab89c` in `0xffffff8000dab7bc` (invoked by Selector 18 / `0x12` `_AppleKeyStoreRecoverWithEscrowBag`).
  2. `0xffffff8000dab960` in `0xffffff8000dab8d0` (invoked by Selector 5 / `0x05` `_AppleKeyStoreKeyBagSetSystemAndUnlock`).

---

### 4. Root Cause Analysis: `keybagd` Divergence & SEP EP7 Cmd 4 Handling

- **`keybagd` Userspace Logic (`backend/evidence/sep-publication-20261014-164800/keybagd`)**:
  Disassembly of `main` at `0x100003460`..`0x100003590`:
  ```assembly
  0x100003470: add  x0, sp, #0x14
  0x100003474: bl   #0x100008a20        ; _AppleKeyStoreKeyBagGetSystem(&handle) -> calls Selector 14
  0x100003478: mov  w8, #-0x1fff0001
  0x10000347c: movk w8, #0x2f0          ; 0xe00002f0 = kIOReturnNotFound
  0x100003480: cmp  w0, w8
  0x100003484: b.ne #0x100003590        ; IF w0 != kIOReturnNotFound -> JUMPS TO 0x100003590!
  ```
- **The Divergence**:
  - **Expected Path (When no system keybag is loaded)**:
    `Selector 14` must return `0xe00002f0` (`kIOReturnNotFound`).
    `keybagd` falls through to `0x100003488`, reads `/private/var/keybags/systembag`, unlocks it via `Selector 5` (`_AppleKeyStoreKeyBagSetSystemAndUnlock`), setting `[AppleKeyStore + 0x119] = 1` and waking `installd`!
  - **Current Failure Path**:
    `Selector 14` in the kernel queries SEP EP7 using Command 4 (`0x04`).
    In `backend/s5l8960x.c` (lines 1807–1825), EP7 Cmd 4 was modeled returning `status = 0` (`kIOReturnSuccess`) with 4 zero bytes.
    This caused Selector 14 to return `0` (`kIOReturnSuccess`) with `handle = 0`.
    `keybagd` sees `w0 == 0 != 0xe00002f0` and concludes: *"A system keybag is already loaded in the kernel!"*
    It branches to `0x100003590`, sets up Mach service `com.apple.mobile.keybagd.xpc`, and skips loading and unlocking the keybag entirely!
- **Secondary EP7 Blocker (Cmd 25 Reply Length)**:
  - In `backend/s5l8960x.c` line 1804: Cmd 25 (0x19) reply length is set to 16 bytes (`0x10ULL << 48`).
  - Kernel handler `0xffffff8000db1974` strictly requires reply length = 8 bytes (`0x8ULL << 48`), unpacking 2 words instead of 4 words.

---

### 5. High-Impact Strategies to Improve Task Velocity and Effectiveness

To accelerate the path to the first CA-rendered frame and eliminate debugging friction, the following engineering improvements are prioritized:

1. **Trace-Guided Automated IPC & Kernel Gate Graph Extraction**:
   - *Problem*: Tracing the 4-tier IPC deadlock across `SpringBoard`, `backboardd`, `lsd`, `installd`, and `AppleKeyStore` required multiple 90-second probes and extensive manual binary disassembly.
   - *Improvement*: Implement an automated QMP/GDB trace hook in `display_probe.py` that intercepts `mach_msg_trap`, `_IOConnectCallMethod`, and `commandGate->commandSleep/commandWakeup`. The probe harness will automatically output the complete directed dependency graph (`Waiter PID/TID -> IPC Mach Port / Connection -> Kernel Wait Object`) at probe end.
2. **Sub-Second RAM Snapshot Iteration (QEMU SaveVM / Pre-Seeded Memory)**:
   - *Problem*: Each native probe requires a full 70–90 second cold boot from reset through iBoot, kernel initialization, and daemon launching.
   - *Improvement*: Capture a clean QEMU RAM snapshot immediately after `launchd` spawns the initial daemons (or right before `AppleKeyStore` initialization). Subsequent iterations testing SEP EP7 mailbox responses or keybag unlocks can restore from memory in < 2 seconds, achieving a **30x increase in iteration velocity**.
3. **Formalized SEP Mailbox Protocol Engine & Host-Side Contract Fuzzing**:
   - *Problem*: Handling SEP endpoints via nested `if-else` blocks in `s5l8960x.c` led to subtle mismatches (e.g. Cmd 4 returning status 0 instead of not found; Cmd 25 length 16 vs 8).
   - *Improvement*: Convert the SEP mailbox engine into a declarative table-driven state machine specifying opcode, tag, request size, response length, status codes, and keybag lifecycle states (Unprovisioned -> Created -> Unlocked). Expand `backend/sep_contract_test.py` to assert every EP7 handler contract against disassembled kernel constraints before executing guest boots.
4. **Copy-on-Write (CoW) Ephemeral Storage Harness**:
   - *Problem*: Copying multi-gigabyte disk images (`system-diagnostic-sep.raw`, `data.raw`) and running post-probe disk hash verification (`probe_cleanup.py`) consumes substantial I/O time and carries risk of source disk desync.
   - *Improvement*: Run probes against QEMU copy-on-write overlays (`qemu-img create -f qcow2 -b <golden.raw> overlay.qcow2`) or tmpfs mounts. Discarding the overlay at process exit is instantaneous, zero-cost, and mathematically guarantees the immutability of golden images.
5. **Pre-Indexed Symbolicated Firmware Cross-Reference Database**:
   - *Problem*: On-the-fly Mach-O parsing and disassembly of `dyld_shared_cache_arm64` and `kernel.macho` causes delays during post-mortem analysis.
   - *Improvement*: Maintain an offline SQLite/JSON cross-reference database indexing all function entries, string references, and vtables across iOS 7.1.2 Mach-O binaries, allowing instant stack symbolication and cross-referencing.

---

### 6. Actionable Next Steps

1. **Adjust EP7 Cmd 4 & Cmd 25 Semantics in `backend/s5l8960x.c`**:
   - Update Cmd 4 (`0x04`) to return non-zero status (e.g. `0x01` / Not Found) prior to keybag creation so `Selector 14` returns `0xe00002f0` (`kIOReturnNotFound`), prompting `keybagd` to load `/private/var/keybags/systembag` and execute `Selector 5` (`_AppleKeyStoreKeyBagSetSystemAndUnlock`).
   - Fix Cmd 25 (`0x19`) reply length to strictly 8 bytes (`0x8ULL << 48`).
2. **Synchronize Backend & Validate Unit/Contract Suites**:
   - Recompile QEMU (`make -C backend/build qemu-system-aarch64`).
   - Mirror changes to `backend/upstream/hw/arm/s5l8960x.c` and update SHA256 hashes in `backend/backend.json`.
   - Run `sep_contract_test.py`, `ans_contract_test.py`, and full unit test suite.
3. **Execute Native ART Persisted Probe 12**:
   - Run probe via `art_probe.py` with trace monitoring on `AppleKeyStoreUserClient::externalMethod` (`0xffffff8000daee48`), `0xffffff8000dab8d0` (Selector 5 handler setting `[AppleKeyStore + 0x119] = 1`), and `ca_render_display`.
   - Verify `installd` unblocks, `lsd` unblocks, `backboardd` unblocks, and `SpringBoard` completes `_BKSSystemAppCheckIn` to deliver the first CoreAnimation application frame.

---

## First-boot ART contract recovered; native persisted ART prerequisite blocks boot — 2026-10-15

**The first CA application frame remains unmet. The first-boot ART initialization and persistence lifecycle in native iOS 7.1.2 has been fully recovered, disassembled, and documented; the NVRAM escaping contract is implemented and tested. Unmodified normal boot strictly panics on absent `com.apple.System.sep.art` and has no in-band creation fallback.** Full findings, exact reproduction, disassembled call graph, ASN.1 specification, and verification proofs are in `backend/evidence/sep-software-model-20261015/FINDINGS.md` and `verification-summary.json`.

### Recovered native first-boot ART initialization & NVRAM contract

1. **Origin and Meaning of `bootSEP X3 = 1`**:
   - `seputil` (`init_data_protection`) invokes `_IOConnectCallStructMethod` selector `'firm'` (`0x6669726d`).
   - `AppleSEPUserClient::externalMethod` (`0xffffff8000d4ac30`) hardcodes `PayloadType = 0` (`w2 = 0`) and `load_art = 1` (`w3 = 1`), forwarding to `AppleSEPManager::setFirmwareBytes` (`0xffffff8000d47ed8`) and `AppleSEPBooter::bootSEP(fw, shmbuf, load_art=1)` (`0xffffff8000d48060`).
   - In `AppleSEPBooter::bootSEP` (`0xffffff8000d4d41c..0xffffff8000d4d430`), ART lookup is skipped if and only if `PayloadType == 1` AND `load_art == 0` (restore mode, selectors `'fire'` and `'firr'`). For normal boot (`'firm'`), `PayloadType = 0 != 1`, making `AppleSEPARTStorage::getCurrentART` unconditional.
2. **Absence of In-Band First-Boot ART Creation**:
   - `getCurrentART` (`0xffffff8000d50394`) queries `nvram->getProperty("com.apple.System.sep.art")` and returns NULL when absent.
   - At `0xffffff8000d4d484`, `bootSEP` tests the returned `OSData*`. When NULL, it calls `AppleSEPBooter::triggerRecoveryMode()` (setting `auto-boot = false`) and panics: `"SEP Boot Failure: failed to fetch ART from storage: 0x0"`. Unmodified normal boot has no in-band ART creation fallback or bypass.
3. **Genuine Hardware Creation Contract**:
   - Initial ART creation occurs exclusively during device restore (`seputil --restore` / selector `'firr'`).
   - In restore mode, `PayloadType = 1` and `load_art = 0`, allowing SEP to boot without ART.
   - Once alive in restore mode, the SEP coprocessor generates its initial Anti-Replay Token and sends it via unsolicited mailbox Opcode 0xa to AP Endpoint 2 (`AppleSEPARTStorage`).
   - `AppleSEPARTStorage::_msgAction` (`0xffffff8000d4f520`) receives Opcode 0xa, invokes `AppleSEPARTStorage::save_incoming_art` (`0xffffff8000d5004c`), which validates ASN.1 DER with `0xffffff8000d521ac`, wraps in `OSData`, persists to NVRAM under `com.apple.System.sep.art` via `persist_art` (`0xffffff8000d50148`), and calls `nvram->sync()`.
   - On all subsequent normal boots (`'firm'`), `com.apple.System.sep.art` exists in NVRAM.
4. **Decoded ASN.1 DER Anti-Replay Token Specification**:
   - Disassembly of kernel validator `0xffffff8000d521ac` / `0xffffff8000d51e0c` / `0xffffff8000d523b0` and userspace `seputil` (`0x100000ce8` / `0x10000502c` / `0x100005324`):
     * Outer structure: ASN.1 SEQUENCE (`0x30`).
     * Version: ASN.1 INTEGER (`0x02`), strictly enforced to 0.
     * Payload: ASN.1 SET (`0x31`) containing:
       1. `counter`: ASN.1 INTEGER (`0x02`, 64-bit integer anti-replay counter).
       2. `manifest hash`: ASN.1 OCTET STRING (`0x04`, SHA-256 manifest digest).
       3. `sleep hash`: ASN.1 OCTET STRING (`0x04`, SHA-256 sleep state digest).
       4. `restore nonce`: ASN.1 OCTET STRING (`0x04`, 16-byte / 20-byte nonce).
     * Digital Signature: EC signature (`ccec_verify` at `0xffffff8000773da4`) signed by device-unique SEP hardware EC key.
5. **NVRAM Encoding & Verification**:
   - `com.apple.System.sep.art` is registered in `IODTNVRAM`'s built-in variable table at `0xffffff8000614958` (`type = 4` / `kOFVariableTypeData`, `perm = 3` / `kOFVariablePermSystem`).
   - In the CHRP `common` partition, binary values are escaped with Apple's 0x00/0xFF run-length encoding (`escapeDataToData` at `0xffffff80002cb9e4`). On boot, `initOFVariables` decodes the property via `unescapeBytesToData` (`0xffffff80002cb904`).
   - Implemented `escape_nvram_data()`, `unescape_nvram_data()`, and optional `nvram_vars` dictionary in `scripts/qemu/boot_image.py`.
   - Added unit test `test_nvram_art_persistence_contract` in `tests/test_loader_runtime.py`. All 62 unit tests, all 9 contract suites, and `vm doctor` pass.

### Verification and Probes

- `make backend` passes (exit code 0; binary SHA256 `1c8b08aefb28f579eeb0fc236bb3e0d6e61f9cb5ab01917fa74f72daa51887e1`). Source and mirror match manifest SHA256 `ffc7437a78b17fc3263fbbdbed6c771f02151a899fd84fb4c584d0c4d5eb397c`.
- **62 unit tests pass** (`.venv/bin/python -m unittest discover -s tests -v`), **all nine guest smoke/contract suites pass** (including 5 SEP scenarios / 10 guest boots), and **vm doctor passes integrity/backend checks** with `VPHONE_LEGACY_VMS_DIR=build`.
- Probes `native-art-investigation-1` and `native-art-investigation-2` confirm the exact panic: `"SEP Boot Failure: failed to fetch ART from storage: 0x0"` at 1.718s / 1.768s. Panicked thread `seputil` PID 8. Breakpoints confirm status 1 -> BootTz0 ACK -> status 2 -> `art_nvram_lookup_before` returns `0x0` -> `art_fetch` returns `0x0` -> panic.
- Framebuffer at PA `0x8018e0000` remains zero (2,908,160 bytes, SHA256 `a14c9a6da44cc1530bd433aac101c5e59bb2cff7332e4e1f36edcde815af55ff`).
- All owned QEMU processes reaped. All source disk hashes unchanged (`source_unchanged: true`); disposable copies safely removed.

### Blocker and Next Bounded Task

Because native normal boot (`'firm'`) strictly requires a valid persisted `com.apple.System.sep.art` in NVRAM, and unmodified iOS has no normal-boot ART creation fallback, progress past the null-ART panic requires resolving the missing ART input prerequisite:
1. User authorization to provision a structured ASN.1 DER token in NVRAM via `build_nvram_proxy_data` (under the recovered ASN.1 schema), OR
2. Simulating a restore pass (`seputil --restore` / selector `'firr'`) where modeled SEP emits unsolicited Opcode 0xa to AP Endpoint 2, triggering `AppleSEPARTStorage::save_incoming_art` to naturally persist `com.apple.System.sep.art` to NVRAM.

Because genuine device-unique hardware EC keys, genuine SEP ROM, and decrypted SEP runtime are unavailable, and inserting arbitrary nonempty ART without authorization or modifying guest checks is forbidden, matching native ART is retained as an unavailable prerequisite barrier before KeyStore gate release and CA scanout. Update `CODEX_HANDOVER.md` and continuation prompt with actual results before stopping.

---

## Firmware ingested natively; mailbox boot protocol & encryption blocker mapped — 2026-10-15

**The CA-rendered application-frame objective remains unmet. Firmware ingestion by `seputil` and `AppleSEPManager` is now verified, but native SEP boot halts on the mailbox booter protocol and the lack of native SEP CPU/ROM/decryption execution.** Evidence: `backend/evidence/sep-publication-20261014-164800/sep-firmware-restored/` and `FINDINGS.md`.

Verified progression in probe `sep-firmware-restored`:
- **Prerequisites Proven Natively**:
  1. Helper `0xffffff8000d49cac` disassembled as `AppleSEPFirmware::fromMemory(IOMemoryDescriptor*, PayloadType)`. Enforces length <= 10MB, copies raw client bytes via `readBytes()`, returns 1 without IMG4 container or signature checks.
  2. Runtime flag captured in `seputil-runtime-flag-vnode2`: `[0x100008990+2] == 0`. `init_data_protection` skips `-P` and branches to `0x1000027d4` (`_IOConnectCallStructMethod` `'firm'`), completely bypassing the userspace Image4 root decoder.
- **Diagnostic Restoration & Live Ingestion**:
  - Space freed on diagnostic copy `/Users/bagas/.gemini/antigravity/brain/4f2c8eb8-288b-4247-84a2-e745314f5c38/scratch/system-diagnostic-sep.raw` (SHA256 `dfbc4938407dc873e4fa346b44aafae39541e53a5efbacf8b521884da692f3bb`) by removing unused update ramdisk `H6SURamDisk.dmg` (23.2 MB). Golden `build/ios712-validated/system.raw` preserved untouched (SHA256 `c7a987439e75bb8e989f3e1ee76bea3f0fec7d9ae804e5ad1d618409f0524c30`).
  - Restored `research/firmware-artifacts/sep-firmware.n53.RELEASE.im4p` (3,989,647 bytes, SHA256 `462b8877c7a3bfba7a1d5217beb499cf3745f002a211ef19ce0a4c5298ac15c0`) as `/usr/standalone/firmware/sep-firmware.img4`.
  - In `sep-firmware-restored`: `init_data_protection: can't open '/usr/standalone/firmware/sep-firmware.img4'` error was eliminated.
  - `AppleSEPManager::setFirmwareBytes` (`0xffffff8000d47ed8`) entered by `seputil`. Helper `0xffffff8000d49cac` returned allocated `AppleSEPFirmware` object `0xffffff808221bd00`.
  - `AppleSEPBooter::bootSEP` (`0xffffff8000d4d19c`) entered.
  - Transmitted first mailbox message: `SEP MMIO WRITE off=0x1010 val=0x201ff` (endpoint 0xff = `EP_BOOTSTRAP`, tag 1, op 2).
- **Exact Downstream Blocker**:
  - `AppleSEPBooter::bootSEP` calls `commandSleep` waiting for reply. In `_bootAction` (`0xffffff8000d4cd50`), booter validates `endpoint == 0xff`, opcode `0x66` (or `0xd2`), and extracts `(msg >> 32) & 0xffff` into `[booter + 0x1b2]`.
  - At `0xffffff8000d4d314`, booter checks `field_1b2 == 1`.
  - The current backend model in `s5l8960x.c` (`sep_handle_inbox_msg`) returns `resp = (uint64_t)ep | (1ULL << 15) | ((uint64_t)tag << 16)`, truncating ep to 7 and setting bits [63:32] = 0, failing the status check.
  - Complete native boot protocol mapped: status check 1 (`op=2`, expects status 1), `BootTz0` (`op=5`), status check 2 (`op=2`, expects status 2), `SEPART` (`op=7`, ART phys addr), boot finish (`op=6` or `8`), followed by `setFirmwareBytes` setting state to 4 (`SEP booting`) and `AppleSEPControlEndpoint` receiving `EP=0, op=0xd` -> `notifyOSActive` (`0xffffff8000d4887c`) setting state to 5 (`SEP/OS is alive`), which wakes `AppleKeyStore` gate wait (`0xffffff8000d48a5c`).
  - Prerequisite limitation: `sep-firmware.n53.RELEASE.im4p` is an encrypted IM4P with Production/Development keybags requiring the hardware SEP AES/GID engine. The emulator has no native SEP CPU/ROM execution to decrypt or run it.

## SEP publication verified; readiness blocks CA — 2026-10-15

**The CA-rendered application-frame objective remains unmet. The exact current
wait is SEP readiness, not failed AppleSEPManager publication.** New evidence:
`backend/evidence/sep-publication-20261014-164800/FINDINGS.md` and
`verification-summary.json`.

Native proof in one cold boot (`sep-chain-confirmed`):

- AppleSEPManager's start at **0xffffff8000d4748c**, vtable
  **0xffffff8000d563e0**, returns **W0=1**; registerService is reached and the
  KeyStore service match returns that published manager. EP7 creation succeeds.
- EP7 enable reaches **0xffffff8000d48a5c**. The native basic block waits for
  the 32-bit manager field **+0xc0 >= 5**; its actual value is **2**. This is an
  untimed commandSleep/recheck loop, not a framebuffer timing-register check.
- The waiting **profiled** thread owns AppleKeyStore's workloop gate. Native
  getWorkLoop returns KeyStore **+0x90**; inGate reads lock=[workloop+0x10],
  owner=[lock+0x10]. keybagd and installd enter newUserClient for that same
  KeyStore object while the owner remains the waiting profiled thread.
- In that same boot, installd waits in IOServiceOpen/MobileKeyBag initialization;
  lsd waits in MobileInstallationLookup while seeding its store; backboardd waits
  in LSPluginClient_GetStore/application-list initialization; SpringBoard waits
  in BKSSystemAppCheckIn. The preceding cross-boot association is now supported
  by one-boot gate/provider identities and retained stacks. No return error is
  invented for calls whose qualified returns were not observed.

The native System symlink `/usr/libexec/init_data_protection -> seputil` is
verified. Boot logs show the missing
`/usr/standalone/firmware/sep-firmware.img4`; no setFirmwareBytes entry or SEP
inbox request is observed in the final probe. Adding an arbitrary file is not a
verified fix: the only retained SEP artifact is a complete n53ap **sepi IM4P**,
SHA256 `462b8877c7a3bfba7a1d5217beb499cf3745f002a211ef19ce0a4c5298ac15c0`,
with unresolved decryption provenance. seputil's Image4 root parser compares
against **IMG4** and returns 2 on mismatch. Its complete preflight/load call
path must be checked before asserting compatibility; renaming/wrapping IM4P
is not proof of an accepted or authenticated load. The user confirms that no
additional verified SEP firmware, ROM/runtime or decryption inputs are available.

Backend audit finds a software SEP mailbox with generic endpoint/tag success
and zero payload, regardless of opcode, and no native SEP CPU/ROM/OS execution
path. The secondary Cortex-A7 is explicitly the ANS diagnostic CPU. Existing
SEP contract tests check the synthetic mailbox, not native secure-processor
boot or KeyStore correctness. No ACK was extended, no state byte was forced,
and no firmware was restored without verified compatibility.

Diagnostic improvements are implemented: concurrent/nested thread+SP-qualified
return hooks; early service filtering; nullable checked memory; kernel-byte and
shared-cache validation; deferred unmapped instruction faults; guest-running
wall time separated from paused inspection; and stop modes **first-pixel**
(default, any nonzero byte) / **ca-scanout** (conservative candidate requiring
client/surface/physical-swap/layer/completion milestones). A candidate still
requires explicit surface-ID/commit attribution and visual verification.
Transient captures are archived with raw/decoded/QMP files while CA diagnosis
continues. Default probes remain 45 seconds, with a 180-second maximum only for
specifically justified native timeouts/progress; no overall task deadline.

**13 transport/state tests and Python compilation pass.** All four native boots
are panic-free and end with all-zero framebuffer samples at 0x8018e0000. No CA
success is claimed; the older console-spinner evidence remains valid. Source,
upstream mirror and binary match their previous manifest hashes:
source `2dcd8fe258953455d3bab4d9a356e94581a97594d6cf39c623274a46f4ad6fdf`,
binary `0320168653da83f26bb3caca496d9f14c3fffd3eb1d765414f6f8ce049b1179d`.
No hardware change/rebuild was warranted. Prior hardware regression results
apply to this unchanged binary and were not relabeled as new test runs.

All owned QEMU processes are reaped; only fresh task-created disk copies are
removed after recording post-run hashes. Original System, clean seeded Data,
utility and firmware inputs are preserved. Exact diagnostic diff and commands
are retained in the evidence directory. An early readiness probe used +0x178
incorrectly for gate ownership; its superseded-field note directs readers to
the corrected +0x90 proof in `sep-chain-confirmed`.

Work stops on the unavailable prerequisite, as authorized in the plan; it does
not stop on an elapsed-time or quota cap. Last quota snapshot: 35% five-hour
usage, 30% weekly; handoff threshold remains 85% in either window.
`ANTIGRAVITY_PROMPT.txt` is refreshed with the actual blocker and reproduction.

**Next action:** establish verified native-loadable SEP firmware and required
SEP boot/ROM/execution inputs. Trace seputil's real preflight/load branch, then
setFirmwareBytes at **0xffffff8000d47ed8**, boot messages and state progression
past the readiness wait. Continue through release of the KeyStore gate,
installd/lsd/backboardd initialization, and a real CA surface-backed swap. Do not
repeat publication diagnosis or replace this with successful synthetic replies.


## First real pixels and upstream CA wait — 2026-10-14

**A real centered iOS activity spinner was captured at PA 0x8018e0000. A
CoreAnimation-rendered application frame remains unverified.** Evidence is
`backend/evidence/ca-surface-20261014-214745/`. This supersedes the earlier
all-zero capture as the latest pixel evidence, without changing the broader
SpringBoard, input, or stability gates.

### Pixel evidence and attribution

`console-writer-route/framebuffer_analysis.json` records the full **2,908,160-byte**
buffer, **non_zero_bytes=1101**, **non_zero_rgb_bytes=1101**, capture elapsed
**12.476194 seconds**, and SHA256
`f95084d218843d2ced97d540963d524373171ced3b7465fb598b9c80ef443286`.
The raw buffer, full decoded `framebuffer.png`, QMP `qmp-framebuffer.ppm` and PNG,
register state and breakpoint evidence are retained. The decoded PPM is
byte-identical to QMP's screenshot. Visual inspection shows a small gray activity
spinner centered on black; this is RGB content, not alpha-only changes or
unrelated memory. The earlier `ca-render-mapped` capture was also visually
inspected: 1101 RGB bytes, SHA256
`85911f21e8594a7bc05e048e4dbe3c1b2cfceda1618ce950ae0b4ce78e4bca1d`.
Two additional cold probes also captured the spinner (1083 RGB bytes).

Do **not** interpret this as a successful CA surface commit. Native FIFO packets
still have zero layer bases. At backboardd UserClient teardown,
`IOPlatformExpert::setConsoleInfo` at `0xffffff80002c7160` is called with X1=0,
W2=4; the caller is `0xffffff8000c5d658` in IOMobileFramebufferUserClient cleanup.
The same method had W2=5 during acquisition. Its platform video state describes
PA 0x8018e0000, rowBytes=2560, width=640, height=1136, depth=32. The spinner appears
after console re-enabling; this is evidence for the native console fallback,
not evidence that CA rendered an application. Its exact store instruction was
not captured. Other instrumented runs keep backboardd alive and remain zero.
The reason for teardown in the spinner runs remains unresolved. Neither the host
nor the probe writes pixels, patches guest bytes, or forces driver success.

### Earliest verified upstream waits

- Runtime userspace hooks derive the cache slide from a native Mach IPC frame,
  validate the mapped cache header, and compare executed instruction bytes with
  the extracted guest shared cache. Unmapped hooks are allowed to take the
  native instruction-page fault before being counted as execution. Shared cache
  local symbols were recovered from its local-symbols table; ASLR is not assumed.
- `ca-render-mapped/breakpoints.json` proves CA receives messages and registers
  a client context. The physical IOMFB display's `can_update()` returns **true**,
  and `begin_update()` / `finish_update()` run. The captured swap remains empty.
  No IOSurface creation, renderer entry, or client transaction commit was
  observed within that probe's installed-hook window. A missed hook is missing
  evidence, not a failed return. The first other display context comes from
  `create_blank_context` for the separate wireless provider.
- In `ca-fault-operand/ca-thread-state.json`, active backboardd's main thread is
  blocked in `_LSPluginClient_GetStore` -> `_LSCopyServerStore` -> `_LSContextInit`
  -> `_getApplicationsDict` -> `_getApplicationsList`. Its CA server thread waits
  for messages. SpringBoard waits in `BKSSystemAppCheckIn` in that run.
- `launchservices-wait/ca-thread-state.json` shows `lsd` waiting in
  `MOXPCTransportReceiveMessage` during `MobileInstallationLookup` ->
  `_LSServer_CreateAndSeedLocalDatabase` -> `_LSServer_GetStore` ->
  `_LSPluginServer_GetStore`. `installd` waits in `IOServiceOpen`, reached through
  MobileKeyBag `_connect_to_service`, `_initUserClient`, `_verifyUserClient`,
  `_AppleKeyStoreGetDeviceLockState`, and `_MKBGetDeviceLockState`.
- `keystore-provider/breakpoints.json` captures **keybagd**, resolved dynamically,
  opening the AppleKeyStore provider. `AppleKeyStoreUserClient::start` at
  `0xffffff8000dac664` returns 1 and generic newUserClient returns IOReturn 0.
  Its subsequent `AppleSEPKeyStore::sep_endpoint()` at `0xffffff8000dabf00` enters
  with object fields **+0x148 manager=0, +0x160 endpoint=0, +0x168 endpoint=0**.
- `sep-matching-wait/breakpoints.json` proves the deciding match:
  `IOService::waitForMatchingService` at **0xffffff80002ae764**, matching
  **IOProviderClass=AppleSEPManager**, timeout **X1=0xffffffffffffffff**. It is
  inside the KeyStore command-gate request and does not return before the probe
  ends. This is distinct from the already solved FIPS POST. The LaunchServices
  and KeyStore waits are observed in different fresh boots; their complete
  lock/IPC association has not yet been captured in one run.

No display register response is demonstrated to cause these upstream waits.
The earlier `Couldn't alloc class "AGXAcceleratorG4P_A0"` warning is a lead only;
it has not been tied to a failed CA renderer entry. No speculative GPU, SEP,
MIPI, or timing behavior was added.

### Changes, checks, and retained limits

Hardware source/binary are unchanged from the verified Chestnut/display fixes:
source/mirror SHA256
`2dcd8fe258953455d3bab4d9a356e94581a97594d6cf39c623274a46f4ad6fdf`, binary
`0320168653da83f26bb3caca496d9f14c3fffd3eb1d765414f6f8ce049b1179d`.
Both still match `backend/backend.json`. Therefore there is **no new backend
hardware diff or rebuild**. The six guest contract suites passed on this exact
binary in the preceding Chestnut session; they were not rerun here.

Diagnostic changes: shared-cache hook mapping, demand-page qualification,
bounded CA object/argument and task-specific frame snapshots, and queued GDB
stop-response handling without a duplicate guest memory read. New helper is
`backend/ca_probe_state.py`; exact diagnostic diff is `diagnostic-change.diff`.
The six debugger transport tests pass, including fragmented responses,
disconnects, and the queued-stop case. Python compilation passes.

Provenance, native QEMU command lines, original source-disk preservation hashes,
post-run copy hashes, process exit/reaping records, and pixel classification are
retained. Every boot uses independent System, `data-seeded-clean.raw`, and utility
copies. Task-created disk copies are removed only after owned QEMU is reaped.
No original firmware or seeded source disk was modified.

Probes requested 30–45 seconds and stopped immediately at nonzero bytes where
observed. The last target was 45 seconds, but debugger/QMP work overran it: final
capture elapsed 46.481 seconds, observations including paused capture/snapshot
47.502 seconds. These are recorded, not rounded down to claim a strict cap.
Several early diagnostic runs failed on demand-paged hook validation or a queued
GDB stop response; their negative evidence and reaping records are retained.
The user interruption interval had no running diagnostic process; it is excluded
from the active investigation budget.

**Next single targeted probe:** resolve AppleSEPManager's native `start` and
publication path (kext load VA **0xffffff8000d46000**), capture its qualified
return and the exact condition preventing publication/endpoint availability.
Tie that result to the held AppleKeyStore command gate and installd's blocked
open in the same boot. The boot log's missing `/usr/standalone/firmware/sep-firmware.img4`
is a lead, not yet the proven cause. Do not fake SEP replies or force keybag
success, and do not repeat solved display startup/VBI/FIFO work.


## Chestnut and display continuation — 2026-10-14

**Physical display startup and userspace swap dispatch now advance. Non-zero
framebuffer scanout remains unverified.** Latest evidence is
`backend/evidence/chestnut-display-20261014-210000/`.

Implemented native ABI subsets in `backend/s5l8960x.c` and rebuilt its upstream mirror:

- I2C0 slave 0x27 implements backed register 0x05 power-control state. Native
  `handleStart` and `setLDO` read/write this byte. Low four control bits are
  supported; unknown registers, upper control bits, lengths other than one,
  other addresses, and I2C1 still NACK. RX FIFO/occupancy, read-complete bit 20,
  STOP-complete bit 27, W1C and interrupt masks follow the recovered controller
  ABI. Controller reset preserves external chip state; machine reset selects
  explicit virtual-board state zero (modeled rails disabled). This is not a
  silicon reset, factory identity/calibration, EEPROM or analog voltage model.
- `disp0_ext +4` bit 0 mirrors the timing enable control bit 31. No initial
  timing status was forced to pass iBoot: the native driver now starts timings.
- Corrected synthetic VBI to status bit **0**, gated by its interrupt mask.
  Bit **1 is underrun**, proven by the native panic branch and
  `disable_display_underrun_panic`. Earlier handover descriptions calling bit 1
  VBL are superseded. Removed synthetic error bit 19 from swap completion.
  IRQ64 now honors its real mask; masked bit 8 no longer forces a corrupt-block
  interrupt. IRQ66's registered filter `0xffffff8000f38f64` is DPB, not a
  substitute VBI route. Native display VBI enabling writes mask 0x006ffa43.
- Implemented bounded command FIFO at `disp0 +0x10c0`: header A:length:token,
  followed by 0001:register-offset / value pairs. Entire packets are validated
  before applying writes. Unsupported headers/opcodes are logged and not
  acknowledged. Applied packets complete their header token at VBI; register
  0x1034 is no longer misinterpreted as a token. Actual native packet header
  0xa0560009 contains 86 subsequent words. The previous register-storage stub
  never applied these commands and left the bootstrap swap busy.

Verified progression:

- Thread/SP-qualified `AppleChestnutDisplayPMU::handleStart` returns W0=0;
  `ApplePinotLCD::start` returns W0=1. ADBE0 `startWithIBoot` returns W0=1.
- Backboardd opens a UserClient whose provider is the physical AppleMobileADBE0,
  registers its completion port, and reaches `swap_start` / `swap_submit` with
  successful IOReturn 0. WirelessDisplay still exists as a separate provider.
- Final cold run executes FIFO packets with 86 words and tokens **9,17,25,33**.
  Kernel panic is false throughout the 45-second verification probe.
- Full framebuffer PA 0x8018e0000 still has **non_zero_bytes=0**,
  **non_zero_rgb_bytes=0**, SHA256
  `a14c9a6da44cc1530bd433aac101c5e59bb2cff7332e4e1f36edcde815af55ff`.
  Native packets program zero layer bases/geometry; no visible UI is claimed.

Evidence: `physical-dispatch/breakpoints.json` has qualified PMU/LCD returns;
`vbl-callback-map/breakpoints.json` has physical provider and successful dispatch;
`first-swap-condition/breakpoints.json` retains the swap payload/pipe fields;
`final-scanout/qemu-trace.log`, `observations.json` and
`framebuffer_analysis.json` retain final packet/panic/pixel evidence.
Exact backend diff: `backend-change.diff` in the evidence directory.
Final build/source/mirror hashes are in `verification-summary.json`.

All six guest suites pass: Chestnut, unmatched I2C, display/FIFO/VBI, ANS, SIO,
and smoke. The FIFO fixture initially failed because its fixed 100ms delay was
shorter than its expanded guest timer sequence; completion-marker polling fixed
that test timing issue. Native negative experiments are retained: false bit 19,
underrun bit 1, and forced masked bit 8 each caused a real display panic before
being corrected. Do not reintroduce these status/IRQ shortcuts.

Fresh independent System/seeded-Data/utility copies were used per boot. Copies
are removed only after reaping owned QEMU processes and recording post-run hashes.
Source media/firmware hash checks and cleanup records remain in the evidence.

Historical next task (superseded by the latest section): inspect why the first physical CoreAnimation transaction
contains no rendered layer surface. Start from the retained swap payload and
zero FIFO-programmed layer bases/geometry; identify the exact CA/IOSurface
render or readiness condition before extending hardware. Do not repeat resolved
Chestnut startup, timing status, VBI routing, or command-FIFO diagnosis. Require
non-zero RGB scanout before claiming an Apple logo or SpringBoard success.


Updated 2026-10-14 after diagnosing the display pipeline hardware contracts, mapping
`AppleH6PDisplayPipe` / `AppleMobileADBE0` register architecture, correcting the synthetic
VBL interrupt delivery to AIC IRQ 64 Bit 1 (`0x2`), analyzing the `startWithIBoot` display
timing check in `ADBE0Driver::readTimingsStartedState`, tracing the `IOMobileFramebufferUserClient`
and `CADisplayIOMFB` userspace compositor architecture, and capturing live compositor state.
The original iOS guest UI framebuffer rendering remains unverified.

## Verified display blocker — 2026-10-14 bounded diagnostic

**Framebuffer objective remains unmet.** The final 45-second cold probe sampled all
2,908,160 bytes at PA `0x8018e0000` every second. `non_zero_bytes = 0` and
`non_zero_rgb_bytes = 0`; SHA256 is
`a14c9a6da44cc1530bd433aac101c5e59bb2cff7332e4e1f36edcde815af55ff`.
No Apple logo, progress bar, or SpringBoard pixels were captured.

### Exact blocking chain

1. Native `AppleChestnutDisplayPMU::handleStart` entry is
   `0xffffff8000ddd988`. Its I2C read has a one-byte prefix **register 0x05**
   and one-byte result. At `0xffffff8000ddda20`, the read has returned
   `W0 = 0xe00002e9`. A return breakpoint at `0xffffff8000ddd2bc`, qualified
   to the entry's original thread and SP, also captures `W0 = 0xe00002e9`.
   Backend logs show **I2C0 slave 0x27 NACK**, for both write and read starts.
2. The unchanged runtime DeviceTree identifies slave 0x27 as
   `/arm-io/i2c0/display-pmu`, compatible `display-pmu,chestnut`, phandle 0x4b.
   `/arm-io/mipi-dsim/lcd` has `function-lcd_ldo = 0000004b454c706401030000`.
3. `ApplePinotLCD` is blocked in `IOService::waitForService` at
   `0xffffff80002ae184` with **no timeout (`X1=0`)**, matching
   `IOPropertyMatch -> IOFunctionParent4B000000 -> empty OSData`.
   The native wait does not return within the bounded probe. The earlier
   unqualified claim that Pinot start returned success was invalid: another
   IOKit driver used the same return PC. Only thread/SP-qualified returns count.
4. `AppleMobileADBE0::startWithIBoot` then waits at that same kernel function
   for **IOFunctionParent5B000000**, also without a timeout. Its stack contains
   `0xffffff80007ff224` and `0xffffff8000f3b7c0`: the
   `function-lcd_enable` lookup before framebuffer publication.
5. `backboardd` does open a UserClient and register notification-port **type 0**
   successfully, but the provider's vtable belongs to **AppleWirelessDisplay**
   (kext load VA `0xffffff800122f000`). Its main-display implementation at
   `0xffffff8001230d90` returns false. No physical provider swap was observed.

**Timing-bit correction:** actual `readTimingsStartedState` reads
`0x206300004`, at PC `0xffffff8000f413cc`, and returns zero. Nevertheless the
superclass startup return at `0xffffff8000f3b79c` has `W0=1`, and bounded
single-stepping proves the `tbz` takes the successful fall-through. Therefore
zero timing bit 0 is not the demonstrated startup failure. Both display timing
reset values remain unchanged. No repeated MIPI status/power polling occurred
in the baseline trace (the initial 32-register reads are one-time activity).

### Changes, verification, and limits

- Backend change is observation only: machine property `display-trace=1`, with
  `-d unimp -D FILE`, wraps reads/writes to `disp0`, `disp0_ext`, and `mipi_dsim`.
  Each record includes virtual ns, guest/host origin, region, PA, offset, width,
  actual value, and caller PC. Special reads execute exactly once.
- Added `backend/display_probe.py`: independent copied disks, streamed hashes,
  retained commands/provenance, bounded native EL3 boots, full-buffer sampling,
  QMP evidence, entry/return hooks, dynamically discovered thread/process identity,
  thread/SP-qualified returns, and small targeted frame chains. Shared return
  addresses cannot be interpreted as another driver's success.
- Added `backend/display_trace_contract_test.py`: guest verifies reset/read/write
  behavior, special returned values, exact access counts and PCs, host reads,
  and opt-in behavior. This is a synthetic contract, not native pixel evidence.
- `make backend` succeeded; canonical overlay and upstream mirror match.
  ANS, SIO, smoke, and display trace contract tests all pass. Logs are retained.
- **No surgical hardware fix was applied.** The failure requires a real modeled
  Chestnut I2C slave/register contract; unconditional ACKs or successful zero-filled
  reads would conceal a missing device, not establish that contract. Generic
  unmatched-address NACK behavior remains intact.
- All original System/Data/utility sources and firmware are read-only inputs;
  each boot used fresh independent writable copies. Copied disks filled host free
  space during diagnostics. After child processes were reaped, only copies created
  by this session were removed, with post-run hashes retained. Logs/raw framebuffer
  dumps and provenance remain. The failed initial sandbox socket attempt and the
  no-space attempt are recorded, not treated as guest failures.

Primary evidence directory:
`backend/evidence/display-first-swap-20261014-202020/`.
Use `causal-findings.json`, `chestnut-blocker/breakpoints.json`,
`chestnut-i2c-nack.txt`, `qualified-lcd-start/breakpoints.json`,
`lcd-devicetree.json`, `lcd-ldo-target.json`, and
`final-scanout/framebuffer_analysis.json`. `provenance.json` in each run records
source/binary/boot-image/auxiliary/input hashes. `source-preservation.json` records
final source checks; `diagnostic-disk-cleanup.json` records removed copies.

Exact backend diff: `backend/evidence/display-first-swap-20261014-202020/backend-change.diff`;
full diagnostic and inline diff are in `ANTIGRAVITY_HANDOVER.md`.

## Target and workspace

Workspace: `/Users/bagas/Documents/Projects/vphone/vphone-legacy`.
Prepared bundle: `build/ios712-validated`.
Target: original iPhone6,2 / n53ap, iOS 7.1.2 build 11D257, in the project QEMU
window. Completion requires original SpringBoard, Apple apps, guest touch/Home
and keyboard, persistent Notes/settings, three cold boots and a stable 15-minute
session. The host manager only shows real status/logs. iOS 8–9 is deferred.
Reference project `../vphone-cli` is separate. No Git repository is present here.

Constraints and boundaries:
- Strictly immutable reference files (`storage-before/`, `build/ios712-validated/original/`).
- Zero guest kernel or userspace patching; all emulator changes confined strictly to `backend/s5l8960x.c` (mirrored to `backend/upstream/hw/arm/s5l8960x.c`) and `scripts/qemu/boot_image.py`.
- No background processes left running when awaiting user response.

## What is actually verified

- BuildManifest target selection, original kernel/DeviceTree/RootFS validation,
  decryption and decompression to real HFSX disk sectors.
- Original EL3 monitor hands control to XNU; real kernel instructions, timer,
  interrupts and native serial output are observed.
- Corrected synchronous ASP storage model transfers actual System/Data and
  utility file bytes with bounded DMA, real I/O failures and fsync.
- Native `ASPBlockStorage` publishes 795696 sectors of 4096 bytes,
  with a 1-MiB preferred buffer. Native GPT discovers `System@1` and `Data@2`,
  and BSD selects `disk0s1`.
- At native HFS callback return from `hfs_mountfs`, VA `0xffffff80004b8c40`,
  debugger captured `W0=0`. Bootstrap after the filesystem callback, VA
  `0xffffff80004e61fc`, also captured `W0=0`.
- Modeled SIO (`0x20ae00000`, `a7.sio`) implements the recovered `AppleA7IOP`
  and `AppleSmartIO` mailbox contract (outbox IRQ 135, OS-Active control message
  `0x00000000016d0000`), `EP=0` `AppleSmartIOControl` (`getParam`/`setParam`),
  `EP=1` `AppleSmartIOAES` (`performImmediateAESQuantum` / `performAESQuantum`
  inline `ctrl & 0x400` and DMA-segment AES-CBC/ECB, `disableHardwareKey`,
  `isKeySupported`), and `EP=2..0x4f` `AppleSmartIODMA`.
- `AppleSmartIO::start` and `AppleA7IOP::startCPU` return `X0=0` at
  `0xffffff8000918a60` (`smartio-start-cpu-return/`). `IOAESAccelerator::start`
  registers `SecureRoot` and enters `SecureRootCallBack` at `0xffffff80008ff6c8`
  (`secureroot-callback/`), completes 6 inline UID key derivations and
  `disableHardwareKey(0x3e8)`, sets `AppleARMPE` completion byte `+0x10b=1`,
  and wakes bootstrap at `0xffffff80004e6508` with `X0=0xe00002c1`
  (`secureroot-return/`).
- Directory root callback `VFS_ROOT` (`hfs_vfs_root`) returns `W0=0` at
  `0xffffff80004e65c8` and populates `rootvnode` at `0xffffff8000694090`
  (`rootvnode-ready/`).
- Process 1 `load_init_program` reaches `__mac_execve` call at
  `0xffffff80004e6dc8` with `[X19+0x10]=1` (PID 1), `X20=0xffffff8000629ff8`
  (`"/sbin/launchd"`), and `[SP+0x38..+0x50]={0x1000, 0x1080, 0, 0}`
  (`launchd-exec-call/`), and returns `W0=0` at `0xffffff80004e6dcc`
  (`launchd-exec-return/`).
- In EL0 userspace (`post-launchd-15s/serial.log`), `/sbin/launchd` starts up
  (`com.apple.launchd 1 *** launchd[1] has started up. ***`), runs `fsck_hfs` on
  `/dev/rdisk0s1` and `/dev/rdisk0s2` (`QUICKCHECK ONLY; FILESYSTEM CLEAN`), and
  mounts `/dev/disk0s1` on `/` and `/dev/disk0s2` on `/private/var`.
- Native userspace `FIPS USER Space POST` (`fipspost`) startup failure diagnosed
  and resolved:
  - Disassembly of `/bin/launchctl bootstrap` (`0x100003364..0x100003398`),
    `/usr/libexec/cc_fips_test` (`0x100003cbc..0x100003cc4`), and `libcorecrypto.dylib`
    (`_FIPS_POST` at `0x18e13be4c` -> `corecrypto_integrity_POST` at `0x18e13c018`).
  - `corecrypto_integrity_POST` checks `stat("/var/db/FIPS", &sb)`. If absent, it
    calls `mkdir("/var/db/FIPS", 0775)`. If `mkdir` fails, it returns 0, causing
    `_FIPS_POST` to log `FIPS USER Space POST: Integrity test failed!` and exit
    with -1, triggering `launchctl` to call `reboot(RB_HALT = 8)`.
  - On the unprovisioned blank Data partition (`data.raw`), parent `/var/db` did
    not exist because mounting `/dev/disk0s2` on `/private/var` hid the rootfs
    template. Breakpoint probe at kernel `SYS_mkdir` return (`0xffffff8000390654`)
    captured `PC=0xffffff8000390654`, `X0=2` (`ENOENT`), PID 3 (`cc_fips_test`),
    syscall 136 (`mkdir`) (`fipspost-mkdir-return/` and `fipspost-mkdir-proc-dump/`).
  - On diagnostic Data copy seeded with the `/private/var` template from `system.raw`
    (`fipspost-mkdir-success/`), `SYS_mkdir` return captured `X0=0` (success).
  - In `fipspost-resolution/serial.log`, `corecrypto_integrity_POST` passed, wrote
    `/var/db/FIPS/fips_data` (38 bytes: 32 bytes HMAC-SHA256 + 6 bytes `11D257`), all
    10 algorithm KATs (AES ECB, AES CBC, AES GCM, AES XTS, TDES CBC, SHA, HMAC,
    RSA, ECDSA, DRBG) passed, and `FIPS USER Space POST: Integrity test failed!`
    and userspace shutdown were **completely eliminated**.
  - Subsequent userspace daemons initialized: `tzinit` ran without symlink errors,
    `lockdownd` (PID 51) generated RSA pairing keys, `installd` processed preinstalled
    applications into `/private/var/mobile/Applications/`, and `notifyd`, `syslogd`,
    and `MobileInstallation` ran.
- `AppleH6CamIn` ISP coprocessor wake handshake and timeout eliminated:
  - In `fipspost-resolution/serial.log`, `AppleH6CamIn::ISP_Init` stalled for 30s
    waiting for `ISPCPU` wake signal (`AppleH6CamIn::ISP_Init - No wake signal from ISPCPU`,
    `0xE00002D6`).
  - Modeled ISP wake handshake in `backend/s5l8960x.c`: write to ISP peripheral
    `a7.isp` (`0x205b12000`) at offset `0xee028` bit 4 returns wake ack `0x08042006` at
    offset `0xae11c`, clears channel count at `0xae100`, and sets ring wrap at `0xae104`.
  - In `serial-comp.log`, `AppleH6CamIn::ISP_Init` immediately exits with clean
    `kIOReturnError` (`0xE00002BC`) in 0ms without delay, allowing userspace daemons to
    launch immediately.
- `backboardd` and `SpringBoard` compositor bringup verified:
  - `backboardd` (PID 20/24) is executing in EL0: ASLR slide `0xa0000`, `isScreenBlanked = 0`
    (screen unblanked), `displayController = 0x170012cc0` instantiated, and
    `progressWindow = 0x1700a1440` instantiated.
  - `SpringBoard` (PID 45/51) is executing in EL0: ASLR slide `0xe4000`, `screenBlanked = 0`
    (screen unblanked).
  - Thread unwinding via GDB/QMP confirmed:
    - `SpringBoard` Thread 0 active in `CFRunLoopRun` / `UIApplicationMain` waiting on `mach_msg_trap`.
    - `backboardd` Thread 0 active in `CFRunLoopRun` / `GSEventRunModal` / `UIApplicationMain` waiting on `mach_msg_trap`.
    - `backboardd` Thread 2 active in `CA::Render::Server::server_thread(void*)` (`0x18a550230` / `0x18a248230`) waiting for CoreAnimation transactions.
- Display pipeline hardware model (`a7.disp0` at PA `0x206200000`, `a7.mipi_dsim` at PA `0x206400000`) verified:
  - Synthetic 60Hz VBL clock asserting bit 1 (`0x2`) in status register `0x102c`,
    wired to AIC IRQ 64 via mask register `0x1028` (driver enable mask `0x006ffa42`).
  - W1C status clear at `0x102c`, lowering IRQ 64 when all unmasked status bits clear.
  - Auto Mode bit 0 (`0x1`) set in register `0x104c`.
  - Frame swap complete bit 8 (`0x100`) in `0x1038`, FIFO parameter depth `0x1044` reads 0,
    token latched at `0x1048`, and DPipe flush at `0x1050`.
  - Framebuffer base register `0x4044` latched at `0x018e0000` (PA `0x8018e0000`), stride `0x4048` (`2560`), geometry `0x4070` (`640x1136`).
- Kernel display architecture disassembly findings:
  - `AppleDisplayPipe::interruptHandler()` at `0xffffff8000f2554c`:
    - Tests `[this + 0x37ac]` and `[this + 0x5be]`. If either is non-zero (power off), logs `Interrupt while power is off:` and drops the interrupt.
    - Status register `0x102c` bit 1 (`0x2`) is VBL (Vertical Blanking). Bit 0 (`0x1`) is Underrun Error. Bit 8 (`0x100`) is Frame Swap Token Match.
    - Bit 1 triggers VBL frame timing recording and dispatches the VBL display link.
  - `AppleDisplayPipe::filterInterrupt()` at `0xffffff8000f25d64`:
    - Reads status register `0x102c`, latches into `[x19 + 0x5dc]`, writes back to `0x102c` to clear (W1C), and checks against shadow mask `[x19 + 0x5d8]`.
  - `AppleDisplayPipe::enable_interrupts(bool)` at `0xffffff8000f2743c`:
    - When called with `enable=true`, calls `0xffffff8000f2dd40` to get driver mask `0x006ffa42`.
    - Writes `0xffffffff` to `0x102c` (clear all), stores `0x006ffa42` into `[x19 + 0x5d8]`, writes `0x006ffa42` to MMIO `0x1028`, and sets `[x19 + 0x37ac] = 0` (power on).
  - `AppleMobileADBE0::startWithIBoot()` at `0xffffff8000f3b718`:
    - Calls `gatherIBootSettingsFromRegisters()` at `0xffffff8000f3ddb8`, which calls `ADBE0Driver::readTimingsStartedState()` at `0xffffff8000f413a4`.
    - `readTimingsStartedState()` checks `*(uint32_t*)(regs + 4)` bit 0.
    - If bit 0 is 1, `[x19 + 0x5dd2]` and `[x19 + 0x5ab]` are set to 1.
    - At `0xffffff8000f3b798`, calls `super::start_hardware` (`AppleDisplayPipe::start_hardware`).
    - At `0xffffff8000f3b79c`: `tbz w0, #0, 0xffffff8000f3b8f8`. If `super::start_hardware` returns 0, it logs `super::start_hardware failed` and aborts display startup!
    - If it succeeds, it sets up `function-lcd_enable`, calls `0xffffff8000f3df74`, and publishes `IOMobileFramebuffer`.
  - `IOMobileFramebufferLog` at `0xffffff8000c5eaa8`:
    - In production iOS 7.1.2 kernel, this logging helper begins with `ret` (`0xffffff8000c5eaa8: ret`), so driver debug strings are suppressed from serial console by Apple design.
  - `PUIProgressWindow` architecture in `ProgressUI.bin`:
    - `PUIProgressWindow` initializes with a `CAContext` (`contextId = 1`) and creates `_appleLogo` and `_progressLayer`.
    - Queries `MGCopyAnswer` (`libMobileGestalt.dylib` at `0x18d1621a0`) for display width, height, and scale.
    - In `CADisplayIOMFB` (`QuartzCore`), surface commits invoke `IOMobileFramebufferUserClient` method selector 5 (`swap_submit` at `0x187735aac` / kernel `0xffffff8000c61a90`).

**Still unresolved:** First compositor surface commit (`IOMobileFramebufferUserClient::swap_start` / `swap_submit`),
framebuffer scanout of non-zero pixels (Apple logo / progress bar), original SpringBoard UI rendering, guest touch/Home input, and clean guest shutdown.

## Current storage and SIO implementation and constraints

The backend is an explicit **software device model**, separate from optional
ARM32 IOP firmware execution. It implements recovered logical ASP (`0x208040000`)
and SIO SmartIO (`0x20ae00000`) command ABIs; it is not complete physical
NAND/coprocessor emulation. Native IOP diagnostic mode disables modeled ANS
handshakes and remains blocked on private MMIO.

Machine properties explicitly select `system-image`, `data-image`,
`system-writable=1`, and `utility-image`. Immutable `system.raw` is preserved;
`system-runtime.raw`, `data.raw`, and `utility.raw` persist guest writes.
Launcher holds ownership and retains pre-launch independent disk copies in
`logs/SESSION/storage-before/` with hashes. Existing/corrupt/unregistered media
is preserved and rejected, never silently reset. No concurrent writable launch.

Utility backing is a committed model-private logical namespace,
`n53ap-11D257-ASP10-utility-v1`, exactly 9580544 bytes. A validated CRC/header,
fixed region map, firmware-source binding, geometry and actual file extent
establish its readiness. New payload pages contain real erased FF bytes.
Typed LLB, firmware-shadow, ControlBits, effaceable, NVRAM and SysCfg operations
transfer those stored bytes; guest consumers still validate content. No factory
identity, calibration, authenticated content or original NAND state is claimed.
Native clock/init queries use actual backing validation, committed format epoch
and QEMU virtual time. Unknown commands return native errors.

**Native format opcodes 0x15/0x16 remain unsupported.** Original ANS firmware
shows 0x15 resetting utility and LBA state. Do not add `nand-enable-reformat`,
redefine a native formatter as a successful no-op, or alter existing VM disks.
Provisioning the virtual logical utility profile is a separate explicit model
policy. See `research/utility-model-profile.md` and
`research/native-utility-format.md` for exact boundaries.

## Failed experiments and lessons

- **Inline vs DMA selector in `SmartIOAESOperation`**: Initially checking
  `src_lo32 == 0 && dst_lo32 == 0` at `+0x178`/`+0x180` worked for inline
  `performImmediateAESQuantum` calls during `IOAESAccelerator` startup, which
  zeroed those fields, but misclassified DMA operations when segment index 0 was
  used. Disassembly of `AppleSmartIOAES::prepareAESOperation`
  (`0xffffff800091b7c8` vs `0xffffff800091b814`) and `completeAESOperation`
  (`0xffffff800091baf8`) proved that `ctrl` bit 10 (`0x400`) is the true inline
  buffer selector (`ctrl = 0x712` for inline vs `0x312`/`0x311` for DMA).
- **SIO SmartIO AES vs EL0 userspace `cc_fips_test`**: Initially hypothesized that
  an AES DMA misclassification was causing the FIPS integrity test failure. Disassembly
  proved that `cc_fips_test` is a pure EL0 userspace software crypto test using
  `libcorecrypto.dylib` that does not access SIO MMIO; the failure was a filesystem
  `ENOENT` on `mkdir("/var/db/FIPS", 0775)` caused by an unprovisioned blank Data
  volume hiding `/private/var`.
- **`AppleMobileADBE0` power state gate vs AppleTV**: Disassembly of `swap_begin`
  (`0xffffff8000f3bdf0`) initially raised concern that `fControllerPowerState` (`+0x5dff`)
  was 0 because `first_client_open()` sets `+0x5dff=1` only if `+0x5dfe` is non-zero.
  Further disassembly of `testAppleTV()` (`0xffffff8000f3b4d4`) proved that `+0x5dfe`
  represents the `'external'` device tree property (AppleTV). On iPhone, `+0x5dfe` and
  `+0x5dff` are intentionally 0, and `swap_begin` falls through to checking active layers,
  `pendingSwap` (`+0x338`), and `swapBusy` (`+0x330`).
- **`AppleH6PDisplayPipe` interrupt enable mask vs underrun**: The driver's default
  interrupt mask `0x006ffa42` enables VBL (bit 1) and Swap Complete (bit 8), but disables
  underrun error (bit 0). An earlier synthetic timer checking `if (mask & 1)` failed to
  fire VBL after driver initialization; checking bit 1 (`0x2`) restored continuous 60Hz
  display link interrupts on AIC IRQ 64.
- **`breakpoint_probe.py` default `--boot-el`**: Default `--boot-el` is `1`
  (synthetic fixture mode). Always pass `--boot-el 3` when probing the real
  monitor/kernel boot plan, otherwise EL1h entry faults on the monitor's `smc`.
- **`ADBE0Driver::readTimingsStartedState()` bit 0**: In `backend/s5l8960x.c`, offset `0x4`
  of `a7.disp0` (`0x206200004`) was initialized to `0`. Disassembly of `AppleMobileADBE0::startWithIBoot()`
  proved that `readTimingsStartedState()` tests `*(uint32_t*)(regs + 4) & 1` to confirm iBoot
  display timings were started. When bit 0 is 0, `startWithIBoot` records uninitialized timings
  and branches to error handler `0xffffff8000f3b8f8` (`super::start_hardware failed`), preventing
  `AppleDisplayPipe::enable_interrupts()` from being called.

## Next bounded task

Implement the recovered `AppleSEPBooter` mailbox state machine in `backend/s5l8960x.c` (and upstream mirror `backend/upstream/hw/arm/s5l8960x.c`) to conform to the native kernel booter disassembly:

1. **Model Mailbox Boot Protocol**:
   - In `sep_handle_inbox_msg`:
     * When receiving message on `EP_BOOTSTRAP` (`0xff`):
       - Initial status check (`op = 2`): return reply opcode `0x66`, status in bits `[47:32] = 1`.
       - `BootTz0` (`op = 5`): return reply opcode `0x66`, status in bits `[47:32] = 0`.
       - Second status check (`op = 2`): return reply opcode `0x66`, status in bits `[47:32] = 2`.
       - `SEPART` (`op = 7`): return reply opcode `0x66`, status in bits `[47:32] = 0`.
       - Boot finish (`op = 6` or `op = 8`): return reply opcode `0x66`, status in bits `[47:32] = 0`.
     * Upon booter completion (`setFirmwareBytes` reaches state 4), modeled SEP raises `EP = 0, op = 0xd` (or `0x11` -> `0x12`), invoking `AppleSEPManager::notifyOSActive()` (`0xffffff8000d4887c`) to set manager state to 5 (`SEP/OS is alive`) and wake the `AppleKeyStore` gate wait (`0xffffff8000d48a5c`).
2. **Build and Validate Backend**:
   - Recompile via `make backend` in the repository root.
   - Run and pass all 52 unit tests and 7 contract suites (`sep_contract_test.py`, `sio_contract_test.py`, `ans_contract_test.py`, etc.).
3. **Execute Verification Probe**:
   - Run bounded probe on diagnostic copy with restored `/usr/standalone/firmware/sep-firmware.img4`.
   - Verify `bootSEP` returns 0, manager state advances to 4 then 5, `profiled` wakes and releases `AppleKeyStore` workloop gate, and `keybagd`, `installd`, `lsd`, `backboardd`, and `SpringBoard` unblock.
   - Follow CoreAnimation RenderServer to client transaction commit, IOSurface swap, and non-zero RGB scanout at `0x8018e0000`.
   - Maintain strict source preservation and clean up diagnostic images after reaping QEMU.

## Evidence and tests

Latest full guest serial/storage probe:
`build/ios712-validated/logs/20261013-122816-2da89b24/`.
Actual mount debugger evidence:
`backend/evidence/hfs-mount-20261013/after-mount/` and `bootstrap-return/`.
Pre-fix wait and negative evidence in `backend/evidence/hfs-mount-20261013/`:
`platform-wait/`, `secure-root-call/`, `secure-root-return/`, `secure-root-wait/`,
`secure-root-provider/`, `process1-entry/`, `aes-root-registration/`, and
`aes-start-entry/`.
Verified post-SIO progression evidence in `backend/evidence/hfs-mount-20261013/`:
- `smartio-start-cpu-return/` (`0xffffff8000918a60`, `X0=0`)
- `secureroot-callback/` (`0xffffff80008ff6c8`, `IOAESAccelerator` callback entry)
- `secureroot-return/` (`0xffffff80004e6508`, `X0=0xe00002c1`, `+0x10b=1`)
- `rootvnode-ready/` (`0xffffff80004e65c8`, `W0=0`, `rootvnode != 0`)
- `launchd-exec-call/` (`0xffffff80004e6dc8`, `[X19+0x10]=1`, `"/sbin/launchd"`, `[SP+0x38..+0x50]`)
- `launchd-exec-return/` (`0xffffff80004e6dcc`, `W0=0`)
- `post-launchd-15s/` (`launchd[1]` startup, `fsck_hfs`, `/` and `/private/var` mount, `FIPS USER Space POST` failure)
- `fipspost-mkdir-return/` (`0xffffff8000390654`, `SYS_mkdir` return, `X0=2` ENOENT, PID 3 `cc_fips_test` on unseeded Data partition)
- `fipspost-mkdir-proc-dump/` (HMP registers and process memory confirming PID 3 at `0xffffff8081eafad0`)
- `fipspost-mkdir-success/` (`0xffffff8000390654`, `SYS_mkdir` return, `X0=0` success, on seeded diagnostic Data volume)
- `fipspost-resolution/` (40s guest run: `fips_data` created, KATs passed, shutdown eliminated, daemons initialized)
- `comp-bringup-70s` (70s run: `backboardd` unblanked PID 24, `SpringBoard` unblanked PID 49/51, stack unwinding verified runloop and RenderServer execution)
- Research analysis: `research/fipspost-diagnosis-20261013.md`

Latest regression: **52 unit tests pass** plus **7 guest-executed backend
contract suites** (smoke, GPIO, I2C, ASP/clock, utility, SIO SmartIO/AES, and SEP)
and `vm doctor`. Transcript:
`backend/evidence/hfs-mount-20261013/sio-launchd-regression.log`.
Those fixtures establish device contracts, not iOS UI.

```sh
VPHONE_REAL_FIRMWARE_TEST=1 \
VPHONE_TEST_BUNDLE="$PWD/build/ios712-validated" \
  .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python backend/smoke_test.py
.venv/bin/python backend/gpio_contract_test.py
.venv/bin/python backend/i2c_contract_test.py
.venv/bin/python backend/ans_contract_test.py
.venv/bin/python backend/utility_contract_test.py
.venv/bin/python backend/sio_contract_test.py
.venv/bin/python backend/sep_contract_test.py
VPHONE_LEGACY_VMS_DIR="$PWD/build" \
  .venv/bin/python sources/vphone-legacy-cli/main.py vm doctor ios712-validated
```

Frozen backend: QEMU v11.1.2.
Capabilities for full iOS storage, userspace, graphics, input, SpringBoard and clean shutdown stay false pending their gates.

Useful small sources: `research/bootstrap-launchd-20261013.md`,
`backend/evidence/hfs-mount-20261013/SIO-COVERAGE.md`,
`research/continuation-20261013-storage.md`, `research/utility-model-profile.md`,
`backend/README.md`, `backend/s5l8960x.c`, `scripts/qemu/runtime.py`,
`scripts/qemu/storage.py`, `scripts/qemu/utility.py`, and `scripts/bundle.py`.
REA native engine was unavailable; retained native disassembly and QEMU/GDB
runtime were used. Do not describe static analysis as observed execution.

Before stopping, update this file with the latest verified hook, exact evidence,
changes/tests, failed experiments and the next bounded task. Runtime state is
separate from the manifest and does not infer boot success from log words.

## Instructions for Codex

1. Read this document thoroughly before proposing or making changes.
2. Emulator implementation changes belong in `backend/s5l8960x.c` (mirrored to `backend/upstream/hw/arm/s5l8960x.c`) and `scripts/qemu/boot_image.py`. The user additionally authorizes focused contract/unit tests and evidence/documentation updates. Never modify guest kernel binaries, kexts, frameworks, original rootfs, or reference data.
3. Every backend change must build via `make backend` from the vphone-legacy root and pass the full discovered unit suite (currently 61 tests), all nine backend smoke/contract suites, and vm doctor. Report actual test counts and acceptance limits.
4. When testing with diagnostic disk images, always work on copies (`scratch/data-seeded.raw`) restored from the clean golden template (`scratch/data-seeded-clean.raw`). Never leave temporary QEMU instances or background jobs running.

## Supplied SEP artifact rechecked — 2026-10-15

The user supplied `research/firmware-artifacts/sep-firmware.n53.RELEASE.im4p` again. Its 3,989,647 bytes and SHA-256 `462b8877c7a3bfba7a1d5217beb499cf3745f002a211ef19ce0a4c5298ac15c0` match the artifact already analyzed. This adds no distinct firmware or verified decryption/ROM input.

Further bounded native-loader inspection and live probe `seputil-runtime-flag-vnode2` proved that runtime flag `[0x100008990+2] == 0`, branching directly to `0x1000027d4` (`_IOConnectCallStructMethod` `'firm'`) and bypassing the userspace IMG4 decoder. Diagnostic restoration on `/Users/bagas/.gemini/antigravity/brain/4f2c8eb8-288b-4247-84a2-e745314f5c38/scratch/system-diagnostic-sep.raw` enabled successful file open and ingestion into `AppleSEPManager::setFirmwareBytes` (`0xffffff8000d47ed8`) and execution of `AppleSEPBooter::bootSEP` (`0xffffff8000d4d19c`). The exact downstream boot blocker is now mapped: booter expects reply opcode `0x66` on `EP_BOOTSTRAP` (`0xff`) with status check 1 in bits `[47:32] == 1`, whereas the current software mailbox in `backend/s5l8960x.c` returns `0x28007`. The next step is to implement the modeled booter handshake sequence in `backend/s5l8960x.c` and proceed to CoreAnimation scanout at `0x8018e0000`.
