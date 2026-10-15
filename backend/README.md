# Experimental A7 QEMU backend

This is a bring-up machine, **not a bootable iOS emulator**. It loads actual
kernel memory, exposes a real guest framebuffer and records unsupported
operations. It does not print Darwin/SpringBoard messages or draw iOS UI.

Upstream QEMU is pinned in `source.lock.json`. `s5l8960x.c` is the project's
source overlay; `scripts/qemu/build_backend.py` installs that overlay into an
ignored source checkout and builds one project-local binary. The build verifies
the exact upstream commit and writes `backend.json` with hashes and capabilities.
No globally installed QEMU binary is required or selected.

```
python3 scripts/qemu/build_backend.py --fetch --install-build-deps
```

macOS requires Xcode/Command Line Tools, GLib, Pixman and their pkg-config files.
Python build dependencies live in `backend/.venv`. If the system linker cannot
read the default SDK's `.tbd` files, pass `--sdk /path/to/a/matching/MacOSX.sdk`.
The builder makes a local symlink so SDK paths containing spaces work.

Run `python3 backend/smoke_test.py` after building. This synthetic test verifies
initial PC/x0/EL1h, actual guest instructions, UART bytes, a framebuffer pixel
written by the guest and captured through QMP, stop/quit, and invalid CPU/RAM
or missing-image failures. It does not demonstrate XNU or SpringBoard boot.
Restricted environments must permit a local Unix socket for QMP.

## Loader interface

```
backend/bin/qemu-system-aarch64 \
  -machine s5l8960x,boot-el=3,boot-entry=0x80060c120,boot-args=0x8007fffd0 \
  -accel tcg -smp 1 -m 1024 -kernel KernelMemory.bin \
  -serial file:serial.log -qmp unix:qmp.sock,server=on,wait=off \
  -display cocoa -d unimp,guest_errors -D trace.log
```

`KernelMemory.bin` is raw DRAM contents beginning at physical `0x800000000`;
it is not an IPSW or Mach-O. The firmware pipeline prepares zero-filled BSS,
kernel/monitor segments, Apple boot arguments and Apple DeviceTree in that
image. With `boot-el=3`, the machine begins at the native monitor physical
entry in EL3h, with interrupts masked and MMU off. `x0` points to the monitor's
six-u64 boot arguments; the actual firmware performs the kernel handoff.
The default `boot-el=1` direct EL1h entry is used by synthetic fixtures.
It exposes 1 GiB RAM
and exactly one CPU. A nonexistent/empty image or invalid boot addresses fail.

The fixed scanout reads 640×1136 BGRX32 pixels at physical `0x83fc00000`, stride
2560; `framebuffer=0xADDRESS` may override the in-RAM address. A black display
means the guest has not drawn. Scanout is a diagnostic framebuffer; no AGX,
AppleH6PDisplayPipe register emulation or compositor compatibility is claimed.
Input, graceful guest shutdown and iOS boot remain unverified. The experimental
ASP software model below supports checked raw block I/O; successful native
filesystem mount remains unverified.

## Implemented contracts and limits

- CPU: upstream Cortex-A57 ARMv8 interpreter under TCG, 24 MHz architected
  timer. Native `boot-el=3` enables EL2/EL3 because the recovered monitor
  initializes both. Direct EL1 fixture mode disables them. This is
  **not Apple Cyclone**.
- Exact Apple c15 encodings listed in `s5l8960x.c` store values for kernel
  bring-up and log writes under `-d unimp`: HID, performance counter,
  configuration and error registers. Counter increments, cache behavior and
  other hardware effects are unimplemented. Unlisted encodings still fault.
  HID8's DataSetID0/1 fields match the actual kernel's read/OR/write sequence
  and [Apple XNU's definitions](https://github.com/opensource-apple/xnu/blob/master/pexpert/pexpert/arm64/arm64_common.h).
  TCG does not model physical cache datasets; storing this configuration is
  an explicit compatibility approximation, not Cyclone cache emulation.
- UART0 at `0x20a0a0000`: S5L polled RX/TX status and FIFO, serial0 chardev;
  received bytes can assert IRQ 140. DMA/modem support is unimplemented.
- AIC v1 at `0x20e100000`: 1024 IRQ mask/software-pending slots, prioritized
  event/auto-mask behavior, one CPU, basic self-IPI and timer outputs to FIQ.
  Only UART is a hardware IRQ source; other peripherals are unimplemented.
- PMGR, GPIO, 15 bridge ranges and 28 peripheral ranges now have experimental
  register storage/readback. PMGR power-state writes mirror target into actual
  state immediately; MCC configuration writes update its polled status; the
  GPU range supplies a provisional core ID/reset response. These limited
  responses allow native driver initialization to advance; they do not model
  NAND/ANS commands, I²C slave responses, SEP execution, GPU rendering or display
  controller behavior. Generic peripheral registers currently return their
  stored value (initially zero), without logging every unsupported operation.
  Consequently absence of an MMIO error is **not** evidence of device support.
- MMIO outside these explicit ranges remains unmapped. Use serial, breakpoints,
  device-state inspection and `-d unimp,guest_errors,int` together to locate
  missing hardware; a quiet trace alone cannot prove successful initialization.
- Migration/snapshots are unsupported for the machine device state.

The native ANS startup contract and firmware bytes have been recovered from
iBoot, including its `region-base`/`region-size` fixups. With those present, the
AP initially reached an I2C0/PMU SDA-stuck-low panic. Restricted released-line
pull-ups on native pins 80..83 now pass that guard. A restricted I2C controller
model now reports an unmatched-address NACK with native event/IRQ semantics;
the former I2C1 `als` interrupt timeout is passed. Fresh five-second native
probes produce real PMU `e00002e9` errors and proceed through additional driver
initialization without a panic during that bounded interval. A subsequent
30-second production probe reaches an ANS incompatible-version panic
(`e00002d6 -10`, `bad.ffff`). Storage and userspace remain unverified. See
[resumed bring-up evidence](../research/resume-20261013.md).
The native region at `0x83f800000` belongs to ANS firmware, not display RAM.
The diagnostic framebuffer is a separate lower boot allocation recorded in
`boot_plan.json`; production and diagnostic launchers pass its address explicitly.

An opt-in IOP instruction-execution diagnostic is available with
`-machine s5l8960x,...,iop-diagnostic=1 -accel tcg,thread=single`;
`backend/probe.py --iop-diagnostic` supplies these options. It creates a separate
held-reset Cortex-A7 prototype while the machine still accepts one AP CPU.
Its exact native CPU identity is unknown. It aliases the native 8 MiB ANS region
at local PA zero and external DRAM for native LPAE mappings. Bootstrap table
backing at PA `0x04000000..0x04002000` is provisional anonymous scratch;
architected counter frequency is the experimental 24 MHz model value.

The diagnostic starts only after the native ANS region/start-register sequence
and runtime image structure checks. Firmware integrity remains the strict
loader's responsibility. Unsupported IOP peripheral accesses return a bus error;
no ready or mailbox response is generated. Real ARM32/Thumb startup and its MMU
have executed, reaching an unsupported write of `3` at VA `0x38040024`, translated
PA `0x208040024`, firmware PC `0x1592`. This is a diagnostic, not ANS compatibility
or a completed iOS boot. Details and traces are in
[the ANS contract](evidence/resume-20261013/ANS-CONTRACT.md).

GPIO pins 80..83 have a separate input sample and stored output latch view.
Native input/I2C modes release the line; native output modes preserve low/high
drive. External pull-ups with no slave holding the line low are an explicit
board-model assumption restricted to those four pins. Other GPIOs retain their
previous behavior. `backend/gpio_contract_test.py` executes guest instructions
to verify release, low/high drive, I2C mode and an unchanged adjacent pin. This
does not implement I2C transactions or acknowledge a slave device.

The native I2C controller ranges `0x20a110000` and `0x20a111000` consume
START/address FIFO commands and report NACK bit 21 plus completion bit 27 for
every unmodeled address. RX remains empty (`0x100`); no ACK, device ID or receive
payload is generated. Event register `0x14` uses W1C; register `0x18` gates AIC
IRQ 154/155 independently from AIC masking/acknowledgement. Register `0x10`
bit 31 resets/flushes pending events and IRQ enable. Reset self-clear and
clearing IRQ enable are bounded model assumptions: native firmware writes
reset after disabling IRQ and clearing events, and does not poll its bit.
Transactions complete immediately with NACK; bus timing and successful slave
transactions are unimplemented. `backend/i2c_contract_test.py` executes guest
instructions to check RX-empty, IRQ claim/reassert, W1C, reset, and preservation
of an unrelated interrupt. Native source anchors are recorded in
[the native I2C contract](../research/native-i2c-gpio-contract.md).

## Modeled ASP storage continuation

The resumed backend adds a synchronous CSI/ASP software device model serving
actual raw System/Data files. It is separate from native ARM32 IOP execution.
Machine properties `system-image` and `data-image` explicitly select both files;
`system-writable=1` permits writes to a persistent System derivative. The
launcher preserves immutable `system.raw`, registers `system-runtime.raw`, and
retains pre-launch disk snapshots. Files are locked, DMA is bounded to RAM and
I/O/flush errors reach the native guest completion status. Unknown commands
return errors. Geometry follows actual file lengths rather than a fixed capacity.

Native protocol and journal evidence are in
[the storage audit](../research/continuation-20261013-storage.md);
[repair evidence](evidence/resume-20261013/ANS-HLE-AUDIT.md) records boundary tests
and limitations. `backend/ans_contract_test.py` proves guest reads/writes and
persisted bytes across a new QEMU process; it does not prove filesystem boot.
Native clock/timestamp and initialization-status commands use QEMU virtual time
and actual backing validation. `utility-image` selects a committed, validated
logical utility container; its exact profile and erased initial region contents
are documented in [the utility profile](../research/utility-model-profile.md).
Typed requests transfer actual persistent bytes, including native guest NVRAM
writes. Unsupported native FORMAT15/16 fail and do not change System/Data.
This is a model-private logical namespace, not original physical NAND state.

The native SIO range `0x20ae00000` now models the `AppleA7IOP` / `AppleSmartIO`
mailbox (outbox IRQ 135, OS-Active message `0x00000000016d0000`), `EP=0`
control parameter configuration, `EP=1` `AppleSmartIOAES` inline (`ctrl & 0x400`)
and DMA-segment AES-CBC/ECB operations plus hardware-key support/disable, and
`EP=2..0x4f` `AppleSmartIODMA` channel responses. `backend/sio_contract_test.py`
executes guest instructions to verify OS-Active startup, IRQ 135, `EP=0`
`setParam`/`getParam`, inline UID round-trip, DMA-segment NIST SP 800-38A
AES-128-CBC vectors, and `disableHardwareKey`.

With that SIO/AES contract present, the real guest publishes nonzero storage,
selects `disk0s1`, returns `W0=0` from `hfs_mountfs` and the HFS root-mount
callback, completes the `IOAESAccelerator` / `AppleARMPE` `SecureRootName`
handshake (`secureroot-callback/`, `secureroot-return/`), populates `rootvnode`
(`rootvnode-ready/`), and executes `/sbin/launchd` in process 1 (`PID=1`,
`launchd-exec-call/` and `launchd-exec-return/` with `W0=0`). In userspace
(`post-launchd-15s/serial.log`), `launchd[1]` starts up, runs `fsck_hfs` on
`/dev/rdisk0s1` and `/dev/rdisk0s2`, mounts `/` and `/private/var`, and then
reaches `FIPS USER Space POST: Integrity test failed!`, which triggers a
`launchd` userspace shutdown before SpringBoard or UI. See
[the native bootstrap evidence](../research/bootstrap-launchd-20261013.md) and
[SIO coverage](evidence/hfs-mount-20261013/SIO-COVERAGE.md).

## Source references

The model is original project code licensed GPL-2.0-or-later. Address and
register contracts were checked against these primary sources; they are
references and do not prove iOS compatibility:

- [Linux A7 DeviceTree](https://github.com/torvalds/linux/blob/master/arch/arm64/boot/dts/apple/s5l8960x.dtsi)
- [Linux AIC driver](https://github.com/torvalds/linux/blob/master/drivers/irqchip/irq-apple-aic.c)
  (GPL-2.0-or-later, Asahi Linux contributors).
- [Linux Samsung/Apple UART driver](https://github.com/torvalds/linux/blob/master/drivers/tty/serial/samsung_tty.c)
- [Pinned QEMU upstream](https://github.com/qemu/qemu/tree/4fc49f46dc95d4a27de2509e7fceb2931e91faeb)

To distribute a binary, distribute the overlay and pinned upstream source with
the corresponding GPL notices. Generated binaries/build checkouts are ignored.

## 2026-10-14 native display ABI continuation

The board now models the recovered Chestnut I2C0 0x27 register-0x05 control
subset, timing-enable/status handshake, mask-gated bit-0 VBI and validated
display command-FIFO writes/token completion. This is an explicit software
ABI model; it does not supply factory identity, calibration or analog behavior.
Unknown I2C addresses/registers remain errors. Native physical UserClient swaps
are observed, but non-zero iOS framebuffer pixels are still unverified.
`display-trace=1 -d unimp -D FILE` logs display regions and I2C0 with guest
PC/value/width and host/guest origin. See the current handover for exact evidence.

Native SEP readiness remains blocked (2026-10-15): publication and EP7 creation
are proven, but endpoint enable waits on manager +0xc0=2 while requiring >=5,
holding the KeyStore gate upstream of CA. See the latest handover; no native SEP
execution or CA UI success is claimed by synthetic mailbox tests.

`display_probe.py --stop-condition first-pixel` is the default and retains the
legacy any-nonzero-byte stop. `--stop-condition ca-scanout` archives transient
pixels and gates a candidate on CA/surface/physical-swap/scanout milestones;
manual surface-ID/commit attribution and visible RGB verification are required.
`--seconds` budgets guest-running wall time (paused inspection excluded), defaults
to 45 and allows up to 180 for an explicitly justified targeted probe. Cleanup
with `backend/probe_cleanup.py OUTPUT` only after its process-exit record confirms
reaping. The helper preserves original-media hashes and removes owned copies.

## 2026-10-15 contract-driven keybag probes

`backend/contract_probe.py` runs the persisted-ART preset or summarizes an existing
run without booting. EP7 command 4 distinguishes the recovered absent systembag
query and returns signed status -3 (`0xfd`); command 25 writes eight reply bytes.
Executable fixtures check actual replies, negative cases, buffer bounds and reset.

Native probes verify keybagd's NotFound return and loading branch, then prove
`open("/private/var/keybags/systembag.kb", 0) == -1` with errno ENOENT. A valid
restored bag input and native import/lifecycle contract remain unavailable;
loaded/unlocked state and the first CA application frame remain unverified.
See [findings and reproduction](evidence/keybag-contract-20261015/FINDINGS.md).
The optional `--keybagd-loader-hooks` mode stops on directly observed open failure.
