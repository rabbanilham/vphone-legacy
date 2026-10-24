# T20: IOKit quiet attribution (incomplete hardware fix)

2026-10-22, branch `wt/iokit-quiet`, worktree `${WORKTREES_ROOT}/vphone-wt-iokit-quiet`.

**Result: the requested early-quiet fix is NOT established.** No device-model change is justified as a fix by the supplied evidence. This commit adds the missing read-only IOService busy-tree/kernel-task capture and records the static audit, so the next permitted boot can identify the actual owner. It does not claim that InterfaceNamer now publishes `*QUIET*`, or that CommCenter/SpringBoard start earlier. No backend build, emulator run, guest modification, disk attachment, or disk write was performed.

## Evidence and limitations

Read T19 and CLAUDE_HANDOVER section 20. Inputs are the specified `scratchpad/probe133/{ca-thread-state-045s.json,ca-thread-state.json,serial.log}`, validated KernelMemory.bin and DeviceTree.runtime, shared cache, and the existing read-only research copies `/tmp/t19/configd` and `/tmp/t19/CommCenter`. The B2-iokit-quiet.md and B3-commcenter-helper.md files were absent when checked, including at completion; no conclusions are attributed to them.

**VERIFIED:** probe133's 45-second JSON contains lockdownd, installd, SpringBoard, configd, backboardd, CommCenter and lsd, but no kernel task and no registry snapshot. It therefore cannot identify which kernel IOService remains busy. User task kernel frames, if present, are not the missing kernel matching-worker stacks. A static unbounded wait does not establish a live wait, and a live wait does not by itself establish an outstanding IOService busy reference.

Address convention: `fileoff = kernel_VA - 0xffffff8000a00000 + 0x1000000`. Capstone was used from the specified legacy venv. KernelMemory SHA256 is `bba9a4bace9b505b80c05bc15648b46122c8948f86ac4f0d0625f7804d78d7d8`; runtime DT SHA256 is `b75e0054fb52aedc461ecaf88475e93f5b9f6d370c0e0cc040273b4b224d810b`. Cache addresses below are unslid. Reconstructed the image list from header +24 and its 32-byte `<QQQII>` records, using `/tmp/t4-touch-evidence/cache.py` and `/tmp/t19/cachex.py`, not the incomplete index image list.

## InterfaceNamer's actual quiet contract

**VERIFIED (configd binary):** this build uses asynchronous registry busy notifications, not a thread sleeping in IOKitWaitQuiet:

- Executable `0x1000153c0..3cc` gets `IOService:/` (`0x100031168`) with IORegistryEntryFromPath.
- `0x1000153dc..3fc` installs IOServiceAddInterestNotification with `IOBusyInterest` (`0x1000311a3`) and callback `0x1000157d8`.
- `0x100015520..528` queries IOServiceGetBusyState. The quiet timer is created at `0x100015540..570` using the default 60-second value at `0x10003aaa0`. The separate stack budget is 300 seconds (T19).
- Callback `0x1000157f8` rejects a nonzero x3 (busy argument). `0x1000157fc..808` checks message `0xe0000120` (service busy-state change). The zero-busy path `0x10001580c..820` publishes `*QUIET*`. Timer callback `0x1000158b8`, publication at `0x1000158e4..8f0`, instead publishes `*TIMEOUT*`.
- CommCenter TID463 at 45 seconds has return PCs `0x18d320794`, `0x18d34e144`, `0x18d3498cc`; subtract slide `0x8d30000` to obtain `0x1845f0794`, `0x18461e144`, `0x1846198cc`. These are the SCDynamicStoreNotifyWait / InterfaceNamer initializer / pthread_once chain from T19. The dynamic-store receive has no private deadline.

**VERIFIED (kernel):** IOService::getBusyState at `0xffffff80002ae23c` reads the byte at service+0x4c. `_adjustBusy` at `0xffffff80002a919c` updates this count (`0x2a9270..27c`), emits busy-interest notifications (`0x2a9310..330`), and propagates transitions to the service parent (`0x2a9334..350`). Thus an ancestor's busy count is not a count of independently hung drivers, and its presence does not identify its own start method as stuck. IOService::waitQuiet at `0xffffff80002ae244` calls its wait implementation `0xffffff800056d194`; that implementation tests the same low byte at `0x56d210..218`, sets the waiter flag, and sleeps on the service, indefinitely for timeout -1 or with a computed deadline otherwise (`0x56d234..2c4`). Completing real matching/start work is the relevant contract; fabricating a quiet notification or clearing the count would conceal it.

## Modem/HSIC: verified dependency, unverified busy owner

**VERIFIED:** AppleBasebandN92::findHSICPort starts at `0xffffff8000f0eaf8`. `0xf0eb3c..48` builds the `hsic-baseband` name match (`0xffffff8000f10c12`), then `0xf0eb4c..54` passes timeout -1 to stub `0xffffff8000f10388`, resolving to IOService::waitForMatchingService at `0xffffff80002ae764`. Its no-deadline sleep is `0x2ae7e0..808`.

**VERIFIED:** N41's port-enable path `0xffffff8000f0fc50` calls the HSIC-enable routine `0xffffff8000f0e99c` at `0xf0fc64`, which calls findHSICPort at `0xf0e9b8`. Other direct callers are `0xf0ecd0` and `0xf0f39c`. This is a real missing service dependency.

**VERIFIED:** the baseband start implementations are `0xf0fb74` (N41), `0xf0f198` (N94), `0xf0e5c8` (J2), `0xf0d9c0` (N92), and `0xf0b644` (base). Their explicit superclass/start-body chain does not call findHSICPort; J2 initializes its cached port slot to NULL at `0xf0e64c`. N41 installs the port-enable callback via vtable slot 0x620 at `0xf0fb98..ba0`. This disproves treating the findHSICPort address alone as evidence of a driver start blocked there. Platform-function creation and subsequent asynchronous callbacks can still wait elsewhere; the audit is not a proof that all baseband activity is quiet.

**VERIFIED:** probe133 logs `AppleS5L8960XUSBPhy::start: hsic disabled`. AppleUSBPhy::start `0xffffff8000e5f208` checks the `hsic` boot argument (default 1) and `hsic-enabled` registry property (`0xe5f318..36c`); its disabled log path is `0xe5f374..3a4`. The supplied `/arm-io/otgphyctrl` has no `hsic-enabled` property. The board has no EHCI/HSIC model. Adding an EHCI MMIO region alone does not satisfy this earlier publication prerequisite.

**INFERRED:** an HSIC port model plus the correct native provider publication could resolve the later modem power/port request. **NOT VERIFIED:** that request is currently a registry-busy owner at 45 seconds. Changing modem presence or deleting its DT nodes is excluded: the prior missing-node boot restarted CommCenter. This commit preserves both modem nodes and the existing no-Oscar plan.

## Other waits audited

**VERIFIED static matching-service call sites** (not a list of measured busy owners):

| Call site | Dependency / relevant current evidence |
|---|---|
| `0xffffff8000e5f4a4` | AppleUSBPhy::start waits indefinitely for AppleEmbeddedUSBArbitrator, using phy-id matching. This is a direct start-path candidate. |
| `0xffffff8000e60254`, `0xffffff8000e60380` | USB helper paths wait for AppleUSBPhy / AppleEmbeddedUSBArbitrator with phy-id. Native provider ordering must be captured, not guessed from hsic-disabled alone. |
| `0xffffff8000e9ca0c` | TriStar built-in start constructs matching from provider's `dock` property and waits indefinitely. DT tristar points to dock phandle 0x77. |
| `0xffffff8000bc048c` | AppleH6CamIn::start waits for IOCoreSurfaceRoot. Serial later says `About to register service`; subsequent ISP firmware loading fails allocation with 0xe00002bc, rather than evidence of an unbounded firmware wait. |
| `0xffffff8000ec8238`, `0xffffff8000ec82bc` | MemCacheController start obtains arm-io/pram dependencies; serial explicitly reaches `start: finished`. |
| `0xffffff8000d77974`, `0xffffff8000dabf2c` | Biometric/keystore dependencies on AppleSEPManager. Serial shows SEP registered, OS-active and ART requests; no measured stuck start in these paths. |
| `0xffffff8001132680` | Audio dependency path involving `i2s-multi` / `i2s-slave`. No kernel stack or busy-reference evidence identifies its live state. |
| `0xffffff80011c3c3c` | VXE start waits for IOCoreSurfaceRoot. This cannot be ruled in/out by the user snapshots. |
| `0xffffff80010418c4`, `0xffffff80010452bc` | Baseband USB composite/PDP paths wait for baseband / IONetworkStack. The HSIC USB device must exist before these driver paths can match. |

**VERIFIED:** serial has `AppleCBTL1610::start: could not read REV` and `AppleTriStar2::start: could not initialize AppleCBTL1610`. The REV read is register 0x0f, one byte, at `0xffffff8000e9e8c8..8e4`; failure returns false at `0xe9e914`. I2C0 tristar is address 0x1a and the board NACKs it. This is another concrete absent peripheral, but a failed start normally unwinds its matching work. A fabricated revision byte is not proof of a complete accessory-controller model or of removal of the busy owner.

**VERIFIED:** VXD's serial `Loading META FW...` is followed by `META is alive!`; do not classify that log prefix alone as a stuck firmware start. Oscar's 16 UART ROM waits are absent from this plan by design. None of these observations rules out outstanding matching, PM or interrupt work in another service.

## Implemented diagnostic and tests

`backend/iokit_probe_state.py` walks the IOService-plane links and records all nodes, raw vtables, recognized native metaclass names, busy counts and busy children. It checks four native instruction fingerprints before reading registry offsets, uses bounded reads and traversal, detects corrupt counts and kernel thread queue cycles, and reports incomplete data explicitly. An unknown metaclass getter leaves class unknown. It never invokes guest code, edits registry objects, clears busy state, or injects events.

Offsets verified against the supplied kernel:

- IORegistryEntry::getRegistryRoot `0xffffff80002a31d8`: root pointer global `0xffffff800067d090`. The root object is allocated with size 0x28 at `0x2a30c0`; it is not safe to read root+0x4c as an IOService busy count.
- getChildSetReference `0xffffff80002a3b64`: registry dictionary at entry+0x18; plane child key at plane+0x20. gIOServicePlane is `0xffffff8000694668`. The link values are OSArrays: makeLink calls OSArray::withObjects at `0xffffff80002a3a30`. OSArray's count/storage are +0x20/+0x18 (`getObject`, `0xffffff800028c6d8`); no OSSet indirection is used.
- getName `0xffffff80002a4ba8`: plane name key +0x28, generic name-key global `0xffffff80006944f0`; fallback obtains metaClass via vtable+0x38 and its name at meta+0x18. OSString char pointer +0x18 is verified at `0xffffff8000294ea0`.
- OSDictionary count/storage +0x20/+0x18, 16-byte key/value pairs, verified at `0xffffff800028efc0`, `0xffffff800028f578`.

The accompanying bounded kernel-task thread walker uses global `_kernel_task=0xffffff8000694008` and the existing ca_probe_state task/thread offsets. **INFERRED/needs live validation:** saved-context +0x680 and its fp/lr/sp offsets are inherited from that tool, not newly established by a kernel stack capture. Output marks this limitation. Running-thread saved contexts may be stale; the new registry busy bytes are independent of them. Do not treat an empty frame list as proof that no thread is blocked.

Every existing CA snapshot now also saves `iokit-state-045s.json` (or `iokit-state.json` for the final capture), while the probe already has all vCPUs stopped. No hot global breakpoint is added. Synthetic tests cover busy ancestors/leaves and idle siblings, missing memory vs zero, traversal/count limits, class getter decoding, kernel thread link handling and malformed frame chains. Full suite: **142 tests OK, 4 skipped**. `/* The TA command whose parameter buffer` remains in backend/s5l8960x.c. No runtime early-quiet claim follows from these host tests.

## Maintainer verification / next required evidence

Run the existing no-Oscar SMP plan with these probe sources and the usual built backend. No new RAM plan or backend rebuild is needed for this diagnostic-only commit. This command runs cloned diagnostic disks through display_probe; do not point QEMU at the source disks directly.

```sh
VPHONE_TCG_THREAD=multi VPHONE_DISPLAY=cocoa \
VPHONE_SNAPSHOT_PROCS=CommCenter,configd,lockdownd,CommCenterRootHe,CommCenterMobile \
python3 backend/display_probe.py \
  --plan backend/evidence/keybag-contract-20261015/boot-plan-smp-nooscar/boot_plan.json \
  --output /tmp/t20-iokit --seconds 100 --stop-condition duration \
  --diagnostic-system ${REPO_ROOT}/build/diagnostic/system-diagnostic-sep.raw \
  --diagnostic-data ${REPO_ROOT}/build/diagnostic/data-diagnostic-systembag.raw \
  --breakpoint userclient_start=0xffffff8000c5cf18 --max-hits userclient_start=0 \
  --user-hooks backend/boot_wait_hooks.json --max-hits boot_wait_=32 \
  --snapshot-at 45
```

The helper names are truncated p_comm selectors; inspect process_inventory for actual names before relying on their capture. The new registry capture does not depend on selecting those processes.

Inspect `iokit-state-045s.json`: require `complete=true`, find the deepest busy services, their full IOService path/class and all busy ancestors. Symbolicate kernel saved frames against KernelMemory.bin and correlate matching/PM work with each service. If no busy count remains at 45 s yet CommCenter still awaits InterfaceNamer, capture configd's notification/timer state; that observation would invalidate the assumption of a continuously busy registry. An incomplete capture must be repaired before choosing hardware based on its absent entries.

Acceptance for a subsequent hardware fix remains: InterfaceNamer publishes **QUIET before its timer**, CommCenter has passed its InterfaceNamer initializer at 45 s, and SpringBoard/lockdownd are no longer in the downstream CoreTelephony sync XPC wait. Pair boot_wait_interfaces_begin/done for CommCenter, require hooks unsaturated, and distinguish early completion via QUIET from completion via TIMEOUT. A later helper wait is a separate dependency. Compare identical plan/disks and host settings; probe elapsed excludes debugger pauses and is not ARM virtual-counter time.

**Outstanding:** identify the actual busy owner dynamically, then implement and host-test its hardware contract. The supplied artifacts cannot establish that identity, and the explicit no-build/no-run constraint prevents collecting it here. This report and diagnostic are preparatory work, not completion of the requested emulator fix.
