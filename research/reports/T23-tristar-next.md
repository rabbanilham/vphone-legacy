# T23: the next TriStar-enabled fault is EHCI capability MMIO

2026-10-22, `/Users/bagas/Downloads/vphone-wt-tristar-next`, branch `wt/tristar-next`.

Implemented A7 EHCI controller wiring using the pinned QEMU EHCI engine, plus the separate USB complex configuration registers. **Host checks pass; native boot acceptance remains unverified.** No QEMU build or execution was performed. No guest software, kernel, runtime DT, boot plan, firmware, or disk image was changed. TriStar remains opt-in (`VPHONE_TRISTAR=1`); absence of a further panic and completion of baseband/iap matching are not established by these host tests.

## Fault and call chain

Inputs: T21/T22, supplied `probe142/serial.log` and `trace-tail.txt`, validated `KernelMemory.bin` and `DeviceTree.runtime` in the legacy checkout. Capstone ran with `/Users/bagas/Downloads/vphone-legacy/.venv/bin/python`; kernel file offsets use the supplied `VA - 0xffffff8000a00000 + 0x1000000` formula. Addresses here are unslid kernel VAs, with prefix `0xffffff8000` omitted in abbreviated addresses.

The fault is **AppleUSBEHCI::start**, beginning `eefaac`, not a TriStar register read or an accessory IPC routine. Its superclass vtable pointer at `efa220` resolves to `b8c570`. The subclass start slots establish the full chain:

| Function | Start | Captured return PC / call |
|---|---|---|
| AppleS5L8960XUSBEHCI::start | `1111928` | calls Synopsys start at `11119f0`, returns at `11119f4` |
| AppleSynopsysUSBEHCI::start | `110c568` | calls ARM start at `110c698`, returns at `110c69c` |
| AppleUSBEHCIARM::start | `110abfc` | calls EHCI start at `110add8`, returns at `110addc` |
| AppleUSBEHCI::start | `eefaac` | fault at `eefb10` |

Class strings are at `1111f8c` (S5L8960X), `110cfc6` (Synopsys), `110cc0c` (ARM). Superclass vtable start slots: `*(efb240+0x2b8)=eefaac`, `*(110d1e0+0x2b8)=110abfc`, `*(110e100+0x2b8)=110c568`.

At `eefac8..eefadc`, the driver calls its provider's slot `0x3c8` with index=0, options=0 (`mapDeviceMemoryWithIndex`), saving the IOMemoryMap at driver+`0x3a8`. It allocates a 16-byte capability-header cache at +`0x3c0`. At `eefaf8..eefb04` it calls the map's slot `0x70` (`getVirtualAddress`). The returned x0 is `0xffffff80a7ab0000`. The loop `eefb08..eefb20` copies four 32-bit words:

```
mov x8, #0
ldr x9, [x20, #0x3c0]
ldr w10, [x0, x8, lsl #2]   // eefb10: first word faults
str w10, [x9, x8, lsl #2]
add x8, x8, #1
cmp w8, #4
b.ne eefb10
```

Captured x8=0 and FAR=x0 prove that this is the first capability word, not an operational-register access at a bad CAPLENGTH. `eefb24..eefb40` would obtain the map VA again, read the first cached byte (CAPLENGTH), and save VA+CAPLENGTH at driver+`0x3c8` for operational registers. No capability value has been read successfully yet.

## Provider reg translation and abort cause

The DT uses little-endian 64-bit address/size cells here:

- `/arm-io/ranges`: child 0 -> parent `0x200000000`, length `0x100000000`.
- `/arm-io/usb-complex/ranges`: child 0 -> arm-io `0x0c000000`, length `0x600000`.
- `usb-ehci0/reg[0]`: complex `0x200000`, size `0x10000` -> **PA `0x20c200000`**.
- `usb-ehci1/reg[0]`: complex `0x400000`, size `0x10000` -> **PA `0x20c400000`**.
- EHCI0 `reg[1]`: complex 0, size 8 -> **PA `0x20c000000`**.
- EHCI1 `reg[1]`: complex 8, size 8 -> **PA `0x20c000008`**.

S5L start maps index 1 at `111193c..1111968` into driver+`0x2788`/`0x2790`. The superclass later maps index 0 as above. The provider is the platform nub passed through all four starts (x19 in the fault frame is `0xffffff808218e900`), not the TriStar I2C nub. DT IRQs are 163 and 165.

**Strong inference: this capture is EHCI0**, hence the fault VA maps PA `0x20c200000`. The Synopsys start enumerates child `port-type` and `port` properties (`110c5c0..110c674`) before calling its superclass. Captured x24=`110cc5c` and x25=`110cc66` are those two property-name strings; x28=2 is the last enumerated child's port type. EHCI0's last child is `usb-ehci0-port1` with type 2; EHCI1 has only wlan with type 3. This also agrees with the newly released baseband/dock dependency. The two supplied files contain no page tables, provider-property dump, or IOMemoryMap physical-address dump, so the exact VA->PA pair is not independently measured. Both possible EHCI ranges have the identical board defect and are now implemented; no fixed subtraction from the dynamic kernel VA is assumed.

Before this patch neither range is registered by the board. The only nearby peripheral is PHY `0x20c000020..3f`; the `0x20caff000` bridge is elsewhere. `bridge_ops` and generic `peripheral_ops` use value-returning read/write callbacks, never a `MemTxResult` bus-error callback. Thus this fault is an **unmapped physical MMIO transaction**, not a bridge producing an error. `ldr w10` is an aligned 4-byte transaction; access width is not the cause. ESR `0x96000010` is consistent with a synchronous external abort on that load.

## Implementation

`backend/s5l8960x.c` defines two small `TYPE_SYS_BUS_EHCI` subclasses. They use capabilities at offset 0, operational registers at `0x10`, PORTSC at operational+`0x44`, and 3/1 root ports, covering DT EHCI0 ports 1 and 3 and EHCI1 port 1. These counts are a chosen DT-compatible modeled topology, not a readback of real silicon capabilities. No USB endpoint is attached. IRQ callbacks maintain the individual AIC device-level bits without disturbing adjacent IRQ164 or one another.

The existing upstream EHCI engine supplies the actual register behavior rather than a new all-zero or write-retaining approximation. In the pinned source (`backend/upstream/hw/usb/hcd-ehci{,-sysbus}.{c,h}` in the legacy checkout), `usb_ehci_init`, `ehci_reset`, `ehci_opreg_write`, `ehci_update_halt`, and port operations establish:

- Capability words `0x01000010`, port count, `0x80`, 0: EHCI 1.0, CAPLENGTH 16, frame cache, no 64-bit DMA advertised.
- Reset USBCMD=`0x80000`, USBSTS=`0x1000` (halted), USBINTR=0, FRINDEX=0, disconnected powered ports (`PORTSC=0x1000`, CCS/CSC/PE clear).
- HCRESET clears itself and resets operational state. Run/stop updates halted state. Native status W1C, read-only capability fields, frame timing, schedule handling and interrupt signaling use the upstream implementation.
- No manufactured accessory, USB modem, carrier event, or modem-ready GPIO.

The two 8-byte control windows are added as one zero-reset 16-byte bridge storage range at `0x20c000000`; this does not overlap the existing PHY at +`0x20`. S5L start uses its second map for cfg-incr and clock-control RMW (`1111bcc..1111c20`). Existing bridge machine reset clears these bytes. EHCI HCRESET does not reset that external control range. `scripts/qemu/build_backend.py` explicitly selects `USB_EHCI_SYSBUS`, which selects the common EHCI/USB code; relying on unrelated boards' default build selection would be fragile.

Only the standard engine's `0x1000`-byte MMIO aperture is implemented at each DT controller base. The DT's `0x10000` reservation does not imply recovered behavior for its unused tail. No catch-all USB-complex mapping masks future accesses to other controllers. OHCI and the USB device controller remain outside this change.

**Limits:** using the EHCI core establishes supported controller behavior, not verified iOS DMA addressing. It advertises 32-bit DMA and uses the system address space; no USB DART/IOVA translation was invented. Native schedule/DMA setup, later HSIC/baseband waits, and possible activation of the unmodeled OHCI/device controller must be checked during the maintainer boot. This is why TriStar remains opt-in. The change fixes the identified missing register aperture but does not claim the requested no-busy-leaves/CommCenter outcome before that run.

## Verification

- `python3 -m unittest discover -s tests -p 'test_*.py'`: **150 tests OK, 4 skipped**.
- New host test compiles the actual board EHCI class setup, type registrations, device construction and IRQ callback against bounded QEMU API stubs. It checks both physical ranges, CAPLENGTH/PORTSC layout, DT-compatible port counts, IRQ163/165 routing and deassertion isolation with adjacent IRQ164 held high. This does not execute the QEMU EHCI engine.
- A build-path test intercepts writes before any configure/build invocation and checks explicit EHCI Kconfig selection.
- Added maintainer-only `python3 backend/ehci_contract_test.py`: synthetic bare-metal word loads matching `eefb10`, both capability headers, disconnected ports, HCRESET, W1C, run/halt and independent shared-control storage. **Not run here**, per the no-build/no-emulator instruction. It creates only a temporary synthetic RAM payload and loads no iOS or disks.
- `git diff --check`: passes. `/* The TA command whose parameter buffer` remains present.

After rebuilding, run the synthetic test and the requested original no-Oscar plan with `VPHONE_TRISTAR=1 VPHONE_TCG_THREAD=multi VPHONE_DISPLAY=cocoa` and `--snapshot-at 45`. Require no panic, complete/error-free `iokit-state-045s.json` with no busy leaves (including iap/baseband), and CommCenter past InterfaceNamer. Check actual stack/registry evidence if another controller, DMA initialization, or HSIC wait is reached. These are acceptance checks for the maintainer, not results obtained in this task.
