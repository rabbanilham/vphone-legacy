# T19: absent modem and Oscar boot waits

2026-10-22, `wt/baseband`, `/Users/bagas/Downloads/vphone-wt-baseband`.

## Result and scope

The board advertises two external processors which the emulator does not implement. The loader now omits `/baseband`, `/arm-io/usb-complex/usb-ehci0/hsic-baseband`, and `/arm-io/uart2/oscar` (including its sensor children) from its generated hardware description. Controllers, other children, PMU/GPIO wiring, and baseband audio remain unchanged. This is a board-population correction, not a claim to emulate an operational modem or M7. No guest executable, kernel instruction, filesystem, disk image, timeout, clock, or scheduler was changed.

There is a significant correction to the proposed diagnosis: **CommCenter is waiting for InterfaceNamer at 45 s, but for a helper RPC at 65 s.** The snapshots do not establish a single 40-second pthread_once hold or a modem reply as the immediate cause of both samples. The static dependency and the phantom hardware are established below; the exact critical-path savings and disappearance of the PM warning require the maintainer's boot comparison. No emulator or backend build/run was performed here.

## Evidence and address conventions

Inputs: `scratchpad/probe130/{ca-thread-state-045s.json,ca-thread-state-065s.json,ca-thread-state-085s.json,serial.log}` under the supplied `/private/tmp/claude-501/-Users-bagas-Downloads-vphone-legacy/8d4c9f73-518d-4e4f-a9c7-5a124b947693/` directory.

All three cache slides are `0xafd8000`. Cache PCs below are unslid. Parsed mappings from the index, but reconstructed **all images from header +24 (`imagesOffset, imagesCount`), 32-byte `<QQQII>` records**, then LC_SYMTAB, LC_DYSYMTAB/indirect stubs and export tries, as in T15. Used Capstone from the specified legacy venv. Kernel addresses use `fileoff = VA - 0xffffff8000a00000 + 0x1000000`.

Read-only disk attachment supplied CommCenter and configd for disassembly; `/dev/disk4` was detached afterwards. Local research copies are `/tmp/t19/`. SHA256:

| Input | SHA256 |
|---|---|
| supplied KernelMemory.bin | `bba9a4bace9b505b80c05bc15648b46122c8948f86ac4f0d0625f7804d78d7d8` |
| supplied DeviceTree.runtime | `b75e0054fb52aedc461ecaf88475e93f5b9f6d370c0e0cc040273b4b224d810b` |
| disk CommCenter | `c76c1c4e93f1b7a41325aaa42c59e426061b39f22c23d0d3419154e17b7e7124` |
| disk configd | `43848ebe0013906cbe2038b8ec583ece1319f02cd06132679eabf1eff7fc832d` |

## CommCenter: actual waits and modem dependency

At 45 s, TID 474's frame sequence resolves as follows:

- `0x18f5c8794 - slide = 0x1845f0794`: immediately after `mach_msg`, in `SCDynamicStoreNotifyWait` (`0x1845f05c8`). Receive options are 2, timeout argument zero, **without MACH_RCV_TIMEOUT**: indefinite notification wait, not a 100-ms timeout (the `0x64` argument is receive buffer size).
- `0x18f5f6144 - slide = 0x18461e144`: initializer `0x18461e028` has just called `SCDynamicStoreNotifyWait`. It subscribes to `%@InterfaceNamer`, then repeatedly reads the dynamic-store value until the array contains `*QUIET*` or `*TIMEOUT*`. These strings are CFStrings at `0x18ebf2440`, `0x18ebf2480`, `0x18ebf24a0`.
- `0x18f5f18cc - slide = 0x1846198cc`: `__SCNetworkInterfaceCopyAllWithPreferences` just called `pthread_once` on that initializer at `0x1846198c8`. Thus this is a wait for network-interface enumeration/IOKit settlement, not direct modem IPC.

The producer is configd's built-in InterfaceNamer. Its worker at executable `0x100014e04` reads `WaitStackTimeout` and `WaitQuietTimeout` from the bundle Info dictionary. Defaults at data `0x10003aaa0/+8` are **300 and 60 seconds** respectively; error paths at `0x100014ee4` and `0x100014f64` restore those values. The supplied bundle has no overrides. These are distinct budgets, not two delays proven to be consumed by this boot. The quiet timer callback `0x1000158b8` publishes `*TIMEOUT*`; the quiet notification path near `0x10001580c` publishes `*QUIET*`. CommCenter's notification wait itself has no private finite deadline. The observed 30–90 s mostly-WFI window is consistent with this 60-second producer deadline, not evidence that CPUs need more compute.

At **65 s**, the same main thread has left that initializer. CommCenter executable slide is **0x64000**, independently identified by the top frame's call to the `_mach_msg` stub and its caller chain:

```
1001d5e94 -> unslid 100171e94 (MIG reply wait after mach_msg)
10019ca00 -> unslid 100138a00 (helper connection setup)
100146b88 -> unslid 1000e2b88
1001480c8 -> unslid 1000e40c8
1001463c8 -> unslid 1000e23c8
```

`0x1001388e8` calls `bootstrap_look_up` at `0x1001389c0`, allocates a reply port, then calls MIG routine `0x100171e24`. The request ID is `0x491f6e`, send size 40, receive size 48, options **3** (send+receive), timeout zero **without timeout options**: an indefinite synchronous **CommCenter helper** reply wait. Its error text is `Unable to locate service: %s. Are the CommCenter helper processes present on the system?`. Do not label this an HSIC or baseband power register wait. The captured tasks omit the helper's threads, so its server-side blocking point and actual duration are not established. By 85 s CommCenter's main thread is in the CF run loop. SpringBoard/lockdownd's CoreTelephony synchronous XPC wait can be downstream of both phases.

Hardware discrepancy relevant to modem startup:

| Firmware DT contract | Emulator situation |
|---|---|
| `/baseband`, `compatible=baseband,n41`, `class=<3>`, `device_type=baseband` | Native `AppleBaseband::start` does run (serial); no modem model |
| PMU `function-bb_on`: GPIO2; `function-bb_ldo`: GPIO14; PMU exton input GPIO1 | PMU storage/GPIO implementation, no external modem state machine |
| SoC `function-bb_rst`: GPIO10; reset-detect GPIO104, interrupt type 4; radio-on GPIO111; HSIC-ready GPIO17 | No external modem reset/boot response |
| HSIC port 3, identifier `0x00020001`; host-ready GPIO163, device-ready GPIO164/type 4 | No EHCI/HSIC implementation to publish a functioning port |

`AppleBasebandN92::findHSICPort` at kernel `0xffffff8000f0eaf8` constructs a name match for **`hsic-baseband`** at `0xffffff8000f0eb3c..48`, then calls `0xffffff8000f10388` with x1=`UINT64_MAX`. That stub resolves to kernel `0xffffff80002ae764`, the matching-service wait. Its `timeout == -1` branch (`0xffffff80002ae7e0..808`) sleeps without a deadline. Callers include the HSIC-enable path `0xffffff8000f0e99c` and later port-control paths. This is an **indefinite service dependency**, not a measured 18.5- or 30-second wait. The snapshots do not capture a thread in it; do not assert it alone held the IOKit busy count in probe130.

User-side `TelephonyBasebandCreateControllerWithParams` (`0x18cf7b038`) reaches `0x18cf7b648`, which uses `IOServiceMatching`/`IOServiceGetMatchingService` for `AppleBaseband`; missing service follows its failure return, with no wait added there. MobileGestalt's `/baseband` property readers (`0x18d167098`, `0x18d168cf8` through `0x18d16d434`) use `IORegistryEntryFromPath` and return missing data when the node is absent. There is no need to invent an IMEI, a property that claims readiness, a file, or an enabled UART modem. UART2 is Oscar's connection, not the cellular modem's HSIC transport. Telephony still knows the n53 product identity; this change does not turn iPhone6,2 into an iPad or promise cellular service.

## Oscar: ready event and timing

Oscar is advertised below **UART2**, MMIO base **`0x20a0a8000`**, size `0x4000`, AIC IRQ **142**, SIO RX channel **65**. Its reset uses SoC GPIO38; `function-oscar_power1/power2` target PMU LDO15/LDO17 and sensor power targets LDO3. GPIO160 is time sync. These are not a special MMIO `BOOT_ROM_READY` register.

`AppleOscarProcessor::WaitForBootROMReady` starts at kernel **`0xffffff8000ccfa34`**. It computes one deadline using `clock_interval_to_deadline(1, 1000000000, ...)` at `0xffffff8000ccfa5c..68`. It tests the software byte **`this+0x269`**, then calls the command gate's timed sleep with that byte's address and the same deadline (`0xffffff8000ccfab4..fad0`). It does **not** spin on a hardware register and does not reset the deadline on every wake. Timeout path logs `WaitForBootROMReady: TIMEOUT` and returns `0xe00002d6`.

`HandleBootROMReadyGated` at **`0xffffff8000ccd6c4`** sets that byte to 1 and wakes its gate event (`0xffffff8000ccd6dc..744`). That is an incoming UART/protocol event delivered through AppleOscar's transport. The UART receive boundary is RX data `+0x24`, FIFO count `+0x18`, status `+0x10`, and IRQ142 / DMA65; the ready-wait function itself reads none of them. A constant UART “ready” bit alone cannot implement this exchange. The board implements only UART0's RX FIFO/IRQ; UART2 falls inside `a7.uart_other`'s passive register storage. It cannot generate Oscar ROM protocol traffic, download firmware, or answer subsequent commands.

The start routine searches **16 baud entries** at kernel `0xffffff8000cdcf38`:

```
1500000, 1500000, 1455000, 1410000, 1365000, 1320000, 1275000, 1230000,
1200000, 1545000, 1590000, 1635000, 1680000, 1725000, 1770000, 1800000
```

Loop `0xffffff8000cd0330..380` tries indices 0 through 15. The reset/download path calls the same ready wait at `0xffffff8000cd0a88`; reset also calls it at `0xffffff8000cd00f8`. Probe130 contains **16 timeout lines**. That is about **16 seconds of deadline waiting**, plus transport/power transitions and scheduling, distributed across boot work. It is not necessarily 16 additional seconds on the UI critical path; it can overlap InterfaceNamer's 60-second budget. No faster measured boot is claimed here.

## Implementation and checks

`omit_unpopulated_devices` operates on exact direct-child paths in `boot_image.py` after parsing the n53 firmware template. It removes the modem's two nubs together so AppleBaseband cannot be left waiting for a port deliberately omitted separately. Removing Oscar's entire subtree avoids matching gyro/accelerometer/cma to an absent CPU. Replaces the previous compass-only omission. No phandle-leading function/interrupt references to the removed nubs were found elsewhere in the supplied runtime DT. Board C comment records why UART2 has no matching processor nub.

New synthetic contract tests use the real `build_image` path, check both serialized DT and embedded RAM, preservation of controllers/siblings/properties, unchanged input bytes, missing parents, and idempotence. Existing TA comment `/* The TA command whose parameter buffer` remains in place. Full suite: **136 tests OK, 4 skipped**. `git diff --check` passes. Hook PCs in `backend/boot_wait_hooks.json` were checked against the supplied cache and existing backboardd executable bytes. Host tests verify board population, not runtime speed or helper RPC completion.

## Exact maintainer verification

Rebuilding the backend alone does **not** apply this fix: the DT is embedded in generated RAM. Keep the old plan and disks for A/B, generate a new plan with this worktree's loader. From this worktree (no commands below change guest disks):

```sh
LEGACY=/Users/bagas/Downloads/vphone-legacy
python3 scripts/qemu/boot_image.py \
  --kernel "$LEGACY/build/ios712-validated/kernel.macho" \
  --devicetree "$LEGACY/build/ios712-validated/DeviceTree.raw" \
  --monitor "$LEGACY/build/ios712-validated/monitor.macho" \
  --ans-firmware "$LEGACY/build/ios712-validated/ans-firmware.bin" \
  --boot-args '-v serial=3 debug=0x14e cpus=2' \
  --out-dir /tmp/t19-boot-plan > /tmp/t19-loader-plan.json
```

Use the original SMP plan's exact `boot_args` instead if different. Keep NVRAM/ART inputs identical when comparing; CLI above uses default NVRAM (no persistent ART). For a persisted-ART comparison use the Python `build_image(..., nvram_vars=...)` API with the same original values. The old baseline is `$LEGACY/backend/evidence/keybag-contract-20261015/boot-plan-smp/boot_plan.json`; do not overwrite it or its named RAM/ANS files.

Build the backend by your normal procedure, then use the **same built binary** for both runs. `display_probe.py` expects it in this worktree's `backend/bin/qemu-system-aarch64`. This worktree must also resolve its usual `build/ios712-validated` storage inputs (`utility.raw` in particular). Explicitly select identical diagnostic system and data sources; the probe clones disks into each output directory before running.

```sh
LEGACY=/Users/bagas/Downloads/vphone-legacy
VPHONE_TCG_THREAD=multi VPHONE_DISPLAY=cocoa \
python3 backend/display_probe.py \
  --output /tmp/t19-after --seconds 120 --stop-condition duration \
  --plan /tmp/t19-boot-plan/boot_plan.json \
  --diagnostic-system "$LEGACY/build/diagnostic/system-diagnostic-sep.raw" \
  --diagnostic-data "$LEGACY/build/diagnostic/data-diagnostic-systembag.raw" \
  --breakpoint userclient_start=0xffffff8000c5cf18 \
  --max-hits userclient_start=0 \
  --user-hooks backend/boot_wait_hooks.json --max-hits boot_wait_=32 \
  --snapshot-at 25 --snapshot-at 45 --snapshot-at 65 --snapshot-at 85
```

Repeat with output `/tmp/t19-before` and `--plan "$LEGACY/backend/evidence/keybag-contract-20261015/boot-plan-smp/boot_plan.json"`. Do not use `first-pixel`: the progress overlay is not the greeting. For the cleanest A/B, first generate a baseline plan using the pre-commit loader and otherwise identical native inputs/boot args/NVRAM; historical plans can contain other differences. Save provenance and environment. No diagnostic pokes or seeded activation state.

Inspect:

1. **Must disappear:** all `AppleOscarProcessor[...]::WaitForBootROMReady: TIMEOUT` lines (16 in probe130); `AppleBaseband::start(...): baseband`; Oscar sensor calibration/start logs. The devices are now absent, so an `Oscar started` line is not expected. If any survive, the wrong/stale RAM plan was launched.
2. **Expected improvement to verify, not a proven result:** InterfaceNamer finishes before its 60-second timeout and CommCenter's initialization is earlier; `PM notification timeout (pid ..., CommCenter)` should cease if it was downstream. PID22 is not stable. The supplied serial.log actually has no PM-notification-timeout line; that observation comes from the task description. Do not treat its absence in a new serial capture alone as a speed measurement.
3. Require all three hooks in `user-hook-mapping.json` and no 32-hit saturation. In `breakpoints.json`, filter `identity.process == 'CommCenter'`, pair `boot_wait_interfaces_begin` with `boot_wait_interfaces_done`, and subtract their `elapsed` fields. Record first `boot_wait_sb_checkin` in backboardd as received system-app check-in. It is the previously verified native `systemAppCheckIn` dismiss call at unslid exe `0x10003a1c4`, not a guessed shared-cache address.
4. Inspect `pixel-captures/*/framebuffer.ppm` and the Cocoa window for the **actual Setup Assistant greeting**. Record the earliest matching capture's `framebuffer_analysis.json` `elapsed_seconds`, and its previous capture time as the lower bound. Check-in, first nonblack pixel and greeting are different milestones. Also compare WFI/idle time over the 30–90 s interval using the same host sampling procedure.
5. The probe's `elapsed` is **host elapsed while guest is running**, excluding debugger pauses; `wall_elapsed` includes them. It is not XNU's ARM virtual counter. Compare both on the same host/settings, preferably three alternating runs, and keep debug hooks identical. For true guest-time attribution use the native counter/timebase (24 MHz) from your sampler; do not call host-running seconds guest virtual seconds.

If InterfaceNamer still consumes its budget, capture IOKit's busy registry/service stack at its timeout (configd's diagnostic dumps go to system logging), rather than shortening that timer. If the 65-second helper wait remains, include helper processes/ports in the next thread capture; current probe130 cannot identify that server-side cause. These are explicit acceptance conditions, not assumed benefits of the population change.
