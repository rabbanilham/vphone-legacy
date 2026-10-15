# Real iOS 7.1.2 bring-up observations

These observations describe kernel instruction execution, not a completed boot.

## HID8 configuration

The first real-kernel trace stopped at physical `0x800226278`:

```
mrs x12, S3_0_C15_C8_0
orr x12, x12, #0xf0
orr x12, x12, #0xf00
msr S3_0_C15_C8_0, x12
```

Apple's [published XNU header, mirrored here](https://github.com/opensource-apple/xnu/blob/master/pexpert/pexpert/arm64/arm64_common.h)
names this register HID8 and defines DataSetID0/1 with these exact masks. The
experimental machine now stores and reads back this configuration, consistent
with its existing HID treatment. TCG has no physical cache datasets; no cache
timing/ordering/hardware behavior is asserted. Only HID8 was added, with no
wildcard system-register catch-all. The synthetic smoke test verifies that
read/modify/write/read returns `0xff0`.

## Next observed failure

A bounded 0.25 second probe of the original kernel reached C kernel code after
the bootstrap/MMU sequence. Its first remaining exception was:

```
Data Abort: ESR=0x96000004
ELR=0xffffff80003624d0
FAR=0xffffff7800000000
```

At that location, Capstone decodes `ldr x14, [x13, x12, lsl #3]`. The preceding
code loads a first-level table descriptor, masks its physical address, and adds
`gVirtBase-gPhysBase`. The fault address equals that difference from the current
boot arguments, so a selected descriptor apparently had physical address zero.
This inference needs breakpoint/table-memory confirmation; it is not proof of
a firmware patch requirement or a peripheral model defect. No fabricated RAM
mapping or read-as-zero device was added to conceal it.

The next exception references VA `0xffffff80003604f8`, FAR `0x18`. Serial output
remains empty. No filesystem mount, launchd, display compositor, or SpringBoard
has been observed. `backend.json` retains all iOS/userspace/storage/graphics/input
completion capabilities as false.

Local evidence is in `backend/evidence/ios712-hid8/`: exact command, before/after
register dumps, serial, exception/MMIO trace, stderr, and a QMP framebuffer
capture. Regenerate with `backend/probe.py`; it stops the guest and exits QEMU
after a bounded interval. Firmware, boot image, and bundle manifests are not
modified by the probe.

## Debugger-confirmed loader memory requirements

A GDB breakpoint at `0xffffff80003624d0` confirms the failing range is
`0xffffff8040000000..0xffffff8040004000`: the first 16 KiB beyond the advertised
1 GiB virtual DRAM range. Physical L1 table `0x8012e5000` has a valid first
descriptor `0x8012e6003` and a zero second descriptor. The actual legacy kernel
caller at `0xffffff800021c4f0` begins protection at `phystokv(topOfKernelData)+4
pages`, then computes the size as `static_memory_end-phystokv(topOfKernelData)`.
It consequently extends its range by four pages beyond advertised memory.

The loader already reserves the final 4 MiB for video. Advertising that memory
as ordinary usable RAM was inconsistent with the carveout. An experimental
image changing only `boot_args.memSize` from `0x40000000` to `0x3fc00000`
eliminates the zero-L1-descriptor fault. The machine still exposes 1 GiB of real
physical RAM. This probe produces actual UART panic output identifying
`xnu-2423.10.71~1/RELEASE_ARM64_S5L8960X`; it is not a success log.

Evidence: `evidence/pmap-zero-table/` and
`evidence/memsize-framebuffer-carveout/run/`.

## Populate the PRAM bootloader template

The next read fault is caused by DeviceTree `pram.reg`: both the original
firmware template and runtime tree contain two zero u64 values. The kernel
looks up `pram`, reads `reg`, calls `ml_io_map`, then reads the returned address
at `0xffffff800021cdc0`. A zero-size mapping has no backing pages and faults.
The exact kernel string anchors are `pram` at `0xffffff80005ebe75` and `reg` at
`0xffffff80005ebe0e`. This also demonstrates that the runtime DeviceTree pointer
and size ABI are operational; it says nothing about compositor/video support.

A second experimental image reserves 16 KiB immediately below the framebuffer:
`pram.reg = <u64 0x83fbfc000, u64 0x4000>`, usable RAM `0x3fbfc000`, physical RAM
still 1 GiB. Its PRAM reads succeed and the translation fault disappears.
PRAM must be excluded from the allocator's usable RAM and described as a real
reserved region; do not retain a zero-length template. Evidence:
`evidence/pram-carveout/` and `evidence/dynamic-before-fault/`.

## Current remaining barrier: secure monitor service 0x800

With the memory/PRAM handoff corrected, the first exception is SMC `#0x11` at
`0xffffff8000229198` (instruction `d4000223`). The machine deliberately disables
EL3, so this produces an actual undefined-instruction panic over UART.
The [published Apple wrapper](https://github.com/opensource-apple/xnu/blob/master/osfmk/arm64/machine_routines_asm.s)
defines SMC `#0x11` as the C-ABI `monitor_call` interface. A breakpoint observes
`x0=0x800`, `x1=0x800224000`, `x2=x3=0`, with caller `0xffffff8000361eb0`.
This service receives a physical kernel code/vector address. Its exact secure
monitor semantics are unproven; it must not be replaced by a generic successful
return or confused with the later monitor lockdown service.

Evidence: `evidence/monitor-service-800/`. No kernel bytes were patched in these
experiments. Experimental images reside only in ignored backend evidence
directories. A bootable userspace and iOS display remain unverified.

## Recovered native secure monitor and verified handoff

The exact decrypted n53ap kernel IM4P has a second ARM64 Mach-O immediately
after its `complzss` stream, at payload offset `0x7683bb`. It is the native
monitor, not part of the decompressed kernel. Its linked base is
`0x4100000000`, entry `0x410000c120`, and segment/BSS extent `0x21000`.
The monitor's SMC `#0x11` dispatch table entry at `0x410000e158` points to
`0x410000a6c4`, which handles service `0x800` by storing the kernel reset entry.
No synthetic SMC handler, forced return, or kernel patch was needed.

Recovered iBoot partition logic and DeviceTree defaults establish:

| Region | Physical base | Size |
| --- | --- | --- |
| SEP reservation | `0x800000000` | `0x600000` |
| Native monitor | `0x800600000` | `0x200000` |
| Kernel physical mapping base | `0x800800000` | `0x3effb000` usable |

The monitor arguments at `0x8007fffd0` contain six little-endian u64 fields:
version `1`, virtual base `0x4100000000`, physical base `0x800600000`, size
`0x200000`, physical kernel boot-arguments pointer, and physical kernel entry.
Using the entire DRAM size as the monitor partition size corrupts kernel
memory; it is not a permissible fallback. `boot-el=3` enters the actual monitor
in EL3h with EL2/EL3 enabled. A bounded probe demonstrates firmware `ERET` into
XNU, kernel SMC entry into native EL3, and return to the next kernel instruction.
Evidence: `evidence/native-monitor/run-native-layout/`.

## Further handoff templates and zero KASLR slide

The next fault was an eight-byte physical copy of `RUNNMOSX` into an address
that translated to UART0. A write watchpoint proves the kernel initializes its
sleep-token pointer by looking up DeviceTree `stram.reg` at
`0xffffff800021cbe4..0xffffff800021cc2c`. Like PRAM, the original firmware tree
contains a zero address/size bootloader template. A zero-size mapping is later
reused for UART, which caused the external abort. A reserved-RAM `stram.reg`
eliminates this fault; extending UART access sizes would conceal the wrong
memory handoff. iBoot investigation subsequently recovered the exact native
regions: PRAM `0x83f7f7000/0x4000`, STRAM `0x83f7fb000/0x1000`, ANS firmware RAM
`0x83f800000/0x800000`. Selector 3 was initially mislabeled framebuffer;
iBoot's actual ANS property writes correct that inference. STRAM and ANS lie outside the advertised
kernel usable range, whose end is `0x83f7fb000`. PRAM occupies the last
16 KiB of that advertised range; honoring its separate `PanicRAM`
DeviceTree memory-map reservation requires runtime verification. Its
bootloader description alone is not proof that the kernel allocator excludes it.

The original `cpu0.timebase-frequency` is also a zero bootloader template.
This causes an infinite shift loop in timer initialization at
`0xffffff80003645c0`. Populating the eight-byte property with `24000000`,
consistent with the backend's architected counter, resolves the loop and
produces actual timer FIQ entry/return and kernel thread scheduling.

The initial loader's 1-GiB-aligned virtual base caused a kernel slide of
`-0x200000`. A real kext Mach-O header at VA `0xffffff8000717000` was consequently
parsed at `0xffffff8000517000`, which contains kernel instructions. Treating
those instructions as load-command sizes caused a real data abort. The
zero-slide mapping base for this kernel is `__TEXT.vmaddr - 0x2000`, namely
`0xffffff8000200000`. Mapping the original segments against this base yields
kernel entry PA `0x800826058`; rebuilding all physical pointers and runtime
DeviceTree memory-map properties resolves that malformed-header fault.

A bounded one-second native-monitor probe with these handoff fixes has no
data abort/panic and executes kernel threads and architected timer FIQs.
Serial has only the genuine `iBoot version:` message. This is still not a
filesystem mount, launchd execution, usable iOS, or compositor proof.
Evidence: `evidence/native-monitor/run-native-stram/`,
`run-native-timebase/`, and `run-native-virtbase/`.
