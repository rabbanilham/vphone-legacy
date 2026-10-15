# n53ap I2C bus-status and GPIO line contract

This read-only audit uses the unchanged iOS 7.1.2 kernel SHA256 `1e0f584ff54040ded8a2ef4ed532b4298f2500b774fc14d88020453c91465666` and original DeviceTree. Bounded native disassembly is saved in `firmware-artifacts/i2c-native-disassembly.txt`. Runtime observations come from `backend/evidence/resume-20261013/native-ans-run/serial.log` and `backtrace.json`; no guest or backend patch was applied by this audit.

## Observed panic and actual branch

The guest reports `i2c0::_checkBusStatus SDA is stuck low; last read status 0aa00040 xfer 00000000 fifo 00000000 for device pmu`. The caller VA 0xffffff8000dbb510 is the diagnostic routine's return from panic, not the original status read.

Native `_checkBusStatus` at VA 0xffffff8000dbb020 reads the controller register **+0x14**, saves it to driver state +0x15c, and checks bit **28**. If set, or if the SDA GPIO read is zero, it enters `_unjamBus` at 0xffffff8000dbb578. When recovery returns an error, saved bit 28 selects the diagnostic `XIP is still set (SDA may be stuck low)`; otherwise it reports `SDA is stuck low`. Independently, a subsequent zero SCL GPIO read leads to `SCL is stuck low`. The panic's status **0x0aa00040 has bit 28 clear**, so that word alone does not establish stuck SDA. The zero GPIO line inputs and unsuccessful recovery are the concrete missing behavior.

## GPIO identity, offsets and input sampling

Original DeviceTree `function-iic_sda` and `function-iic_scl` records point to GPIO phandle 0x1a and contain the `GPIO` function tag, pin number, and four configuration bytes `02 01 01 00`.

| Controller | SDA pin / register PA | SCL pin / register PA |
| --- | --- | --- |
| i2c0 | 80 / 0x20e300140 | 81 / 0x20e300144 |
| i2c1 | 82 / 0x20e300148 | 83 / 0x20e30014c |

GPIOIC base is **0x20e300000**; one pin register is at **pin*4**. Controller driver state +0x100 and +0x108 store the SDA and SCL platform-function objects respectively, proven by startup string lookups and stores at 0xffffff8000dba9ac–0xffffff8000dba9d0. GPIOFunction's vtable slot 0x78 resolves to VA 0xffffff8000eca214. Its read with explicit mode 2 reaches GPIOIC slot 0x5c0 at VA 0xffffff8000ec9f50, which reads pin*4 and masks **bit 0**. The descriptor's inversion byte is one; the two XORs at 0xffffff8000eca328/32c therefore cancel for these I2C functions. The reported Boolean is the actual sampled bit 0.

The current backend's GPIO register array resets to zero and reads back its stored values. This represents an electrically low idle line, even after the guest releases the pin. It does not model the external pull-up of an idle open-drain I2C bus. **Board-model assumption:** these four lines have pull-ups and no modeled slave currently holds them low. The native pin identity and sampling ABI are proven; the physical resistor values and silicon mux electrical behavior are not extracted from these bytes.

## Native pin-mode transformations

GPIOIC slot 0x5a8 resolves to VA 0xffffff8000ec9d9c. Its jump table at 0xffffff8000ec9eb0 establishes these transformations on the saved pin configuration:

| Native mode | Mask replaced | Replacement | Observed role |
| ---: | --- | --- | --- |
| 0 | 0x26e | 0x200, or retained non-output mode bits OR 0x200 | release/input path used before sampling |
| 1 | 0x6e | 0x02 | output mode preserving bit-0 latch |
| 2 | 0x260 | 0x220 | native I2C function's configured pin mode |
| 14 | 0x6f | 0x02 | drive low |
| 15 | 0x6f | 0x03 | drive high |

Modes 14/15 are selected by GPIOIC output setter at 0xffffff8000ec9ef0. Configuring input explicitly sets bit 9 and preserves bit 0 rather than writing an input sample itself. **Bit 0 is read as pin data; simply preserving its reset value zero after release is wrong for a pulled-up I2C line.** Bit 9's full hardware mux/enable meaning across unrelated GPIO uses remains unverified. These transformations justify separate sample/config/latch state and restricted I2C pin pull-up behavior; they do not justify globally forcing every GPIO high.

Native recovery at 0xffffff8000dbb578 uses platform-function mode overrides, samples both lines, toggles SCL up to nine times, samples again, resets/restores the controller and restores native GPIO modes. It must see the physical effect of release and low drive. The backend should model those line transitions before pretending a transaction occurred.

## Controller MMIO evidence

Original DT maps i2c0 to PA **0x20a110000**, i2c1 **0x20a111000**, each length 0x1000, with AIC interrupt IDs 0x9a and 0x9b.

| Offset | Native usage and evidence |
| --- | --- |
| +0x00 | FIFO/command writes, including address flag 0x100 and last-byte flag 0x200, VA 0xffffff8000dbae7c–0xffffff8000dbaeec |
| +0x04 | RX FIFO read; bit 8 causes empty/pending polling, byte read when clear, VA 0xffffff8000dbb9f4–0xffffff8000dbba00 |
| +0x08 | FIFO state/counts; low byte TX occupancy and next byte RX occupancy used with requested length, VA 0xffffff8000dbb218–0xffffff8000dbb23c |
| +0x0c | transfer state printed in panic; additional field semantics unresolved |
| +0x10 | reset/init write 0x80000000 after recovery and timed-out transfer; persistence of reset bit not established |
| +0x14 | status/event register; XIP bit 28; driver writes mask 0x0aa00040 during init/recovery and acknowledges saved status during completion |
| +0x18 | interrupt/event enables: native writes mask 0x08800040 for write transfers and 0x00b00040 for read transfers; zero disables events |
| +0x1c | timing/control value low byte with minimum 4; on NACK path bit 9 set |

Polling completion helper at 0xffffff8000dbb8e0 uses +0x14 mask **0x00800040** as an error condition and returns IOReturn 0xe00002d6. Bit 27 or, for a read, bit 20 allows a completion check. Bit 21 then selects the NACK/error path (IOReturn 0xe00002e9); its clear path accounts completion differently. Exact names for every interrupt/status bit require further evidence; labels must not be guessed from their numerical positions.

The initialization write 0x0aa00040 is subsequently observed unchanged in backend status. Additional native iBoot evidence now proves **+0x14 bit 21 (NACK) and bit 27 (transfer complete) are write-one-to-clear events**. iBoot initializes +0x14 with 0xffffffff at file offset 0x7ea8. Before each transfer it writes 0x08200000 (bits 27/21) at 0x8010 or 0x81bc; a write transfer then polls bit 27 at 0x820c until set. If bit 21 is set, it terminates with error and clears only that event by writing 0x00200000 at 0x8250. The read path does the same NACK clear at 0x8108. Thus an ordinary RW array makes the native pre-transfer clear look like immediate complete/NACK status and is incompatible. Other status-bit acknowledgement rules remain bounded unknowns. Clearing an event cannot manufacture a received byte or PMU acknowledgement.

`_runInterruptMode` at 0xffffff8000dbb164–0xffffff8000dbb19c programs **+0x18** with write mask 0x08800040 (bits 27/23/6) or read mask 0x00b00040 (bits 23/21/20/6). Read mode also programs **+0x2c** with `(requested_count << 8) & 0xff00`. Completion resets +0x18 to zero at 0xffffff8000dbb9a8. The aligned status/mask fields support a controller IRQ level derived from enabled pending events; this electrical level policy is an inference from native usage, while the masks and native AIC 154/155 IDs are observed facts. A slave-address NACK is a failed, completed attempt, requiring NACK bit 21 and completion bit 27 so the native write and read masks can observe the failure.

All six kernel writes to +0x10 use **0x80000000**, in initialization/power restore (0xffffff8000dbab74/abc4), timeout recovery (0xffffff8000dbb2dc), bus recovery (0xffffff8000dbb7c0), and controller reset paths. They follow interrupt disable and event clear. No normal-transfer run toggle or bit-31 read/poll appears in this driver; the FIFO address command starts the transfer. Reset/flush command semantics are a bounded inference consistent with these sites; other +0x10 control fields and self-clear timing remain unproven.

## Native PMU transaction contract

The original `arm-io/i2c0/pmu` node is compatible with `pmu,d2045`; its first `reg` word **0x74 is a seven-bit slave address**. Kernel controller target setter at 0xffffff8000dbad78 shifts that value left one and stores 0xe8 in driver state +0x140. Native iBoot uses 0xe8 for write and 0xe9 for read in its direct I2C callers, independently confirming this interpretation. Do not shift 0x74 right one as though the DT already contained a wire address.

AppleDialogPMU `_readRegs` at 0xffffff80010513ec and `_writeRegs` at 0xffffff800105164c serialize register addresses as **two big-endian bytes**, high byte followed by low byte. They pass two prefix bytes and the requested data length to the I2C nub. Read mode is 1 and write mode 2 in this nub API; these modes are unrelated to GPIO pin-mode numbers.

The first explicit PMU startup read at 0xffffff8001050b30 requests register **0x0000, count 4** into PMU driver state +0xc0, before its `AppleD2045PMU::start` log. The recorded panic also precedes that log, so this is a strong candidate for the blocked transaction, **not a runtime-observed register request**. Confirm it by capturing current I2C driver state +0x138: descriptor +0x10 prefix count, +0x18 prefix pointer, +0x20 data count, +0x28 data pointer. Subsequent native startup operations include write 0x0070/count 12, reads 0x04a9 and 0x04aa/count 1, 0x0519/count 8 and 0x04a0/count 8. Actual PMU silicon register meanings, calibration and reset values are unresolved; returning an invented identity or zero-filled successful read would not implement the device contract.

The return from the first 0x0000 read is not checked before continuing. A bounded instruction scan of PMU code VA 0xffffff8001050000–0xffffff800105eb00 found no direct loads of the stored four bytes at object +0xc0…+0xc3, and no further immediate ADD of those offsets besides the read destination. This does **not** prove the bytes are unused in every indirect path, but establishes no verified identity comparison or expected identity constants. An accepted startup branch cannot be used to invent a physical PMU ID.

Required writable/readback state observed in startup is more substantial than identity:

| Register(s) | Native operation | Evidence/limit |
| --- | --- | --- |
| 0x0070…0x007b | write eleven 0xff bytes followed by 0xed | initialization loop at 0x1050d08–0x1050d40; interrupt masking inference |
| 0x04a9/0x04aa | read one byte each; combine `(first << 4) | (second & 0xf)` | stored as uint16 at object +0x100 |
| 0x0519…0x0520 | read eight bytes; derive fixed-point endpoint/difference fields | arithmetic at 0x1050d98–0x1050e40; calibration/reset values unknown |
| 0x04a0…0x04a7 | read eight bytes into object +0x124 | persisted driver calibration/state; meanings unresolved |
| 0x0444 | read then write original OR 0x0f | 0x1050fc4–0x1050fe4 |
| 0x0011 | read then write original OR 1 | 0x1050ff8–0x1051018 |
| 0x4001 | write 0x30 | 0x10511d8–0x10511ec |
| 0x0050…0x005b | read twelve event bytes and dispatch set bits | interrupt handler 0x10514ec–0x1051608; no explicit status write acknowledgement observed in that handler |

The calibration reads immediately feed signed arithmetic and slope fields. An all-zero success response produces degenerate calibration; it is not a recovered native default. The event handler's absence of explicit register writes suggests possible read-clear status, but hardware latch/clear semantics need independent confirmation. PMU transfers should continue to fail explicitly until an actual supported register/state model exists.

Native controller FIFO commands at +0x00 encode address marker **bit 8** and end-of-transfer marker **bit 9**. Writes send `(wire_address & 0xfe)|0x100` followed by payload bytes, marking the last byte with 0x200 where the API requests termination. A read sends `wire_address|0x101`, followed by **0x600 | byte_count**. +0x04 returns a byte when bit 8 is clear; bit 8 set means no byte available, and iBoot additionally checks +0x14/NACK to terminate instead of spinning. Full electrical repeated-start/STOP timing and FIFO error handling still need a modeled transaction trace; these command encodings alone do not establish them.

## Implementation boundary

The next justified backend change is real idle/release/sample behavior on the four native I2C GPIO pins, preserving driven-low behavior and recording transitions. Then implement controller transaction state against an actual modeled I2C bus. Unknown PMU addresses/registers should NACK or report an explicit unsupported device. No synthetic PMU ID, successful FIFO transfer, or fabricated IRQ should be used to hide the remaining hardware gap. Passing this guard would establish pin behavior only, not usable PMU, storage, filesystem mount, or userspace.
