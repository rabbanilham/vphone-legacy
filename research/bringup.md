# iPhone6,2 n53ap / iOS 7.1.2 11D257 bring-up

This file records measured progress and open obligations. Prepared firmware and a QEMU process are not boot success. Genuine launchd, SpringBoard, input, persistence and the 15 minute stability test have not passed.

## Firmware gate

The local IPSW SHA256 is `3d080c18cd0eeacd27070203a9fb55954331ae748518b8846c3830dac9a146e9`. The exact n53ap erase BuildIdentity selects components; encrypted KernelCache/DeviceTree digests match BuildManifest. Decrypted kernel is an ARM64 executable with LC_UNIXTHREAD entry `0xffffff8000226058`; __TEXT begins `0xffffff8000202000`. DeviceTree model is iPhone6,2 with the N53AP compatible token. Kernel and DeviceTree are preserved unchanged.

RootFS decryption produces UDIF v4. `qemu-img` conversion and APM inspection use the advertised 2048-byte logical blocks; extracting the HFSX partition yields 2185363456 bytes at offset 32768. A native readonly mount independently confirms SystemVersion 7.1.2/11D257 and the actual Apple applications/services. [Firmware content and hashes](firmware-content.md) record this inspection. The mount was detached.

The data volume is a real, blank, 1 GiB HFSX filesystem; it is not initialized iOS user data. Original fstab maps disk0s1 to / readonly and disk0s2 to /private/var writable. The private/var seed is 336768902 bytes. Do not hide it with an empty data mount and infer successful first boot. Ownership, symlinks, extended attributes and initialization must be preserved and verified.

## Loader and machine contracts

The direct-kernel loader replaces the former log-printing shim. It copies each Mach-O segment to guest DRAM, initializes BSS, derives entry from LC_UNIXTHREAD, and reserves boot arguments, DeviceTree and bootstrap tables. It does not run iBoot or fabricate kernel instructions.

Initial contracts: one CPU, TCG, 1 GiB at physical `0x800000000`; kernel virtBase `0xffffff8000000000`; physical entry `0x800226058`; x0 points to boot_args; EL1h, masked interrupts, MMU off. Kernel entry itself reads boot_args offsets 8/16/24/32, constructs translation tables and enables MMU. Those four fields have instruction evidence. The full legacy boot_args ABI, revision/video fields and DeviceTree memory-map expectations still need verification against later kernel use; similarity to newer Apple loaders does not establish compatibility.

The upstream source is QEMU v11.1.2 commit `4fc49f46dc95d4a27de2509e7fceb2931e91faeb`. The machine is project code with binary/source hashes checked at launch. Current CPU is Cortex-A57 plus narrowly modeled Apple registers, not a verified Cyclone model. UART and a simplified AIC/timer are implemented. No storage controller, touch, buttons, SEP, AGX or compositor compatibility is available. [Backend contracts](../backend/README.md) list actual limits.

REA structurally inspected the firmware Mach-O. Its native analysis provider was unavailable (Ghidra was not configured and Hopper/IDA were absent), so further instruction inspection used local Capstone. No decompiler results or runtime capabilities were invented.

## Actual kernel execution

First bounded project-launcher probe: `build/ios712-validated/logs/20261012-192111-6f891676/`.

The CPU began at the genuine kernel entry and reached the kernel's exception vector after bootstrap/MMU. Initial blocker was an undefined MRS at physical `0x800226278`, register S3_0_C15_C8_0. The subsequent kernel instructions OR `0xf0`, OR `0xf00`, then write the register. Apple XNU defines this as HID8 with matching DataSetID masks. The backend now implements read/write storage for that exact register, with explicit hardware-effect limitations. No wildcard system-register emulation or guest patch was used. [HID8 evidence](../backend/BRINGUP.md) includes the source reference and before/after behavior.

After rebuilding, actual kernel probes reached C code with this first remaining failure:

```text
Data Abort ESR=0x96000004
ELR=0xffffff80003624d0
FAR=0xffffff7800000000
```

The instruction is a pmap table lookup. FAR equals virtBase minus physBase; a zero selected table descriptor is a current inference, awaiting a breakpoint and table-memory proof. Later aborts and a fixed PC occur. UART serial remains zero bytes. Evidence: `backend/evidence/ios712-hid8/` and project launch trace `build/ios712-validated/logs/20261012-192751-fbcece3a/`. Neither trace proves mounted storage or a running userspace.

This initial fault was subsequently resolved with debugger evidence below. No arbitrary RAM mapping or skipped lookup was used.

## Verified memory reservations and current barrier

The pmap breakpoint captured a requested range ending 16KiB past the advertised 1GiB limit, crossing into an empty L1 descriptor. Only changing boot_args.memSize from `0x40000000` to `0x3fc00000` eliminated this fault and produced real UART panic output. The final 4MiB already held the diagnostic framebuffer and must not also be advertised to the kernel allocator. Physical machine RAM remains 1GiB; memSizeActual retains that physical size.

Next, arm_init found the original DeviceTree template's `pram.reg=<0,0>`, mapped a zero-size panic/trace buffer and dereferenced the unmapped VA. Breakpoints and the pram/reg lookup identify this contract. Filling it with a real reserved 16KiB at `0x83fbfc000`, and reducing usable memSize to `0x3fbfc000`, removes that fault. PRAM is zeroed volatile RAM, not a claim of persistent secure storage. Loader changes are runtime DeviceTree/boot argument derivations; the original kernel/DT remain unchanged.

Evidence: `backend/evidence/pmap-before-fault/`, `pmap-zero-table/`, `memsize-framebuffer-carveout/` and `pram-carveout/`. The lookup and successful PRAM access also establish the operational DeviceTree pointer/length fields at boot_args offsets 96/104. Full video ABI/compositor behavior remains unverified.

The next historical barrier was SMC #0x11 at `0xffffff8000229198`, opcode `0xd4000223`. A debugger breakpoint captured service x0=`0x800`, x1=`0x800224000`, x2=x3=0, caller `0xffffff8000361eb0`. This is the monitor kernel-entry registration call, not a no-op or the later lockdown service. The initial direct-EL1 machine raised a real undefined-instruction panic. The native monitor handoff below supersedes this barrier.

Historical failure evidence: `build/ios712-validated/logs/20261012-193633-b93180d5/`. Real UART identifies RELEASE_ARM64_S5L8960X, XNU 2423.10.71~1; a register/trace chain proves execution independently of that identity. Runtime kernel/process state was failed, launcher exit 1, and registers/framebuffer captured before quitting.

## Current native monitor/kernel handoff

The monitor is embedded immediately after the compressed kernel stream. The extractor now validates both Mach-O executables and rejects absent/truncated/corrupt monitor data. Monitor is 81920 bytes, SHA256 `1b2eade4ae032f92c79798749db39b9d279f3cf521c660b3de228a9277b81aa4`. Originals remain immutable. [Recovered boot firmware](secure-monitor-inventory.md) proves the six-u64 version-1 ABI and exact native partitions from iBoot instructions and original DeviceTree defaults.

Production now loads native monitor at PA `0x800600000`, size 2MiB, with linked VA `0x4100000000` and physical entry `0x80060c120`. Its arguments reside at `0x8007fffd0`. The initial 6MiB is a SEP reservation, without a running SEP implementation. Native kernel physical base is `0x800800000`. The machine enables EL2/EL3 and starts in EL3h. Trace captures original firmware ERET into XNU and the real SMC #0x11 EL1→EL3→EL1 roundtrip. No synthesized monitor code, forced SMC return or NOP patch is used.

Original `stram.reg=<0,0>` caused a zero-size sleep-token mapping whose VA was reused by UART. An eight-byte RUNNMOSX copy then aborted on UART. This was corrected by filling the actual sleep-token RAM range, rather than relaxing UART writes. [Native reservation evidence](native-memory-reservations.md) gives PRAM `0x83f7f7000/0x4000`, STRAM `0x83f7fb000/0x1000`, ANS firmware RAM `0x83f800000/0x800000`; the unexplained 16KiB gap is retained. Selector 3 was initially mislabeled framebuffer; actual ANS property writes recovered on October 13 correct that inference. Diagnostic scanout is now a separate lower loader allocation. PRAM overlaps the last 16KiB of the advertised usable span; it is separately named in runtime memory-map, and allocator protection still requires verification.

The zero timebase template stalled timer initialization. Filling CPU timebase-frequency with the backend's 24MHz counter produces actual FIQ entry/return and kernel thread scheduling. A later kext header fault was a false -2MiB kernel slide: the loader had rounded virtBase to 1GiB. The correct linked base `__TEXT.vmaddr−0x2000=0xffffff8000200000` gives kernel entry PA `0x800826058`. Segment, boot argument and DeviceTree physical pointers were rebuilt against it. Real kext Mach-O headers are now parsed at their actual addresses.

Historical production 30-second probe: `build/ios712-validated/logs/20261012-200452-0d0fa92b/`. It runs kernel threads and timer FIQs without panic. Two handled write-permission VM faults resume successfully; these are not unsupported-MMIO failures. Serial contains only the genuine iBoot-version line; no iBoot execution/version was fabricated. Later bring-up advances beyond this earlier wait and exposes a real AppleCSI(ANS) startup panic; see [October 13 continuation](resume-20261013.md). Filesystem mount, launchd, graphics and input remain unverified.

## Runtime and manager verification

- Legacy bundle validation fails before QEMU; no fallback to Homebrew virt/A57 or log shim.
- Real launcher integration passes: paused project QEMU, second launch rejected, unsupported graceful shutdown rejected, force request consumed by the owning QMP connection, child reaped, pid cleared and unclean status retained.
- Combined regression suite: 30 tests pass, including actual kernel/monitor/DeviceTree extraction, native segment/boot-argument placement, failure handling and real QEMU lifecycle integration.
- Backend smoke test executes a test-only guest program: UART MMIO produces bytes, guest RAM writes produce an exact red pixel in QMP scanout, pause/quit work, invalid CPU/RAM/missing image fail.
- Swift GUI builds and signs. It has no fake phone applications, log-based readiness, success delay, or hardcoded shell results. Runtime stages remain unverified without evidence. The guest agent is unavailable by default.

Exit code zero from a bounded probe means the probe/QEMU stopped successfully. It never certifies XNU or SpringBoard boot. Forced stops are recorded unclean. An exact XNU panic header on guest UART is treated as failure, not readiness; launcher exits nonzero even if QMP quit makes QEMU itself exit zero. At 16MiB, verbose interrupt tracing is disabled while device-error tracing continues: normal FIQs/page faults must not be mislabeled as a failing exception loop. No Darwin/SpringBoard string promotes a boot stage.

## Remaining acceptance obligations

1. Identify the remaining bootstrap wait and continue through each observed CPU/device failure. Native monitor service 0x800 now executes its original firmware handler.
2. Obtain stable serial boot beyond panic; implement guest-compatible storage and verified fstab mapping; provision data with original seed/ownership; reach /sbin/launchd.
3. Observe service failures before patching watchdog, keybag, SEP or activation. No blanket daemon removal. A no-passcode, offline first-boot profile remains unimplemented.
4. Trace actual backboardd/QuartzCore/IOMobileGraphicsFamily/AppleH6PDisplayPipe/AGX contracts. Guest software rendering is not proven available. Diagnostic scanout alone cannot satisfy this stage.
5. Implement guest touch down/move/up and Home/Power events. Verify input reaches backboardd. Type using the actual iOS keyboard.
6. Record three cold boots, Apple app interaction, Notes/settings persistence after clean shutdown, guest pause freezing framebuffer, and a 15 minute session without crashes/restart loops.

Primary references: [QEMU virt platform](https://www.qemu.org/docs/master/system/arm/virt.html), [Linux A7 DeviceTree](https://github.com/torvalds/linux/blob/master/arch/arm64/boot/dts/apple/s5l8960x.dtsi), [qemu-t8030 bring-up](https://github.com/TrungNguyen1909/qemu-t8030/wiki/Bringing-up-the-emulator). qemu-t8030 targets another SoC/OS; it is a reference, not evidence of A7 support.
