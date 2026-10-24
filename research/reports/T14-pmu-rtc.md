# T14: Dialog D2045 PMU and RTC

Implemented in `backend/s5l8960x.c` on `wt/pmu-rtc`. No emulator build or execution was performed. No guest kernel, device tree, software, or disk was modified. The maintainer must verify integration with iOS.

## Sources and method

Read the supplied `KernelMemory.bin` and `DeviceTree.runtime` from `${REPO_ROOT}/build/ios712-validated/boot/`. The supplied memory image SHA-256 is `bba9a4bace9b505b80c05bc15648b46122c8948f86ac4f0d0625f7804d78d7d8`; DT SHA-256 is `b75e0054fb52aedc461ecaf88475e93f5b9f6d370c0e0cc040273b4b224d810b`. This memory-image digest differs from the original kernel-file digest in the earlier I2C report; the present address evidence was decoded directly from the supplied memory image. Disassembled its A64 instructions with the supplied virtualenv's Capstone, using `file_offset = VA - 0xffffff8000a00000 + 0x1000000`. Kernel exported names were checked against its embedded Mach-O symbol table (header at file 0x802000). All instruction addresses below are full kernel VAs; abbreviated addresses in parentheses retain prefix `0xffffff8000`.

The earlier investigation in `research/native-i2c-gpio-contract.md` agrees with the recovered prefix, slave address, controller events, and startup transfers. Supplied probe109 and `pmu-serial-lines.txt` establish failures before this change, not successful execution of the new model.

## Device tree and I2C protocol

`/device-tree/arm-io/i2c0/pmu`:

- `compatible = "pmu,d2045\0"`; no second compatible string or literal AppleD2045PMU string in this node. AppleD2045PMU is the matching driver class.
- `reg` bytes `74 00 00 00 c4 09 00 00 00 00 00 00 00 00 00 00`: first little-endian word is **seven-bit address 0x74**. The second word is 2500; its exact controller interpretation was not recovered here.
- Parent I2C0 MMIO is **0x20a110000**, AIC IRQ154. Wire addresses are **0xe8 write / 0xe9 read**. There is no D2045 on I2C1.
- `device_type = "interrupt-controller"`, interrupt-parent phandle 0x1a (GPIOIC), `interrupts = <15, 1>` in little-endian words: GPIO15, level interrupt. Modeled as active low, consistent with the native GPIOIC level-low type 6 and the PMU event handler.
- PMU phandle 0x47; its interrupt cells property is one. Event labels associate PMU GPIO2 with baseband, 4 dock, 6 battery, 7 WLAN, 8 codec, 10 Bluetooth, 12 mesa, 13 SPU. Child `vib-pwm/vibrator` has `reg=0x26`, enable via GPIO161, and an intensity configuration.
- No RTC, ADC, calibration, epoch, or register-layout properties are present in the PMU subtree. Those contracts come from the driver, not DT.

`AppleDialogPMU::_readRegs` **0xffffff80010513ec**, specifically 1051438–1051464, serializes register high byte then low byte, calls nub slot 0x558 with mode 1, prefix count 2, data count and destination. `_writeRegs` **0xffffff800105164c**, 1051698–10516cc, uses the same prefix and mode 2. These are **16-bit big-endian addresses and byte payloads**. No per-byte address is sent in a burst: successful contiguous reads/writes require slave auto-increment. Observed counts include identity 0x0000/4, calibration 0x0519/8 and 0x04a0/8, event/mask 0x0050/12 and 0x0070/12, RTC 0x05c6/6, offset 0x4004/4, alarm 0x05c0/4, ADC 0x0502/2.

Controller commands preserve the existing ABI: START `0x100|wire_address`, prefix/data bytes with optional final `0x200`, repeated read START `0x1e9`, read request `0x600|count`. PMU supports 1–255 bytes per controller read request, uint16 address wrapping, and either prefix STOP or repeated START. +4 pops bytes / returns 0x100 empty; +8 exposes RX occupancy in bits 15:8. +0x14 completion is bit27 for writes, bit20 for reads; errors retain NACK bit21 + bit27. +0x18 masks controller interrupts; +0x14 remains W1C. Controller reset flushes RX/prefix state while preserving the external PMU's registers/time. The previous unmatched-slave fixture now probes address 0x75, because 0x74 is implemented.

## RTC and calendar path

| Registers | Recovered native use | Implementation / confidence |
| --- | --- | --- |
| 0x05c6–0x05cb | **10532b0–10532f0** reads six bytes and forms `b[2] | b[3]<<8 | b[4]<<16 | b[5]<<24` | Last four bytes are verified little-endian uint32 seconds, **not BCD/calendar fields**. First two bytes are ignored by this caller; fractional role and 16-bit LE fraction encoding are model assumptions. |
| 0x4004–0x4007 | **105cc18–105cc24** loads four bytes into RTC object +0xe8; **105cca0–105cce0** writes changed offset | Persistent LE uint32 calendar offset, reset zero, ordinary writable storage. |
| 0x05c0–0x05c3 | **1052cd4–1052d34** writes argument as four native-endian bytes when nonzero | LE seconds alarm target. Stored/readable, no timed wake/power-on alarm delivery implemented. |
| 0x05c4 | Same routine writes 0x41 with a target, 1 without; **1052754–105278c** reads/clears bit6 | Alarm enable/control; stored. Full bit definitions and silicon reset value unverified. |

`AppleD2045PMURTC` get path **105cd38–105cd60** returns `counter + offset` with 32-bit arithmetic. Set path **105cd64–105cd90** writes `requested_seconds - counter` via 105cca0; it does **not** overwrite the running counter. Calendar alarm path **105cd94–105cdc8** converts the supplied calendar structure and subtracts the same offset before saving the target in PMU object +0x180. Power/sleep preparation **105540c–1055418** passes that target to 1052cd4.

The calendar conversion import at **105eddc** resolves through pointer **10610e0** to **0xffffff800080dacc**. This routine explicitly counts days starting in **1970** (`0x7b2`), then hours/minutes/seconds: the wall-time epoch is Unix. The raw counter is a running/monotonic quantity; the offset, not necessarily the raw silicon counter, establishes its relation to Unix time.

Platform chain verified in this image:

1. Exported `PEGetGMTTimeOfDay` **0xffffff80002c7ff4** dispatches platform-expert vtable slot 0x5c0; `PESetGMTTimeOfDay` **0xffffff80002c8018** uses slot 0x5c8.
2. AppleARMPE get **0xffffff8000805388–80544c** waits for `IORTC`, then calls platform function `getGMTTimeOfDay`; set uses `setGMTTimeOfDay` at **80545c–8054f8**.
3. AppleARMRTC registers `IORTC` at **80d7c4–80d7d0**. Its function dispatcher **80dbdc–80dcec** recognizes those two strings and calls RTC slots **0x560 get / 0x568 set**. PMURTC's slots resolve to the counter-plus-offset and offset-setting routines above.
4. Exported `clock_get_calendar_nanotime` **0xffffff80002056fc** locks clock state and obtains elapsed time from the machine timebase (`CNTPCT_EL0` at 205750), rather than doing an I2C transfer on each user calendar-time read. This audit did not identify every private clock initialization/resume or IOPMrootDomain caller; the verified platform RTC contract is the boundary this model repairs. Correct calendar seeding/resume still requires a boot probe.

The model initializes raw counter to host Unix `time(NULL)` and offset to zero, which makes their sum the desired wall time. `VPHONE_RTC_EPOCH=<decimal uint32 Unix seconds>` overrides the initial time on machine reset, e.g. `1700000000` = 2023-11-14 22:13:20 UTC. Invalid, negative, overflowing, or trailing-text values report an error and fall back to host time. Thereafter only **QEMU virtual time** advances the counter, so host delays while paused do not advance it. Reads snapshot one counter per burst to avoid torn rollover. Guest writes to counter bytes are supported as well as the actual native offset set protocol. Full machine reset reseeds time/storage; controller reset does not. There is no RTC backing file or migration state.

The old date close to the 2106 uint32 wrap is consistent with failed reads and uninitialized stack bytes: 10532b0 ignores `_readRegs`' return before decoding. This is a diagnosis supported by instructions, not proof that the time-valued SpringBoard pointer had the same cause. A new boot must establish whether that crash changes.

## Register map and ADC

| Register(s) | Evidence and behavior |
| --- | --- |
| 0x0000–3 | Startup **1050b30** reads identity; no recovered identity check or physical ID constants. Reset zero is explicitly synthetic; no fabricated chip ID. |
| 0x0050–0x005b | **10514ec–1051608** reads 12 event bytes, dispatches set bits. Model resets to no pending events; read clears latches. Read-clear hardware behavior is inferred from absence of explicit acknowledge. Writes defensively clear written-one bits. |
| 0x0070–0x007b | **1050d08–1050d40** writes eleven 0xff bytes and final 0xed. Modeled interrupt masks, one=masked; mask polarity inferred from this operation and internal event91 handling. Reset all masked. Only ADC completion generates a modeled PMU event. |
| 0x0060–0x006c | **1054ef0–1054f00** reads 13 bytes of status. Stored, reset zero. No charger/cable events or battery state machine synthesized. |
| 0x0400–0x040f, including **0x405** | **1054f04**, **1054fe0**, **10550ec** operate on `0x400 + gpio_index`, bounded 0–15. They manipulate GPIO configuration/readback; **0x405 is PMU GPIO5**, not ADC status or the event bank. Unrelated battery/charger meanings must not be assigned from polling frequency. Model stores writes; electrical GPIO5 function/sample is not recovered. |
| 0x04a9/0x04aa | **1050d44–1050d84** combines `(first<<4)|(second&15)` as temperature trim. Model 0x42/5 gives 0x425, the driver's own fallback trim (**10539e4–10539f0**). |
| 0x04a0–0x04a7 | Startup at **1050e44–1050e54**, later temperature calculations; reset zero, synthetic calibration baseline. |
| **0x0500** | **1053444–1053460** writes `(channel & 31) | saved_config | 0x80`: conversion start. Model schedules conversion after 1 ms virtual time; clearing start cancels. |
| **0x0501** | **10533c8–1053400**, **10535c4–10535f4**, **1054178–10541a8** perform ADC/accumulator control RMW, including bit6 and preserved bit0. **Not an interrupt-status register.** Bit6 settling path waits about 80 us. Stored; no independent accumulator simulation. |
| 0x0502/0x0503 | **105354c–105359c** reads two bytes, result `(second<<4)|(first&15)`. First-byte bit7 is an invalid indicator for channels 5/9; model leaves it clear. |
| 0x0541–0x0543 | **1053670–1053728** alternate ADC: write channel/start bit7, delay 1 ms, read start bit until clear, then two result bytes with same nibble layout. Modeled immediate completion with busy clear. |
| 0x0519–0x0520 | **1050d88–1050e40** calibration endpoints feeding signed slopes/division. Synthetic endpoints 1024 and 3072 (first pair has asymmetric byte order in this driver) avoid an all-zero slope. Not recovered OTP defaults. |
| 0x4000–0x4003, 0x4008–0x400b | **10518cc–10519f0**, **1052eb8–10530fc** saved boot/power metadata and four-byte readback/hash-like value. Stored; not event registers. |

**Why “ADC read bad state: 1”:** `AppleD2045PMU::_readADCGated` **10532f4–105366c** uses driver object +0xe8 as a software wait state. It sets **1** at **1053494**, sleeps on its command gate, and requires **2** at **105353c–1053544** before reading the ADC result. Merely making 0x500/501 readable cannot satisfy this. The PMU interrupt handler checks **event byte 0x5b bit1** at **105152c**, changes state **1 → 2** at **1051540–1051554**, and wakes the waiter at **1051558–1051568**. Bad state1 means the completion wake never arrived before timeout, not that the PMU returned a hardware ADC state byte equal to one.

The model schedules completion at 1 ms virtual time, writes the result, clears start, sets **event91 = 0x5b bit1**, and asserts active-low GPIO15 if unmasked. GPIO group selection comes from GPIOIC pin config bits23:16; level status contributes to AIC108+group, using the existing GPIO interrupt aggregation. Event read-clear releases the physical PMU line; the GPIOIC W1C latch still needs its normal guest acknowledgement. Other event bytes remain zero. No guest memory or driver state is patched.

Raw sample choices are emulation policy, checked against the native formulas rather than claimed measurements:

- Channels 0/4 (VCC_MAIN/VBAT, name table **1054534–1054544**, **105477c**) = **2130**. Voltage formula **1053c48–1053ca8** yields about **3.800 V** (16.16 result).
- Channel8 (TJUNC, **1054800**) = **1061**, with trim0x425; **10539e4–1053a20** yields about **26 C**.
- Channels2/7 = **2048**, a nominal divider/NTC voltage near **1.25 V**. Full board NTC calibration is not verified.
- Channels11–14 = **432**, giving approximately **25 C** with the modeled zero calibration baseline and native tdev formula **1053a54–1053ac0**.
- Remaining channels default to 2048. Channel10 is **TCAL**, not VBAT (name selection **1054828**); no claim of complete analog modeling. Accumulated current/voltage counters, real charger behavior, PMU GPIO edges, and timed alarm delivery are outside this implementation.

## Validation and next maintainer probe

Executed successfully:

- `python3 backend/pmu_contract_test.py --host`: compiles **actual extracted PMU, I2C, and GPIO aggregation C** with bounded host clock/timer/IRQ stubs. Checks burst prefix/readback, FIFO draining/empty/count, time progression/fractional snapshot coherence, direct seconds set/wrap, native offset storage, controller-reset persistence, machine-reset reseed, ADC result encoding and masked event/GPIO-level assertion/read-clear, alternate ADC, maximum read length, uint16 wrap, malformed prefix/zero length, I2C1 NACK, and Chestnut routing.
- `python3 -m unittest discover -s tests -p 'test_*.py'`: **121 tests, OK, 4 skipped**. Updated the SPI host harness's extraction endpoint and minimal state for the shared GPIO PMU input. AGX source extraction marker remains in its original relationship to the AGX helper functions.
- `git diff --check`.

Prepared but **not executed**: `python3 backend/pmu_contract_test.py` (AArch64 guest/controller MMIO fixture, same QMP pattern as Chestnut). It sets `VPHONE_RTC_EPOCH=1700000000`, checks RTC seconds, four-byte register burst/readback, event idle state, FIFO occupancy/empty, controller reset, I2C1 isolation. Existing `backend/i2c_contract_test.py` and `backend/chestnut_contract_test.py` should also be run by the maintainer after building.

Next iOS boot should check:

1. No PMU `_readRegs failed` / `_writeRegs failed` lines, including 0x501, 0x405, 0x5c6 and metadata registers. Unmatched devices should still NACK normally.
2. No `_readADCGated(...): ADC read bad state: 1` lines or sustained GPIO15/AIC interrupt storm. If present, capture GPIO15 config, group status, PMU 0x5b/0x7b, and timing around 0x500 writes. End-to-end interrupt timing remains unverified.
3. Crash-report/log timestamps match host UTC (allow virtual-time run duration) or the configured epoch, replacing the 2106 wrap-adjacent dates. Check actual date through userland time APIs and exercise RTC set/offset readback if a probe is available.
4. Battery/temperature reports are plausible; compare actual converted channel4/8/11–14 results, as synthetic ADC calibration is not validated in iOS. Record any new charger/power-source behavior caused by successful reads.
5. Reassess the SpringBoard crash separately. Correcting RTC does not establish that every bad-pointer symptom is repaired.

No emulator build/run or successful iOS boot is claimed by these host checks.
