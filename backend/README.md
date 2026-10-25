# Experimental A7 QEMU backend

For current capabilities and acceptance evidence, read [status](../docs/status.md) and [architecture](../docs/architecture.md). Recorded local research profiles reach original iOS Home/Settings through modeled peripherals and partial AGX command replay; the baseline IPSW pipeline does not yet reproduce that profile.

The contract notes below were accumulated during bring-up. Dated negative results describe those historical configurations, not the current aggregate feature set. Missing `evidence/` links refer to local uncommitted artifacts. Use the maintained [firmware guide](../docs/firmware.md) for setup.

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
and one or two application CPUs (`-smp 1` or `-smp 2`, default one). A nonexistent/empty image or invalid boot addresses fail.

The fixed scanout reads 640×1136 BGRX32 pixels at physical `0x83fc00000`, stride
2560; `framebuffer=0xADDRESS` may override the in-RAM address. A black display
means the guest has not drawn. Scanout is a diagnostic framebuffer; no AGX,
AppleH6PDisplayPipe register emulation or compositor compatibility is claimed.
Input, graceful guest shutdown and iOS boot remain unverified. The experimental
ASP software model below supports checked raw block I/O; successful native
filesystem mount remains unverified.

For dual-core experiments, put `cpus=2` in the loader boot arguments and use
`-smp 2 -accel tcg,thread=single`. The runtime launcher reads the core count
from the generated boot plan. CPU1 stays powered off until the guest writes
its bit to PMGR `0x20e030008`. Native EL3 release enters the secure monitor's
warm-reset trampoline at `0x800600000` with x0=0; direct EL1 fixtures enter
`boot-entry` and distinguish cores through MPIDR. The monitor retains control
of the kernel handoff. CPU error MMIO blocks at `0x202050000` and `0x202150000`
provide storage, without emulating hardware error injection.

After rebuilding, run `python3 backend/smp_contract_test.py` for the synthetic
two-core startup and bidirectional/self IPI contract. Native iOS SMP execution
remains unverified. Evidence and limitations are in [T16-smp.md](../research/T16-smp.md).

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
  event/auto-mask behavior, per-core self/cross IPIs and timer outputs to each core's FIQ. IRQ target masks select the receiving core; source masks remain shared.
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
PC/value/width and host/guest origin. See the research archive for historical evidence.

Native SEP readiness remains blocked (2026-10-15): publication and EP7 creation
are proven, but endpoint enable waits on manager +0xc0=2 while requiring >=5,
holding the KeyStore gate upstream of CA. See the current status page; no native SEP
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

## Scripted n53 touch input

After rebuilding the backend, set `VPHONE_TOUCH_SCRIPT` on the QEMU process.
The existing six-field swipe form remains supported:

```sh
VPHONE_TOUCH_SCRIPT='125,60,1018,600,1018,0.3'
```

Fields are `start_s,x0,y0,x1,y1,duration_s`. Coordinates are integer framebuffer
pixels with a top-left origin (X 0..640, Y 0..1136), not UIKit points.
Start is seconds of QEMU virtual time relative to machine reset (0..86400).
Legacy duration is 0.016..60 seconds. The model emits state1 (path start),
state3 (down), `max(1,floor(duration*60))` state4 moves including the endpoint,
then state7 (up), with the same separate start/down/up intervals as before.
Contact ID is always 1 and identity is 2 (finger).

For holds, variable speed, taps, or repeated gestures use the segment form:

```sh
VPHONE_TOUCH_SCRIPT='at=125;down 100,1018;hold 0.15;move 560,1018 dur=0.8 steps=48;hold 0.1;up'
VPHONE_TOUCH_SCRIPT='at=125;tap 320,568|at=128;down 60,1018;move 600,1018 dur=1;up'
```

Each gesture begins with `at=seconds;`, then exactly one `down x,y` and a final
`up`, or a single `tap x,y`. Commands are separated by `;`; gestures by `|`.
Do not add a trailing separator. Gestures must be ordered, with each `at=`
strictly after the previous up's scheduled time. Whitespace around numbers
and separators is accepted; command names are lowercase as shown.

- `down x,y` emits start immediately, then down one 60 Hz tick later.
- `move x,y dur=seconds [steps=N]` linearly interpolates from the current point.
  The first move occurs one interval after the preceding frame, and the final
  move reaches the requested endpoint exactly at `dur` after that frame.
  Default steps are `max(1,floor(dur*60))`; explicit steps are integers 1..3600
  and can override the usual cadence for diagnostic probes.
- `hold seconds` emits stationary state4 frames at approximately 60 Hz,
  including the exact duration endpoint. Move and hold durations are 0.001..60
  seconds. Holds do not lift or restart the contact.
- `up` emits state7 at the current point one 60 Hz tick after the prior frame.
- `tap x,y` expands to down, a 0.1-second hold (six frames), then up.

The entire script is validated before scheduling; errors identify the gesture,
command, nearby input and reason through `VPHONE_TOUCH_SCRIPT: ...; injection disabled` (one log line). Invalid scripts inject nothing. The bounded timeline
supports up to 16384 frames; exceeding it is an error. Unset the variable to
disable injection. There is no interactive monitor command.

Cadence uses QEMU virtual time, not wall time. If userland has not enabled
reporting yet, injection waits. A bounded FIFO preserves transitions when SPI
reads lag. Reporting waits and full-queue backpressure stretch the remaining
timeline, including subsequent gestures, rather than dropping or bursting frames.
A reset cancels the previous timeline and parses the environment again.

Report 0x70 is UI lock control (`MTDeviceSetUILocked`): 1 enters UI lock,
2 exits it. The firmware model queues reset-complete `60 02` at final execute
and on unlock; it preserves a pending notification across policy changes.
MultitouchHID uses that notification to restore its deferred InputDetectionMode.
Report 0xbf contains two LE 16-bit masks (detectable inputs, inputs suppressing
fingers); the injector waits for detectable bit2 with UI lock inactive. Reports
0xaf and 0xc8 retain the face-detection and input-report controls used by that path.
The earlier requirement for 0x70=1 was incorrect.

For diagnostics only, `VPHONE_TOUCH_FORCE=1` bypasses the reporting gate while
retaining the same script timing, FIFO and IRQ protocol. It defaults off and
does not change the values returned by reports 0x70 or 0xbf. A forced gesture
alone does not demonstrate that native startup policy works.

Touch data uses HBPP EB phase-0 length / phase-1 data exchanges, LE
checksums, and GPIO22 through GPIOIC group0 / AIC108. The chosen contact
personality is report 0x74 with a 26-byte header and a 30-byte contact. D3's
endian byte is 1 (LE); its rows/columns and D9 dimensions retain the existing
16×27 and 640×1136 personality. These are modeled values, not captured sensor
firmware output. End-to-end iOS behavior requires integrator validation.

### Mouse input in the Cocoa window

Host mouse touch input is enabled by default. Set `VPHONE_MOUSE_TOUCH=0` on
the QEMU process to disable registration of the mouse handler. The board's
640×1136 graphic console belongs to the named `a7-touch-display` sysbus device;
its bound QEMU absolute-pointer handler scales X/Y from `0..INPUT_EVENT_ABS_MAX`
to `0..640` / `0..1136` top-left pixels. Cocoa supplies the position without
relative-pointer acceleration. Input is committed at QEMU's sync boundary,
after Cocoa's button event and both coordinate events have arrived.

Left press emits start1 followed by down3 at the press position. Dragging emits
move4 at most once per 16,666,667 ns of QEMU virtual time (about 60 Hz), coalescing
intermediate positions to the latest point. A stationary hold queues no extra
frames. Release emits a final move4 at the release position, then up7, even for
a quick click. Transitions are spaced by one tick; a full touch FIFO delays
delivery without losing transitions or bursting overdue motion. Both mouse and
script use the same report 0x74 builder, identity 2, raw `(x,1136-y)`, FIFO and
GPIO22 falling-edge IRQ path.

Mouse presses are ignored while **any scripted frames remain**, including the
wait before a scheduled script and gaps between its gestures. They are also
ignored while a previous mouse release is draining or the reporting gate is
closed. Release and press again after the script finishes/reporting resumes;
a held ignored press never starts later. `VPHONE_TOUCH_FORCE=1` bypasses the
same BF detectable/UI-lock reporting gate for mouse input, but does not bypass
script ownership. Controller stop/UI-lock or disabling BF contact detection
cancels the current mouse session along with queued contacts; a fresh click is
required. Machine reset clears the mouse state and pending timer.

SpringBoard processes touches slowly under emulation. For the lock-screen
slide, drag slowly rightward near y=1036, from about x=40 to x=600 over roughly
3 guest seconds, then **pause for about 1 guest second before releasing**.
A fast swipe may bounce back because UIKit has not processed enough movement
before the release. Guest seconds can take substantially longer than wall time.

`tests/test_mouse_touch.py` compiles the actual sequencer, QEMU callbacks and
frame builder with host stubs under ASan/UBSan. It checks coordinate scaling,
Cocoa event ordering, contact states/identity/raw coordinates, motion coalescing,
quick clicks, FIFO/gate waits, final release coordinates, ignored buttons/consoles,
and script/force/disable policies. The SPI harness also checks controller-stop
cancellation. These tests do not replace a rebuilt QEMU/Cocoa/iOS run.

### Trackpad gestures (iOS Simulator style)

`backend/patches/0004-cocoa-trackpad-touch-gestures.patch` makes the Cocoa
window translate host gestures into touchscreen fingers:

| Host input | Guest touch |
|---|---|
| Click | Tap (one finger) |
| Click + drag | Pan / swipe (one finger) |
| Two-finger trackpad scroll | One finger dragged by the scroll deltas; lifts when the fingers leave the trackpad (iOS supplies its own momentum) |
| Mouse wheel notch | Short one-finger drag (40 px per notch), lifted after 150 ms without notches |
| Option + click + drag | Pinch: two fingers mirrored around the screen centre |
| Option + Shift + click + drag | Two fingers (and the pinch centre) move together |
| Trackpad pinch | Two fingers around the pointer, spread by the magnification |

While Option is held over the screen, two grey dots show where the pinch
fingers would land, as in the Simulator. Click and drag keep the absolute
pointer path described above. Scroll, Option and pinch gestures use QEMU
multi-touch (MTT) slots 0 and 1, which the board turns into report-0x74 frames
carrying every live finger: header u16 at 12 = total contact bytes (30·n), byte
14 = contact count n, path ID = slot + 1 and identity = 2 + slot. MTT fingers follow the
mouse cadence: start1, down3, move4 every tick even when stationary, up7. When
the last finger lifts after moving more than 10 px, 18 settle frames are sent first;
otherwise 3. A finger that leaves a live pinch goes up at once. A re-touch
while the previous contact drains (rapid flicks) lifts that contact on the next
tick and then starts a fresh one. A mouse finger and MTT gesture never overlap;
a blocked begin needs a fresh one. QMP `input-send-event` accepts the same `mtt`
events (`device: a7-touch-display`), so gestures can also be scripted headlessly.

Run `python3 backend/touch_input_contract_test.py` and its diagnostic `--force`
variant against the rebuilt QEMU for a diskless AArch64 GPIO/AIC/SPI test. `tests/test_spi_dma.py` compiles the
actual source with host stubs and exercises a complete gesture under ASan/UBSan.
For a live screen probe, use `backend/display_probe.py --stop-condition duration`
with `--seconds 90` so it continues through the scheduled gesture; this mode
still captures the final framebuffer and stops on a kernel panic.
