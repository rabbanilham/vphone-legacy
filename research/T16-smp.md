# T16: A7 application-processor SMP

Worktree: `/Users/bagas/Downloads/vphone-wt-smp`, branch `wt/smp`.
Research inputs were read only. QEMU was neither built nor run by this agent.

## Implemented

- Machine maximum is two application processors. `-smp 2` creates two
  Cortex-A57s with affinities 0 and 1 and the same EL/clock/Apple-c15 setup.
  The default remains one CPU. CPU1 starts powered off on every machine reset.
- Apple c15 storage and physical/virtual timer levels are per CPU. Timer
  outputs are ORed into the corresponding CPU's FIQ, independently.
- AIC current-CPU window at +0x2000 and explicit CPU windows at
  +0x5000/+0x5080. IPI pending and auto-mask/ACK state are per CPU. SEND low
  bits select destination CPUs (OTHER event 1); bit 31 selects SELF (event 2).
  ACK clears pending; mask-clear reenables delivery. IAR masks the event but
  does not silently acknowledge its pending bit.
- Shared hardware/software IRQs retain shared source masks, with target bits
  selecting the CPU that sees IRQ/IAR. IAR auto-masks a shared source before
  updating both output lines, so a second core cannot acknowledge it again.
  Single-CPU IRQ selection remains the old behavior, ignoring target bits.
  With two CPUs, target zero defaults to CPU0 for compatibility.
- PMGR +0x30004 remains stored preparation state; a 32-bit write with bit 1 to
  +0x30008 releases CPU1 once. EL1 fixtures enter `boot-entry`, x0=0. Native
  EL3 release enters the monitor allocation's warm-reset trampoline, x0=0.
  CPU1's QEMU RVBAR property describes that reset address. The monitor
  determines the eventual kernel entry; no synthetic kernel-entry patching
  or guest-code replacement is involved.
- `/arm-io` CPU error MMIO resources `0x202050000` and `0x202150000`, 0x1000
  bytes each, now provide separate reset-cleared storage. Hardware error
  injection, syndrome production and physical cache effects are not modeled.
- The optional ANS diagnostic Cortex-A7 moves from affinity 1 to 0x100 to
  avoid colliding with application CPU1, including its power-control lookup.
- Loader accepts one `cpus=1` or `cpus=2`, rejects duplicate/conflicting or
  unsupported limits, preserves both DT CPU IDs/functions, and records
  `cpu_count` in the plan. Runtime uses this count for `-smp` and uses
  `tcg,thread=single` for two CPUs. Existing unspecified/default limits stay 1.
- Added actual-C host register checks and a user-run bare-metal two-CPU
  contract. Updated the old smoke test's unsupported CPU count from 2 to 3.
  The AGX extraction comment `/* The TA command whose parameter buffer` is
  retained verbatim.

## VERIFIED: static kernel / monitor evidence

Kernel input:
`/Users/bagas/Downloads/vphone-legacy/build/ios712-validated/boot/KernelMemory.bin`.
Kernel disassembly used the supplied relation
`offset = VA - 0xffffff8000a00000 + 0x1000000`, with Capstone from the legacy
venv. Monitor VA 0x4100000000 corresponds to file offset 0x600000.

- `0xffffff8000ecb890–0xffffff8000ecb8c8`: recognizes `Core` (0x436f7265)
  and constructs its platform-function object. The DT CPU0/CPU1
  `function-enable_core` arguments are bitmasks 1 and 2, provider phandle
  0x11 (`pmgr`).
- `0xffffff8000ece0bc–0xffffff8000ece0d0`: loads the function's core mask,
  derives the boolean enable parameter, and calls 0xffffff8000ecca38.
- `0xffffff8000eccacc–0xffffff8000eccaf4`: enable writes that mask to
  PMGR +0x30004 (`str` at 0xffffff8000eccad8), followed by +0x30008
  (`str` at 0xffffff8000eccaf4). The disable path instead writes +0x30000.
  This change implements initial release, not CPU hot-unplug/suspend.
- Monitor `0x410000c194–0x410000c1b8`: writes its physical entry to the
  trampoline literal and copies the trampoline to the allocation base.
  `0x410000c2e0–0x410000c2e8`: trampoline loads that entry, clears x0, branches.
  `0x410000c154`/`0x410000c1d4`: x0=0 skips the primary argument-copy path.
  `0x410000c24c–0x410000c250`: MPIDR affinity 0 selects the core index.
  `0x410000c2bc–0x410000c2dc`: secondary path selects per-core monitor state,
  calls the monitor continuation and returns through ELR_EL3/ERET.
- `0xffffff80010e6728–0xffffff80010e6730`: cross-IPI SEND at AIC +0x2008
  with a destination bit derived at 0xffffff80010e6710–0xffffff80010e6724.
  `0xffffff80010e67c4–0xffffff80010e67d0`: self-IPI SEND uses bit 31.
- `0xffffff80010e6928`, `0xffffff80010e6964–0xffffff80010e696c`: IPI ACK
  at +0x200c. `0xffffff80010e69c0–0xffffff80010e69e4`: reads current CPU ID
  at +0x2000, maps the event to that CPU's two DT interrupt IDs, and selects
  OTHER bit 0 versus SELF bit 31. Thus CPU ID must follow the executing CPU.
- `0xffffff80010e65e4–0xffffff80010e6614`: restores shared IRQ target words
  starting at +0x3000. `0xffffff80010e6628–0xffffff80010e667c`: restores
  shared source masks with +0x4100/+0x4180. These are not private CPU mask
  banks; only local IPI mask/pending/IAR selection needs CPU-local state.
- Runtime DT `/cpus/cpu0` and `/cpus/cpu1` have little-endian reg values 0
  and 1, states running/off, and valid provider references. Their IPI IDs
  are 192/193 and 194/195. `function-error_handler` uses provider phandle
  0x0e (`arm-io`) with core arguments 0/1. `/arm-io` reg[1]/reg[2] are
  0x202050000/0x202150000, length 0x1000 each.
  `0xffffff8000ecf034–0xffffff8000ecf06c` recognizes `ErrH`;
  `0xffffff8000ecf800–0xffffff8000ecf804` saves its DT core argument.

## INFERRED / runtime validation still required

- Initial hardware warm-reset address is modeled as the start of the
  monitor's 2-MiB-aligned allocation (0x800600000 in this boot profile).
  The trampoline and its secondary path are statically verified; actual
  PMGR-to-monitor-to-kernel execution was not observed here. No guest-written
  programmable RVBAR MMIO register was established, so none was invented.
- Explicit AIC CPU banks at 0x5000 + cpu*0x80 extend the preexisting board
  register model. Native kernel evidence above establishes the current-CPU
  window and SEND/ACK semantics; explicit-bank use is an emulation contract.
- CPU error resources are address-verified DT resources, with storage-only
  semantics. This is not a claim of complete error-controller emulation.
- This is a Cortex-A57 compatibility model, not a Cyclone implementation.
  Native SMP boot, scheduling, monitor services, suspend/resume, and MTTCG
  have not been validated. CPU0-only native boot also needs a runtime check.
- R1-cpu-start.md, R2-aic-smp.md and R3-percpu.md were not present in the
  supplied reports directory during this implementation; no claims from
  those unavailable reports were assumed. Evidence above was checked directly.

## Validation and user-run procedure

Completed here:

```
python3 -m unittest discover -s tests -p 'test_*.py'
# 132 tests, OK, 4 skipped

git diff --check
```

The new host test extracts the board's actual AIC/release/timer C helpers and
checks bank isolation, both IPI classes, ACK versus auto-mask, target routing,
shared source reassertion, independent FIQ ORing, duplicate release handling,
and legacy one-CPU routing. Loader tests exercise both preserved CPU DT nodes.
These checks do not compile the board against QEMU or run guest code.

After building in this worktree, run:

```
python3 backend/smoke_test.py
python3 backend/smp_contract_test.py
```

The SMP fixture runs direct EL1 with `-smp 2 -accel tcg,thread=single`.
CPU0 queues an IPI while CPU1 is off and records the still-zero secondary
marker, then writes PMGR +0x30004/+0x30008. CPU1 writes its marker, consumes
the queued IPI, receives another from CPU0, sends CPU0 an IPI, and each CPU
sends itself an IPI. IRQ exception handlers read their local IAR, ACK, unmask,
and publish counters. Expected physical words at 0x800020000 are:

```
00000011 00000001 00000002 00000001 00000001 00000000 0000face 00000000
```

For native validation regenerate the boot image using `cpus=2`, preserving the
native monitor and DT, and launch with `-smp 2 -accel tcg,thread=single`.
The normal runtime launcher selects these from the manifest boot arguments.
Check PMGR release and PCs on both application CPUs, then native CPU1 online
state and bidirectional IPI progress. Repeat with `cpus=1` / `-smp 1` to compare
CPU0 behavior. Runtime success is for the user to establish.

Input SHA-256:

- `KernelMemory.bin`: `bba9a4bace9b505b80c05bc15648b46122c8948f86ac4f0d0625f7804d78d7d8`
- `DeviceTree.runtime`: `b75e0054fb52aedc461ecaf88475e93f5b9f6d370c0e0cc040273b4b224d810b`
